"""scripts/airgap/validate.sh pre-flight validation tests (issue #15).

Tests validate.sh in dry-run mode against hermetic stubs:
- Required env vars fail-closed when unset.
- Format checks: positive integer DENSE_DIM, http/https EMBED_BASE_URL.
- Storage class checks: NFS-looking names refused.
- Missing CLI tools fail-closed.
- Clean configuration exits 0.
"""

import os
import re
import shutil

import pytest

from tests.helpers_airgap import (
    D53595E_SHAPED_ENV,
    REPO,
    RETIRED_ENV_KEYS,
    RETIRED_ENV_VALUES,
    STUB_TOOL,
    copy_chart,
    make_bin_tree,
    retired_keys_listed,
    run_sh,
    symlink_tools,
    write_signed_manifest,
    write_stub,
)

IMAGE_SHA = "b" * 40


@pytest.fixture
def tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "validate.sh"])
    copy_chart(tmp_path)
    for name in ("skopeo", "helm", "kubectl", "oc"):
        write_stub(tmp_path / "bin" / name, STUB_TOOL)
    return tmp_path


NOT_FOUND = 'Error from server (NotFound): objects "x" not found'
FORBIDDEN = "Error from server (Forbidden): the object is forbidden: User cannot get resource"


def _kubectl_stub(resource, stderr_text, rc=1):
    """kubectl stub: any invocation naming `resource` fails with the given
    client error text; everything else succeeds."""
    return (
        "#!/bin/sh\nfor arg in \"$@\"; do\n"
        f"  if [ \"$arg\" = \"{resource}\" ]; then cat >&2 <<'EOT'\n{stderr_text}\nEOT\n    exit {rc}; fi\n"
        "done\nexit 0\n"
    )


def _run(tree, extra_env=None):
    env = {
        "PATH": f"{tree / 'bin'}:/usr/bin:/bin",
        "AIRGAP_DRYRUN": "1",
        # Issue #478: an empty regular file (not /dev/null) keeps the
        # environment-only shape while ignoring repo ./airgap.env. A
        # distinct name: case.env stays owned by per-test file content.
        "AIRGAP_ENV": _write_empty_env_file(tree),
        "IMAGE_SHA": IMAGE_SHA,
        "INTERNAL_REGISTRY": "reg.internal:5000",
        "NAMESPACE": "mainframe-rag",
        "STORAGE_CLASS": "standard",
        "EMBED_MODEL": "ibm-granite/granite-embedding-125m-english",
        "DENSE_DIM": "768",
        "EMBED_MODEL_REVISION": "rev-1",
        "EMBED_BASE_URL": "http://vllm:8000/v1",
        "OPENWEBUI_ENABLED": "false",  # default-on (ADR-0008): fixtures opt out explicitly
        # Issue #360: fixtures select the explicit single-node 1/1/1 profile;
        # the production preset is exercised by its own test below.
        "QDRANT_SHARD_NUMBER": "1",
        "QDRANT_REPLICATION_FACTOR": "1",
        "QDRANT_WRITE_CONSISTENCY_FACTOR": "1",
    }
    if extra_env:
        for k, v in extra_env.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return run_sh(tree / "scripts" / "airgap" / "validate.sh", env, tree)


