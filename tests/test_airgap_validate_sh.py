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
    connected_checkout,
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
    connected_checkout(env, set(extra_env or {}))
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


@pytest.mark.parametrize(("template", "downgrade", "needle"), [
    ("agent-deployment.yaml", False, "read-only-api-key"),
    # Issue #366 mirror: ingest owns corpus mutation, so read-only must fail too.
    ("ingest-job.yaml", True, "to api-key"),
    ("ingest-job.yaml", False, "read-only"),
], ids=["agent-write-key", "ingest-readonly", "ingest-copresent-readonly"])
def test_validate_qdrant_key_role_fails_closed(tree, template, downgrade, needle):
    overlay = tree / "charts/mainframe-rag/templates" / template
    text = overlay.read_text()
    if downgrade:
        lines = ["key: read-only-api-key" if line.strip() == "key: api-key" else line
                 for line in text.splitlines()]
        overlay.write_text("\n".join(lines) + "\n")
    elif template == "agent-deployment.yaml":
        overlay.write_text(text.replace("key: read-only-api-key", "key: api-key"))
    else:
        anchor = "key: api-key\n"
        assert anchor in text
        overlay.write_text(text.replace(anchor, anchor + (
            "            - name: QDRANT_READ_API_KEY\n"
            "              valueFrom:\n"
            "                secretKeyRef:\n"
            "                  key: read-only-api-key\n"
            "                  name: __QDRANT_RELEASE__-apikey\n")))
    r = _run(tree)
    assert r.returncode != 0
    assert needle in r.stderr


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


def test_validate_optional_model_url_bad_scheme_refused(tree):
    r = _run(tree, {"LLM_BASE_URL": "gateway.internal:4000/v1"})
    assert r.returncode != 0
    assert "LLM_BASE_URL must begin with http:// or https://" in r.stderr


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


@pytest.mark.parametrize(("stub", "rc", "needle"), [
    (None, 0, "Gateway key Secret 'gateway-api-keys' verified"),
    ("secret", 1, "Secret 'gateway-api-keys' not found"),
    ("namespace", 0, "does not exist yet"),
], ids=["secret-present", "secret-missing", "namespace-missing"])
def test_validate_live_verifies_the_gateway_secret(tree, stub, rc, needle):
    if stub:
        write_stub(tree / "bin" / "kubectl", _kubectl_stub(stub, NOT_FOUND))
    r = _live(tree, GATEWAY_API_KEY_SECRET="gateway-api-keys")
    assert r.returncode == rc, r.stderr
    assert needle in (r.stdout if rc == 0 else r.stderr)


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


# The connected opt-out (#1118 F1): accepted only with CI_REHEARSAL=1.
_OPT_OUT = {"AIRGAP_UNVERIFIED_CHECKOUT": "1", "CI_REHEARSAL": "1"}

# Fixed refusal messages of the verified-release guard (#1118 F1), whole.
_NO_MANIFEST = ("FAIL: checkout is not a verified signed release (no packed MANIFEST) — run from the "
                "checkout that bootstrap.sh creates from the signed bundle, with SNEAKERNET_TRUSTED_PUB set; "
                "only a connected developer or CI lane may set AIRGAP_UNVERIFIED_CHECKOUT=1 with "
                "CI_REHEARSAL=1 instead\n")
_NO_ANCHOR = ("FAIL: no release trust anchor is configured — set SNEAKERNET_TRUSTED_PUB to the release "
              "signing public key obtained out of band (docs/release-verification.md); trust on first use "
              "never establishes a release\n")
_OPT_OUT_REFUSED = ("FAIL: AIRGAP_UNVERIFIED_CHECKOUT=1 is refused without CI_REHEARSAL=1 in the process "
                    "environment — the unverified-checkout opt-out is for connected development and the "
                    "connected CI and Kind lanes only, never the air gap; run from a verified signed release "
                    "instead\n")
