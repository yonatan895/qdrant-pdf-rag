"""Render + fail-closed tests for scripts/run_local_gateway.sh (local-dev only).

Hermetic: rendering uses GATEWAY_DRYRUN=1 and dummy sk-test-* keys. Transport
cases use executable Docker/curl fakes and tiny loopback HTTP servers; no daemon,
external network, GPU, real gateway or model service is contacted.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run_local_gateway.sh"

DUMMY_KEYS = {
    "GATEWAY_MASTER_KEY": "sk-test-master",
    "GATEWAY_LLM_KEY": "sk-test-llm",
    "GATEWAY_EMBED_KEY": "sk-test-embed",
    "GATEWAY_RERANK_KEY": "sk-test-rerank",
}


def _render(extra_env: dict[str, str] | None = None, *, keys: bool = True,
            drop=None) -> subprocess.CompletedProcess:
    """Render the real script with dummy keys; `keys=False` and `drop` model a
    caller whose environment has none of the gateway variables set."""
    env = {key: value for key, value in os.environ.items() if not (drop and drop(key))}
    env["GATEWAY_DRYRUN"] = "1"
    if keys:
        env.update(DUMMY_KEYS)
    env.update(extra_env or {})
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)


def _read_cfg(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == 0, proc.stderr
    cfg_dir = Path(proc.stdout.strip().splitlines()[-1])
    try:
        return (cfg_dir / "config.yaml").read_text()
    finally:
        shutil.rmtree(cfg_dir, ignore_errors=True)


def test_render_routes_legs_and_carries_master_key_and_overrides():
    text = _read_cfg(_render())
    # reasoning_effort must reach vLLM (the agent's control), not be dropped.
    assert "model: strict_openai/google/gemma-4-E4B-it-qat-mobile-ct" in text
    assert 'allowed_openai_params: ["reasoning_effort"]' in text
    assert "api_base: http://host.docker.internal:8000/v1" in text
    assert "model: openai/Qwen/Qwen3-Embedding-0.6B" in text
    assert "api_base: http://host.docker.internal:8001/v1" in text
    # Native /rerank goes through the hosted_vllm provider.
    assert "model: hosted_vllm/BAAI/bge-reranker-v2-m3" in text
    assert "master_key: sk-test-master" in text
    # No native /v1/score in LiteLLM: exact-path pass-through to the vLLM backend.
    assert 'target: "http://host.docker.internal:8002/v1/score"' in text
    overridden = _read_cfg(_render({"GATEWAY_RERANK_URL": "http://gpu-box:8002/v1"}))
    assert "api_base: http://gpu-box:8002/v1" in overridden
    assert 'target: "http://gpu-box:8002/v1/score"' in overridden


@pytest.fixture(autouse=True)
def _o2_credentials(tmp_path_factory, monkeypatch):
    """The stack reads the 0600 OpenObserve env file; tests never touch the real one."""
    path = tmp_path_factory.mktemp("o2") / "o2.env"
    path.write_text("ZO_ROOT_USER_EMAIL=r@x.invalid\nZO_ROOT_USER_PASSWORD=fixture\nO2_BASIC_AUTH=Zm9v\n")
    path.chmod(0o600)
    monkeypatch.setenv("O2_ENV_FILE", str(path))


@pytest.mark.parametrize('endpoint', ['', 'http://host.docker.internal:4318'])
def test_render_callbacks_and_content_logging(endpoint):
    """Issue #636: the strict finish guard is always installed; the bare "otel"
    callback (content, key hashes, hidden params) never is; the allowlisting
    exporter only when an endpoint is set; content and key metadata logging
    are off either way."""
    cfg = yaml.safe_load(_read_cfg(_render({'GATEWAY_OTEL_ENDPOINT': endpoint} if endpoint else None)))
    settings = cfg['litellm_settings']
    assert settings['custom_provider_map'] == [
        {'provider': 'strict_openai', 'custom_handler': 'strict_finish.strict_openai'}
    ]
    expected = ['scoped_passthrough.guard'] + (['trace_privacy.otel'] if endpoint else [])
    assert settings['callbacks'] == expected
    assert settings['turn_off_message_logging'] is True
    assert settings['redact_user_api_key_info'] is True


def test_render_includes_the_gateway_hook_beside_its_configuration():
    proc = _render()
    assert proc.returncode == 0
    directory = Path(proc.stdout.strip().splitlines()[-1])
    try:
        assert (directory / 'strict_finish.py').read_bytes() == (SCRIPT.parent / 'gateway/strict_finish.py').read_bytes()
        assert (directory / 'scoped_passthrough.py').read_bytes() == (SCRIPT.parent / 'gateway/scoped_passthrough.py').read_bytes()
        assert (directory / 'trace_privacy.py').read_bytes() == (SCRIPT.parent / 'gateway/trace_privacy.py').read_bytes()
    finally:
        shutil.rmtree(directory)


@pytest.mark.parametrize('url', ['http://gpu:8000', 'http://gpu:8000/v1/'])
def test_tokenize_targets_reasoning_origin(url):
    cfg = yaml.safe_load(_read_cfg(_render({'GATEWAY_REASONING_URL': url})))
    routes = cfg['general_settings']['pass_through_endpoints']
    route = next(r for r in routes if r['path'] == '/tokenize')
    assert route == {'path': '/tokenize', 'target': 'http://gpu:8000/tokenize', 'methods': ['POST']}


def test_render_generates_keys_when_unset():
    text = _read_cfg(_render(keys=False, drop=lambda name: name.startswith("GATEWAY_")))
    assert "master_key: sk-local-" in text
    # Generated values are terminal-only material: distinct per start, and
    # the committed tree must never contain one.
    assert "sk-test-" not in text


def test_env_file_handoff_written_mode_600(tmp_path):
    env_file = tmp_path / "gateway.env"
    proc = _render({"GATEWAY_ENV_FILE": str(env_file)})
    assert proc.returncode == 0, proc.stderr
    _read_cfg(proc)  # also cleans the render dir
    body = env_file.read_text()
    # Every leg is routed at the gateway with its own key; the master key
    # never leaves the process.
    for line in (
        "export LLM_BASE_URL=http://localhost:4000/v1",
        "export LLM_MODEL_REASONING=google/gemma-4-E4B-it-qat-mobile-ct",
        "export LLM_API_KEY=sk-test-llm",
        "export EMBED_BASE_URL=http://localhost:4000/v1",
        "export EMBED_API_KEY=sk-test-embed",
        "export EMBED_MODEL_REVISION=local:Qwen/Qwen3-Embedding-0.6B",
        "export RERANK_ENABLED=true",
        "export RERANK_BASE_URL=http://localhost:4000/v1",
        "export RERANK_API_KEY=sk-test-rerank",
    ):
        assert line in body, line
    assert "master_key" not in body and "sk-test-master" not in body
    assert (env_file.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("revision", [
    " rev:tag | 'quoted' \"$HOME\" $(touch \"$REVISION_CANARY\") `touch \"$REVISION_CANARY\"`\nnext \tend\n",
    "",
])
def test_explicit_embedding_revision_survives_sourced_handoff(tmp_path, revision):
    env_file = tmp_path / "gateway.env"
    proc = _render({
        "GATEWAY_ENV_FILE": str(env_file),
        "GATEWAY_EMBED_MODEL": "Qwen/Qwen3-Embedding-8B",
        "GATEWAY_EMBED_MODEL_REVISION": revision,
    })
    assert proc.returncode == 0, proc.stderr
    _read_cfg(proc)
    consumer = subprocess.run(
        ["sh", "-c", '. "$1"; printenv EMBED_MODEL_REVISION', "consumer", str(env_file)],
        capture_output=True, text=True, check=False,
        env={**os.environ, "REVISION_CANARY": str(tmp_path / "executed")},
    )
    assert consumer.returncode == 0, consumer.stderr
    assert consumer.stdout == (revision or "local:Qwen/Qwen3-Embedding-8B") + "\n"
    assert not (tmp_path / "executed").exists()


@pytest.mark.parametrize("revision", [
    " rev:tag | 'quoted' \"$HOME\" $(touch \"$REVISION_CANARY\")\nnext \tend\n",
])
def test_gateway_revision_reaches_stack_ingest_and_serving(tmp_path, revision):
    """Real gateway producer and stack consumer; fake only external processes.

    Stop after the serving process records its environment, before any app runs.
    A pre-exported EMBED_MODEL_REVISION must not override the explicit gateway
    revision and a correct generated string alone does not prove child export.
    """
    scripts = tmp_path / "checkout" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("run_local_gateway.sh", "run_local_stack.sh", "local_lane_budget.sh"):
        shutil.copyfile(REPO / "scripts" / name, scripts / name)
    shutil.copytree(REPO / "scripts" / "gateway", scripts / "gateway")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text("#!/bin/sh\nexit 99\n")
    curl = bindir / "curl"
    curl.write_text('''#!/bin/sh
case "$*" in
    */healthz*) printf '503' ;;
    *) printf '200' ;;
esac
''')
    python = bindir / "consumer-python"
    python.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
with open(os.environ["CONSUMER_LOG"], "a") as output:
    output.write(json.dumps({"argv": sys.argv[1:],
        "revision": os.environ.get("EMBED_MODEL_REVISION"),
        "model": os.environ.get("EMBED_MODEL"),
        "dimension": os.environ.get("DENSE_DIM")}) + "\\n")
if sys.argv[1:3] == ["-m", "uvicorn"]:
    sys.exit(23)
''')
    for file in bindir.iterdir():
        file.chmod(0o755)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    log = tmp_path / "consumers.jsonl"
    result = subprocess.run(
        ["sh", str(scripts / "run_local_stack.sh")],
        capture_output=True, text=True, check=False, timeout=15,
        env={**os.environ, **DUMMY_KEYS,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "PY": str(python), "CONSUMER_LOG": str(log),
            "LOCAL_STACK_DRYRUN": "0", "GATEWAY_DRYRUN": "1",
            "GATEWAY_ENV_FILE": str(tmp_path / "gateway.env"),
            "LOCAL_STACK_LOG_DIR": str(tmp_path), "CORPUS_DIR": str(corpus),
            "GATEWAY_EMBED_MODEL": "Qwen/Qwen3-Embedding-8B",
            "GATEWAY_EMBED_MODEL_REVISION": revision,
            "EMBED_MODEL_REVISION": "stale-preexported-revision", "DENSE_DIM": "4096",
            "REVISION_CANARY": str(tmp_path / "executed"), "FORCE_START": "1",
            "TMPDIR": str(tmp_path)},
    )
    assert result.returncode == 1 and "agent exited early" in result.stderr, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    for role in ("mainframe_rag.ingest.run_ingest", "uvicorn"):
        (consumer,) = [call for call in calls if call["argv"][:2] == ["-m", role]]
        assert consumer["revision"] == revision
        assert consumer["model"] == "Qwen/Qwen3-Embedding-8B"
        assert consumer["dimension"] == "4096"
    assert not (tmp_path / "executed").exists()
    assert not (tmp_path / "gateway.env").exists(), "stack cleanup owns the private handoff"


@pytest.mark.parametrize("var", ["GATEWAY_MASTER_KEY", "GATEWAY_RERANK_KEY"])
def test_bad_key_charset_fails_closed(var):
    proc = _render({var: "sk-bad_$key!"})
    assert proc.returncode != 0
    assert "must contain only alphanumerics" in proc.stderr


def test_bare_hostname_url_fails_closed():
    proc = _render({"GATEWAY_REASONING_URL": "gpu-box:8002"})
    assert proc.returncode != 0
    assert "must begin with http:// or https://" in proc.stderr


def _gateway_args(proc: subprocess.CompletedProcess) -> list[str]:
    assert proc.returncode == 0, proc.stderr
    cfg_dir = Path(proc.stdout.strip().splitlines()[-1])
    try:
        return (cfg_dir / "gateway.args").read_text().splitlines()
    finally:
        shutil.rmtree(cfg_dir, ignore_errors=True)


@pytest.mark.parametrize(
    ("value", "debug_on"),
    [(None, False), ("0", False), ("1", True)],
)
def test_detailed_debug_only_when_explicitly_enabled(value, debug_on):
    """Issue #590: the old ${GATEWAY_DEBUG:+...} expansion turned
    --detailed_debug on for the default "0" (and for any non-empty value),
    logging full request bodies (manual text). Off unless 1/true."""
    proc = (_render(drop=lambda name: name == "GATEWAY_DEBUG") if value is None
            else _render({"GATEWAY_DEBUG": value}))
    args = _gateway_args(proc)[0]
    assert args.startswith("--config /app/gateway/config.yaml")
    assert ("--detailed_debug" in args) is debug_on


@pytest.mark.parametrize("value", ["yes", "2"])
def test_invalid_debug_value_fails_before_any_container(tmp_path, value):
    """A typo must not silently pick a mode: it fails before docker is called."""
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    marker = tmp_path / "docker-called"
    docker = stub_bin / "docker"
    docker.write_text(f"#!/bin/sh\necho \"$@\" >> {marker}\nexit 0\n")
    docker.chmod(0o755)
    env = {**os.environ, **DUMMY_KEYS, "GATEWAY_DEBUG": value,
           "PATH": f"{stub_bin}:{os.environ['PATH']}", "TMPDIR": str(tmp_path)}
    proc = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False, timeout=60)
    assert proc.returncode != 0
    assert "GATEWAY_DEBUG must be 1/true or 0/false/empty" in proc.stderr
    assert not marker.exists()


def test_gateway_log_defaults_off_tmpfs_and_honors_override(tmp_path):
    """Issue #590: the container log no longer goes to /tmp (tmpfs: RAM on the
    reference host); default is the XDG state dir, GATEWAY_LOG overrides."""
    state = tmp_path / "state"
    default_log = _gateway_args(_render({"XDG_STATE_HOME": str(state)}))[1]
    assert default_log == f"log {state}/mainframe-rag/local-litellm-gateway.log"
    custom = tmp_path / "gw.log"
    assert _gateway_args(_render({"GATEWAY_LOG": str(custom)}))[1] == f"log {custom}"


def _load_trace_privacy(monkeypatch):
    """Import gateway/trace_privacy.py with only the LiteLLM logger stubbed
    (the pinned image provides it); the OpenTelemetry SDK is the real one."""
    import importlib.util
    import sys
    import types

    captured = {}

    class Config:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("litellm.integrations.opentelemetry")
    module.OpenTelemetryConfig = Config
    module.OpenTelemetry = lambda config: config
    for name in ("litellm", "litellm.integrations"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "litellm.integrations.opentelemetry", module)
    spec = importlib.util.spec_from_file_location("trace_privacy_under_test", SCRIPT.parent / "gateway" / "trace_privacy.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded, captured


def test_trace_export_keeps_only_allowlisted_attributes(monkeypatch):
    """Issue #636: content, key-derived metadata, hidden params, key records,
    exception text, events and status descriptions never reach the exporter;
    model, operation, usage and status do. Unknown future attributes drop."""
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExportResult
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import SpanContext, Status, StatusCode, TraceFlags

    tp, captured = _load_trace_privacy(monkeypatch)
    assert captured["capture_message_content"] == "NO_CONTENT"
    canary = "CANARY-manual-excerpt-and-key-material"
    leaky = {
        "gen_ai.input.messages": canary, "gen_ai.output.messages": canary,
        "llm.strict_openai.messages": canary, "llm.None.input": canary,
        "metadata.user_api_key_hash": canary, "metadata.user_api_key_alias": canary,
        "hidden_params": canary, "response.token_id": canary, "response.key_name": canary,
        "exception": canary, "error.message": canary, "some.future.attribute": canary,
    }
    safe = {"gen_ai.request.model": "Qwen/Qwen3-Embedding-0.6B", "llm.request.type": "embedding",
            "gen_ai.usage.input_tokens": 3, "http.response.status_code": 200, "error.type": "Timeout"}
    span = ReadableSpan(
        name="litellm_request", attributes={**leaky, **safe},
        context=SpanContext(trace_id=0x1, span_id=0x2, is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED)),
        events=(Event("gen_ai.content.prompt", {"gen_ai.prompt": canary}),),
        status=Status(StatusCode.ERROR, canary), start_time=1, end_time=2,
    )
    sink = InMemorySpanExporter()
    exporter = tp.AllowlistExporter(sink)
    assert exporter.export([span]) is SpanExportResult.SUCCESS
    assert exporter.force_flush()
    (out,) = sink.get_finished_spans()
    assert dict(out.attributes) == safe
    assert out.events == () and out.links == ()
    assert out.status.status_code is StatusCode.ERROR and not out.status.description
    assert out.name == "litellm_request" and (out.start_time, out.end_time) == (1, 2)
    assert canary not in repr(out.to_json())
    exporter.shutdown()


TRANSPORT_IMAGE = "example.invalid/gateway@sha256:" + "a" * 64
TRANSPORT_NETWORK = "rag-owned-model-net"
BRIDGE_ID = "b" * 64

GATEWAY_DOCKER_FAKE = r'''
import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ["FAKE_GATEWAY_ROOT"])
args = sys.argv[1:]
kind = pathlib.Path(sys.argv[0]).name
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps([kind, args]) + "\n")
if kind == "curl":
    if "-w" in args:
        print("401" if any("sk-local-wrong" in arg for arg in args) else "200", end="")
    sys.exit(0)
state_path = root / "networks.json"
networks = json.loads(state_path.read_text())
containers_path = root / "containers.json"
containers = json.loads(containers_path.read_text()) if containers_path.exists() else {}
def container(value):
    return next((item for item in containers.values() if value in (item["name"], item["id"])), None)
def retain_containers():
    containers_path.write_text(json.dumps(containers))
def network(value):
    return next((item for item in networks.values() if value in (item["name"], item["id"])), None)
if args[:2] == ["image", "inspect"]:
    sys.exit(1 if os.environ.get("FAKE_GATEWAY_SCENARIO") == "missing-image" else 0)
if args[:2] == ["network", "inspect"]:
    item = network(args[2])
    if item is None:
        sys.exit(1)
    assert args[3:] == ["--format", "{{.Id}} {{.Driver}}"]
    print(item["id"] + " " + item["driver"])
elif args[:2] == ["network", "create"]:
    if network(args[2]):
        sys.exit(1)
    count_path = root / "network-count"
    count = int(count_path.read_text()) + 1 if count_path.exists() else 1
    count_path.write_text(str(count))
    networks[args[2]] = {"name": args[2], "id": f"{count:064x}", "driver": "bridge"}
    if os.environ.get("FAKE_GATEWAY_SCENARIO") == "replace-after-create":
        networks[args[2]]["id"] = "d" * 64
    state_path.write_text(json.dumps(networks))
    print(f"{count:064x}")
elif args[:2] == ["network", "ls"]:
    assert args[2:] == ["--no-trunc", "--filter", args[4], "--format", "{{.ID}}"]
    identifier = args[4].removeprefix("id=")
    item = network(identifier)
    if item:
        print(item["id"])
elif args[:2] == ["network", "rm"]:
    item = network(args[2])
    if item is None:
        sys.exit(1)
    # Docker daemon resources outlive the client process. A connected PG
    # prevents network removal even after its original shell was killed.
    if any(value["network"] in (item["id"], item["name"]) for value in containers.values()):
        sys.exit(1)
    del networks[item["name"]]
    state_path.write_text(json.dumps(networks))
elif args and args[0] == "ps":
    selected = list(containers.values())
    if "--filter" in args:
        label = args[args.index("--filter") + 1]
        assert label.startswith("label=io.mainframe-rag.gateway-run=")
        key, expected = label.removeprefix("label=").split("=", 1)
        selected = [item for item in selected if item["labels"].get(key) == expected]
    if "--format" in args:
        assert args[args.index("--format") + 1] == "{{.Names}}"
        print("\n".join(item["name"] for item in selected))
    else:
        assert "-aq" in args and "--no-trunc" in args
        print("\n".join(item["id"] for item in selected))
elif args and args[0] == "inspect":
    format_at = args.index("--format")
    targets = [value for index, value in enumerate(args[1:], 1) if index not in (format_at, format_at + 1)]
    assert len(targets) == 1
    item = container(targets[0])
    if item is None:
        sys.exit(1)
    assert args[format_at + 1] == '{{.Id}} {{index .Config.Labels "io.mainframe-rag.gateway-run"}}'
    print(item["id"] + " " + item["labels"].get("io.mainframe-rag.gateway-run", "<no value>"))
elif args and args[0] in ("stop", "rm"):
    targets = [value for value in args[1:] if not value.startswith("-")]
    failed = False
    for target in targets:
        item = container(target)
        if item is None:
            failed = True
        else:
            del containers[item["id"]]
    retain_containers()
    sys.exit(1 if failed else 0)
elif args and args[0] == "exec":
    item = container(args[1])
    assert args[2:] == ["pg_isready", "-U", "litellm"]
    if item is None:
        sys.exit(1)
    (root / "pg-readiness-entered").write_text(item["id"])
    sys.exit(1 if os.environ.get("FAKE_GATEWAY_SCENARIO") == "postgres-notready" else 0)
elif args and args[0] == "run":
    item = network(args[args.index("--network") + 1])
    if item is None:
        sys.exit(7)
    if "--entrypoint" in args:
        assert args[args.index("--entrypoint") + 1] == "python"
        assert "--add-host=host.docker.internal:host-gateway" in args
        assert args[args.index("--pull") + 1] == "never"
        code_index = args.index("-c")
        assert args[code_index - 1] == os.environ["FAKE_GATEWAY_IMAGE"]
        urls = args[code_index + 2:]
        expected = [os.environ["FAKE_GATEWAY_REASONING_URL"], os.environ["FAKE_GATEWAY_EMBED_URL"]]
        if urls != expected:
            sys.exit(8)
        # The discriminating case: default bridge remains healthy while the
        # later gateway's actual network cannot reach these same addresses.
        if (item["name"] != "bridge" and
                (item["name"] != os.environ["FAKE_GATEWAY_NETWORK"] or
                 os.environ.get("FAKE_GATEWAY_SCENARIO") == "gateway-network-down")):
            sys.exit(7)
        subprocess.run([sys.executable, "-c", args[code_index + 1], *urls], check=True)
    else:
        name = args[args.index("--name") + 1]
        if container(name):
            sys.exit(1)
        counter_path = root / "container-count"
        count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
        counter_path.write_text(str(count))
        identifier = f"{count:064x}"
        labels = {}
        for index, value in enumerate(args):
            if value == "--label":
                key, label = args[index + 1].split("=", 1)
                labels[key] = label
            elif value.startswith("--label="):
                key, label = value.removeprefix("--label=").split("=", 1)
                labels[key] = label
        containers[identifier] = {"id": identifier, "name": name, "network": item["id"], "labels": labels}
        retain_containers()
        if "-d" in args:
            assert args[args.index("-v") + 1].endswith(":/var/lib/postgresql/data")
            print(identifier)
        else:
            assert "--add-host=host.docker.internal:host-gateway" in args
            mount = args[args.index("-v") + 1]
            assert mount.endswith(":/app/gateway:ro")
            source = mount.removesuffix(":/app/gateway:ro")
            (root / "rendered-gateway.yaml").write_text((pathlib.Path(source) / "config.yaml").read_text())
            # A short successful gateway exits naturally. Docker --rm removes
            # it; the daemon PG remains until explicit owned cleanup.
            assert "--rm" in args
            del containers[identifier]
            retain_containers()
            count = int(os.environ.get("FAKE_GATEWAY_LOG_BYTES", "0"))
            sys.stdout.buffer.write(b"X" * count)
            sys.stdout.buffer.flush()
            sys.stderr.buffer.write(b"Y" * count)
            sys.stderr.buffer.flush()
            sys.exit(int(os.environ.get("FAKE_GATEWAY_EXIT_CODE", "0")))
elif args and args[0] == "volume":
    pass
else:
    raise AssertionError("unsupported fake Docker operation")
'''


@pytest.fixture
def gateway_transport(tmp_path):
    root = tmp_path / "gateway-owner"
    root.mkdir()
    binary_dir = root / "bin"
    binary_dir.mkdir()
    for name in ("owned-docker", "curl"):
        executable = binary_dir / name
        executable.write_text(f"#!{sys.executable}\n" + GATEWAY_DOCKER_FAKE)
        executable.chmod(0o755)
    # Explicit Docker executable selection must survive every owner operation.
    docker = binary_dir / "docker"
    docker.write_text("#!/bin/sh\nexit 99\n")
    docker.chmod(0o755)
    (root / "networks.json").write_text(json.dumps({
        "bridge": {"name": "bridge", "id": BRIDGE_ID, "driver": "bridge"},
    }))
    (root / "containers.json").write_text("{}")
    hits = []
    servers = []
    threads = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.server.server_port, self.path))
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, *args):
            pass

    for _ in range(2):
        server = http.server.ThreadingHTTPServer(("127.0.0.2", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        servers.append(server)
        threads.append(thread)
    urls = [f"http://127.0.0.2:{server.server_port}/v1" for server in servers]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GATEWAY_", "PG_", "LITELLM_", "FAKE_GATEWAY_"))
           and key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    env.update({
        **DUMMY_KEYS, "GATEWAY_NAME": "rag-owned-gateway", "PG_NET": TRANSPORT_NETWORK,
        "GATEWAY_DOCKER_BIN": str(binary_dir / "owned-docker"),
        "LITELLM_IMAGE": TRANSPORT_IMAGE, "GATEWAY_REASONING_URL": urls[0],
        "GATEWAY_EMBED_URL": urls[1], "GATEWAY_OTEL_ENDPOINT": "http://unused.invalid:4318",
        "GATEWAY_ENV_FILE": str(root / "gateway.env"), "GATEWAY_LOG": str(root / "gateway.log"),
        "TMPDIR": str(root), "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "FAKE_GATEWAY_ROOT": str(root), "FAKE_GATEWAY_NETWORK": TRANSPORT_NETWORK,
        "FAKE_GATEWAY_IMAGE": TRANSPORT_IMAGE, "FAKE_GATEWAY_REASONING_URL": urls[0],
        "FAKE_GATEWAY_EMBED_URL": urls[1],
    })
    try:
        yield root, env, urls, hits
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=1)
            assert not thread.is_alive()


def _transport_operation(lab, operation="transport-probe", **extra):
    return subprocess.run(["sh", str(SCRIPT), operation], env={**lab[1], **extra},
                          capture_output=True, text=True, timeout=15, check=False)


def _transport_calls(lab):
    path = lab[0] / "calls.jsonl"
    return [json.loads(line)[1] for line in path.read_text().splitlines()] if path.exists() else []


def test_transport_probe_uses_gateway_image_network_and_actual_addresses_without_services(gateway_transport):
    root, _, urls, hits = gateway_transport
    result = _transport_operation(gateway_transport)
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof == {
        "network_name": TRANSPORT_NETWORK, "network_id": "0" * 63 + "1", "created": True,
        "gateway_image": TRANSPORT_IMAGE, "reasoning_url": urls[0], "embedding_url": urls[1],
    }
    calls = _transport_calls(gateway_transport)
    runs = [args for args in calls if args[0] == "run"]
    assert len(runs) == 1 and "--entrypoint" in runs[0]
    assert runs[0][runs[0].index("--network") + 1] == proof["network_id"]
    assert hits == [(int(urls[0].split(":")[2].split("/")[0]), "/health"),
                    (int(urls[1].split(":")[2].split("/")[0]), "/health")]
    assert not (root / "gateway.env").exists() and not (root / "gateway.log").exists()
    assert not list(root.glob("local-gateway.*"))
    assert all(args[0] not in ("exec", "stop", "volume") for args in calls)


def test_gateway_network_failure_refuses_despite_healthy_host_and_default_bridge_then_recovers(gateway_transport):
    import urllib.request

    root, env, urls, _ = gateway_transport
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for origin in urls:
        with opener.open(origin.removesuffix("/v1") + "/health", timeout=2) as response:
            assert response.status == 200
    # A wrong default-bridge probe really succeeds under the same failure.
    bridge = subprocess.run([
        env["GATEWAY_DOCKER_BIN"], "run", "--rm", "--pull", "never", "--network", "bridge",
        "--add-host=host.docker.internal:host-gateway", "--entrypoint", "python", TRANSPORT_IMAGE,
        "-c", "import sys,urllib.request\nfor u in sys.argv[1:]: urllib.request.urlopen(u.removesuffix('/v1')+'/health',timeout=2).close()",
        *urls,
    ], env={**env, "FAKE_GATEWAY_SCENARIO": "gateway-network-down"}, capture_output=True,
        text=True, timeout=5, check=False)
    assert bridge.returncode == 0, bridge.stderr
    failed = _transport_operation(gateway_transport, FAKE_GATEWAY_SCENARIO="gateway-network-down")
    assert failed.returncode != 0 and "gateway network transport probe failed" in failed.stderr
    failed_identity = json.loads(failed.stdout)
    assert failed_identity["created"] is True
    assert json.loads((root / "networks.json").read_text()) == {
        "bridge": {"name": "bridge", "id": BRIDGE_ID, "driver": "bridge"},
    }
    recovered = _transport_operation(gateway_transport)
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["network_id"] == "0" * 63 + "2"


def test_preexisting_network_survives_probe_failure_and_release(gateway_transport):
    root = gateway_transport[0]
    network_file = root / "networks.json"
    networks = json.loads(network_file.read_text())
    networks[TRANSPORT_NETWORK] = {"name": TRANSPORT_NETWORK, "id": "c" * 64, "driver": "bridge"}
    network_file.write_text(json.dumps(networks))
    failed = _transport_operation(gateway_transport, FAKE_GATEWAY_SCENARIO="gateway-network-down")
    assert failed.returncode != 0
    identity = json.loads(failed.stdout)
    assert identity["created"] is False and identity["network_id"] == "c" * 64
    release = _transport_operation(gateway_transport, "transport-release",
                                   GATEWAY_TRANSPORT_NETWORK_ID=identity["network_id"],
                                   GATEWAY_TRANSPORT_NETWORK_CREATED="0")
    assert release.returncode == 0, release.stderr
    assert json.loads(network_file.read_text()) == networks
    assert not any(args[:2] == ["network", "rm"] or args[0] == "stop"
                   for args in _transport_calls(gateway_transport))


@pytest.mark.parametrize("existing", ["container-name", "owner-label"])
def test_preexisting_container_or_owner_scope_is_never_adopted_by_native_launch(gateway_transport, existing):
    root, env, _, _ = gateway_transport
    owner = "rag-" + "a" * 16
    foreign = {"f" * 64: {
        "id": "f" * 64,
        "name": env["GATEWAY_NAME"] + "-pg" if existing == "container-name" else "preexisting-owner-container",
        "network": BRIDGE_ID,
        "labels": {"io.mainframe-rag.gateway-run": owner if existing == "owner-label" else "rag-" + "b" * 16},
    }}
    containers = root / "containers.json"
    containers.write_text(json.dumps(foreign))
    probe = _transport_operation(gateway_transport, GATEWAY_RUN_OWNER=owner)
    if existing == "owner-label":
        assert probe.returncode != 0, "probe adopted a preexisting run-labelled daemon"
        assert not any(argv[:2] == ["network", "create"] for argv in _transport_calls(gateway_transport))
    else:
        assert probe.returncode == 0, probe.stderr
        identity = json.loads(probe.stdout)
        refused = _transport_operation(
            gateway_transport, "serve", GATEWAY_RUN_OWNER=owner,
            GATEWAY_TRANSPORT_NETWORK_ID=identity["network_id"], GATEWAY_TRANSPORT_NETWORK_CREATED="1",
        )
        assert refused.returncode != 0 and "already exists" in refused.stderr
        released = _transport_operation(
            gateway_transport, "transport-release", GATEWAY_RUN_OWNER=owner,
            GATEWAY_TRANSPORT_NETWORK_ID=identity["network_id"], GATEWAY_TRANSPORT_NETWORK_CREATED="1",
        )
        assert released.returncode == 0, released.stderr
    assert json.loads(containers.read_text()) == foreign
    assert all(argv[0] not in ("stop", "rm", "volume") for argv in _transport_calls(gateway_transport))


def test_probe_then_normal_launch_reuses_exact_network_and_renders_probed_urls(gateway_transport):
    root, _, urls, _ = gateway_transport
    probe = _transport_operation(gateway_transport)
    assert probe.returncode == 0, probe.stderr
    identity = json.loads(probe.stdout)
    started = _transport_operation(gateway_transport, "serve",
                                   GATEWAY_TRANSPORT_NETWORK_ID=identity["network_id"],
                                   GATEWAY_TRANSPORT_NETWORK_CREATED="1")
    assert started.returncode == 0, started.stderr
    normal_runs = [args for args in _transport_calls(gateway_transport)
                   if args[0] == "run" and "--entrypoint" not in args]
    assert len(normal_runs) == 2
    assert all(args[args.index("--network") + 1] == identity["network_id"] for args in normal_runs)
    config = yaml.safe_load((root / "rendered-gateway.yaml").read_text())
    assert [item["litellm_params"]["api_base"] for item in config["model_list"][:2]] == urls
    assert sum(args[:2] == ["network", "create"] for args in _transport_calls(gateway_transport)) == 1


def test_network_name_reuse_refuses_launch_and_preserves_replacement_during_cleanup(gateway_transport):
    root = gateway_transport[0]
    probe = _transport_operation(gateway_transport)
    assert probe.returncode == 0, probe.stderr
    identity = json.loads(probe.stdout)
    network_file = root / "networks.json"
    networks = json.loads(network_file.read_text())
    networks[TRANSPORT_NETWORK]["id"] = "d" * 64
    network_file.write_text(json.dumps(networks))
    before = len(_transport_calls(gateway_transport))
    refused = _transport_operation(gateway_transport, "serve",
                                   GATEWAY_TRANSPORT_NETWORK_ID=identity["network_id"],
                                   GATEWAY_TRANSPORT_NETWORK_CREATED="1", GATEWAY_RESET_KEYS="1")
    assert refused.returncode != 0 and "transport network identity changed" in refused.stderr
    release = _transport_operation(gateway_transport, "transport-release",
                                   GATEWAY_TRANSPORT_NETWORK_ID=identity["network_id"],
                                   GATEWAY_TRANSPORT_NETWORK_CREATED="1")
    # Absence of the old exact ID is successful cleanup; its replacement lives.
    assert release.returncode == 0, release.stderr
    following = _transport_calls(gateway_transport)[before:]
    assert all(args[0] not in ("run", "stop", "volume") and args[:2] != ["network", "rm"]
               for args in following)
    assert json.loads(network_file.read_text()) == networks


def test_owned_network_release_then_next_probe_uses_new_identity(gateway_transport):
    first = _transport_operation(gateway_transport)
    assert first.returncode == 0, first.stderr
    identity = json.loads(first.stdout)
    release = _transport_operation(gateway_transport, "transport-release",
                                   GATEWAY_TRANSPORT_NETWORK_ID=identity["network_id"],
                                   GATEWAY_TRANSPORT_NETWORK_CREATED="1")
    assert release.returncode == 0, release.stderr
    assert ["network", "rm", identity["network_id"]] in _transport_calls(gateway_transport)
    assert all(args[0] != "stop" for args in _transport_calls(gateway_transport))
    repeated = _transport_operation(gateway_transport, "transport-release",
                                    GATEWAY_TRANSPORT_NETWORK_ID=identity["network_id"],
                                    GATEWAY_TRANSPORT_NETWORK_CREATED="1")
    assert repeated.returncode == 0, repeated.stderr
    next_probe = _transport_operation(gateway_transport)
    assert next_probe.returncode == 0, next_probe.stderr
    assert json.loads(next_probe.stdout)["network_id"] == "0" * 63 + "2"


def test_network_replacement_after_create_cannot_inherit_cleanup_ownership(gateway_transport):
    root = gateway_transport[0]
    failed = _transport_operation(gateway_transport, FAKE_GATEWAY_SCENARIO="replace-after-create")
    assert failed.returncode != 0 and "transport network identity is unavailable" in failed.stderr
    assert json.loads((root / "networks.json").read_text())[TRANSPORT_NETWORK]["id"] == "d" * 64
    calls = _transport_calls(gateway_transport)
    assert ["network", "rm", "0" * 63 + "1"] in calls
    assert all(args[0] != "run" and args != ["network", "rm", "d" * 64] for args in calls)


def test_missing_pinned_image_refuses_before_network_creation(gateway_transport):
    failed = _transport_operation(gateway_transport, FAKE_GATEWAY_SCENARIO="missing-image")
    assert failed.returncode != 0 and "pinned image already present" in failed.stderr
    assert all(args[0] == "image" for args in _transport_calls(gateway_transport))


@pytest.mark.parametrize("overrides", [
    {"GATEWAY_REASONING_URL": "http://host.docker.internal:8000/v1"},
    {"LITELLM_IMAGE": "example.invalid/gateway:latest"},
])
def test_unprobed_address_or_unpinned_image_refuses_before_docker(gateway_transport, overrides):
    failed = _transport_operation(gateway_transport, **overrides)
    assert failed.returncode != 0
    assert "pinned image and numeric IPv4 HTTP backend URLs" in failed.stderr
    assert _transport_calls(gateway_transport) == []


BOUNDED_GATEWAY_ENV = {
    "GATEWAY_MEMORY_MIB": "2048", "PG_MEMORY_MIB": "512",
    "GATEWAY_LOG_MAX_BYTES": "256", "GATEWAY_DOCKER_LOG_MAX_BYTES": "8388608",
    "GATEWAY_DOCKER_LOG_FILES": "2",
}


def test_selected_gateway_limits_bound_both_owned_daemons_without_swap(gateway_transport):
    result = _transport_operation(gateway_transport, "serve", **BOUNDED_GATEWAY_ENV)
    assert result.returncode == 0, result.stderr
    runs = [args for args in _transport_calls(gateway_transport)
            if args[0] == "run" and "--entrypoint" not in args]
    assert len(runs) == 2
    for args, memory in zip(runs, ("512m", "2048m"), strict=True):
        assert args[args.index("--memory") + 1] == memory
        assert args[args.index("--memory-swap") + 1] == memory
        assert args[args.index("--log-driver") + 1] == "json-file"
        options = [args[index + 1] for index, value in enumerate(args) if value == "--log-opt"]
        assert options == ["max-size=8388608", "max-file=2"]
    assert json.loads((gateway_transport[0] / "containers.json").read_text()) == {}


@pytest.mark.parametrize("override", [
    {"PG_MEMORY_MIB": ""}, {"GATEWAY_MEMORY_MIB": "1048577"},
    {"GATEWAY_LOG_MAX_BYTES": "67108865"}, {"GATEWAY_DOCKER_LOG_FILES": "9"},
    {"GATEWAY_DOCKER_LOG_MAX_BYTES": "-1"},
])
def test_invalid_or_incomplete_gateway_limits_refuse_before_docker(gateway_transport, override):
    result = _transport_operation(gateway_transport, "serve", **{**BOUNDED_GATEWAY_ENV, **override})
    assert result.returncode != 0 and "gateway resource limits" in result.stderr
    assert _transport_calls(gateway_transport) == []


def test_gateway_release_ignores_invalid_startup_limits_and_allows_next_launch(gateway_transport):
    root = gateway_transport[0]
    probe = _transport_operation(gateway_transport)
    assert probe.returncode == 0, probe.stderr
    identity = json.loads(probe.stdout)
    owner = "rag-" + "e" * 16
    (root / "containers.json").write_text(json.dumps({"f" * 64: {
        "id": "f" * 64, "name": "original-owned-pg", "network": identity["network_id"],
        "labels": {"io.mainframe-rag.gateway-run": owner},
    }}))
    released = _transport_operation(
        gateway_transport, "transport-release", GATEWAY_RUN_OWNER=owner,
        GATEWAY_TRANSPORT_NETWORK_ID=identity["network_id"], GATEWAY_TRANSPORT_NETWORK_CREATED="1",
        GATEWAY_MEMORY_MIB="invalid", GATEWAY_LOG_MAX_BYTES="0",
    )
    assert released.returncode == 0, released.stderr
    assert json.loads((root / "containers.json").read_text()) == {}
    assert json.loads((root / "networks.json").read_text()) == {
        "bridge": {"name": "bridge", "id": BRIDGE_ID, "driver": "bridge"},
    }
    ordinary = _transport_operation(gateway_transport, "serve", **BOUNDED_GATEWAY_ENV)
    assert ordinary.returncode == 0, ordinary.stderr
    assert json.loads((root / "containers.json").read_text()) == {}


def test_saturated_gateway_log_preserves_docker_failure_reaps_and_next_launch_works(gateway_transport):
    root = gateway_transport[0]
    # Observe the real sink process lifetime through the executable boundary,
    # without changing its source or adding an application test-only option.
    python = root / "bin/python3"
    python.write_text(f"#!{sys.executable}\n" + """