def test_validate_clean_exits_zero(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    assert "SUCCESS: Pre-flight validation passed (dry-run mode)." in r.stdout
    assert "Collection policy: S=1 RF=1 W=1" in r.stdout
    assert "Tracing:           ON (bundled OpenObserve)" in r.stdout
    assert "Trace backend:     OpenObserve (bundled)" in r.stdout
    assert "jaeger" not in r.stdout.lower()
    assert "EMBED_MODEL_REVISION: rev-1" in r.stdout


RECORDING_TOOL = "#!/bin/sh\nprintf '%s\\n' \"$0 $*\" >> \"$TOOL_LOG\"\nexit 0\n"


@pytest.mark.parametrize("value", ["true", "TRUE", "yes", "Yes"])
def test_dryrun_spellings_are_a_dry_run_and_never_touch_the_cluster(tree, value):
    for name in ("skopeo", "helm", "kubectl", "oc"):
        write_stub(tree / "bin" / name, RECORDING_TOOL)
    log = tree / "tool.log"
    r = _run(tree, {"AIRGAP_DRYRUN": value, "TOOL_LOG": str(log)})
    assert r.returncode == 0, r.stderr
    assert "(dry-run mode)" in r.stdout
    assert not log.exists(), log.read_text()


@pytest.mark.parametrize("value", ["false", "0", "no"])
def test_dryrun_off_spellings_run_live(tree, value):
    for name in ("skopeo", "helm", "kubectl", "oc"):
        write_stub(tree / "bin" / name, RECORDING_TOOL)
    log = tree / "tool.log"
    _run(tree, {"AIRGAP_DRYRUN": value, "TOOL_LOG": str(log)})
    assert log.exists(), "an explicit live run must reach the cluster tools"


@pytest.mark.parametrize("value", [" 1", "1 ", "1\n", "on", "y", "2", "maybe", "tru"])
def test_unknown_dryrun_values_are_refused_before_any_stage(tree, value):
    for name in ("skopeo", "helm", "kubectl", "oc"):
        write_stub(tree / "bin" / name, RECORDING_TOOL)
    log = tree / "tool.log"
    r = _run(tree, {"AIRGAP_DRYRUN": value, "TOOL_LOG": str(log)})
    assert r.returncode != 0
    assert "AIRGAP_DRYRUN must be" in r.stderr
    assert not log.exists(), "a refused flag must not run any stage"


@pytest.mark.parametrize("extra_env,exit_code,messages", [
    pytest.param({'QDRANT_REPLICATION_FACTOR': None}, 1, ('collection distribution policy is incomplete', 'QDRANT_REPLICATION_FACTOR'),
                 id='validate_partial_policy_fails_closed'),
    pytest.param({'QDRANT_SHARD_NUMBER': '6', 'QDRANT_REPLICATION_FACTOR': '2', 'QDRANT_WRITE_CONSISTENCY_FACTOR': '3'}, 1, ('exceeds',),
                 id='validate_write_above_replication_fails_closed'),
    pytest.param({'OTEL_EXPORTER_OTLP_ENDPOINT': 'jaeger:4318'}, None, ('must be http(s) or off',),
                 id='validate_tracing_bad_endpoint_fails_closed'),
    pytest.param({'INTERNAL_REGISTRY': None, 'STORAGE_CLASS': None, 'EMBED_MODEL_REVISION': None}, None, ('INTERNAL_REGISTRY', 'STORAGE_CLASS', 'EMBED_MODEL_REVISION', 'required variables unset'),
                 id='validate_lists_all_missing_vars_at_once'),
    pytest.param({'INTERNAL_REGISTRY': None, 'REGISTRY_INTERNAL': 'old.example:5000'}, None, ('REGISTRY_INTERNAL (retired in 547d04d)', 'set INTERNAL_REGISTRY'),
                 id='validate_removed_registry_alias_is_refused'),
    pytest.param({'STORAGE_CLASS': 'nfs-client'}, None, ('looks like NFS',),
                 id='validate_nfs_storage_refused'),
    pytest.param({'DENSE_DIM': 'not-a-number'}, None, ('DENSE_DIM must be a positive integer',),
                 id='validate_dense_dim_non_integer_refused'),
    pytest.param({'DENSE_DIM': '0'}, None, ('DENSE_DIM must be greater than 0',),
                 id='validate_dense_dim_zero_refused'),
    pytest.param({'EMBED_MODEL_REVISION': '   '}, None, ('EMBED_MODEL_REVISION must be a non-blank',),
                 id='validate_whitespace_embed_revision_fails'),
    pytest.param({'RERANK_ENDPOINT_ORDER': 'bogus'}, None, ('RERANK_ENDPOINT_ORDER must be score_first or rerank_first',),
                 id='validate_rerank_endpoint_order_bad_value_refused'),
    pytest.param({'EMBED_BASE_URL': 'ftp://vllm:8000'}, None, ('EMBED_BASE_URL must begin with http:// or https://',),
                 id='validate_embed_url_bad_scheme_refused'),
    pytest.param({'GATEWAY_API_KEY_SECRET': 'Bad_Name!'}, None, ('GATEWAY_API_KEY_SECRET must be a DNS-subdomain name',),
                 id='validate_gateway_secret_bad_name_fails_closed'),
    pytest.param({'GATEWAY_BASE_URL': 'sample-api/v1'}, None, ('GATEWAY_BASE_URL must begin with http:// or https://',),
                 id='shared_gateway_invalid_url_refuses'),
    pytest.param({'AGENT_ROUTE': 'maybe'}, None, ('AGENT_ROUTE must be true/false',),
                 id='validate_route_invalid_boolean_refused_even_in_dryrun'),
    pytest.param({'AGENT_NETWORK_POLICY': 'maybe'}, None, ('AGENT_NETWORK_POLICY must be true/false',),
                 id='validate_network_policy_invalid_boolean_refused'),
    pytest.param({'AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES': 'ok, spaced'}, None,
                 ('AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES must be comma-separated namespace names',),
                 id='validate_network_policy_namespace_list_strict'),
    pytest.param({'AGENT_NETWORK_POLICY': 'false', 'AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES': 'mcp'}, None,
                 ('requires AGENT_NETWORK_POLICY to be enabled',),
                 id='validate_network_policy_allow_list_needs_the_policy'),
    pytest.param({'FEEDBACK_ENABLED': 'maybe'}, None, ('FEEDBACK_ENABLED must be true/false',),
                 id='validate_feedback_invalid_boolean_refused'),
    pytest.param({'FEEDBACK_ENABLED': 'true'}, None, ('FEEDBACK_SECRET is required',),
                 id='validate_feedback_needs_a_secret_name'),
    pytest.param({'FEEDBACK_ENABLED': 'true', 'FEEDBACK_SECRET': 'fb', 'AGENT_NETWORK_POLICY': 'false'}, None,
                 ('FEEDBACK_ENABLED=true requires AGENT_NETWORK_POLICY to be enabled',),
                 id='validate_feedback_needs_the_network_policy'),
    pytest.param({'FEEDBACK_ENABLED': 'true', 'FEEDBACK_SECRET': 'fb', 'FEEDBACK_RETENTION_DAYS': '0'}, None,
                 ('FEEDBACK_RETENTION_DAYS must be an integer from 1 to 3650',),
                 id='validate_feedback_retention_bounds'),
    pytest.param({'FEEDBACK_ENABLED': 'true', 'FEEDBACK_SECRET': 'fb', 'FEEDBACK_STORAGE_SIZE': 'big'}, None,
                 ('FEEDBACK_STORAGE_SIZE must be a quantity',),
                 id='validate_feedback_storage_quantity'),
    pytest.param({'FEEDBACK_ENABLED': 'true', 'FEEDBACK_SECRET': 'Bad_Name!'}, None,
                 ('FEEDBACK_SECRET must be a DNS-subdomain',),
                 id='validate_feedback_secret_name_fails_closed'),
])
def test_invalid_configuration_fails_closed(tree, extra_env, exit_code, messages):
    result = _run(tree, extra_env)
    assert result.returncode != 0 if exit_code is None else result.returncode == exit_code, result.stderr
    for message in messages:
        assert message in result.stderr


def test_validate_agent_write_key_fails_closed(tree):
    """Issue #366: a prod agent overlay wiring the full-access Qdrant key
    must fail preflight before anything reaches the cluster."""
    overlay = (
        tree / "charts/mainframe-rag/templates/agent-deployment.yaml"
    )
    overlay.write_text(overlay.read_text().replace("key: read-only-api-key", "key: api-key"))
    r = _run(tree)
    assert r.returncode != 0
    assert "read-only-api-key" in r.stderr


def test_validate_ingest_readonly_key_fails_closed(tree):
    """Issue #366 mirror: downgrading the ingest overlay to read-only must
    also fail preflight — ingest owns corpus mutation."""
    overlay = (
        tree / "charts/mainframe-rag/templates/ingest-job.yaml"
    )
    lines = [
        "key: read-only-api-key" if line.strip() == "key: api-key" else line
        for line in overlay.read_text().splitlines()
    ]
    overlay.write_text("\n".join(lines) + "\n")
    r = _run(tree)
    assert r.returncode != 0
    assert "to api-key" in r.stderr


def test_validate_ingest_copresent_readonly_key_fails_closed(tree):
    """Anti-revert parity with the agent gate: a co-present read-only key
    in the ingest overlay fails preflight even with api-key present."""
    overlay = (
        tree / "charts/mainframe-rag/templates/ingest-job.yaml"
    )
    text = overlay.read_text()
    anchor = "key: api-key\n"
    assert anchor in text
    overlay.write_text(
        text.replace(
            anchor,
            anchor
            + "            - name: QDRANT_READ_API_KEY\n"
            + "              valueFrom:\n"
            + "                secretKeyRef:\n"
            + "                  key: read-only-api-key\n"
            + "                  name: __QDRANT_RELEASE__-apikey\n",
        )
    )
    r = _run(tree)
    assert r.returncode != 0
    assert "read-only" in r.stderr


def test_validate_tracing_off_deploys_no_trace_backend_and_explicit_collector_passes_through(tree):
    # OpenObserve is the only bundled backend (#956): off removes it, an
    # explicit http(s) collector is reported as given; the retired
    # JAEGER_ENABLED switch is refused (#1070), never silently ignored.
    off = _run(tree, {"OTEL_EXPORTER_OTLP_ENDPOINT": "off"})
    assert off.returncode == 0, off.stderr
    assert "Trace backend:     not deployed" in off.stdout
    ok = _run(tree, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.platform:4318"})
    assert ok.returncode == 0, ok.stderr
    assert "Tracing:           ON (http://collector.platform:4318)" in ok.stdout


@pytest.mark.parametrize("var", ["LLM_BASE_URL"])
def test_validate_optional_model_url_bad_scheme_refused(tree, var):
    r = _run(tree, {var: "gateway.internal:4000/v1"})
    assert r.returncode != 0
    assert f"{var} must begin with http:// or https://" in r.stderr


def test_validate_missing_skopeo_fails(tree):
    os.remove(tree / "bin" / "skopeo")
    symlink_tools(tree, ("sh", "dirname", "awk", "sed", "head", "ls", "python3"))
    r = _run(tree, {"PATH": str(tree / "bin")})
    assert r.returncode != 0
    assert "skopeo is required" in r.stderr


POISON_ENV_FILE = """\
INTERNAL_REGISTRY=wrong.invalid:5000
NAMESPACE=file-ns
STORAGE_CLASS=nfs-client
EMBED_MODEL=file-model
EMBED_MODEL_REVISION=file-rev-should-lose
DENSE_DIM=not-a-number
EMBED_BASE_URL=ftp://file-vllm:8000
GATEWAY_API_KEY_SECRET=file-secret-should-lose
"""

VALID_ENV_FILE = """\
INTERNAL_REGISTRY=reg.internal:5000
NAMESPACE=mainframe-rag
STORAGE_CLASS=standard
EMBED_MODEL=ibm-granite/granite-embedding-125m-english
EMBED_MODEL_REVISION=file-rev-1
DENSE_DIM=768
EMBED_BASE_URL=http://vllm:8000/v1
"""


def _write_env_file(tree, content):
    p = tree / "case.env"
    p.write_text(content)
    return str(p)


def _write_empty_env_file(tree):
    p = tree / "empty.env"
    p.write_text("")
    return str(p)


def test_explicit_env_beats_env_file(tree):
    # Every file value here would fail validation on its own (NFS storage,
    # non-http URL, non-integer dim); exit 0 proves the explicit environment
    # won on all keys. The secret name is charset-valid either way, so
    # precedence is pinned by echoing the winner instead.
    r = _run(
        tree,
        {
            "AIRGAP_ENV": _write_env_file(tree, POISON_ENV_FILE),
            "GATEWAY_API_KEY_SECRET": "explicit-secret",
        },
    )
    assert r.returncode == 0, r.stderr
    assert "SUCCESS: Pre-flight validation passed (dry-run mode)." in r.stdout
    assert "GATEWAY_API_KEY_SECRET: explicit-secret" in r.stdout
    assert "file-secret-should-lose" not in r.stdout
    assert "file-rev-should-lose" not in r.stdout


def test_env_file_feeds_unset_and_empty_vars_from_path_with_spaces(tree):
    # Empty is unset everywhere (${VAR:-} idiom), so the file feeds those keys
    # too; the selected path contains spaces.
    spaced = tree / "my env" / "poc file.env"
    spaced.parent.mkdir(parents=True, exist_ok=True)
    spaced.write_text(VALID_ENV_FILE)
    r = _run(
        tree,
        {
            "AIRGAP_ENV": str(spaced),
            "INTERNAL_REGISTRY": "",
            "NAMESPACE": None,
            "STORAGE_CLASS": None,
            "EMBED_MODEL": None,
            "EMBED_MODEL_REVISION": None,
            "DENSE_DIM": None,
            "EMBED_BASE_URL": None,
        },
    )
    assert r.returncode == 0, r.stderr


RECORD_STUB = "#!/bin/sh\necho \"$0\" >> \"$STUB_SENTINEL\"\nexit 0\n"


def _run_with_recording_stubs(tree, extra_env):
    """Replace tool stubs with sentinel-recording ones: any external command
    (registry/Helm/cluster mutation vector) leaves a trace to assert against."""
    sentinel = tree / "stub-calls.log"
    if sentinel.exists():
        sentinel.unlink()
    for name in ("skopeo", "helm", "kubectl", "oc"):
        write_stub(tree / "bin" / name, RECORD_STUB)
    env = {"STUB_SENTINEL": str(sentinel)}
    env.update(extra_env)
    r = _run(tree, env)
    calls = sentinel.read_text().split() if sentinel.exists() else []
    return r, calls


def test_missing_explicit_env_file_fails_before_any_tool(tree):
    # Issue #478: a typo'd selection must fail even when every other
    # required variable is valid — never silently become env-only.
    missing = str(tree / "poc-478-missing.env")
    assert not os.path.exists(missing)
    r, calls = _run_with_recording_stubs(tree, {"AIRGAP_ENV": missing})
    assert r.returncode != 0, r.stdout
    assert f"AIRGAP_ENV selects '{missing}'" in r.stderr
    assert "not a readable regular file" in r.stderr
    assert calls == [], f"loader refusal must precede every external command: {calls}"
    # The diagnostic names the path only — no config values leak.
    assert "reg.internal:5000" not in r.stderr


def test_taskfiles_do_not_load_airgap_env():
    # Locks the precedence fix at its new locus (issue #402 D): no Taskfile
    # may declare a `dotenv:` that loads the executable airgap.env file —
    # a task-level load would silently override explicit caller values with
    # stale file values. Scripts source the file themselves (see common.sh
    # OPERATOR_ENV_KEYS); Task passes per-task bridges only.
    taskfiles = [REPO / "Taskfile.yml", *sorted((REPO / "taskfiles").glob("*.yml"))]
    assert len(taskfiles) >= 7, [p.name for p in taskfiles]
    offenders = [p for p in taskfiles
                 if any("dotenv" in ln
                        for ln in p.read_text(encoding="utf-8").splitlines()
                        if not ln.lstrip().startswith("#"))]
    assert offenders == [], (
        "no Taskfile may load operator configuration via dotenv: "
        f"{[p.name for p in offenders]}"
    )


# ------------------------------------------------------- gateway keys (LiteLLM)


def test_validate_refuses_plaintext_gateway_key_in_env_file(tree):
    # Every per-leg key name is refused; a key in the env file would travel on
    # sneakernet media instead of the cluster Secret reference.
    for var in ("LLM_API_KEY", "EMBED_API_KEY", "RERANK_API_KEY", "CONTEXT_LLM_API_KEY"):
        r = _run(tree, {"AIRGAP_ENV": _write_env_file(tree, f"{var}=sk-plaintext-must-die\n")})
        assert r.returncode != 0, var
        assert "plaintext gateway virtual key" in r.stderr, var
        assert "sk-plaintext-must-die" not in r.stdout + r.stderr


@pytest.mark.parametrize("line", [
    "export LLM_API_KEY=sk-plaintext-must-die",
    "readonly LLM_API_KEY=sk-plaintext-must-die",
    "declare -x LLM_API_KEY=sk-plaintext-must-die",
    "typeset -x LLM_API_KEY=sk-plaintext-must-die",
    "export -- LLM_API_KEY=sk-plaintext-must-die",
    "X=1; export LLM_API_KEY=sk-plaintext-must-die",
    "export OTHER=1 LLM_API_KEY=sk-plaintext-must-die",
    "export LLM_API_KEY='sk-plaintext-must-die'",
    "eval 'LLM_API_KEY=sk-plaintext-must-die'",
], ids=["export", "readonly", "declare", "typeset", "dashdash", "chained", "multi", "quoted", "eval"])
def test_validate_refuses_plaintext_gateway_key_under_any_shell_syntax(tree, line):
    """The operator file is sourced, so export/readonly forms must not bypass the guard."""
    r = _run(tree, {"AIRGAP_ENV": _write_env_file(tree, f"{line}\n")})
    assert r.returncode != 0
    assert "plaintext gateway virtual key" in r.stderr
    assert "sk-plaintext-must-die" not in r.stdout + r.stderr


def test_validate_allows_empty_and_commented_gateway_keys(tree):
    r = _run(
        tree,
        {"AIRGAP_ENV": _write_env_file(tree, "LLM_API_KEY=\n#EMBED_API_KEY=sk-commented\n")},
    )
    assert r.returncode == 0, r.stderr


def test_validate_pull_secret_bad_name_fails_closed(tree):
    # Same gate as the gateway secret name: sed-active chars (&, |) would
    # silently rewrite the manifest via wire_pull_secret / Helm --set.
    r = _run(tree, {"PULL_SECRET": "a&b"})
    assert r.returncode != 0
    assert "PULL_SECRET must be a DNS-subdomain name" in r.stderr


def test_validate_live_verifies_gateway_secret(tree):
    # Non-dry-run against all-zero stubs: namespace + Secret resolve.
    r = _run(tree, {"AIRGAP_DRYRUN": "0", "GATEWAY_API_KEY_SECRET": "gateway-api-keys"})
    assert r.returncode == 0, r.stderr
    assert "Gateway key Secret 'gateway-api-keys' verified" in r.stdout


def test_validate_live_missing_gateway_secret_fails(tree):
    write_stub(
        tree / "bin" / "kubectl",
        _kubectl_stub("secret", NOT_FOUND),
    )
    r = _run(tree, {"AIRGAP_DRYRUN": "0", "GATEWAY_API_KEY_SECRET": "gateway-api-keys"})
    assert r.returncode != 0
    assert "Secret 'gateway-api-keys' not found" in r.stderr


def test_validate_live_missing_namespace_notices_secret(tree):
    write_stub(
        tree / "bin" / "kubectl",
        _kubectl_stub("namespace", NOT_FOUND),
    )
    r = _run(tree, {"AIRGAP_DRYRUN": "0", "GATEWAY_API_KEY_SECRET": "gateway-api-keys"})
    assert r.returncode == 0, r.stderr
    assert "does not exist yet" in r.stdout


def _git_checkout(tree, manifest_sha):
    """Turn the fixture tree into a git checkout at a different HEAD than
    the packed manifest: the exact IMAGE_SHA-only bypass shape (issue #414).
    Returns the checkout HEAD."""
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=tree, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tree, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tree, check=True)
    subprocess.run(["git", "add", "."], cwd=tree, check=True)
    subprocess.run(["git", "commit", "-m", "stale checkout"], cwd=tree, check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tree, text=True).strip()
    assert head != manifest_sha
    dist = tree / "dist"
    dist.mkdir(exist_ok=True)
    write_signed_manifest(dist, f"sha: {manifest_sha}\n")
    return head


def test_validate_live_refuses_image_sha_only_bypass(tree):
    """Issue #414: workspace HEAD=A with dist/MANIFEST from bundle B and
    IMAGE_SHA=B must fail before any mutation — the operator setting alone
    never establishes which code executes."""
    _git_checkout(tree, IMAGE_SHA)
    r = _run(tree, {"AIRGAP_DRYRUN": "0"})
    assert r.returncode != 0
    assert "executing checkout HEAD" in r.stderr
    assert "overriding IMAGE_SHA alone" in r.stderr


def test_validate_live_matching_checkout_passes_manifest_section(tree):
    """The same gate stays green when the checkout resolves to the packed
    SHA: identity established, preflight proceeds past the manifest check."""
    import subprocess

    dist = tree / "dist"
    dist.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=tree, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tree, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tree, check=True)
    subprocess.run(["git", "add", "."], cwd=tree, check=True)
    subprocess.run(["git", "commit", "-m", "approved checkout"], cwd=tree, check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tree, text=True).strip()
    write_signed_manifest(dist, f"sha: {head}\n")
    r = _run(tree, {"AIRGAP_DRYRUN": "0", "IMAGE_SHA": head})
    assert r.returncode == 0, r.stderr
    assert "Verified matching MANIFEST" in r.stdout