_UNREADABLE_ANCHOR = ("FAIL: release trust anchor SNEAKERNET_TRUSTED_PUB is not a readable file — point it "
                      "at the release signing public key obtained out of band (docs/release-verification.md)\n")
_BAD_SIGNATURE = ("FAIL: SHA256SUMS signature verification failed for the packed MANIFEST — do not trust "
                  "this bundle; rerun bootstrap.sh from the signed bundle\n")
_FOREIGN_KEY = ("FAIL: bundle pubkey does not match any configured release trust anchor "
                "(SNEAKERNET_TRUSTED_PUB) — untrusted bundle; use the signed release bundle or fix the anchor\n")
_HEAD_DIFFERS = ("FAIL: executing checkout HEAD does not match the packed MANIFEST sha — run from the approved "
                 "bundle checkout (git rev-parse HEAD and the MANIFEST sha: line must agree); overriding "
                 "IMAGE_SHA alone never changes which code executes\n")
_TRACKED_CHANGE = ("FAIL: executing checkout has tracked changes against the packed MANIFEST sha — restore "
                   "them (git diff HEAD lists them; operator settings belong in the untracked airgap.env) "
                   "before running a release\n")
_HIDDEN_CHANGE = ("FAIL: executing checkout hides tracked files from change detection (assume-unchanged, "
                  "skip-worktree or sparse checkout) — clear the flags (git update-index "
                  "--no-assume-unchanged / --no-skip-worktree; git ls-files -v marks them) before running "
                  "a release\n")


def _guard_probe(tree):
    probe = tree / "scripts" / "airgap" / "probe-guard.sh"
    probe.write_text(
        '#!/bin/sh\n. "$(dirname -- "$0")/common.sh"\n'
        "MANIFEST=\"$GUARD_MANIFEST\"\n"
        "check_checkout_sha\n"
    )
    return probe


def _guard_run(tree, manifest_rel, extra_env=None):
    """Run the real guard. The release trust anchor (#1118 F1) is configured
    unless the caller overrides it; a None value removes a key."""
    import subprocess

    from tests.helpers_airgap import release_anchor

    env = {"PATH": "/usr/bin:/bin", "GUARD_MANIFEST": manifest_rel,
           "SNEAKERNET_TRUSTED_PUB": str(release_anchor())}
    for key, value in (extra_env or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        ["sh", str(_guard_probe(tree))], capture_output=True, text=True,
        env=env, cwd=tree, check=False,
    )


def _guard_tree(tree, *, commit=False):
    """A tree that runs the real common.sh guard against dist/MANIFEST.txt."""
    (tree / "scripts" / "airgap").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "scripts" / "airgap" / "common.sh", tree / "scripts" / "airgap" / "common.sh")
    (tree / "dist").mkdir(exist_ok=True)
    write_signed_manifest(tree / "dist", f"sha: {IMAGE_SHA}\n")
    if commit:
        (tree / "marker.txt").write_text("stale\n")
        _git(tree, "init", "-b", "main")
        _git(tree, "config", "user.name", "Test")
        _git(tree, "config", "user.email", "test@example.com")
        _git(tree, "add", ".")
        _git(tree, "commit", "-m", "stale")
    return tree


def test_checkout_guard_dryrun_keeps_the_notice_with_a_mismatched_checkout(tree):
    """Preview renders nothing, so the dry-run lane keeps its notice-only
    behavior even with a mismatched checkout (issue #414)."""
    _guard_tree(tree, commit=True)
    r = _guard_run(tree, "dist/MANIFEST.txt", {"AIRGAP_DRYRUN": "1"})
    assert r.returncode == 0, r.stderr
    assert "not release-verified" in r.stderr
    # Connected development (no reachable MANIFEST, no bundle evidence) needs
    # the explicit opt-out since #1118 F1; the plain run is refused.
    shutil.rmtree(tree / "dist")
    assert _guard_run(tree, "dist/MANIFEST.txt").returncode != 0
    dev = _guard_run(tree, "dist/MANIFEST.txt", _OPT_OUT)
    assert dev.returncode == 0
    assert "not release-verified" in dev.stderr


