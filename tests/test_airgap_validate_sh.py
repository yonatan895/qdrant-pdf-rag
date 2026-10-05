"""scripts/airgap/validate.sh pre-flight validation tests (issue #15).

Tests validate.sh in dry-run mode against hermetic stubs:
- Required env vars fail-closed when unset.
- Format checks: positive integer DENSE_DIM, http/https VLLM_BASE_URL.
- Storage class checks: NFS-looking names refused.
- Missing CLI tools fail-closed.
- Clean configuration exits 0.
"""

import os
import shutil

import pytest

from tests.helpers_airgap import (
    REPO,
    STUB_TOOL,
    copy_chart,
    make_bin_tree,
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
        "VLLM_BASE_URL": "http://vllm:8000/v1",
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
    assert "Tracing:           ON (http://jaeger:4318)" in r.stdout
    assert "EMBED_MODEL_REVISION: rev-1" in r.stdout



def test_validate_production_preset_supplies_tuple(tree):
    """Issue #360: with no explicit selection the checked-in production
    preset supplies 6/3/2 to preflight (and therefore to the ingest render)."""
    target = tree / "scripts" / "airgap"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "scripts" / "airgap" / "collection-policy.env", target)
    r = _run(
        tree,
        {
            "QDRANT_SHARD_NUMBER": None,
            "QDRANT_REPLICATION_FACTOR": None,
            "QDRANT_WRITE_CONSISTENCY_FACTOR": None,
        },
    )
    assert r.returncode == 0, r.stderr
    assert "Collection policy: S=6 RF=3 W=2" in r.stdout