def _guard_probe(tree):
    probe = tree / "scripts" / "airgap" / "probe-guard.sh"
    probe.write_text(
        '#!/bin/sh\n. "$(dirname -- "$0")/common.sh"\n'
        "MANIFEST=\"$GUARD_MANIFEST\"\n"
        "check_checkout_sha\n"
    )
    return probe


def _guard_run(tree, manifest_rel, extra_env=None):
    import subprocess

    env = {"PATH": "/usr/bin:/bin", "GUARD_MANIFEST": manifest_rel}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["sh", str(_guard_probe(tree))], capture_output=True, text=True,
        env=env, cwd=tree, check=False,
    )


def test_checkout_guard_dryrun_skips_mismatch(tree):
    """Preview renders nothing, so the dry-run lane keeps its notice-only
    behavior even with a mismatched checkout (issue #414)."""
    import subprocess

    (tree / "scripts" / "airgap").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "scripts" / "airgap" / "common.sh", tree / "scripts" / "airgap" / "common.sh")
    subprocess.run(["git", "init", "-b", "main"], cwd=tree, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tree, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tree, check=True)
    (tree / "marker.txt").write_text("stale\n")
    subprocess.run(["git", "add", "."], cwd=tree, check=True)
    subprocess.run(["git", "commit", "-m", "stale"], cwd=tree, check=True, capture_output=True)
    (tree / "dist").mkdir(exist_ok=True)
    write_signed_manifest(tree / "dist", f"sha: {IMAGE_SHA}\n")
    r = _guard_run(tree, "dist/MANIFEST.txt", {"AIRGAP_DRYRUN": "1"})
    assert r.returncode == 0, r.stderr
    assert "not release-verified" in r.stderr
    # Connected development: no reachable MANIFEST and no bundle evidence.
    shutil.rmtree(tree / "dist")
    dev = _guard_run(tree, "dist/MANIFEST.txt")
    assert dev.returncode == 0
    assert "not release-verified" in dev.stderr


def test_checkout_guard_refuses_unresolved_checkout_when_manifest_is_packed(tree):
    """A packed MANIFEST claims a release. A tree whose checkout identity
    cannot be resolved (a copied tree without .git, or no usable git) must not
    run live under that claim just because IMAGE_SHA was set to the packed
    SHA (issue #414). Dry-run keeps its notice-only behavior."""
    (tree / "scripts" / "airgap").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "scripts" / "airgap" / "common.sh", tree / "scripts" / "airgap" / "common.sh")
    (tree / "dist").mkdir(exist_ok=True)
    write_signed_manifest(tree / "dist", f"sha: {IMAGE_SHA}\n")
    r = _guard_run(tree, "dist/MANIFEST.txt")
    assert r.returncode != 0
    assert "cannot be resolved" in r.stderr
    assert "overriding IMAGE_SHA alone" in r.stderr
    dry = _guard_run(tree, "dist/MANIFEST.txt", {"AIRGAP_DRYRUN": "1"})
    assert dry.returncode == 0
    assert "not release-verified" in dry.stderr


def test_checkout_guard_refuses_release_evidence_without_manifest(tree):
    """dist/SHA256SUMS is bundle evidence from bootstrap. With it present, a
    missing dist/MANIFEST.txt must not downgrade the run to the connected
    development notice: removing one file plus overriding IMAGE_SHA would
    bypass identity (issue #414). Without any bundle evidence the notice
    path stays (approved connected development)."""
    (tree / "scripts" / "airgap").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "scripts" / "airgap" / "common.sh", tree / "scripts" / "airgap" / "common.sh")
    (tree / "dist").mkdir(exist_ok=True)
    dev = _guard_run(tree, "dist/MANIFEST.txt")
    assert dev.returncode == 0 and "not release-verified" in dev.stderr
    (tree / "dist" / "SHA256SUMS").write_text("")
    r = _guard_run(tree, "dist/MANIFEST.txt")
    assert r.returncode != 0
    assert "bundle evidence" in r.stderr
    dry = _guard_run(tree, "dist/MANIFEST.txt", {"AIRGAP_DRYRUN": "1"})
    assert dry.returncode == 0