def test_checkout_guard_refuses_unresolved_checkout_when_manifest_is_packed(tree):
    """A packed MANIFEST claims a release. A tree whose checkout identity
    cannot be resolved (a copied tree without .git, or no usable git) must not
    run live under that claim just because IMAGE_SHA was set to the packed
    SHA (issue #414). Dry-run keeps its notice-only behavior."""
    _guard_tree(tree)
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
    path stays only under the connected opt-out (#1118 F1), and the opt-out
    never waives bundle evidence."""
    (tree / "scripts" / "airgap").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "scripts" / "airgap" / "common.sh", tree / "scripts" / "airgap" / "common.sh")
    (tree / "dist").mkdir(exist_ok=True)
    dev = _guard_run(tree, "dist/MANIFEST.txt", _OPT_OUT)
    assert dev.returncode == 0 and "not release-verified" in dev.stderr
    (tree / "dist" / "SHA256SUMS").write_text("")
    for extra in (None, _OPT_OUT):
        r = _guard_run(tree, "dist/MANIFEST.txt", extra)
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


def test_checkout_guard_refuses_tracked_edit_at_matching_head(approved):
    """Equal HEAD is not enough: an edited tracked script/chart/Taskfile
    must not run under the release claim. The refusal names no file and
    echoes no content."""
    path = "scripts/airgap/common.sh"
    with (approved / path).open("a") as fh:
        fh.write("\n# SECRET-EDIT-MARKER\n")
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


@pytest.mark.parametrize(("tamper", "needle"), [
    ("edited", "does not match its signed checksum entry"),
    ("forged", "signature verification failed"),
    ("twice-listed", "does not match its signed checksum entry"),
], ids=["edited-after-signing", "forged-and-resummed", "listed-twice"])
def test_checkout_guard_refuses_a_broken_signed_manifest(approved, tamper, needle):
    """HEAD still matches, but the signed chain no longer binds the MANIFEST:
    an edit after signing, a consistently rewritten pair without the key, or a
    duplicate sums entry are each refused (the diagnostic echoes no content)."""
    from tests.helpers_airgap import sha256_bytes, sign_sums

    dist = approved / "dist"
    target = dist / "MANIFEST.txt"
    if tamper == "edited":
        target.write_text(target.read_text() + "chart_sha256: " + "0" * 64 + "\n")
    elif tamper == "forged":
        target.write_text(target.read_text() + "note: forged\n")
        (dist / "SHA256SUMS").write_text(f"{sha256_bytes(target.read_bytes())}  MANIFEST.txt\n")
    else:
        digest = sha256_bytes(target.read_bytes())
        (dist / "SHA256SUMS").write_text(f"{digest}  MANIFEST.txt\n" * 2)
        sign_sums(dist, _signing_key())  # validly signed, wrong inventory
    r = _approved_run(approved)
    assert r.returncode != 0
    assert needle in r.stderr
    assert "chart_sha256" not in r.stdout + r.stderr and "forged" not in r.stderr


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
    assert "does not match any configured release trust anchor (SNEAKERNET_TRUSTED_PUB)" in r.stderr


def test_checkout_guard_refuses_index_flags_that_hide_edits(approved):
    """`git diff` trusts the index: an edit under assume-unchanged or
    skip-worktree is invisible to it. The flag itself is refused, with the
    edit present or not, and the next run passes once it is cleared."""
    flag = "--assume-unchanged"
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
    trusted only through ../SHA256SUMS(.sig) like dist/ is; a ../SHA256SUMS that
    names MANIFEST.txt (every packed bundle's list does) is bundle evidence even
    with MANIFEST.txt removed, while an unrelated one in the parent (a downloads
    folder) is not, so connected development there (under the opt-out) keeps
    the notice."""
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
    # The parent list is only evidence when it is bundle-shaped.
    (bundle / "SHA256SUMS").write_text(f"{'0' * 64}  some-iso.img\n")
    stray = _guard_run(ws, "dist/MANIFEST.txt", _OPT_OUT)
    assert stray.returncode == 0, stray.stderr
    assert "not release-verified" in stray.stderr
    (bundle / "SHA256SUMS").write_text(f"{'0' * 64}  MANIFEST.txt\n")
    r = _guard_run(ws, "dist/MANIFEST.txt")
    assert r.returncode != 0
    assert "bundle evidence" in r.stderr


def test_checkout_guard_verified_release_passes_and_so_does_the_next_run(approved):
    """#1118 F1: a signed MANIFEST verified against the configured anchor (a
    file of its own, not the bundled key), HEAD equal to its sha and a clean
    tree pass silently; the next ordinary run passes the same way and the
    guard changed nothing in the checkout."""
    first = _approved_run(approved)
    assert first.returncode == 0, first.stderr
    assert first.stdout == first.stderr == ""
    status = _git(approved, "status", "--porcelain", "--ignored")
    again = _approved_run(approved)
    assert again.returncode == 0, again.stderr
    assert again.stdout == again.stderr == ""
    assert _git(approved, "status", "--porcelain", "--ignored") == status


def _resign_with_other_key(approved, *, bundle_pub):
    """A SHA256SUMS signature by a key the anchor does not name. With
    bundle_pub the bundled key is replaced to match (a consistent foreign
    bundle); without it the bundled key stays the anchor's (a bad signature)."""
    import subprocess

    from tests.helpers_airgap import gen_sign_keypair

    dist = approved / "dist"
    other = gen_sign_keypair(approved, "foreign.key")
    if bundle_pub:
        subprocess.run(["openssl", "pkey", "-in", str(other), "-pubout", "-out", str(dist / "sneakernet-signing.pub")],
                       check=True, capture_output=True)
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(other), "-out", str(dist / "SHA256SUMS.sig"),
                    str(dist / "SHA256SUMS")], check=True, capture_output=True)