@pytest.mark.parametrize("extra_env,exit_code,messages", [
    pytest.param({'QDRANT_REPLICATION_FACTOR': None}, 1, ('collection distribution policy is incomplete', 'QDRANT_REPLICATION_FACTOR'),
                 id='validate_partial_policy_fails_closed'),
    pytest.param({'QDRANT_SHARD_NUMBER': '6', 'QDRANT_REPLICATION_FACTOR': '2', 'QDRANT_WRITE_CONSISTENCY_FACTOR': '3'}, 1, ('exceeds',),
                 id='validate_write_above_replication_fails_closed'),
    pytest.param({'OTEL_EXPORTER_OTLP_ENDPOINT': 'jaeger:4318'}, None, ('must be http(s) or off',),
                 id='validate_tracing_bad_endpoint_fails_closed'),
    pytest.param({'INTERNAL_REGISTRY': None, 'REGISTRY_INTERNAL': None}, None, ('INTERNAL_REGISTRY', 'FAIL:'),
                 id='validate_missing_registry_fails'),
    pytest.param({'INTERNAL_REGISTRY': None, 'REGISTRY_INTERNAL': None, 'STORAGE_CLASS': None}, None, ('INTERNAL_REGISTRY', 'STORAGE_CLASS'),
                 id='validate_lists_all_missing_vars_at_once'),
    pytest.param({'STORAGE_CLASS': 'nfs-client'}, None, ('looks like NFS',),
                 id='validate_nfs_storage_refused'),
    pytest.param({'DENSE_DIM': 'not-a-number'}, None, ('DENSE_DIM must be a positive integer',),
                 id='validate_dense_dim_non_integer_refused'),
    pytest.param({'DENSE_DIM': '0'}, None, ('DENSE_DIM must be greater than 0',),
                 id='validate_dense_dim_zero_refused'),
    pytest.param({'EMBED_MODEL_REVISION': None}, None, ('EMBED_MODEL_REVISION', 'required variables unset'),
                 id='validate_missing_embed_revision_fails'),
    pytest.param({'EMBED_MODEL_REVISION': '   '}, None, ('EMBED_MODEL_REVISION must be a non-blank',),
                 id='validate_whitespace_embed_revision_fails'),
    pytest.param({'RERANK_ENDPOINT_ORDER': 'bogus'}, None, ('RERANK_ENDPOINT_ORDER must be score_first or rerank_first',),
                 id='validate_rerank_endpoint_order_bad_value_refused'),
    pytest.param({'VLLM_BASE_URL': 'ftp://vllm:8000'}, None, ('VLLM_BASE_URL must begin with http:// or https://',),
                 id='validate_vllm_url_bad_scheme_refused'),
    pytest.param({'GATEWAY_API_KEY_SECRET': 'Bad_Name!'}, None, ('GATEWAY_API_KEY_SECRET must be a DNS-subdomain name',),
                 id='validate_gateway_secret_bad_name_fails_closed'),
    pytest.param({'GATEWAY_BASE_URL': 'sample-api/v1'}, None, ('GATEWAY_BASE_URL must begin with http:// or https://',),
                 id='shared_gateway_invalid_url_refuses'),
    pytest.param({'LLM_MODEL_REASONING': 'code'}, None, ('LLM_BASE_URL is required when LLM_MODEL_REASONING is set',),
                 id='reasoning_without_resolved_url_fails_preflight'),
    pytest.param({'AGENT_ROUTE': 'maybe'}, None, ('AGENT_ROUTE must be true/false',),
                 id='validate_route_invalid_boolean_refused_even_in_dryrun'),
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


def test_validate_tracing_off_sentinel(tree):
    r = _run(tree, {"OTEL_EXPORTER_OTLP_ENDPOINT": "off"})
    assert r.returncode == 0, r.stderr
    assert "Tracing:           OFF" in r.stdout


def test_validate_jaeger_false_requires_intentional_destination(tree):
    # Issue #568: disabling the bundled backend alone is not a destination.
    r = _run(tree, {"JAEGER_ENABLED": "false"})
    assert r.returncode != 0
    assert "intentional trace destination" in r.stderr
    ok = _run(tree, {"JAEGER_ENABLED": "false", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.platform:4318"})
    assert ok.returncode == 0, ok.stderr
    assert "Tracing:           ON (http://collector.platform:4318)" in ok.stdout


def test_validate_rerank_endpoint_order_rerank_first_accepted(tree):
    r = _run(tree, {"RERANK_ENDPOINT_ORDER": "rerank_first"})
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    "var", ["EMBED_BASE_URL", "LLM_BASE_URL", "RERANK_BASE_URL", "CONTEXT_LLM_BASE_URL"]
)
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
VLLM_BASE_URL=ftp://file-vllm:8000
GATEWAY_API_KEY_SECRET=file-secret-should-lose
"""

VALID_ENV_FILE = """\
INTERNAL_REGISTRY=reg.internal:5000
NAMESPACE=mainframe-rag
STORAGE_CLASS=standard
EMBED_MODEL=ibm-granite/granite-embedding-125m-english
EMBED_MODEL_REVISION=file-rev-1
DENSE_DIM=768
VLLM_BASE_URL=http://vllm:8000/v1
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


def test_env_file_still_feeds_unset_vars(tree):
    r = _run(
        tree,
        {
            "AIRGAP_ENV": _write_env_file(tree, VALID_ENV_FILE),
            "INTERNAL_REGISTRY": None,
            "REGISTRY_INTERNAL": None,
            "NAMESPACE": None,
            "STORAGE_CLASS": None,
            "EMBED_MODEL": None,
            "EMBED_MODEL_REVISION": None,
            "DENSE_DIM": None,
            "VLLM_BASE_URL": None,
        },
    )
    assert r.returncode == 0, r.stderr


def test_empty_env_value_leaves_file_value(tree):
    # Empty is unset everywhere (${VAR:-} idiom): the file still feeds the key.
    r = _run(
        tree,
        {"AIRGAP_ENV": _write_env_file(tree, VALID_ENV_FILE), "INTERNAL_REGISTRY": ""},
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


def test_explicit_env_directory_fails_closed(tree):
    # Directories fail the regular-file check even for root (chmod-based
    # unreadable-file tests cannot fail closed for uid 0, so this is the
    # permission-shape case that holds everywhere).
    r, calls = _run_with_recording_stubs(tree, {"AIRGAP_ENV": str(tree)})
    assert r.returncode != 0, r.stdout
    assert f"AIRGAP_ENV selects '{tree}'" in r.stderr
    assert calls == []


def test_explicit_env_with_spaces_in_path_loads(tree):
    spaced = tree / "my env" / "poc file.env"
    spaced.parent.mkdir(parents=True, exist_ok=True)
    spaced.write_text(VALID_ENV_FILE)
    r = _run(
        tree,
        {
            "AIRGAP_ENV": str(spaced),
            "INTERNAL_REGISTRY": None,
            "REGISTRY_INTERNAL": None,
            "NAMESPACE": None,
            "STORAGE_CLASS": None,
            "EMBED_MODEL": None,
            "EMBED_MODEL_REVISION": None,
            "DENSE_DIM": None,
            "VLLM_BASE_URL": None,
        },
    )
    assert r.returncode == 0, r.stderr
    assert "SUCCESS: Pre-flight validation passed (dry-run mode)." in r.stdout


def test_unset_explicit_env_keeps_environment_only(tree):
    # No mandatory config file: unset selection with no ./airgap.env stays
    # environment-only (the fixture tree has no default file).
    assert not (tree / "airgap.env").exists()
    r = _run(tree, {"AIRGAP_ENV": None})
    assert r.returncode == 0, r.stderr
    assert "SUCCESS: Pre-flight validation passed (dry-run mode)." in r.stdout


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


@pytest.mark.parametrize(
    "var", ["LLM_API_KEY", "EMBED_API_KEY", "RERANK_API_KEY", "CONTEXT_LLM_API_KEY"]
)
def test_validate_refuses_plaintext_gateway_key_in_env_file(tree, var):
    r = _run(tree, {"AIRGAP_ENV": _write_env_file(tree, f"{var}=sk-plaintext-must-die\n")})
    assert r.returncode != 0
    assert "plaintext gateway virtual key" in r.stderr


def test_validate_allows_empty_and_commented_gateway_keys(tree):
    r = _run(
        tree,
        {"AIRGAP_ENV": _write_env_file(tree, "LLM_API_KEY=\n#EMBED_API_KEY=sk-commented\n")},
    )
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("bad_name", ["Bad_Name!", "a&b", "a|b"])
def test_validate_pull_secret_bad_name_fails_closed(tree, bad_name):
    # Same gate as the gateway secret name: sed-active chars (&, |) would
    # silently rewrite the manifest via wire_pull_secret / Helm --set.
    r = _run(tree, {"PULL_SECRET": bad_name})
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


def test_checkout_guard_skips_without_manifest_or_checkout(tree):
    """No reachable MANIFEST (connected development) keeps the notice-only
    behavior: the guard judges only a claimed release (issue #414)."""
    (tree / "scripts" / "airgap").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "scripts" / "airgap" / "common.sh", tree / "scripts" / "airgap" / "common.sh")
    r = _guard_run(tree, "dist/MANIFEST.txt")
    assert r.returncode == 0
    assert "not release-verified" in r.stderr


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


def test_validate_live_refuses_image_sha_override_in_copied_tree_without_git(tree):
    """Standalone launch (not via bootstrap) in a tree with no git identity:
    dist/MANIFEST.txt plus a matching IMAGE_SHA override must still refuse."""
    (tree / "dist").mkdir(exist_ok=True)
    write_signed_manifest(tree / "dist", f"sha: {IMAGE_SHA}\n")
    r = _run(tree, {"AIRGAP_DRYRUN": "0"})
    assert r.returncode != 0
    assert "cannot be resolved" in r.stderr
    assert "Verified matching MANIFEST" in r.stdout  # the override satisfied the SHA cross-check alone


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


def test_checkout_guard_clean_matching_checkout_passes_silently(approved):
    r = _approved_run(approved)
    assert r.returncode == 0, r.stderr
    assert r.stderr == "" and r.stdout == ""


def test_checkout_guard_allows_untracked_operator_files(approved):
    (approved / "airgap.env").write_text("INTERNAL_REGISTRY=site.internal\n")
    (approved / "dist" / "retained-evidence.txt").write_text("x")
    (approved / "scratch-output.yaml").write_text("rendered: true\n")
    r = _approved_run(approved)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("stage", [False, True], ids=["unstaged", "staged"])
@pytest.mark.parametrize("path", ["scripts/airgap/common.sh", "charts/values.yaml", "Taskfile.yml"])
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


@pytest.mark.parametrize("change", ["delete", "stage_new_file"])
def test_checkout_guard_refuses_other_tracked_changes(approved, change):
    if change == "delete":
        (approved / "charts" / "values.yaml").unlink()
    elif change == "stage_new_file":
        (approved / "scripts" / "extra.sh").write_text("echo hi\n")
        _git(approved, "add", "scripts/extra.sh")
    r = _approved_run(approved)
    assert r.returncode != 0
    assert "tracked changes" in r.stderr


def test_checkout_guard_wrong_head_message_wins_over_edit(approved):
    write_signed_manifest(approved / "dist", f"sha: {'c' * 40}\n")
    (approved / "Taskfile.yml").write_text("version: '4'\n")
    r = _approved_run(approved)
    assert r.returncode != 0
    assert "does not match the packed MANIFEST sha" in r.stderr


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


def test_checkout_guard_refuses_signature_by_a_different_key(approved):
    from tests.helpers_airgap import gen_sign_keypair, sign_sums

    other = approved / "other-key"
    other.mkdir()
    sign_sums(approved / "dist", gen_sign_keypair(other))  # sig by key B, pub still A
    r = _approved_run(approved)
    assert r.returncode != 0
    assert "signature verification failed" in r.stderr


@pytest.mark.parametrize("listing", ["unlisted", "duplicate"])
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


def test_checkout_guard_dryrun_keeps_notice_for_unsigned_manifest(approved):
    (approved / "dist" / "SHA256SUMS.sig").unlink()
    dry = _guard_run(approved, "dist/MANIFEST.txt", {"AIRGAP_DRYRUN": "1"})
    assert dry.returncode == 0
    assert "not release-verified" in dry.stderr


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
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


def test_shared_gateway_needs_no_legacy_vllm_url(tree):
    result = _run(tree, {"VLLM_BASE_URL": None, "GATEWAY_BASE_URL": "https://sample-api/v1",
                         "GATEWAY_API_KEY_SECRET": "shared", "GATEWAY_API_KEY_SECRET_KEY": "api-key"})
    assert result.returncode == 0, result.stderr
    assert "EMBED_BASE_URL:    https://sample-api/v1" in result.stdout


def test_model_validation_preserves_caller_over_file_precedence(tree):
    path = tree / "selected.env"
    path.write_text("LLM_BASE_URL=invalid-file-url\nLLM_MODEL_REASONING=code\nGATEWAY_BASE_URL=https://file-gateway/v1\n")
    result = _run(tree, {"AIRGAP_ENV": str(path), "LLM_BASE_URL": "https://caller-reasoning/v1",
                         "GATEWAY_BASE_URL": "https://caller-gateway/v1"})
    assert result.returncode == 0, result.stderr
    assert "EMBED_BASE_URL:    https://caller-gateway/v1" in result.stdout
    result = _run(tree, {"AIRGAP_ENV": str(path), "LLM_BASE_URL": "private-invalid-url"})
    assert result.returncode != 0
    assert "LLM_BASE_URL must begin with http:// or https://" in result.stderr
    assert "private-invalid-url" not in result.stdout + result.stderr


@pytest.mark.parametrize("missing", ["CONTEXT_LLM_BASE_URL", "CONTEXT_LLM_MODEL"])
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
    result = _run(tree, {"AIRGAP_ENV": str(path)})
    assert result.returncode == 0, result.stderr
    expected = "https://shared.example/v1" if shared else "http://vllm:8000/v1"
    assert f"EMBED_BASE_URL:    {expected}" in result.stdout


# ---------------------------------------------- strict operator booleans (#272)

BOOL_KEYS = [
    "UI_ENABLED", "CONTEXTUAL_EMBED_ENABLED", "INGEST_ALIAS_PUBLISH",
    "INGEST_REINGEST", "CHAT_CONDENSE_ENABLED",
]


@pytest.mark.parametrize("key", BOOL_KEYS)
def test_validate_invalid_operator_boolean_refused(tree, key):
    r = _run(tree, {key: "maybe-secretish"})
    assert r.returncode != 0
    assert f"{key} must be true/false" in r.stderr
    assert "maybe-secretish" not in r.stderr + r.stdout


@pytest.mark.parametrize("key", BOOL_KEYS)
@pytest.mark.parametrize("value", ["true", "False", "1", "no", "YES"])
def test_validate_valid_operator_boolean_accepted(tree, key, value):
    env = {key: value}
    if key == "CONTEXTUAL_EMBED_ENABLED":
        env |= {"CONTEXT_LLM_BASE_URL": "http://llm:8000/v1", "CONTEXT_LLM_MODEL": "m"}
    r = _run(tree, env)
    assert r.returncode == 0, r.stderr


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


def test_validate_live_forbidden_message_with_not_found_text_is_forbidden(tree):
    text = 'Error from server (Forbidden): storageclasses "not found-class" is forbidden: User cannot get resource'
    write_stub(tree / "bin" / "kubectl", _kubectl_stub("storageclass", text))
    r = _live(tree)
    assert r.returncode == 0, r.stderr
    assert "NOT verified" in r.stdout


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
    "error: You must be logged in to the server (Unauthorized)",
    "dial tcp 127.0.0.1:6443: connect: connection refused",
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


def test_validate_live_forbidden_namespace_missing_secret_fails(tree):
    write_stub(
        tree / "bin" / "kubectl",
        "#!/bin/sh\nfor arg in \"$@\"; do\n"
        f"  if [ \"$arg\" = namespace ]; then echo '{FORBIDDEN}' >&2; exit 1; fi\n"
        f"  if [ \"$arg\" = secret ]; then echo '{NOT_FOUND}' >&2; exit 1; fi\n"
        "done\nexit 0\n",
    )
    r = _live(tree, GATEWAY_API_KEY_SECRET="gateway-api-keys")
    assert r.returncode != 0
    assert "Secret 'gateway-api-keys' not found" in r.stderr


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


@pytest.mark.parametrize("text", [FORBIDDEN, "dial tcp: i/o timeout"])
def test_validate_live_scc_failure_is_not_standard_kubernetes(tree, text):
    write_stub(tree / "bin" / "kubectl", _kubectl_stub("scc", text))
    r = _live(tree)
    assert r.returncode == 0, r.stderr
    assert "Standard Kubernetes" not in r.stdout
    assert "cluster type is NOT determined" in r.stdout


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


def test_validate_route_live_success(tree):
    _route_tree(tree)
    r = _route_run(tree)
    assert r.returncode == 0, r.stderr
    assert "has a nonempty cookie-secret key" in r.stdout
    assert "Namespace service CA readable" in r.stdout


@pytest.mark.parametrize("state,message", [
    ("notfound", "'rag-agent-oauth-cookie' not found"),
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


@pytest.mark.parametrize("ca", ["", "garbage"])
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


@pytest.mark.parametrize("route_on", [True, False])
@pytest.mark.parametrize("backend", ["rag-agent", "qdrant", "qdrant-headless"])
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