def _git(cwd, *args):
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _make_approved(root):
    """A real git checkout whose HEAD equals the packed MANIFEST sha, with
    tracked executable and chart content and a signed MANIFEST in dist/
    (issue #414)."""
    (root / "scripts" / "airgap").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "airgap" / "common.sh", root / "scripts" / "airgap" / "common.sh")
    (root / "charts").mkdir()
    (root / "charts" / "values.yaml").write_text("replicas: 1\n")
    (root / "Taskfile.yml").write_text("version: '3'\n")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "approved release")
    write_signed_manifest(root / "dist", f"sha: {_git(root, 'rev-parse', 'HEAD')}\n")
    return root


@pytest.fixture
def approved(tmp_path):
    return _make_approved(tmp_path)


def _approved_run(tree):
    return _guard_run(tree, "dist/MANIFEST.txt")


def test_checkout_guard_allows_untracked_operator_files(approved):
    clean = _approved_run(approved)
    assert clean.returncode == 0, clean.stderr
    assert clean.stderr == "" and clean.stdout == ""
    (approved / "airgap.env").write_text("INTERNAL_REGISTRY=site.internal\n")
    (approved / "dist" / "retained-evidence.txt").write_text("x")
    (approved / "scratch-output.yaml").write_text("rendered: true\n")
    r = _approved_run(approved)
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""


@pytest.mark.parametrize("stage", [True], ids=["staged"])
@pytest.mark.parametrize("path", ["scripts/airgap/common.sh"])
def test_checkout_guard_refuses_tracked_edit_at_matching_head(approved, path, stage):
    """Equal HEAD is not enough: an edited tracked script/chart/Taskfile
    must not run under the release claim. The refusal names no file and
    echoes no content."""
    with (approved / path).open("a") as fh:
        fh.write("\n# SECRET-EDIT-MARKER\n")
    if stage:
        _git(approved, "add", path)
    r = _approved_run(approved)
    assert r.returncode != 0
    assert "tracked changes against the packed MANIFEST sha" in r.stderr
    assert "SECRET-EDIT-MARKER" not in r.stdout + r.stderr
    assert path not in r.stderr


def test_checkout_guard_next_run_passes_after_reverting_edit(approved):
    """Lifecycle: refusal is not sticky and nothing was reset for the
    operator; once the edit is reverted the next ordinary run passes."""
    path = approved / "scripts" / "airgap" / "common.sh"
    original = path.read_text()
    path.write_text(original + "\n# local edit\n")
    _git(approved, "add", "scripts/airgap/common.sh")
    refused = _approved_run(approved)
    assert refused.returncode != 0
    # The guard did not touch the operator's staged edit.
    assert "# local edit" in path.read_text()
    assert _git(approved, "diff", "--cached", "--name-only") == "scripts/airgap/common.sh"
    _git(approved, "reset", "-q", "HEAD", "--", "scripts/airgap/common.sh")
    _git(approved, "checkout", "--", "scripts/airgap/common.sh")
    again = _approved_run(approved)
    assert again.returncode == 0, again.stderr
    assert again.stderr == ""


def test_checkout_guard_works_in_git_file_worktree(approved):
    """A linked worktree has a `.git` file, not a directory; the guard must
    judge it the same way (clean passes, tracked edit refuses)."""
    wt = approved.parent / "linked-wt"
    _git(approved, "worktree", "add", "--detach", str(wt))
    assert (wt / ".git").is_file()
    shutil.copytree(approved / "dist", wt / "dist")
    assert _approved_run(wt).returncode == 0
    (wt / "charts" / "values.yaml").write_text("replicas: 9\n")
    r = _approved_run(wt)
    assert r.returncode != 0
    assert "tracked changes" in r.stderr
    _git(wt, "checkout", "--", "charts/values.yaml")
    assert _approved_run(wt).returncode == 0


def test_checkout_guard_refuses_manifest_without_signed_bundle(approved):
    """The guard may not read a bare dist/MANIFEST.txt (issue #414): each of
    the three chain members is required next to it."""
    ok = _approved_run(approved)
    assert ok.returncode == 0, ok.stderr
    for member in ("SHA256SUMS.sig", "sneakernet-signing.pub", "SHA256SUMS"):
        saved = (approved / "dist" / member).read_bytes()
        (approved / "dist" / member).unlink()
        r = _approved_run(approved)
        assert r.returncode != 0, member
        assert "not backed by a signed bundle" in r.stderr, member
        (approved / "dist" / member).write_bytes(saved)
    assert _approved_run(approved).returncode == 0


def test_checkout_guard_refuses_manifest_edited_after_signing(approved):
    """A MANIFEST whose sha still equals HEAD but whose bytes changed after
    signing (here: an added digest line) no longer matches its signed entry."""
    manifest = approved / "dist" / "MANIFEST.txt"
    manifest.write_text(manifest.read_text() + "chart_sha256: " + "0" * 64 + "\n")
    r = _approved_run(approved)
    assert r.returncode != 0
    assert "does not match its signed checksum entry" in r.stderr
    assert "chart_sha256" not in r.stdout + r.stderr


def test_checkout_guard_refuses_forged_manifest_and_sums(approved):
    """Rewriting MANIFEST and SHA256SUMS consistently but without the signing
    key leaves a signature that no longer verifies."""
    from tests.helpers_airgap import sha256_bytes

    manifest = approved / "dist" / "MANIFEST.txt"
    manifest.write_text(manifest.read_text() + "note: forged\n")
    (approved / "dist" / "SHA256SUMS").write_text(f"{sha256_bytes(manifest.read_bytes())}  MANIFEST.txt\n")
    r = _approved_run(approved)
    assert r.returncode != 0
    assert "signature verification failed" in r.stderr


@pytest.mark.parametrize("listing", ["duplicate"])
def test_checkout_guard_requires_manifest_listed_exactly_once(approved, listing):
    from tests.helpers_airgap import sha256_bytes, sign_sums

    dist = approved / "dist"
    digest = sha256_bytes((dist / "MANIFEST.txt").read_bytes())
    lines = {"unlisted": f"{'0' * 64}  other.tar\n", "duplicate": f"{digest}  MANIFEST.txt\n" * 2}[listing]
    (dist / "SHA256SUMS").write_text(lines)
    sign_sums(dist, _signing_key())  # validly signed, wrong inventory
    r = _approved_run(approved)
    assert r.returncode != 0
    assert "does not match its signed checksum entry" in r.stderr


def _signing_key():
    from tests import helpers_airgap

    return helpers_airgap._MANIFEST_KEY


def test_checkout_guard_trusted_pub_pins_the_key(approved):
    from tests.helpers_airgap import gen_other_pub

    pinned = _guard_run(approved, "dist/MANIFEST.txt", {"SNEAKERNET_TRUSTED_PUB": str(approved / "dist" / "sneakernet-signing.pub")})
    assert pinned.returncode == 0, pinned.stderr
    other = gen_other_pub(approved.parent, "guard-other")
    r = _guard_run(approved, "dist/MANIFEST.txt", {"SNEAKERNET_TRUSTED_PUB": str(other)})
    assert r.returncode != 0
    assert "does not match SNEAKERNET_TRUSTED_PUB" in r.stderr


@pytest.mark.parametrize("flag", ["--assume-unchanged"])
def test_checkout_guard_refuses_index_flags_that_hide_edits(approved, flag):
    """`git diff` trusts the index: an edit under assume-unchanged or
    skip-worktree is invisible to it. The flag itself is refused, with the
    edit present or not, and the next run passes once it is cleared."""
    path = "scripts/airgap/common.sh"
    target = approved / path
    original = target.read_text()
    _git(approved, "update-index", flag, path)
    target.write_text(original + "\n# SECRET-HIDDEN-EDIT\n")
    assert _git(approved, "diff", "HEAD", "--name-only") == ""  # the evasion is real
    r = _approved_run(approved)
    assert r.returncode != 0
    assert "hides tracked files from change detection" in r.stderr
    assert "SECRET-HIDDEN-EDIT" not in r.stdout + r.stderr
    assert path not in r.stderr
    # The flag alone (no edit) is refused too: nothing can prove it is clean.
    target.write_text(original)
    assert _approved_run(approved).returncode != 0
    _git(approved, "update-index", flag.replace("--", "--no-"), path)
    again = _approved_run(approved)
    assert again.returncode == 0, again.stderr
    assert again.stderr == ""


def test_checkout_guard_refuses_sparse_checkout(approved):
    """Sparse checkout marks excluded files skip-worktree: part of the
    release tree is not on disk, so the run is refused, not assumed clean."""
    _git(approved, "sparse-checkout", "set", "--no-cone", "/scripts/")
    r = _approved_run(approved)
    assert r.returncode != 0
    assert "hides tracked files" in r.stderr


def test_checkout_guard_parent_bundle_layout_is_verified(tmp_path):
    """The documented unpack-next-to-the-clone layout: ../MANIFEST.txt is
    trusted only through ../SHA256SUMS(.sig) like dist/ is."""
    (tmp_path / "ws").mkdir()
    ws = _make_approved(tmp_path / "ws")
    bundle = tmp_path
    for f in list((ws / "dist").iterdir()):
        shutil.move(f, bundle / f.name)
    (ws / "dist").rmdir()
    ok = _guard_run(ws, "../MANIFEST.txt")
    assert ok.returncode == 0, ok.stderr
    (bundle / "MANIFEST.txt").write_text((bundle / "MANIFEST.txt").read_text() + "x: y\n")
    bad = _guard_run(ws, "../MANIFEST.txt")
    assert bad.returncode != 0
    assert "does not match its signed checksum entry" in bad.stderr