def _commit_on_top(approved):
    (approved / "charts" / "values.yaml").write_text("replicas: 2\n")
    _git(approved, "commit", "-qam", "unreleased change")


@pytest.mark.parametrize(("case", "needle"), [
    ("no-manifest", _NO_MANIFEST),
    ("no-anchor", _NO_ANCHOR),
    ("unreadable-anchor", _UNREADABLE_ANCHOR),
    ("bad-signature", _BAD_SIGNATURE),
    ("foreign-key", _FOREIGN_KEY),
    ("head-differs", _HEAD_DIFFERS),
    ("tracked-change", _TRACKED_CHANGE),
    ("hidden-change", _HIDDEN_CHANGE),
])
def test_checkout_guard_refuses_what_is_not_a_verified_signed_release(approved, case, needle):
    """#1118 F1: each way a checkout falls short of a verified signed release
    is refused outside dry-run with its own fixed message (the whole stderr:
    no path, sha or content), and the same checkout still previews."""
    extra = {}
    if case == "no-manifest":
        shutil.rmtree(approved / "dist")
    elif case == "no-anchor":
        extra = {"SNEAKERNET_TRUSTED_PUB": None}
    elif case == "unreadable-anchor":
        extra = {"SNEAKERNET_TRUSTED_PUB": str(approved / "missing anchor.pub")}
    elif case in ("bad-signature", "foreign-key"):
        _resign_with_other_key(approved, bundle_pub=case == "foreign-key")
    elif case == "head-differs":
        _commit_on_top(approved)
    elif case == "tracked-change":
        (approved / "charts" / "values.yaml").write_text("replicas: 9\n")
    else:
        _git(approved, "update-index", "--assume-unchanged", "charts/values.yaml")
    r = _guard_run(approved, "dist/MANIFEST.txt", extra)
    assert r.returncode == 1
    assert r.stderr == needle
    assert r.stdout == ""
    dry = _guard_run(approved, "dist/MANIFEST.txt", {"AIRGAP_DRYRUN": "1", **extra})
    assert dry.returncode == 0, dry.stderr
    assert dry.stderr == "Notice: dry-run — checkout not release-verified\n"