import os, pathlib, sys
if any(pathlib.Path(arg).name == 'log_capture.py' for arg in sys.argv[1:]):
    (pathlib.Path(os.environ['FAKE_GATEWAY_ROOT']) / 'capture-pid').write_text(str(os.getpid()))
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
""")
    python.chmod(0o755)
    failed = _transport_operation(gateway_transport, "serve", **BOUNDED_GATEWAY_ENV,
                                  FAKE_GATEWAY_LOG_BYTES="262144", FAKE_GATEWAY_EXIT_CODE="37")
    assert failed.returncode == 37, failed.stderr
    log = (root / "gateway.log").read_bytes()
    assert len(log) == 256 and log.startswith(b"X" * 64)
    assert log.endswith(b"\n[gateway log truncated]\n")
    assert "excess output was discarded" in failed.stderr
    assert (root / "gateway.log").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ProcessLookupError):
        os.kill(int((root / "capture-pid").read_text()), 0)
    assert json.loads((root / "containers.json").read_text()) == {}
    assert json.loads((root / "networks.json").read_text()) == {
        "bridge": {"name": "bridge", "id": BRIDGE_ID, "driver": "bridge"},
    }
    assert all(args[:2] != ["volume", "rm"] for args in _transport_calls(gateway_transport))
    ordinary = _transport_operation(gateway_transport, "serve", **BOUNDED_GATEWAY_ENV)
    assert ordinary.returncode == 0, ordinary.stderr
    assert (root / "gateway.log").read_bytes() == b""
    assert (root / "gateway.log.1").read_bytes() == log
    with pytest.raises(ProcessLookupError):
        os.kill(int((root / "capture-pid").read_text()), 0)
    assert json.loads((root / "containers.json").read_text()) == {}


def test_failed_gateway_log_destination_still_drains_without_exception_text(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts/gateway/log_capture.py"), str(tmp_path), "256"],
        input=b"original bytes without newlines" * 16384, capture_output=True, timeout=5, check=False,
    )
    assert result.returncode == 1
    assert result.stdout == b"" and result.stderr == b"Gateway host log capture failed.\n"


# --- #862: retrieval-only gateway (embed + rerank legs, no reasoning leg) ----
RERANK_ENV = {"GATEWAY_REASONING": "off", "GATEWAY_EMBED_MODEL": "Qwen/Qwen3-Embedding-8B",
              "GATEWAY_RERANK_MODEL": "Qwen/Qwen3-Reranker-8B",
              "GATEWAY_RERANK_URL": "http://172.17.0.1:8000/v1",
              "GATEWAY_EMBED_URL": "http://172.17.0.1:8001/v1"}


def test_reasoning_off_renders_embed_and_rerank_routes_only():
    cfg = yaml.safe_load(_read_cfg(_render(RERANK_ENV)))
    assert [(item["model_name"], item["litellm_params"]["model"], item["litellm_params"]["api_base"])
            for item in cfg["model_list"]] == [
        ("Qwen/Qwen3-Embedding-8B", "openai/Qwen/Qwen3-Embedding-8B", "http://172.17.0.1:8001/v1"),
        ("Qwen/Qwen3-Reranker-8B", "hosted_vllm/Qwen/Qwen3-Reranker-8B", "http://172.17.0.1:8000/v1"),
    ]
    # /tokenize targets the reasoning server and /v1/score the reranker: only
    # the latter exists in this profile, and it must reach the rerank backend.
    assert cfg["general_settings"]["pass_through_endpoints"] == [
        {"path": "/v1/score", "target": "http://172.17.0.1:8000/v1/score", "methods": ["POST"]}]
    assert cfg["litellm_settings"]["callbacks"] == ["scoped_passthrough.guard"]


@pytest.mark.parametrize("value", [None, "", "on"])
def test_reasoning_leg_is_on_by_default_and_explicit_on_is_identical(value):
    baseline = _read_cfg(_render())
    explicit = _read_cfg(_render({"GATEWAY_REASONING": value} if value else None))
    assert explicit == baseline and "gemma-4-E4B" in explicit and '"/tokenize"' in explicit


@pytest.mark.parametrize("value", ["false", "OFF", "0", "no"])
def test_reasoning_switch_accepts_only_on_or_off(value):
    proc = _render({"GATEWAY_REASONING": value})
    assert proc.returncode == 1 and "GATEWAY_REASONING must be on or off" in proc.stderr


def test_reasoning_off_env_file_has_per_leg_embed_and_rerank_keys_but_no_llm_leg(tmp_path):
    env_file = tmp_path / "gateway.env"
    proc = _render({**RERANK_ENV, "GATEWAY_ENV_FILE": str(env_file),
                    "GATEWAY_EMBED_MODEL_REVISION": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"})
    _read_cfg(proc)
    body = env_file.read_text()
    for line in (
        "export EMBED_BASE_URL=http://localhost:4000/v1", "export EMBED_MODEL=Qwen/Qwen3-Embedding-8B",
        "export EMBED_API_KEY=sk-test-embed",
        "export EMBED_MODEL_REVISION=1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
        "export RERANK_ENABLED=true", "export RERANK_BASE_URL=http://localhost:4000/v1",
        "export RERANK_MODEL=Qwen/Qwen3-Reranker-8B", "export RERANK_API_KEY=sk-test-rerank",
    ):
        assert line in body, line
    assert "LLM_" not in body and "sk-test-llm" not in body and "master" not in body
    assert (env_file.stat().st_mode & 0o777) == 0o600


def test_reasoning_off_launch_checks_only_the_embed_and_rerank_keys_and_next_default_launch_works(gateway_transport):
    root, _, _, _ = gateway_transport
    off = _transport_operation(gateway_transport, "serve", **RERANK_ENV)
    assert off.returncode == 0, off.stderr
    config = yaml.safe_load((root / "rendered-gateway.yaml").read_text())
    assert [item["model_name"] for item in config["model_list"]] == [
        "Qwen/Qwen3-Embedding-8B", "Qwen/Qwen3-Reranker-8B"]
    curl = [args for kind, args in (json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines())
            if kind == "curl"]
    assert curl and not any("sk-test-llm" in " ".join(args) for args in curl)
    assert {key for args in curl for key in ("sk-test-embed", "sk-test-rerank") if f"Bearer {key}" in " ".join(args)} == {
        "sk-test-embed", "sk-test-rerank"}
    assert "LLM_" not in (root / "gateway.env").read_text()
    # The next ordinary launch keeps the reasoning leg.
    (root / "calls.jsonl").unlink()
    on = _transport_operation(gateway_transport, "serve")
    assert on.returncode == 0, on.stderr
    assert any("Bearer sk-test-llm" in " ".join(args)
               for args in (json.loads(line)[1] for line in (root / "calls.jsonl").read_text().splitlines()))
    assert "LLM_API_KEY=sk-test-llm" in (root / "gateway.env").read_text()