def test_checkout_guard_parent_sums_count_only_when_bundle_shaped(tmp_path):
    """A ../SHA256SUMS that names MANIFEST.txt (every packed bundle's list
    does) is bundle evidence even with MANIFEST.txt removed; an unrelated
    SHA256SUMS in the parent (e.g. a downloads folder) is not, so connected
    development there keeps the notice (issue #414 limit 3)."""
    (tmp_path / "ws").mkdir()
    ws = _make_approved(tmp_path / "ws")
    shutil.rmtree(ws / "dist")
    (tmp_path / "SHA256SUMS").write_text(f"{'0' * 64}  some-iso.img\n")
    stray = _guard_run(ws, "dist/MANIFEST.txt")
    assert stray.returncode == 0, stray.stderr
    assert "not release-verified" in stray.stderr
    (tmp_path / "SHA256SUMS").write_text(f"{'0' * 64}  MANIFEST.txt\n")
    r = _guard_run(ws, "dist/MANIFEST.txt")
    assert r.returncode != 0
    assert "bundle evidence" in r.stderr


def test_checkout_guard_wired_into_all_launch_paths():
    """deploy/ingest/validate/load each invoke the shared checkout guard
    next to their manifest cross-check (issue #414)."""
    for name in ("deploy.sh", "ingest.sh", "validate.sh", "load.sh"):
        text = (REPO / "scripts" / "airgap" / name).read_text()
        assert "check_checkout_sha" in text, name


def test_model_validation_preserves_caller_over_file_precedence(tree):
    path = tree / "selected.env"
    path.write_text("LLM_BASE_URL=invalid-file-url\nLLM_MODEL_REASONING=code\nGATEWAY_BASE_URL=https://file-gateway/v1\n")
    result = _run(tree, {"AIRGAP_ENV": str(path), "LLM_BASE_URL": "https://caller-reasoning/v1",
                         "GATEWAY_BASE_URL": "https://caller-gateway/v1", "EMBED_BASE_URL": None})
    assert result.returncode == 0, result.stderr
    assert "EMBED_BASE_URL:    https://caller-gateway/v1" in result.stdout
    result = _run(tree, {"AIRGAP_ENV": str(path), "LLM_BASE_URL": "private-invalid-url"})
    assert result.returncode != 0
    assert "LLM_BASE_URL must begin with http:// or https://" in result.stderr
    assert "private-invalid-url" not in result.stdout + result.stderr


@pytest.mark.parametrize("missing", ["CONTEXT_LLM_MODEL"])
def test_contextual_model_pair_required_in_preflight(tree, missing):
    config = {"CONTEXTUAL_EMBED_ENABLED": "true", "CONTEXT_LLM_BASE_URL": "https://context/v1",
              "CONTEXT_LLM_MODEL": "context-model"}
    config[missing] = ""
    result = _run(tree, config)
    assert result.returncode != 0
    assert f"{missing} is required when CONTEXTUAL_EMBED_ENABLED is set" in result.stderr


@pytest.mark.parametrize("shared", [False, True])
def test_shipped_example_model_mode_is_explicit(tree, shared):
    path = tree / "shipped.env"
    contents = (REPO / "airgap.env.example").read_text()
    contents += "\nGATEWAY_BASE_URL=https://shared.example/v1\n" if shared else "\nLLM_MODEL_REASONING=\n"
    path.write_text(contents)
    result = _run(tree, {"AIRGAP_ENV": str(path), **({"EMBED_BASE_URL": None} if shared else {})})
    assert result.returncode == 0, result.stderr
    expected = "https://shared.example/v1" if shared else "http://vllm:8000/v1"
    assert f"EMBED_BASE_URL:    {expected}" in result.stdout


# ---------------------------------------------- strict operator booleans (#272)

@pytest.mark.parametrize("key", ["INGEST_REINGEST"])
def test_validate_invalid_operator_boolean_refused(tree, key):
    r = _run(tree, {key: "maybe-secretish"})
    assert r.returncode != 0
    assert f"{key} must be true/false" in r.stderr
    assert "maybe-secretish" not in r.stderr + r.stdout


@pytest.mark.parametrize("key,bad", [
    ("LLM_THINKING_RESERVE_TOKENS_SIMPLE", "-1"),
    ("LLM_REASONING_EFFORT_COMPLEX", "unsupported-private-value"),
])
def test_validate_file_model_budgets_fail_before_later_stages(tree, key, bad):
    result = _run(tree, {"AIRGAP_ENV": _write_env_file(tree, f"{key}={bad}\n")})
    assert result.returncode != 0
    assert key in result.stderr
    assert "2. " not in result.stdout
    assert "unsupported-private-value" not in result.stderr + result.stdout


# One parser (model_config.parse_strict_bool) serves every key, so each key
# appears once with a different accepted spelling. INGEST_ALIAS_PUBLISH accepts
# only unset/true since in-place ingest was retired (#360); its false spellings
# are pinned by the refusal test below.
_ACCEPTED_BOOLEANS = [
    ("CONTEXTUAL_EMBED_ENABLED", "YES"),
    ("LLM_STREAM", "no"),
]


@pytest.mark.parametrize(("key", "value"), _ACCEPTED_BOOLEANS)
def test_validate_valid_operator_boolean_accepted(tree, key, value):
    env = {key: value}
    if key == "CONTEXTUAL_EMBED_ENABLED":
        env |= {"CONTEXT_LLM_BASE_URL": "http://llm:8000/v1", "CONTEXT_LLM_MODEL": "m"}
    r = _run(tree, env)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("value", ["False", "0"])
def test_validate_refuses_the_retired_in_place_switch(tree, value):
    """INGEST_ALIAS_PUBLISH accepts only unset/true: an explicit false fails
    closed at preflight with the retirement, never silently ignored."""
    r = _run(tree, {"INGEST_ALIAS_PUBLISH": value})
    assert r.returncode != 0
    assert "in-place ingest was retired" in r.stderr


def test_validate_invalid_boolean_from_env_file_then_corrected(tree):
    """Next ordinary run: the same file corrected by the operator passes."""
    bad = _run(tree, {"AIRGAP_ENV": _write_env_file(tree, "CONTEXTUAL_EMBED_ENABLED=on\n")})
    assert bad.returncode != 0
    assert "CONTEXTUAL_EMBED_ENABLED must be true/false" in bad.stderr
    good = _run(tree, {"AIRGAP_ENV": _write_env_file(tree, "CONTEXTUAL_EMBED_ENABLED=false\n")})
    assert good.returncode == 0, good.stderr


# -------------------------------------------- snapshot class on NFS (#272)


def test_validate_nfs_snapshot_storage_refused_then_corrected(tree):
    r = _run(tree, {"SNAPSHOT_STORAGE_CLASS": "nfs-client"})
    assert r.returncode != 0
    assert "SNAPSHOT_STORAGE_CLASS='nfs-client' looks like NFS" in r.stderr
    ok = _run(tree, {"SNAPSHOT_STORAGE_CLASS": "gp3-block"})
    assert ok.returncode == 0, ok.stderr


# ------------------------------------- Forbidden vs NotFound vs SCC (#272/#373)


def _live(tree, **extra):
    return _run(tree, {"AIRGAP_DRYRUN": "0", **extra})


def test_validate_live_storageclass_notfound_fails(tree):
    write_stub(tree / "bin" / "kubectl", _kubectl_stub("storageclass", NOT_FOUND))
    r = _live(tree)
    assert r.returncode != 0
    assert "must exist before deployment" in r.stderr


@pytest.mark.parametrize("snapshot_class", ["standard"])
def test_validate_live_shared_snapshot_storageclass_is_read_once(tree, snapshot_class):
    write_stub(
        tree / "bin" / "kubectl",
        "#!/bin/sh\n"
        'if [ "$1" = get ] && [ "$2" = storageclass ]; then\n'
        '  printf \'%s\\n\' "$3" >> storageclass-reads\n'
        "fi\nexit 0\n",
    )
    r = _live(tree, SNAPSHOT_STORAGE_CLASS=snapshot_class)
    assert r.returncode == 0, r.stderr
    assert (tree / "storageclass-reads").read_text().splitlines() == ["standard"]


@pytest.mark.parametrize("client_error,exit_code,message", [
    (None, 0, "StorageClass 'snapshot-tier' verified in cluster"),
    ("dial tcp: connection refused", 1, "cannot read StorageClass 'snapshot-tier' (unexpected client error)"),
])
def test_validate_live_distinct_snapshot_storageclass_probe(tree, client_error, exit_code, message):
    failure = (
        f"  cat >&2 <<'EOT'\n{client_error}\nEOT\n  exit 1\n"
        if client_error else "  exit 0\n"
    )
    write_stub(
        tree / "bin" / "kubectl",
        "#!/bin/sh\n"
        'if [ "$1" = get ] && [ "$2" = storageclass ]; then\n'
        '  printf \'%s\\n\' "$3" >> storageclass-reads\n'
        '  if [ "$3" = snapshot-tier ]; then\n'
        f"{failure}  fi\nfi\nexit 0\n",
    )
    r = _live(tree, SNAPSHOT_STORAGE_CLASS="snapshot-tier")
    assert r.returncode == exit_code, r.stderr
    assert message in r.stdout + r.stderr
    assert (tree / "storageclass-reads").read_text().splitlines() == ["standard", "snapshot-tier"]
    if client_error:
        assert client_error not in r.stdout + r.stderr