def test_checkout_guard_next_run_passes_once_the_anchor_is_set(approved):
    """Lifecycle (#1118 F1): the missing-anchor refusal changes nothing, and
    the next ordinary run with the anchor exported passes."""
    refused = _guard_run(approved, "dist/MANIFEST.txt", {"SNEAKERNET_TRUSTED_PUB": None})
    assert refused.stderr == _NO_ANCHOR
    again = _approved_run(approved)
    assert again.returncode == 0, again.stderr
    assert again.stderr == ""


@pytest.mark.parametrize("ci_rehearsal", [None, "", "0", "true"])
def test_checkout_guard_opt_out_requires_ci_rehearsal(approved, ci_rehearsal):
    """#1118 F1: AIRGAP_UNVERIFIED_CHECKOUT=1 without CI_REHEARSAL=1 is refused
    with a fixed message, even on a checkout that would verify, and dry-run is
    unchanged."""
    extra = {"AIRGAP_UNVERIFIED_CHECKOUT": "1", "CI_REHEARSAL": ci_rehearsal}
    r = _guard_run(approved, "dist/MANIFEST.txt", extra)
    assert r.returncode == 1
    assert r.stderr == _OPT_OUT_REFUSED
    dry = _guard_run(approved, "dist/MANIFEST.txt", {"AIRGAP_DRYRUN": "1", **extra})
    assert dry.returncode == 0
    assert "not release-verified" in dry.stderr


def test_checkout_guard_opt_out_keeps_the_pre_f1_rules(approved):
    """#1118 F1: with CI_REHEARSAL=1 the opt-out runs a plain tree with the
    notice and verifies a MANIFEST on first use without an anchor, but still
    refuses a checkout whose HEAD differs from a present MANIFEST, and a
    configured anchor still pins the key."""
    from tests.helpers_airgap import gen_other_pub

    tofu = _guard_run(approved, "dist/MANIFEST.txt", {**_OPT_OUT, "SNEAKERNET_TRUSTED_PUB": None})
    assert tofu.returncode == 0, tofu.stderr
    assert tofu.stderr == ""
    pinned = _guard_run(approved, "dist/MANIFEST.txt",
                        {**_OPT_OUT, "SNEAKERNET_TRUSTED_PUB": str(gen_other_pub(approved, "opt-out-other"))})
    assert pinned.returncode != 0
    assert "does not match SNEAKERNET_TRUSTED_PUB" in pinned.stderr
    _commit_on_top(approved)
    moved = _guard_run(approved, "dist/MANIFEST.txt", {**_OPT_OUT, "SNEAKERNET_TRUSTED_PUB": None})
    assert moved.returncode != 0
    assert "HEAD does not match the packed MANIFEST sha" in moved.stderr
    shutil.rmtree(approved / "dist")
    dev = _guard_run(approved, "dist/MANIFEST.txt", {**_OPT_OUT, "SNEAKERNET_TRUSTED_PUB": None})
    assert dev.returncode == 0, dev.stderr
    assert dev.stderr == ("Notice: no packed MANIFEST — checkout not release-verified "
                          "(AIRGAP_UNVERIFIED_CHECKOUT=1, connected rehearsal only)\n")


def test_checkout_guard_opt_out_is_process_environment_only(approved):
    """#1118 F1: the operator file travels to the air gap, so it can neither
    opt out nor supply CI_REHEARSAL; a plain tree stays refused."""
    shutil.rmtree(approved / "dist")
    (approved / "airgap.env").write_text("AIRGAP_UNVERIFIED_CHECKOUT=1\nexport CI_REHEARSAL=1\n")
    r = _guard_run(approved, "dist/MANIFEST.txt")
    assert r.returncode == 1
    assert r.stderr == _NO_MANIFEST
    # The caller's process environment still decides: the file cannot clear it.
    (approved / "airgap.env").write_text("AIRGAP_UNVERIFIED_CHECKOUT=\nCI_REHEARSAL=0\n")
    ok = _guard_run(approved, "dist/MANIFEST.txt", _OPT_OUT)
    assert ok.returncode == 0, ok.stderr


