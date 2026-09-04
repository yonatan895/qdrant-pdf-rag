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
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
IMAGE_SHA = "b" * 40

STUB_TOOL = """#!/bin/sh
exit 0
"""


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "charts").mkdir()
    (tmp_path / "scripts" / "airgap").mkdir(parents=True)
    for f in ("common.sh", "validate.sh"):
        shutil.copy(REPO / "scripts" / "airgap" / f, tmp_path / "scripts" / "airgap" / f)
    shutil.copy(next(REPO.glob("charts/qdrant-*.tgz")), tmp_path / "charts")

    for name in ("skopeo", "helm", "kubectl", "oc"):
        p = tmp_path / "bin" / name
        p.write_text(STUB_TOOL)
        p.chmod(0o755)
    return tmp_path


def _run(tree, extra_env=None):
    env = {
        "PATH": f"{tree / 'bin'}:/usr/bin:/bin",
        "AIRGAP_DRYRUN": "1",
        "AIRGAP_ENV": "/dev/null",  # ignore repo ./airgap.env
        "IMAGE_SHA": IMAGE_SHA,
        "INTERNAL_REGISTRY": "reg.internal:5000",
        "NAMESPACE": "mainframe-rag",
        "STORAGE_CLASS": "standard",
        "EMBED_MODEL": "ibm-granite/granite-embedding-125m-english",
        "DENSE_DIM": "768",
        "VLLM_BASE_URL": "http://vllm:8000/v1",
    }
    if extra_env:
        for k, v in extra_env.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run(
        ["sh", str(tree / "scripts" / "airgap" / "validate.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=tree,
        check=False,
    )


def test_validate_clean_exits_zero(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    assert "SUCCESS: Pre-flight validation passed (dry-run mode)." in r.stdout


def test_validate_missing_registry_fails(tree):
    r = _run(tree, {"INTERNAL_REGISTRY": None, "REGISTRY_INTERNAL": None})
    assert r.returncode != 0
    assert "INTERNAL_REGISTRY is unset" in r.stderr or "FAIL:" in r.stderr


def test_validate_nfs_storage_refused(tree):
    r = _run(tree, {"STORAGE_CLASS": "nfs-client"})
    assert r.returncode != 0
    assert "looks like NFS" in r.stderr


def test_validate_dense_dim_non_integer_refused(tree):
    r = _run(tree, {"DENSE_DIM": "not-a-number"})
    assert r.returncode != 0
    assert "DENSE_DIM must be a positive integer" in r.stderr


def test_validate_dense_dim_zero_refused(tree):
    r = _run(tree, {"DENSE_DIM": "0"})
    assert r.returncode != 0
    assert "DENSE_DIM must be greater than 0" in r.stderr


def test_validate_vllm_url_bad_scheme_refused(tree):
    r = _run(tree, {"VLLM_BASE_URL": "ftp://vllm:8000"})
    assert r.returncode != 0
    assert "VLLM_BASE_URL must begin with http:// or https://" in r.stderr


def test_validate_missing_skopeo_fails(tree):
    os.remove(tree / "bin" / "skopeo")
    for tool in ("sh", "dirname", "awk", "sed", "head", "ls"):
        src = shutil.which(tool)
        if src and not (tree / "bin" / tool).exists():
            (tree / "bin" / tool).symlink_to(src)
    r = _run(tree, {"PATH": str(tree / "bin")})
    assert r.returncode != 0
    assert "skopeo is required" in r.stderr