def test_validate_live_storageclass_forbidden_is_not_reported_absent(tree):
    write_stub(tree / "bin" / "kubectl", _kubectl_stub("storageclass", FORBIDDEN))
    r = _live(tree)
    assert r.returncode == 0, r.stderr
    assert "not found" not in r.stdout
    assert "Forbidden" in r.stdout and "NOT verified" in r.stdout


def test_validate_live_notfound_name_containing_forbidden_still_fails(tree):
    """The reason token decides, not free text: a missing StorageClass whose
    name contains 'forbidden' must not be classified as a Forbidden read."""
    text = 'Error from server (NotFound): storageclasses.storage.k8s.io "ceph-forbidden-tier" not found'
    write_stub(tree / "bin" / "kubectl", _kubectl_stub("storageclass", text))
    r = _live(tree)
    assert r.returncode != 0
    assert "must exist before deployment" in r.stderr


def test_validate_live_storageclass_other_error_fails(tree):
    write_stub(tree / "bin" / "kubectl", _kubectl_stub("storageclass", "dial tcp: connection refused"))
    r = _live(tree)
    assert r.returncode != 0
    assert "unexpected client error" in r.stderr


def _namespace_admin_kubectl(api_error=None):
    """kubectl stub shaped like a namespace admin, as measured on a cluster
    (#678): kube-system and cluster-scoped reads are Forbidden; API discovery
    succeeds unless `api_error` models an anonymous/invalid/unreachable one."""
    discovery = (
        f"  if [ \"$arg\" = /api ]; then cat >&2 <<'EOT'\n{api_error}\nEOT\n    exit 1; fi\n"
        if api_error else ""
    )
    return (
        "#!/bin/sh\nfor arg in \"$@\"; do\n"
        "  case \"$arg\" in cluster-info|storageclass|scc)\n"
        f"    echo '{FORBIDDEN}' >&2; exit 1;;\n  esac\n"
        f"{discovery}done\nexit 0\n"
    )


def test_validate_live_namespace_admin_passes_cluster_probe(tree):
    write_stub(tree / "bin" / "kubectl", _namespace_admin_kubectl())
    r = _live(tree)
    assert r.returncode == 0, r.stderr
    assert "cannot connect" not in r.stderr
    assert "existence is NOT verified" in r.stdout
    assert "cluster type is NOT determined" in r.stdout


@pytest.mark.parametrize("api_error", [
    FORBIDDEN,  # anonymous: /version would pass, discovery does not
])
def test_validate_live_unauthenticated_or_unreachable_api_fails(tree, api_error):
    write_stub(tree / "bin" / "kubectl", _namespace_admin_kubectl(api_error))
    r = _live(tree)
    assert r.returncode != 0
    assert "cannot connect to Kubernetes/OpenShift API server" in r.stderr


def test_validate_live_forbidden_namespace_still_checks_secret(tree):
    """A namespace-scoped deployer cannot get the Namespace object; the
    Secret check must still run (found -> verified), not be skipped."""
    write_stub(tree / "bin" / "kubectl", _kubectl_stub("namespace", FORBIDDEN))
    r = _live(tree, GATEWAY_API_KEY_SECRET="gateway-api-keys")
    assert r.returncode == 0, r.stderr
    assert "does not exist yet" not in r.stdout
    assert "Gateway key Secret 'gateway-api-keys' verified" in r.stdout


def test_validate_live_forbidden_secret_fails_not_absent(tree):
    write_stub(tree / "bin" / "kubectl", _kubectl_stub("secret", FORBIDDEN))
    r = _live(tree, GATEWAY_API_KEY_SECRET="gateway-api-keys")
    assert r.returncode != 0
    assert "Forbidden" in r.stderr
    assert "not found" not in r.stderr


def test_validate_live_scc_readable_reports_openshift(tree):
    r = _live(tree)
    assert r.returncode == 0, r.stderr
    assert "OpenShift cluster detected" in r.stdout


def test_validate_live_scc_missing_type_reports_standard_kubernetes(tree):
    write_stub(
        tree / "bin" / "kubectl",
        _kubectl_stub("scc", "error: the server doesn't have a resource type \"scc\""),
    )
    r = _live(tree)
    assert r.returncode == 0, r.stderr
    assert "Standard Kubernetes cluster detected" in r.stdout


@pytest.mark.parametrize("text", ["dial tcp: i/o timeout"])
def test_validate_live_scc_failure_is_not_standard_kubernetes(tree, text):
    write_stub(tree / "bin" / "kubectl", _kubectl_stub("scc", text))
    r = _live(tree)
    assert r.returncode == 0, r.stderr
    assert "Standard Kubernetes" not in r.stdout
    assert "cluster type is NOT determined" in r.stdout


# ------------------------------------------------ feedback service (#804)

FEEDBACK_KUBECTL = """#!/bin/sh
case "$*" in
  *'get secret fb-secret'*'go-template='*)
    [ "${MISSING_KEY:-}" = 1 ] || echo present ;;
  *'get secret fb-secret'*)
    case "${FB_SECRET:-ok}" in
      notfound) echo 'Error from server (NotFound): secrets "fb-secret" not found' >&2; exit 1 ;;
      forbidden) echo 'Error from server (Forbidden): secrets is forbidden' >&2; exit 1 ;;
    esac ;;
esac
exit 0
"""


def _feedback_live(tree, **env):
    write_stub(tree / "bin" / "kubectl", FEEDBACK_KUBECTL)
    return _live(tree, FEEDBACK_ENABLED="true", FEEDBACK_SECRET="fb-secret", **env)


def test_validate_live_verifies_the_feedback_secret_key(tree):
    r = _feedback_live(tree)
    assert r.returncode == 0, r.stderr
    assert "Feedback Secret 'fb-secret' has a nonempty service-token key" in r.stdout


@pytest.mark.parametrize(("env", "message"), [
    ({"FB_SECRET": "notfound"}, "Secret 'fb-secret' not found"),
    ({"FB_SECRET": "forbidden"}, "may not read Secret 'fb-secret'"),
    ({"MISSING_KEY": "1"}, "required Secret key is missing or empty: service-token"),
])
def test_validate_live_feedback_secret_problems_fail(tree, env, message):
    r = _feedback_live(tree, **env)
    assert r.returncode != 0
    assert message in r.stderr


def test_validate_live_feedback_off_reads_no_feedback_secret(tree):
    write_stub(tree / "bin" / "kubectl", _kubectl_stub("fb-secret", NOT_FOUND))
    r = _live(tree)
    assert r.returncode == 0, r.stderr
    assert "Feedback Secret" not in r.stdout


# ----------------------------------------------------- console Route (#373)

GOOD_PIN = "sha256:" + "b" * 64
OAUTH_REF = f"registry.redhat.io/openshift4/ose-oauth-proxy@{GOOD_PIN}"
SERVICE_CA = "-----BEGIN CERTIFICATE-----\nU0VSVklDRS1DQQ==\n-----END CERTIFICATE-----"

ROUTE_KUBECTL = """#!/bin/sh
case "$*" in
  'api-resources -o name') [ "${NO_ROUTE_API:-}" = 1 ] || echo routes.route.openshift.io ;;
  *'get routes.route.openshift.io '*)
    [ "${ROUTES_READ_FAIL:-}" != 1 ] || { echo 'Error from server (Forbidden): routes forbidden' >&2; exit 1; }
    if [ -n "${ROUTES_FILE:-}" ]; then cat "$ROUTES_FILE"; else echo '{"kind":"List","items":[]}'; fi ;;
  *'get secret rag-agent-oauth-cookie'*'go-template='*)
    [ "${MISSING_COOKIE_KEY:-}" = 1 ] || echo present ;;
  *'get secret rag-agent-oauth-cookie'*)
    case "${COOKIE_SECRET:-ok}" in
      notfound) echo 'Error from server (NotFound): secrets "rag-agent-oauth-cookie" not found' >&2; exit 1 ;;
      forbidden) echo 'Error from server (Forbidden): secrets is forbidden' >&2; exit 1 ;;
    esac ;;
  *'get configmap openshift-service-ca.crt'*) printf '%s\\n' "${SERVICE_CA:-}" ;;
esac
exit 0
"""


def _route_tree(tree, pin=GOOD_PIN):
    import re

    images = (tree / "images.txt")
    images.write_text((REPO / "images.txt").read_text())
    images.write_text(re.sub(
        r"^(registry\.redhat\.io/openshift4/ose-oauth-proxy:\S+)[ \t]+\S+$",
        lambda m: f"{m[1]} {pin}", images.read_text(), flags=re.MULTILINE))
    write_stub(tree / "bin" / "kubectl", ROUTE_KUBECTL)
    return tree


def _manifest(tree, *lines):
    dist = tree / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "MANIFEST.txt").write_text("\n".join([f"sha: {IMAGE_SHA}", *lines]) + "\n")


def _route_run(tree, live=True, **env):
    values = {"AGENT_ROUTE": "true", "SERVICE_CA": SERVICE_CA}
    if live:
        values["AIRGAP_DRYRUN"] = "0"
    values.update(env)
    return _run(tree, values)


def _routes_file(tree, *backends):
    import json

    items = [{"apiVersion": "route.openshift.io/v1", "kind": "Route",
              "metadata": {"name": name, "namespace": "mainframe-rag"},
              "spec": {"to": {"kind": "Service", "name": to}}} for name, to in backends]
    path = tree / "routes.json"
    path.write_text(json.dumps({"kind": "List", "items": items}))
    return str(path)