def test_checkout_guard_opt_out_refuses_an_unknown_value(approved):
    r = _guard_run(approved, "dist/MANIFEST.txt", {**_OPT_OUT, "AIRGAP_UNVERIFIED_CHECKOUT": "true"})
    assert r.returncode == 1
    assert r.stderr == ("FAIL: AIRGAP_UNVERIFIED_CHECKOUT must be 1 (connected opt-out, with CI_REHEARSAL=1) "
                        "or unset; refusing to guess\n")


def test_validate_live_refuses_a_plain_checkout_and_passes_with_the_opt_out(tree):
    """#1118 F1 through a real launch path: live validate from a plain tree
    stops at the guard; the connected opt-out runs it to completion."""
    plain = _run(tree, {"AIRGAP_DRYRUN": "0", "AIRGAP_UNVERIFIED_CHECKOUT": None, "CI_REHEARSAL": None})
    assert plain.returncode == 1
    assert _NO_MANIFEST in plain.stderr
    assert "SUCCESS" not in plain.stdout
    opted = _run(tree, {"AIRGAP_DRYRUN": "0", **_OPT_OUT})
    assert opted.returncode == 0, opted.stderr
    assert "not release-verified" in opted.stderr


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


def test_contextual_model_pair_required_in_preflight(tree):
    config = {"CONTEXTUAL_EMBED_ENABLED": "true", "CONTEXT_LLM_BASE_URL": "https://context/v1",
              "CONTEXT_LLM_MODEL": ""}
    result = _run(tree, config)
    assert result.returncode != 0
    assert "CONTEXT_LLM_MODEL is required when CONTEXTUAL_EMBED_ENABLED is set" in result.stderr


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

def test_validate_invalid_operator_boolean_refused(tree):
    r = _run(tree, {"INGEST_REINGEST": "maybe-secretish"})
    assert r.returncode != 0
    assert "INGEST_REINGEST must be true/false" in r.stderr
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
_ACCEPTED_BOOLEANS = [('CONTEXTUAL_EMBED_ENABLED', 'YES'), ('LLM_STREAM', 'no')]


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




def test_validate_live_shared_snapshot_storageclass_is_read_once(tree):
    write_stub(
        tree / "bin" / "kubectl",
        "#!/bin/sh\n"
        'if [ "$1" = get ] && [ "$2" = storageclass ]; then\n'
        '  printf \'%s\\n\' "$3" >> storageclass-reads\n'
        "fi\nexit 0\n",
    )
    r = _live(tree, SNAPSHOT_STORAGE_CLASS="standard")
    assert r.returncode == 0, r.stderr
    assert (tree / "storageclass-reads").read_text().splitlines() == ["standard"]


@pytest.mark.parametrize("client_error,exit_code,message", [
    (None, 0, "StorageClass 'snapshot-tier' verified in cluster"),
    ("dial tcp: connection refused", 1, "cannot read StorageClass 'snapshot-tier' (unexpected client error)"),
])
def test_validate_live_distinct_snapshot_storageclass_probe(tree, client_error, exit_code, message):
    failure = f"  cat >&2 <<'EOT'\n{client_error}\nEOT\n  exit 1\n" if client_error else '  exit 0\n'
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




