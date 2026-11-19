"""Operator configuration key registry and airgap:config show/check contracts (issue #1284).

Covers:
- Single-source stdlib-only operator key registry (`scripts/airgap/operator_keys.py`,
  `python3 -I`-safe).
- Exact sync between the registry and `scripts/airgap/common.sh` (`OPERATOR_ENV_KEYS`),
  `taskfiles/airgap.yml` (`TASK_OP_KEYS` and `TASK_*`), `airgap.env.example`, and
  `tests/fixtures/extension-surface.json`.
- `airgap.env`-file-only (non-exported) propagation of every `OPERATOR_ENV_KEYS`
  entry into `map_values.py` and `model_config.py` via `common.sh`, while keeping
  `CI_REHEARSAL` and `AIRGAP_UNVERIFIED_CHECKOUT` process-env-only.
- `airgap:config show` and `airgap:config check` (`scripts/airgap/config_tool.py`
  and `scripts/airgap/config.sh`), plus non-fatal warning integration in
  `scripts/airgap/validate.sh`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from tests.helpers_airgap import (
    REPO,
    STUB_TOOL,
    connected_checkout,
    copy_chart,
    make_bin_tree,
    run_sh,
    system_path,
    write_stub,
)

REGISTRY = REPO / "scripts" / "airgap" / "operator_keys.py"
CONFIG_TOOL = REPO / "scripts" / "airgap" / "config_tool.py"
CONFIG_SH = REPO / "scripts" / "airgap" / "config.sh"


def _run_isolated_py(script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    clean_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if env:
        clean_env.update(env)
    return subprocess.run(
        [sys.executable, "-I", str(script), *args],
        cwd=REPO,
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )


# ------------------------------------------------------------------ Step 1: Registry & Sync


def test_registry_and_config_tool_are_stdlib_only_under_python_I():
    """Bastion scripts must execute under `python3 -I` with zero third-party or
    `mainframe_rag` imports."""
    for script in (REGISTRY, CONFIG_TOOL):
        r = _run_isolated_py(script, "--help")
        assert r.returncode == 0, f"{script.name} failed under python3 -I: {r.stderr}"
    r = _run_isolated_py(REGISTRY, "--check")
    assert r.returncode == 0, f"operator_keys --check failed:\n{r.stdout}\n{r.stderr}"


def test_registry_entries_are_complete_and_well_typed():
    from scripts.airgap.operator_keys import (
        KEY_TYPES,
        OPERATOR_KEYS,
        OPERATOR_KEYS_BY_NAME,
        VALID_SURFACES,
    )

    assert len(OPERATOR_KEYS) >= 128
    assert len(OPERATOR_KEYS_BY_NAME) == len(OPERATOR_KEYS), "duplicate key name in registry"
    for entry in OPERATOR_KEYS:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]+", entry.name), entry.name
        assert entry.section.strip(), entry.name
        assert entry.key_type in KEY_TYPES, (entry.name, entry.key_type)
        assert entry.description.strip(), entry.name
        assert re.fullmatch(r"#[0-9]+", entry.issue), (entry.name, entry.issue)
        assert entry.surfaces and entry.surfaces <= VALID_SURFACES, (entry.name, entry.surfaces)
        assert "shell_snapshot" in entry.surfaces, entry.name
        assert "task_bridge" in entry.surfaces, entry.name
        if entry.key_type == "enum":
            assert entry.allowed_values, f"enum key {entry.name} must declare allowed_values"
        if "mapper" in entry.surfaces:
            assert entry.helm_path, f"mapper key {entry.name} must declare helm_path"


def test_registry_matches_common_sh_taskfile_example_and_extension_surface():
    """Every registry key reaches all declared surfaces and no surface carries
    an unregistered key."""
    from scripts.airgap.model_config import RETIRED_KEYS
    from scripts.airgap.operator_keys import (
        OPERATOR_KEYS,
        ci_only_example_keys,
        example_keys,
        operator_env_keys,
        render_taskfile_bridge,
        task_bridge_keys,
    )

    # 1. common.sh OPERATOR_ENV_KEYS
    common_text = (REPO / "scripts" / "airgap" / "common.sh").read_text(encoding="utf-8")
    m = re.search(r'^OPERATOR_ENV_KEYS="([^"]+)"', common_text, re.MULTILINE)
    assert m is not None
    assert m.group(1).split() == list(operator_env_keys())

    # 2. taskfiles/airgap.yml TASK_OP_KEYS and TASK_* lines
    taskfile_text = (REPO / "taskfiles" / "airgap.yml").read_text(encoding="utf-8")
    m_task = re.search(r'^\s+TASK_OP_KEYS:\s*"([^"]+)"', taskfile_text, re.MULTILINE)
    assert m_task is not None
    assert m_task.group(1).split() == list(task_bridge_keys())
    expected_block = render_taskfile_bridge()
    assert expected_block in taskfile_text, (
        "taskfiles/airgap.yml operator_env block drifted from operator_keys.render_taskfile_bridge()"
    )

    # 3. airgap.env.example keys and section headers
    example_text = (REPO / "airgap.env.example").read_text(encoding="utf-8")
    found_example_keys = set(re.findall(r"(?m)^#?[ \t]*([A-Z][A-Z0-9_]*)=.*", example_text))
    expected_example_keys = set(example_keys()) | set(ci_only_example_keys(REPO))
    assert found_example_keys == expected_example_keys

    # Every registry section for example keys appears verbatim as `# --- <section> ---`
    example_sections = set(re.findall(r"(?m)^# --- (.+?) ---$", example_text))
    for entry in OPERATOR_KEYS:
        if "example" in entry.surfaces:
            assert entry.section in example_sections, (entry.name, entry.section)

    # 4. tests/fixtures/extension-surface.json
    ext = json.loads((REPO / "tests" / "fixtures" / "extension-surface.json").read_text(encoding="utf-8"))
    assert set(ext["current"]["airgap_env_keys"]) == expected_example_keys
    assert set(ext["current"]["retired_env_keys"]) == {k for k, _, _ in RETIRED_KEYS}


def test_registry_mapper_helm_paths_match_extension_surface_chart_values():
    """Every registry key declaring a `mapper` surface names a Helm value path
    that `map_values.py` actually emits (or an opaque/root block thereof)."""
    from scripts.airgap.operator_keys import OPERATOR_KEYS

    ext = json.loads((REPO / "tests" / "fixtures" / "extension-surface.json").read_text(encoding="utf-8"))
    chart_paths = {p.split(":", 1)[1] for p in ext["current"]["chart_values"] if p.startswith("mainframe-rag:")}
    for entry in OPERATOR_KEYS:
        if "mapper" not in entry.surfaces:
            continue
        assert entry.helm_path is not None
        # Either exact leaf path or prefix of nested paths (e.g. images.agent -> images.agent.repository)
        assert entry.helm_path in chart_paths or any(
            p.startswith(entry.helm_path + ".") for p in chart_paths
        ), f"{entry.name} declares helm_path={entry.helm_path!r} not in chart_values"


def test_every_operator_env_key_from_airgap_env_file_only_reaches_map_values_and_model_config(tmp_path):
    """Issue #1284 contract: setting EVERY key in OPERATOR_ENV_KEYS only inside
    `airgap.env` (never exported in caller process env) must reach both
    `map_app_values` (`map_values.py`) and `validate_model_config` (`model_config.py`),
    while process-env-only keys (`CI_REHEARSAL`, `AIRGAP_UNVERIFIED_CHECKOUT`)
    assigned in `airgap.env` are never exported or honored."""
    from scripts.airgap.operator_keys import operator_env_keys

    make_bin_tree(tmp_path, ["common.sh"])
    write_stub(tmp_path / "bin" / "helm", "#!/bin/sh\necho v4.3.0+g0000000\n")
    keys = list(operator_env_keys())

    # Replace map_values.py and model_config.py with recorders capturing os.environ
    map_log = tmp_path / "map_env.json"
    mc_log = tmp_path / "mc_env.json"
    (tmp_path / "scripts" / "airgap" / "map_values.py").write_text(
        f"import json, os\nopen({str(map_log)!r}, 'w').write(json.dumps(dict(os.environ)))\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "airgap" / "model_config.py").write_text(
        f"import json, os\nopen({str(mc_log)!r}, 'w').write(json.dumps(dict(os.environ)))\n",
        encoding="utf-8",
    )

    # Write every OPERATOR_ENV_KEYS entry into airgap.env (plain unexported assignments),
    # plus process-env-only keys that must NOT be honored from the file.
    expected = {k: f"from-file-{k}" for k in keys}
    expected["AIRGAP_DRYRUN"] = "0"
    expected["AIRGAP_ENV"] = str(tmp_path / "airgap.env")
    expected["OPENWEBUI_OIDC_ALLOWED_ROLES"] = "users"
    expected["OPENWEBUI_OIDC_ADMIN_ROLES"] = "admins"
    lines = [f"{k}='{v}'" for k, v in expected.items()]
    lines.append("CI_REHEARSAL=1")
    lines.append("AIRGAP_UNVERIFIED_CHECKOUT=1")
    (tmp_path / "airgap.env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    harness = tmp_path / "scripts" / "airgap" / "probe.sh"
    harness.write_text(
        "#!/bin/sh\n"
        '. "$(dirname -- "$0")/common.sh"\n'
        "validate_model_config\n"
        "map_app_values\n",
        encoding="utf-8",
    )
    # Clean caller env: NONE of OPERATOR_ENV_KEYS are set in caller env!
    env = {"PATH": system_path(tmp_path / "bin")}
    r = run_sh(harness, env, tmp_path)
    assert r.returncode == 0, f"probe failed: {r.stderr}\n{r.stdout}"

    mc_seen = json.loads(mc_log.read_text(encoding="utf-8"))
    map_seen = json.loads(map_log.read_text(encoding="utf-8"))
    for k, want in expected.items():
        if k == "AIRGAP_ENV":
            # AIRGAP_ENV inside airgap.env is sourced as a variable; verify it reaches child too
            assert mc_seen.get(k) == want, f"model_config missed {k}"
            assert map_seen.get(k) == want, f"map_values missed {k}"
        else:
            assert mc_seen.get(k) == want, f"model_config missed {k}: got {mc_seen.get(k)!r}"
            assert map_seen.get(k) == want, f"map_values missed {k}: got {map_seen.get(k)!r}"

    # Process-env-only opt-ins assigned in airgap.env must stay empty / unhonored
    assert mc_seen.get("CI_REHEARSAL", "") == ""
    assert map_seen.get("CI_REHEARSAL", "") == ""
    assert mc_seen.get("AIRGAP_UNVERIFIED_CHECKOUT", "") == ""
    assert map_seen.get("AIRGAP_UNVERIFIED_CHECKOUT", "") == ""


# ------------------------------------------------------------------ Step 2: airgap:config show & check


def _run_config_sh(tmp_path: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(tmp_path / "scripts" / "airgap" / "config.sh"), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_config_show_groups_by_section_marks_default_vs_set_and_protects_secrets(tmp_path):
    """`airgap:config show` prints effective config grouped by registry section,
    with CLI/env > file > default precedence, secret-ref shown by Secret name
    only, and sensitive values redacted."""
    make_bin_tree(tmp_path, ["common.sh", "config.sh"])
    env_file = tmp_path / "site.env"
    env_file.write_text(
        "INTERNAL_REGISTRY=reg.site.internal/rag\n"
        "NAMESPACE=file-ns\n"
        "STORAGE_CLASS=gp3-csi\n"
        "GATEWAY_BASE_URL=https://gw.site.internal/v1\n"
        "GATEWAY_API_KEY_SECRET=site-gw-secret\n"
        "TRACE_QUERY_PASSWORD=super-secret-trace-pw\n",
        encoding="utf-8",
    )
    env = {
        "PATH": system_path(tmp_path / "bin"),
        "AIRGAP_ENV": str(env_file),
        "NAMESPACE": "cli-override-ns",
        "LLM_API_KEY": "sk-ambient-must-never-appear",
    }
    r = _run_config_sh(tmp_path, env, "show")
    assert r.returncode == 0, r.stderr
    out = r.stdout

    # Grouped by registry section headers
    assert "[Internal registry (push from air-gap, pulled in-cluster)]" in out
    assert "[Target namespace / cluster]" in out
    assert "[Gateway API key (LiteLLM, all model legs)]" in out

    # Precedence & set vs default markers
    assert "NAMESPACE = cli-override-ns" in out
    assert "[set (env)]" in out
    assert "INTERNAL_REGISTRY = reg.site.internal/rag" in out
    assert "[set (file)]" in out
    assert "QDRANT_RELEASE = qdrant" in out
    assert "[default]" in out

    # Secret ref shown by name only; sensitive values redacted; ambient API keys never printed
    assert "GATEWAY_API_KEY_SECRET = site-gw-secret (secret-ref)" in out
    assert "TRACE_QUERY_PASSWORD = <redacted>" in out
    assert "super-secret-trace-pw" not in out + r.stderr
    assert "sk-ambient-must-never-appear" not in out + r.stderr


def test_config_check_passes_for_valid_airgap_env(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "config.sh"])
    env_file = tmp_path / "valid.env"
    env_file.write_text(
        "INTERNAL_REGISTRY=reg.example.internal/rag\n"
        "NAMESPACE=mainframe-rag\n"
        "STORAGE_CLASS=gp3-csi\n"
        "EMBED_BASE_URL=https://gw.example.internal/v1\n"
        "EMBED_MODEL=embed-v1\n"
        "DENSE_DIM=4096\n"
        "EMBED_MODEL_REVISION=rev-2026-11\n"
        "OPENWEBUI_ENABLED=false\n"
        "QDRANT_SHARD_NUMBER=6\n"
        "QDRANT_REPLICATION_FACTOR=3\n"
        "QDRANT_WRITE_CONSISTENCY_FACTOR=2\n",
        encoding="utf-8",
    )
    env = {"PATH": system_path(tmp_path / "bin"), "AIRGAP_ENV": str(env_file)}
    r = _run_config_sh(tmp_path, env, "check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK:" in r.stdout


def test_config_check_catches_unknown_key_with_did_you_mean(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "config.sh"])
    env_file = tmp_path / "typo.env"
    env_file.write_text(
        "EMBED_MODLE=secret-value-never-echo\n"
        "INGEST_WORKER=8\n"
        "TOTALLY_UNKNOWN_XYZ=123\n",
        encoding="utf-8",
    )
    env = {"PATH": system_path(tmp_path / "bin"), "AIRGAP_ENV": str(env_file)}
    r = _run_config_sh(tmp_path, env, "check")
    assert r.returncode != 0
    combined = r.stdout + r.stderr
    assert "EMBED_MODLE" in combined and "did you mean" in combined and "EMBED_MODEL" in combined
    assert "INGEST_WORKER" in combined and "INGEST_WORKERS" in combined
    assert "TOTALLY_UNKNOWN_XYZ" in combined
    assert "secret-value-never-echo" not in combined


def test_config_check_catches_type_bound_mutual_exclusion_and_deprecated_issues(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "config.sh"])
    env_file = tmp_path / "bad.env"
    env_file.write_text(
        "CHAT_MAX_TURNS=999\n"
        "REQUEST_DEADLINE_S=300\n"
        "LLM_EFFORT_TRANSPORT=invalid-transport\n"
        "CHAT_CONDENSE_ENABLED=true\n"
        "CHAT_FOLLOWUP_ANCHOR_ENABLED=true\n"
        "INGEST_AUTHORIZE_FROM_CATALOG=true\n"
        "SERVICEMONITOR_ENABLED=true\n"
        "METRICS_ENABLED=false\n"
        "EMBED_MODE=hash\n"
        "STORAGE_CLASS=nfs-csi\n"
        "OPENWEBUI_ENABLED=false\n"
        "CI_REHEARSAL=1\n",
        encoding="utf-8",
    )
    env = {"PATH": system_path(tmp_path / "bin"), "AIRGAP_ENV": str(env_file)}
    r = _run_config_sh(tmp_path, env, "check")
    assert r.returncode != 0
    combined = r.stdout + r.stderr
    assert "CHAT_MAX_TURNS" in combined
    assert "REQUEST_DEADLINE_S" in combined
    assert "LLM_EFFORT_TRANSPORT" in combined
    assert "CHAT_CONDENSE_ENABLED and CHAT_FOLLOWUP_ANCHOR_ENABLED are mutually exclusive" in combined
    assert "INGEST_AUTHORIZE_FROM_CATALOG requires INGEST_RETIRE_DOCS" in combined
    assert "SERVICEMONITOR_ENABLED" in combined
    assert "EMBED_MODE=hash" in combined
    assert "STORAGE_CLASS" in combined
    assert "CI_REHEARSAL" in combined
    assert "invalid-transport" not in combined


def test_config_check_reports_deprecated_and_retired_keys(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "config.sh"])
    # Deprecated key with true => warning (exit 0 when otherwise valid)
    dep_file = tmp_path / "deprecated.env"
    dep_file.write_text(
        "INGEST_ALIAS_PUBLISH=true\n"
        "OPENWEBUI_ENABLED=false\n",
        encoding="utf-8",
    )
    env = {"PATH": system_path(tmp_path / "bin"), "AIRGAP_ENV": str(dep_file)}
    r = _run_config_sh(tmp_path, env, "check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARNING:" in r.stdout + r.stderr and "INGEST_ALIAS_PUBLISH" in r.stdout + r.stderr

    # Retired key => error (exit nonzero), never echoes value
    ret_file = tmp_path / "retired.env"
    ret_file.write_text("REGISTRY_INTERNAL=secret-old-reg\nOPENWEBUI_ENABLED=false\n", encoding="utf-8")
    env["AIRGAP_ENV"] = str(ret_file)
    r2 = _run_config_sh(tmp_path, env, "check")
    assert r2.returncode != 0
    assert "REGISTRY_INTERNAL" in r2.stdout + r2.stderr
    assert "secret-old-reg" not in r2.stdout + r2.stderr


def test_validate_runs_config_check_as_non_fatal_warning(tmp_path):
    """Issue #1284: `airgap:validate` runs `config check` as a warning first —
    an unknown key typo in `airgap.env` emits a WARNING naming the typo and its
    suggestion, without failing `validate.sh`."""
    make_bin_tree(tmp_path, ["common.sh", "validate.sh"])
    copy_chart(tmp_path)
    for name in ("skopeo", "helm", "kubectl", "oc"):
        write_stub(tmp_path / "bin" / name, STUB_TOOL)
    env_file = tmp_path / "typo.env"
    env_file.write_text("EMBED_MODLE=typo-value\n", encoding="utf-8")
    env = {
        "PATH": system_path(tmp_path / "bin"),
        "AIRGAP_DRYRUN": "1",
        "AIRGAP_ENV": str(env_file),
        "IMAGE_SHA": "b" * 40,
        "INTERNAL_REGISTRY": "reg.internal:5000",
        "NAMESPACE": "mainframe-rag",
        "STORAGE_CLASS": "standard",
        "EMBED_MODEL": "ibm-granite/granite-embedding-125m-english",
        "DENSE_DIM": "768",
        "EMBED_MODEL_REVISION": "rev-1",
        "EMBED_BASE_URL": "http://vllm:8000/v1",
        "OPENWEBUI_ENABLED": "false",
        "QDRANT_SHARD_NUMBER": "1",
        "QDRANT_REPLICATION_FACTOR": "1",
        "QDRANT_WRITE_CONSISTENCY_FACTOR": "1",
    }
    connected_checkout(env, set())
    r = run_sh(tmp_path / "scripts" / "airgap" / "validate.sh", env, tmp_path)
    assert r.returncode == 0, r.stderr
    combined = r.stdout + r.stderr
    assert "WARNING:" in combined
    assert "EMBED_MODLE" in combined
    assert "EMBED_MODEL" in combined
    assert "typo-value" not in combined


def test_validate_clean_env_emits_no_config_check_warning(tmp_path):
    """A clean airgap.env emits zero `config check` warnings from validate.sh."""
    make_bin_tree(tmp_path, ["common.sh", "validate.sh"])
    copy_chart(tmp_path)
    for name in ("skopeo", "helm", "kubectl", "oc"):
        write_stub(tmp_path / "bin" / name, STUB_TOOL)
    env_file = tmp_path / "clean.env"
    env_file.write_text(
        "INTERNAL_REGISTRY=reg.internal:5000\n"
        "NAMESPACE=mainframe-rag\n"
        "STORAGE_CLASS=standard\n"
        "EMBED_MODEL=ibm-granite/granite-embedding-125m-english\n"
        "DENSE_DIM=768\n"
        "EMBED_MODEL_REVISION=rev-1\n"
        "EMBED_BASE_URL=http://vllm:8000/v1\n"
        "OPENWEBUI_ENABLED=false\n"
        "QDRANT_SHARD_NUMBER=1\n"
        "QDRANT_REPLICATION_FACTOR=1\n"
        "QDRANT_WRITE_CONSISTENCY_FACTOR=1\n",
        encoding="utf-8",
    )
    env = {
        "PATH": system_path(tmp_path / "bin"),
        "AIRGAP_DRYRUN": "1",
        "AIRGAP_ENV": str(env_file),
        "IMAGE_SHA": "b" * 40,
    }
    connected_checkout(env, set())
    r = run_sh(tmp_path / "scripts" / "airgap" / "validate.sh", env, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "WARNING: config check:" not in r.stdout + r.stderr


def test_registry_chart_env_declarations_exist_in_chart_templates():
    """Every registry key declaring `chart_env` names env vars that actually
    exist in `charts/mainframe-rag/templates/*.yaml`."""
    from scripts.airgap.operator_keys import OPERATOR_KEYS

    templates_text = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((REPO / "charts" / "mainframe-rag" / "templates").glob("*.yaml"))
    )
    for entry in OPERATOR_KEYS:
        if "chart_env" not in entry.surfaces:
            continue
        assert entry.chart_env, f"{entry.name} declares chart_env surface without env var names"
        for var in entry.chart_env:
            assert var in templates_text, f"{entry.name} chart_env {var!r} not found in chart templates"


def test_operator_keys_check_detects_surface_drift(tmp_path):
    """Mutating any synced surface causes `operator_keys.check_sync` to fail."""
    from scripts.airgap.operator_keys import check_sync

    for rel in (
        "scripts/airgap/common.sh",
        "taskfiles/airgap.yml",
        "airgap.env.example",
        "tests/fixtures/extension-surface.json",
    ):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dst)

    assert check_sync(tmp_path) == []

    # Drop a key from OPERATOR_ENV_KEYS in common.sh
    common = tmp_path / "scripts/airgap/common.sh"
    common.write_text(common.read_text(encoding="utf-8").replace(" UI_ENABLED", ""), encoding="utf-8")
    findings = check_sync(tmp_path)
    assert any("OPERATOR_ENV_KEYS" in f and "UI_ENABLED" in f for f in findings)


def test_config_check_catches_remaining_cross_key_and_plaintext_rules(tmp_path):
    from scripts.airgap.config_tool import check_config

    env_file = tmp_path / "all_cross.env"
    env_file.write_text(
        "LLM_API_KEY=sk-secret-must-not-leak\n"
        "CI_APPROVED_REGISTRY=reg.ci.internal\n",
        encoding="utf-8",
    )
    errors, warnings = check_config(
        {
            "OPENWEBUI_ENABLED": "false",
            "OPENWEBUI_RATINGS_ENABLED": "true",
            "FEEDBACK_ENABLED": "false",
            "GATEWAY_API_KEY_SECRET_KEY": "api-key",
            "OPENOBSERVE_RUN_AS_ID": "10001",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "off",
            "QDRANT_SHARD_NUMBER": "6",
            "QDRANT_REPLICATION_FACTOR": "2",
            "QDRANT_WRITE_CONSISTENCY_FACTOR": "3",
            "REQUEST_MAX_CONCURRENT": "4",
            "REQUEST_QUEUE_WAIT_S": "0",
            "LLM_MODEL_REASONING": "reasoner",
            "CONTEXTUAL_EMBED_ENABLED": "true",
        },
        env_file=env_file,
    )
    joined_errors = "\n".join(errors)
    assert "LLM_API_KEY" in joined_errors and "sk-secret-must-not-leak" not in joined_errors
    assert "OPENWEBUI_RATINGS_ENABLED=true requires FEEDBACK_ENABLED=true" in joined_errors
    assert "GATEWAY_API_KEY_SECRET_KEY requires GATEWAY_API_KEY_SECRET" in joined_errors
    assert "OPENOBSERVE_RUN_AS_ID requires the bundled OpenObserve" in joined_errors
    assert "QDRANT_WRITE_CONSISTENCY_FACTOR must not exceed QDRANT_REPLICATION_FACTOR" in joined_errors
    assert "REQUEST_QUEUE_WAIT_S must be greater than 0" in joined_errors
    assert "LLM_BASE_URL is required when LLM_MODEL_REASONING is set" in joined_errors
    assert "CONTEXT_LLM_MODEL is required when CONTEXTUAL_EMBED_ENABLED is set" in joined_errors
    assert any("CI_APPROVED_REGISTRY" in w for w in warnings)


def test_config_sh_help_and_argument_errors(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "config.sh"])
    env = {"PATH": system_path(tmp_path / "bin")}
    r_help = _run_config_sh(tmp_path, env, "--help")
    assert r_help.returncode == 0
    assert "usage:" in r_help.stdout
    for bad_args in ((), ("unknown",), ("show", "check")):
        r_bad = _run_config_sh(tmp_path, env, *bad_args)
        assert r_bad.returncode == 2
        assert "FAIL:" in r_bad.stderr


def test_airgap_config_task_runs_show_and_check_with_cli_precedence(tmp_path):
    """End-to-end Task runner check: `run-task.sh airgap:config -- show|check`
    preserves CLI > file > default precedence."""
    env_file = tmp_path / "task.env"
    env_file.write_text(
        "INTERNAL_REGISTRY=reg.task.internal/rag\n"
        "NAMESPACE=file-ns\n"
        "STORAGE_CLASS=gp3-csi\n"
        "EMBED_BASE_URL=https://gw.task.internal/v1\n"
        "EMBED_MODEL=embed-v1\n"
        "DENSE_DIM=4096\n"
        "EMBED_MODEL_REVISION=rev-1\n"
        "OPENWEBUI_ENABLED=false\n"
        "QDRANT_SHARD_NUMBER=1\n"
        "QDRANT_REPLICATION_FACTOR=1\n"
        "QDRANT_WRITE_CONSISTENCY_FACTOR=1\n",
        encoding="utf-8",
    )
    r_show = subprocess.run(
        [
            "sh",
            "scripts/tools/run-task.sh",
            "airgap:config",
            f"AIRGAP_ENV={env_file}",
            "NAMESPACE=cli-task-ns",
            "--",
            "show",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r_show.returncode == 0, r_show.stdout + r_show.stderr
    assert "NAMESPACE = cli-task-ns  [set (env)]" in r_show.stdout
    assert "INTERNAL_REGISTRY = reg.task.internal/rag  [set (file)]" in r_show.stdout

    r_check = subprocess.run(
        ["sh", "scripts/tools/run-task.sh", "airgap:config", f"AIRGAP_ENV={env_file}", "--", "check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r_check.returncode == 0, r_check.stdout + r_check.stderr
    assert "OK:" in r_check.stdout


def test_unchanged_airgap_env_renders_byte_identical_values_and_manifests(tmp_path):
    """Proves unchanged `airgap.env` inputs produce byte-identical release values
    and rendered Helm manifests whether supplied via `airgap.env` (file-only,
    non-exported) or via exported process environment."""
    from tests.helpers_airgap import install_rendering_helm

    def _render_pair(workdir: Path, *, via_file: bool) -> dict[str, bytes]:
        workdir.mkdir(parents=True, exist_ok=True)
        make_bin_tree(workdir, ["common.sh", "deploy.sh", "ingest.sh"])
        copy_chart(workdir)
        for name in ("skopeo", "kubectl", "oc"):
            write_stub(workdir / "bin" / name, STUB_TOOL)
        write_stub(workdir / "bin" / "helm", STUB_TOOL)
        install_rendering_helm(workdir)
        shutil.copy2(REPO / "images.txt", workdir / "images.txt")
        shutil.copy2(REPO / "charts/qdrant-openshift.values.yaml", workdir / "charts/qdrant-openshift.values.yaml")
        # Copy vendored qdrant chart tgz stub
        for tgz in (REPO / "charts").glob("qdrant-*.tgz"):
            shutil.copy2(tgz, workdir / "charts" / tgz.name)
        cfg = {
            "IMAGE_SHA": "a" * 40,
            "INTERNAL_REGISTRY": "registry.example.internal/mainframe-rag",
            "NAMESPACE": "mainframe-rag",
            "STORAGE_CLASS": "gp3-csi",
            "CORPUS_PVC": "corpus-pvc",
            "EMBED_MODEL": "ibm-granite/granite-embedding-278m-multilingual",
            "DENSE_DIM": "768",
            "EMBED_MODEL_REVISION": "dryrun-rev-1",
            "EMBED_BASE_URL": "http://vllm.inference.svc.cluster.local:8000/v1",
            "OPENWEBUI_ENABLED": "false",
            "QDRANT_SHARD_NUMBER": "6",
            "QDRANT_REPLICATION_FACTOR": "3",
            "QDRANT_WRITE_CONSISTENCY_FACTOR": "2",
        }
        env = {"PATH": system_path(workdir / "bin"), "AIRGAP_DRYRUN": "1"}
        if via_file:
            env_file = workdir / "unchanged.env"
            env_file.write_text("\n".join(f"{k}={v}" for k, v in cfg.items()) + "\n", encoding="utf-8")
            env["AIRGAP_ENV"] = str(env_file)
        else:
            empty = workdir / "empty.env"
            empty.write_text("", encoding="utf-8")
            env["AIRGAP_ENV"] = str(empty)
            env.update(cfg)
        r_dep = run_sh(workdir / "scripts/airgap/deploy.sh", env, workdir)
        assert r_dep.returncode == 0, r_dep.stderr
        r_ing = run_sh(workdir / "scripts/airgap/ingest.sh", env, workdir)
        assert r_ing.returncode == 0, r_ing.stderr
        return {
            name: (workdir / "dist" / name).read_bytes()
            for name in (
                "mainframe-rag-release-values.yaml",
                "mainframe-rag-ingest-values.yaml",
                "agent-rendered.yaml",
                "ingest-rendered.yaml",
                "ingest-work-rendered.yaml",
            )
        }

    from_file = _render_pair(tmp_path / "from_file", via_file=True)
    from_env = _render_pair(tmp_path / "from_env", via_file=False)
    assert from_file == from_env