def test_validate_reports_the_networkpolicy_selection(tree):
    on = _run(tree, {'AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES': 'mcp'})
    assert on.returncode == 0, on.stderr
    assert "NetworkPolicy:     rag-agent-ingress: agent HTTP 8080 only from this namespace" in on.stdout
    assert "(mcp)" in on.stdout
    off = _run(tree, {'AGENT_NETWORK_POLICY': 'false'})
    assert off.returncode == 0, off.stderr
    assert "NetworkPolicy:     off (AGENT_NETWORK_POLICY=false)" in off.stdout


def test_validate_reports_the_feedback_selection(tree):
    off = _run(tree, {})
    assert off.returncode == 0, off.stderr
    assert "Feedback service:  off (FEEDBACK_ENABLED=false)" in off.stdout
    on = _run(tree, {'FEEDBACK_ENABLED': 'true', 'FEEDBACK_SECRET': 'fb', 'FEEDBACK_RETENTION_DAYS': '90'})
    assert on.returncode == 0, on.stderr
    assert "Feedback service:  on: StatefulSet rag-feedback (1 replica)" in on.stdout
    assert "retention 90 days, token from Secret fb (key service-token)" in on.stdout


def test_validate_route_dryrun_accepts_recorded_consistent_pin(tree):
    _route_tree(tree)
    r = _route_run(tree, live=False)
    assert r.returncode == 0, r.stderr
    assert "oauth-proxy pin recorded; matches the chart" in r.stdout
    assert "--email-domain=*" in r.stdout


def test_validate_route_dryrun_pending_pin_refused(tree):
    _route_tree(tree, pin="sha256:PENDING")
    r = _route_run(tree, live=False)
    assert r.returncode != 0
    assert "oauth-proxy digest recorded" in r.stderr


def test_validate_route_off_ignores_the_oauth_pin(tree):
    _route_tree(tree, pin="sha256:PENDING")
    r = _run(tree, {"AGENT_ROUTE": "false"})
    assert r.returncode == 0, r.stderr


def test_validate_route_pin_tag_must_match_chart(tree):
    _route_tree(tree)
    images = tree / "images.txt"
    images.write_text(images.read_text().replace("ose-oauth-proxy:v4.14", "ose-oauth-proxy:v4.99"))
    r = _route_run(tree, live=False)
    assert r.returncode != 0
    assert "does not match the chart" in r.stderr


def test_validate_route_manifest_without_oauth_member_refused(tree):
    _route_tree(tree)
    _manifest(tree)
    r = _route_run(tree, live=False)
    assert r.returncode != 0
    assert "packed without the oauth-proxy image" in r.stderr


def test_validate_route_manifest_with_other_pin_refused(tree):
    _route_tree(tree)
    _manifest(tree, "oauth_proxy: registry.redhat.io/openshift4/ose-oauth-proxy@sha256:" + "c" * 64)
    r = _route_run(tree, live=False)
    assert r.returncode != 0
    assert "differs from images.txt" in r.stderr


def test_validate_route_manifest_with_matching_pin_passes(tree):
    _route_tree(tree)
    _manifest(tree, f"oauth_proxy: {OAUTH_REF}")
    r = _route_run(tree, live=False)
    assert r.returncode == 0, r.stderr
    assert "and the packed bundle" in r.stdout


# ----------------------------------------- optional Open WebUI image (#1074)

OWUI_PIN = "sha256:" + "e" * 64
OWUI_SOURCE = "ghcr.io/yonatan895/qdrant-pdf-rag-openwebui"
OWUI_REF = f"{OWUI_SOURCE}@{OWUI_PIN}"
# ADR-0007 E1: the allow-list proxy sidecar pin recorded in images.txt.
PROXY_REF = ("docker.io/nginxinc/nginx-unprivileged@sha256:"
             "f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03")
# Independent expectation of the chart's images.openwebui (repository, tag).
OWUI_CHART_BLOCK = "  openwebui:\n    repository: qdrant-pdf-rag-openwebui\n    tag: v0.12.3-slim\n"


def _openwebui_tree(tree, pin=OWUI_PIN):
    images = tree / "images.txt"
    images.write_text(re.sub(rf"^({re.escape(OWUI_SOURCE)}:\S+)[ \t]+\S+$",
                             lambda m: f"{m[1]} {pin}", (REPO / "images.txt").read_text(),
                             flags=re.MULTILINE))
    values = tree / "charts/mainframe-rag/values.yaml"
    text = values.read_text()
    if not re.search(r"^[ \t]+openwebui:\n[ \t]+repository: qdrant-pdf-rag-openwebui\n", text, re.MULTILINE):
        values.write_text(text.replace("\nimages:\n", "\nimages:\n" + OWUI_CHART_BLOCK, 1))
    return tree


OWUI_OPERATOR_ENV = {
    "OPENWEBUI_ENABLED": "true",
    "OPENWEBUI_SECRET": "owui-secrets",
    "OPENWEBUI_HOST": "docs.apps.example.internal",
    "OPENWEBUI_OIDC_PROVIDER_URL": "https://idp.example.internal/.well-known/openid-configuration",
    "OPENWEBUI_OIDC_CLIENT_ID": "docs-frontend",
    "OPENWEBUI_OIDC_ALLOWED_ROLES": "staff",
    "OPENWEBUI_OIDC_ADMIN_ROLES": "docs-admins",
    "OPENWEBUI_IDP_EGRESS_CIDRS": "10.20.0.0/24",
}


def _openwebui_run(tree, **env):
    return _run(tree, {**OWUI_OPERATOR_ENV, **env})


def test_validate_openwebui_accepts_a_recorded_pin_matching_chart_and_bundle(tree):
    _openwebui_tree(tree)
    r = _openwebui_run(tree)
    assert r.returncode == 0, r.stderr
    assert "Open WebUI pin recorded; matches the chart" in r.stdout
    _manifest(tree, f"openwebui: {OWUI_REF}", f"openwebui_proxy: {PROXY_REF}")
    r = _openwebui_run(tree)
    assert r.returncode == 0, r.stderr
    assert "Open WebUI pin recorded; matches the chart and the packed bundle" in r.stdout


@pytest.mark.parametrize("key,needle", [
    ("OPENWEBUI_OIDC_CLIENT_ID", "required inputs are missing: OPENWEBUI_OIDC_CLIENT_ID. Set them, or opt out with OPENWEBUI_ENABLED=false"),
    ("OPENWEBUI_IDP_EGRESS_CIDRS", "required inputs are missing: OPENWEBUI_IDP_EGRESS_CIDRS. Set them, or opt out with OPENWEBUI_ENABLED=false"),
    ("OPENWEBUI_SECRET", "OPENWEBUI_SECRET"),
])
def test_validate_openwebui_refuses_an_incomplete_selection(tree, key, needle):
    _openwebui_tree(tree)
    r = _openwebui_run(tree, **{key: ""})
    assert r.returncode != 0
    assert needle in r.stderr


def test_validate_openwebui_pending_pin_refused_only_when_enabled(tree):
    _openwebui_tree(tree, pin="sha256:PENDING")
    r = _openwebui_run(tree)
    assert r.returncode != 0
    assert "OPENWEBUI_ENABLED=true needs the Open WebUI digest recorded in images.txt" in r.stderr
    off = _run(tree, {"OPENWEBUI_ENABLED": "false"})
    assert off.returncode == 0, off.stderr
    assert "Open WebUI pin" not in off.stdout


@pytest.mark.parametrize("edit,needle", [
    (lambda t: (t / "images.txt").write_text((t / "images.txt").read_text().replace(
        "qdrant-pdf-rag-openwebui:v0.12.3-slim", "qdrant-pdf-rag-openwebui:v0.99.0-slim")),
     "to match the chart's images.openwebui repository/tag"),
    (lambda t: _manifest(t), "packed without the Open WebUI image"),
    (lambda t: _manifest(t, f"openwebui: {OWUI_SOURCE}@sha256:" + "c" * 64),
     "packed MANIFEST openwebui pin to equal images.txt"),
    # ADR-0007 E1: the allow-list proxy gets the same three checks.
    (lambda t: (t / "images.txt").write_text(re.sub(
        r"^(docker\.io/nginxinc/nginx-unprivileged:\S+)[ \t]+\S+$", r"\1 sha256:PENDING",
        (t / "images.txt").read_text(), flags=re.MULTILINE)),
     "allow-list proxy digest (nginx-unprivileged) recorded in images.txt"),
    (lambda t: (t / "images.txt").write_text((t / "images.txt").read_text().replace(
        "nginx-unprivileged:1.30.6-alpine", "nginx-unprivileged:1.99.0-alpine")),
     "to match the chart's images.openwebuiProxy repository/tag"),
    (lambda t: _manifest(t, f"openwebui: {OWUI_REF}"), "openwebui_proxy entry in the packed MANIFEST"),
    (lambda t: _manifest(t, f"openwebui: {OWUI_REF}",
                         "openwebui_proxy: docker.io/nginxinc/nginx-unprivileged@sha256:" + "c" * 64),
     "packed MANIFEST openwebui_proxy pin to equal images.txt"),
])
def test_validate_openwebui_refuses_chart_or_bundle_mismatch(tree, edit, needle):
    _openwebui_tree(tree)
    edit(tree)
    r = _openwebui_run(tree)
    assert r.returncode != 0
    assert needle in r.stderr


def test_validate_route_live_success(tree):
    _route_tree(tree)
    r = _route_run(tree)
    assert r.returncode == 0, r.stderr
    assert "has a nonempty cookie-secret key" in r.stdout
    assert "Namespace service CA readable" in r.stdout


@pytest.mark.parametrize("state,message", [
    ("forbidden", "may not read Secret 'rag-agent-oauth-cookie'"),
])
def test_validate_route_live_cookie_secret_failures(tree, state, message):
    _route_tree(tree)
    r = _route_run(tree, COOKIE_SECRET=state)
    assert r.returncode != 0
    assert message in r.stderr