@pytest.mark.parametrize("text", [
    NOT_FOUND,
    # The reason token decides, not free text: a missing StorageClass whose name
    # contains 'forbidden' must not be classified as a Forbidden read.
    'Error from server (NotFound): storageclasses.storage.k8s.io "ceph-forbidden-tier" not found',
], ids=["notfound", "notfound-name-contains-forbidden"])
def test_validate_live_storageclass_notfound_fails(tree, text):
    write_stub(tree / "bin" / "kubectl", _kubectl_stub("storageclass", text))
    r = _live(tree)
    assert r.returncode != 0
    assert "must exist before deployment" in r.stderr


@pytest.mark.parametrize(("text", "refused"), [
    (FORBIDDEN, False),
    ("dial tcp: connection refused", True),
], ids=["forbidden-is-a-notice", "other-error-fails"])
def test_validate_live_storageclass_client_error_classified(tree, text, refused):
    write_stub(tree / "bin" / "kubectl", _kubectl_stub("storageclass", text))
    r = _live(tree)
    assert text not in r.stdout + r.stderr
    if refused:
        assert r.returncode != 0
        assert "unexpected client error" in r.stderr
        return
    assert r.returncode == 0, r.stderr
    assert "not found" not in r.stdout
    assert "Forbidden" in r.stdout and "NOT verified" in r.stdout


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


def test_validate_live_unauthenticated_or_unreachable_api_fails(tree):
    # anonymous: /version would pass, discovery does not
    write_stub(tree / "bin" / "kubectl", _namespace_admin_kubectl(FORBIDDEN))
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


@pytest.mark.parametrize(("text", "needle"), [
    (None, "OpenShift cluster detected"),
    ('error: the server doesn\'t have a resource type "scc"', "Standard Kubernetes cluster detected"),
    ("dial tcp: i/o timeout", "cluster type is NOT determined"),
], ids=["scc-readable", "scc-missing-type", "scc-unreadable"])
def test_validate_live_detects_the_cluster_type(tree, text, needle):
    if text:
        write_stub(tree / "bin" / "kubectl", _kubectl_stub("scc", text))
    r = _live(tree)
    assert r.returncode == 0, r.stderr
    assert needle in r.stdout
    if text == "dial tcp: i/o timeout":
        assert "Standard Kubernetes" not in r.stdout




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


@pytest.mark.parametrize(("pin", "route_on"), [
    (GOOD_PIN, True),
    ("sha256:PENDING", True),
    ("sha256:PENDING", False),
], ids=["recorded", "pending-refused", "route-off-ignores-pin"])
def test_validate_route_pin_state(tree, pin, route_on):
    _route_tree(tree, pin=pin)
    r = _route_run(tree, live=False) if route_on else _run(tree, {"AGENT_ROUTE": "false"})
    if pin == GOOD_PIN:
        assert r.returncode == 0, r.stderr
        assert "oauth-proxy pin recorded; matches the chart" in r.stdout
        assert "--email-domain=*" in r.stdout
    elif route_on:
        assert r.returncode != 0
        assert "oauth-proxy digest recorded" in r.stderr
    else:
        assert r.returncode == 0, r.stderr


def test_validate_route_pin_tag_must_match_chart(tree):
    _route_tree(tree)
    images = tree / "images.txt"
    images.write_text(images.read_text().replace("ose-oauth-proxy:v4.14", "ose-oauth-proxy:v4.99"))
    r = _route_run(tree, live=False)
    assert r.returncode != 0
    assert "does not match the chart" in r.stderr


@pytest.mark.parametrize(("lines", "rc", "needle"), [
    ((), 1, "packed without the oauth-proxy image"),
    (("oauth_proxy: registry.redhat.io/openshift4/ose-oauth-proxy@sha256:" + "c" * 64,),
     1, "differs from images.txt"),
    ((f"oauth_proxy: {OAUTH_REF}",), 0, "and the packed bundle"),
], ids=["absent", "other-pin", "matching-pin"])
def test_validate_route_manifest_pin_state(tree, lines, rc, needle):
    _route_tree(tree)
    _manifest(tree, *lines)
    r = _route_run(tree, live=False)
    assert r.returncode == rc, r.stderr
    assert needle in (r.stdout if rc == 0 else r.stderr)


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


