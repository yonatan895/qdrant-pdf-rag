"""Render + fail-closed tests for scripts/run_local_gateway.sh (local-dev only).

Hermetic: every case runs with GATEWAY_DRYRUN=1 (render-only, exits before
any docker call) and dummy sk-test-* keys, so no daemon, no network, no GPU.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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


def _render(extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "GATEWAY_DRYRUN": "1", **DUMMY_KEYS, **(extra_env or {})}
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)


def _read_cfg(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == 0, proc.stderr
    cfg_dir = Path(proc.stdout.strip().splitlines()[-1])
    try:
        return (cfg_dir / "config.yaml").read_text()
    finally:
        shutil.rmtree(cfg_dir, ignore_errors=True)


def test_render_routes_three_legs_to_expected_backends():
    text = _read_cfg(_render())
    assert "model_name: google/gemma-4-E4B-it-qat-mobile-ct" in text
    assert "model: strict_openai/google/gemma-4-E4B-it-qat-mobile-ct" in text
    assert "api_base: http://host.docker.internal:8000/v1" in text
    # reasoning_effort must reach vLLM (the agent's control), not be dropped.
    assert 'allowed_openai_params: ["reasoning_effort"]' in text
    assert "model_name: Qwen/Qwen3-Embedding-0.6B" in text
    assert "model: openai/Qwen/Qwen3-Embedding-0.6B" in text
    assert "api_base: http://host.docker.internal:8001/v1" in text
    # Native /rerank goes through the hosted_vllm provider (self-hosted vLLM
    # rerank in the pinned image; Cohere-shaped in/out).
    assert "model_name: BAAI/bge-reranker-v2-m3" in text
    assert "model: hosted_vllm/BAAI/bge-reranker-v2-m3" in text
    assert "api_base: http://host.docker.internal:8002/v1" in text


def test_render_carries_master_key_and_score_passthrough():
    text = _read_cfg(_render())
    assert "master_key: sk-test-master" in text
    # No native /v1/score in LiteLLM: exact-path pass-through to the vLLM
    # backend keeps the score_first wire order exercisable.
    assert 'path: "/v1/score"' in text
    assert 'target: "http://host.docker.internal:8002/v1/score"' in text
    assert 'methods: ["POST"]' in text


def test_render_otel_callback_only_when_endpoint_set():
    # The local waterfall includes the platform stand-in: LiteLLM exports
    # spans when the stack found a local Jaeger. No endpoint (standalone
    # dry-run) keeps only the required upstream finish guard.
    off = yaml.safe_load(_read_cfg(_render()))
    assert off['litellm_settings']['custom_provider_map'] == [
        {'provider': 'strict_openai', 'custom_handler': 'strict_finish.strict_openai'}
    ]
    assert off['litellm_settings']['callbacks'] == ['scoped_passthrough.guard']
    on = yaml.safe_load(_read_cfg(_render({"GATEWAY_OTEL_ENDPOINT": "http://host.docker.internal:4318"})))
    assert on['litellm_settings']['custom_provider_map'] == off['litellm_settings']['custom_provider_map']
    # Issue #636: never the bare "otel" callback — it exports content,
    # key hashes and hidden params. The allowlisting exporter replaces it.
    assert on['litellm_settings']['callbacks'] == ['scoped_passthrough.guard', 'trace_privacy.otel']


@pytest.mark.parametrize('endpoint', ['', 'http://host.docker.internal:4318'])
def test_render_disables_content_and_key_logging(endpoint):
    """Issue #636 defence in depth: message content and API-key-derived
    metadata are off for every logging callback, traced or not."""
    cfg = yaml.safe_load(_read_cfg(_render({'GATEWAY_OTEL_ENDPOINT': endpoint} if endpoint else None)))
    assert cfg['litellm_settings']['turn_off_message_logging'] is True
    assert cfg['litellm_settings']['redact_user_api_key_info'] is True
    assert 'otel' not in cfg['litellm_settings']['callbacks']


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


@pytest.mark.parametrize('url', ['http://gpu:8000', 'http://gpu:8000/v1', 'http://gpu:8000/v1/'])
def test_tokenize_targets_reasoning_origin(url):
    cfg = yaml.safe_load(_read_cfg(_render({'GATEWAY_REASONING_URL': url})))
    routes = cfg['general_settings']['pass_through_endpoints']
    route = next(r for r in routes if r['path'] == '/tokenize')
    assert route == {'path': '/tokenize', 'target': 'http://gpu:8000/tokenize', 'methods': ['POST']}


def test_render_honors_url_and_model_overrides():
    text = _read_cfg(
        _render(
            {
                "GATEWAY_RERANK_URL": "http://gpu-box:8002/v1",
                "GATEWAY_RERANK_MODEL": "BAAI/bge-reranker-v2-m3",
            }
        )
    )
    assert "api_base: http://gpu-box:8002/v1" in text
    assert 'target: "http://gpu-box:8002/v1/score"' in text


def test_render_generates_keys_when_unset():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GATEWAY_")}
    env["GATEWAY_DRYRUN"] = "1"
    proc = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)
    text = _read_cfg(proc)
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
        "export RERANK_ENABLED=true",
        "export RERANK_BASE_URL=http://localhost:4000/v1",
        "export RERANK_API_KEY=sk-test-rerank",
    ):
        assert line in body, line
    assert "master_key" not in body and "sk-test-master" not in body
    assert (env_file.stat().st_mode & 0o777) == 0o600


def test_env_file_absent_when_unset(tmp_path):
    proc = _render({"TMPDIR": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    _read_cfg(proc)
    assert list(tmp_path.glob("*.env")) == []


@pytest.mark.parametrize("var", ["GATEWAY_MASTER_KEY", "GATEWAY_LLM_KEY", "GATEWAY_EMBED_KEY", "GATEWAY_RERANK_KEY"])
def test_bad_key_charset_fails_closed(var):
    proc = _render({var: "sk-bad_$key!"})
    assert proc.returncode != 0
    assert "must contain only alphanumerics" in proc.stderr


@pytest.mark.parametrize(
    "var", ["GATEWAY_REASONING_URL", "GATEWAY_EMBED_URL", "GATEWAY_RERANK_URL"]
)
def test_bare_hostname_url_fails_closed(var):
    proc = _render({var: "gpu-box:8002"})
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
    [(None, False), ("", False), ("0", False), ("false", False), ("1", True), ("true", True)],
)
def test_detailed_debug_only_when_explicitly_enabled(value, debug_on):
    """Issue #590: the old ${GATEWAY_DEBUG:+...} expansion turned
    --detailed_debug on for the default "0" (and for any non-empty value),
    logging full request bodies (manual text). Off unless 1/true."""
    env = {} if value is None else {"GATEWAY_DEBUG": value}
    if value is None:
        env_base = {k: v for k, v in os.environ.items() if k != "GATEWAY_DEBUG"}
        proc = subprocess.run(
            ["sh", str(SCRIPT)], capture_output=True, text=True, check=False,
            env={**env_base, "GATEWAY_DRYRUN": "1", **DUMMY_KEYS},
        )
    else:
        proc = _render(env)
    args = _gateway_args(proc)[0]
    assert args.startswith("--config /app/gateway/config.yaml")
    assert ("--detailed_debug" in args) is debug_on


@pytest.mark.parametrize("value", ["yes", "2", "on", "debug"])
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