def test_validate_route_live_cookie_key_missing(tree):
    _route_tree(tree)
    r = _route_run(tree, MISSING_COOKIE_KEY="1")
    assert r.returncode != 0
    assert "missing or empty: cookie-secret" in r.stderr


@pytest.mark.parametrize("ca", ["garbage"])
def test_validate_route_live_service_ca_must_be_pem(tree, ca):
    _route_tree(tree)
    r = _route_run(tree, SERVICE_CA=ca)
    assert r.returncode != 0
    assert "not a PEM certificate bundle" in r.stderr


def test_validate_route_live_requires_route_api(tree):
    _route_tree(tree)
    r = _route_run(tree, NO_ROUTE_API="1")
    assert r.returncode != 0
    assert "does not serve routes.route.openshift.io" in r.stderr


@pytest.mark.parametrize(("route_on", "backend"), [(True, "rag-agent"), (False, "qdrant-headless")])
def test_validate_live_refuses_foreign_route_to_protected_service(tree, route_on, backend):
    _route_tree(tree)
    env = {"ROUTES_FILE": _routes_file(tree, ("public-console", backend))}
    if route_on:
        r = _route_run(tree, **env)
    else:
        write_stub(tree / "bin" / "kubectl", ROUTE_KUBECTL)
        r = _run(tree, {"AIRGAP_DRYRUN": "0", **env})
    assert r.returncode != 0
    assert "Route public-console exposes a protected Service" in r.stderr


def test_validate_live_unlistable_routes_fail_closed(tree):
    _route_tree(tree)
    r = _route_run(tree, ROUTES_READ_FAIL="1")
    assert r.returncode != 0
    assert "cannot list Routes" in r.stderr


def test_validate_live_unrelated_route_and_owned_route_pass(tree):
    _route_tree(tree)
    r = _route_run(tree, ROUTES_FILE=_routes_file(tree, ("docs", "docs-site"), ("rag-agent", "rag-agent")))
    assert r.returncode == 0, r.stderr


def test_validate_openwebui_passes_with_the_seven_inputs_and_no_service_ca_env(tree):
    """B1: the service CA is fetched by deploy on the live path, so it is not an
    operator input validate may demand."""
    _openwebui_tree(tree)
    env = {**OWUI_OPERATOR_ENV}
    assert "ROUTE_DESTINATION_CA_FILE" not in env
    r = _run(tree, env)
    assert r.returncode == 0, r.stderr
    assert "ROUTE_DESTINATION_CA_FILE" not in r.stderr
    assert "Open WebUI pin recorded; matches the chart" in r.stdout


def test_validate_openwebui_pending_pin_refusal_names_the_opt_out(tree):
    _openwebui_tree(tree, pin="sha256:PENDING")
    r = _openwebui_run(tree)
    assert r.returncode != 0
    assert "or opt out with OPENWEBUI_ENABLED=false" in r.stderr


# --- Issue #1070 (R1): the stored-layout preflight, with cluster access -------

LAYOUT_KUBECTL = r"""#!/bin/sh
printf '%s\n' "$*" >> "$TOOL_LOG"
case "$*" in
  *'get statefulset/qdrant '*) [ -z "${FAKE_QDRANT_URL:-}" ] || echo statefulset.apps/qdrant ;;
  *'exec -i deploy/rag-agent -- python3 -c '*)
    while [ "$1" != "--" ]; do shift; done; shift
    exec env QDRANT_URL="$FAKE_QDRANT_URL" QDRANT_API_KEY="$FAKE_READ_KEY" "$@" ;;
esac
exit 0
"""


@pytest.mark.parametrize("layout", ["legacy", "empty", "fresh"])
def test_validate_live_reports_the_stored_layout(tree, layout):
    from tests.helpers_airgap import FakeQdrant

    qdrant = FakeQdrant()
    try:
        if layout == "legacy":
            qdrant.add_collection("mainframe_manuals", [{"id": 1, "vector": {"dense": [0.0] * 4}}])
        for name in ("kubectl", "oc"):
            write_stub(tree / "bin" / name, LAYOUT_KUBECTL)
        extra = {"AIRGAP_DRYRUN": "0", "TOOL_LOG": str(tree / "tool.log"),
                 "FAKE_READ_KEY": FakeQdrant.READ_KEY}
        if layout != "fresh":
            extra["FAKE_QDRANT_URL"] = qdrant.url
        r = _run(tree, extra)
    finally:
        qdrant.close()
    if layout == "legacy":
        assert r.returncode != 0
        assert "E_LEGACY_COLLECTION" in r.stderr
        assert "retire_legacy.sh --stage copy --confirm mainframe_manuals" in r.stderr
        assert "SUCCESS" not in r.stdout
    else:
        assert r.returncode == 0, r.stderr
        assert ("fresh install" if layout == "fresh" else "no corpus collection yet") in r.stdout
    assert FakeQdrant.READ_KEY not in r.stdout + r.stderr + (tree / "tool.log").read_text()


# --- Retired airgap.env keys (#1070): silent ignoring becomes a loud refusal ---


def _assert_retired_refusal(r, calls, expected):
    assert r.returncode != 0, r.stdout
    assert sorted(retired_keys_listed(r.stderr)) == sorted(expected), r.stderr
    assert "1. Validating" not in r.stdout
    assert calls == []
    for value in RETIRED_ENV_VALUES.values():
        assert value not in r.stdout + r.stderr
    assert "sentinel-" not in r.stdout + r.stderr


def test_validate_refuses_a_d53595e_shaped_env_listing_exactly_the_retired_keys(tree):
    env = {"AIRGAP_ENV": _write_env_file(tree, D53595E_SHAPED_ENV)}
    r, calls = _run_with_recording_stubs(tree, env)
    _assert_retired_refusal(r, calls, RETIRED_ENV_KEYS)


@pytest.mark.parametrize("key", RETIRED_ENV_KEYS)
def test_validate_refuses_each_retired_key_alone(tree, key):
    r, calls = _run_with_recording_stubs(
        tree, {"AIRGAP_ENV": _write_env_file(tree, f"{key}={RETIRED_ENV_VALUES[key]}\n")}
    )
    _assert_retired_refusal(r, calls, [key])


def test_validate_refuses_a_retired_key_from_the_process_environment(tree):
    r, calls = _run_with_recording_stubs(tree, {"VLLM_BASE_URL": RETIRED_ENV_VALUES["VLLM_BASE_URL"]})
    _assert_retired_refusal(r, calls, ["VLLM_BASE_URL"])


def test_validate_accepts_empty_and_commented_retired_keys_then_refuses_the_filled_one(tree):
    empty = "".join(f"{key}=\n" for key in RETIRED_ENV_KEYS) + "".join(
        f"#{key}=x\n" for key in RETIRED_ENV_KEYS
    )
    good = _run(tree, {"AIRGAP_ENV": _write_env_file(tree, empty)})
    assert good.returncode == 0, good.stderr
    # Next ordinary run: the operator fills one key back in and is refused again.
    bad = _run(tree, {"AIRGAP_ENV": _write_env_file(tree, empty + "REGISTRY_INTERNAL=x\n")})
    assert bad.returncode != 0
    assert retired_keys_listed(bad.stderr) == ["REGISTRY_INTERNAL"]


_JAEGER_LINE = "OTEL_EXPORTER_OTLP_ENDPOINT (retired in d7765dc): points at the retired in-cluster Jaeger"


@pytest.mark.parametrize("endpoint", [
    "http://jaeger:4318",
    "https://jaeger:4318/v1/traces",
    "http://Jaeger.mainframe-rag.svc:4318",
    "http://jaeger.mainframe-rag.svc.cluster.local:4318",
])
def test_validate_refuses_the_retired_in_cluster_jaeger_endpoint(tree, endpoint):
    """d53595e's example pointed the exporter at http://jaeger:4318; today any set
    endpoint means an external collector, so it would silently drop traces."""
    r, calls = _run_with_recording_stubs(tree, {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint})
    assert r.returncode != 0
    assert _JAEGER_LINE in r.stderr
    assert "unset it to use the bundled OpenObserve collector, or set your external collector" in r.stderr
    assert retired_keys_listed(r.stderr) == ["OTEL_EXPORTER_OTLP_ENDPOINT"]
    assert endpoint not in r.stdout + r.stderr
    assert calls == []


@pytest.mark.parametrize("endpoint", [
    None,
    "",
    "off",
    "http://collector.platform:4318",
    "http://jaeger-collector.platform:4318",
    "http://jaeger.example.com:4318",
])
def test_validate_accepts_unset_empty_off_and_external_collectors(tree, endpoint):
    extra = {} if endpoint is None else {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint}
    r = _run(tree, extra)
    assert r.returncode == 0, r.stderr


# --- Site sizing keys (#1070 R2): airgap.env values validate like env values ---

@pytest.mark.parametrize("name,value", [
    ("QDRANT_DATA_SIZE", "lots"), ("QDRANT_CPU_LIMIT", "1"), ("INGEST_MEMORY_REQUEST", "64Gi"),
    ("INGEST_CPU_REQUEST", "0"), ("QDRANT_MEMORY_LIMIT", "500m"),
])
@pytest.mark.parametrize("source", ["env", "file"])
def test_validate_refuses_invalid_sizing_from_env_and_airgap_env(tree, name, value, source):
    if source == "env":
        r = _run(tree, {name: value})
    else:
        env_file = tree / "sizing.env"
        env_file.write_text(f"{name}={value}\n")  # unexported, as a real airgap.env assignment
        r = _run(tree, {"AIRGAP_ENV": str(env_file)})
    assert r.returncode != 0, r.stdout
    assert name in r.stderr
    assert value not in r.stderr