def test_validate_openwebui_domain_qualified_roles_single_quoted_pass_unquoted_refused(tree):
    r"""#1070: sh drops a backslash outside single quotes when it sources the file
    (DOMAIN\group -> DOMAINgroup), so validate refuses that form before any tool
    with a fixed message, and accepts the documented single-quoted value."""
    _openwebui_tree(tree)
    base = {k: v for k, v in OWUI_OPERATOR_ENV.items() if not k.endswith("_ROLES")}
    good = tree / "roles-good.env"
    good.write_text("OPENWEBUI_OIDC_ALLOWED_ROLES='CORP\\docs-users'\n"
                    "OPENWEBUI_OIDC_ADMIN_ROLES='CORP\\docs-admins'\n")
    r = _run(tree, {**base, "AIRGAP_ENV": str(good)})
    assert r.returncode == 0, r.stderr
    bad = tree / "roles-bad.env"
    bad.write_text("OPENWEBUI_OIDC_ALLOWED_ROLES='CORP\\docs-users'\n"
                   "OPENWEBUI_OIDC_ADMIN_ROLES=CORP\\docs-admins\n")
    r, calls = _run_with_recording_stubs(tree, {**base, "AIRGAP_ENV": str(bad)})
    assert r.returncode != 0
    assert "has a backslash outside single quotes" in r.stderr
    assert "Single-quote the whole value" in r.stderr and "CORP" not in r.stderr
    assert calls == []


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


def test_validate_route_live_cookie_secret_failures(tree):
    _route_tree(tree)
    for env, message in (({"COOKIE_SECRET": "forbidden"}, "may not read Secret 'rag-agent-oauth-cookie'"),
                         ({"MISSING_COOKIE_KEY": "1"}, "missing or empty: cookie-secret")):
        r = _route_run(tree, **env)
        assert r.returncode != 0
        assert message in r.stderr


def test_validate_route_live_service_ca_must_be_pem(tree):
    _route_tree(tree)
    r = _route_run(tree, SERVICE_CA="garbage")
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


@pytest.mark.parametrize("source", ["file", "environment"])
@pytest.mark.parametrize("key", RETIRED_ENV_KEYS)
def test_validate_refuses_each_retired_key_alone(tree, key, source):
    value = RETIRED_ENV_VALUES[key]
    env = {"AIRGAP_ENV": _write_env_file(tree, f"{key}={value}\n")} if source == "file" else {key: value}
    r, calls = _run_with_recording_stubs(tree, env)
    _assert_retired_refusal(r, calls, [key])


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


@pytest.mark.parametrize(("endpoint", "refused"), [
    # d53595e's example pointed the exporter at http://jaeger:4318; today any set
    # endpoint means an external collector, so it would silently drop traces.
    ("http://jaeger:4318", True), ("https://jaeger:4318/v1/traces", True),
    ("http://Jaeger.mainframe-rag.svc:4318", True),
    ("http://jaeger.mainframe-rag.svc.cluster.local:4318", True),
    (None, False), ("", False), ("off", False), ("http://collector.platform:4318", False),
    ("http://jaeger-collector.platform:4318", False), ("http://jaeger.example.com:4318", False),
], ids=lambda v: "unset" if v is None else (v or "empty"))
def test_validate_trace_endpoint_classification(tree, endpoint, refused):
    extra = {} if endpoint is None else {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint}
    if not refused:
        r = _run(tree, extra)
        assert r.returncode == 0, r.stderr
        return
    r, calls = _run_with_recording_stubs(tree, extra)
    assert r.returncode != 0
    assert _JAEGER_LINE in r.stderr
    assert "unset it to use the bundled OpenObserve collector, or set your external collector" in r.stderr
    assert retired_keys_listed(r.stderr) == ["OTEL_EXPORTER_OTLP_ENDPOINT"]
    assert endpoint not in r.stdout + r.stderr
    assert calls == []


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
