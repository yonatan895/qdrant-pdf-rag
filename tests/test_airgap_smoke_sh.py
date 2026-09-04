"""scripts/airgap/smoke.sh fail-close and operability tests (issue #15).

Hermetic tests: tests dryrun preview, empty collection clean skip (exit code 3),
search success path, search failure path, and namespace validation.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

STUB_KC_TEMPLATE = """#!/bin/sh
# Check if this is the healthz probe or search query
for arg in "$@"; do
    case "$arg" in
        *healthz*)
            exit {health_exit}
            ;;
        *search*)
            exit {search_exit}
            ;;
    esac
done
# If python script passed via stdin
content="$(cat)"
case "$content" in
    *healthz*) exit {health_exit} ;;
    *search*) exit {search_exit} ;;
esac
exit {default_exit}
"""


@pytest.fixture
def smoke_tree(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "scripts" / "airgap").mkdir(parents=True)
    for f in ("common.sh", "smoke.sh"):
        shutil.copy(REPO / "scripts" / "airgap" / f, tmp_path / "scripts" / "airgap" / f)
    return tmp_path


def _setup_stub(tmp_path, health_exit=0, search_exit=0, default_exit=0):
    script = STUB_KC_TEMPLATE.format(
        health_exit=health_exit, search_exit=search_exit, default_exit=default_exit
    )
    for name in ("kubectl", "oc"):
        p = tmp_path / "bin" / name
        p.write_text(script)
        p.chmod(0o755)


def _run_smoke(tmp_path, *extra_env):
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "NAMESPACE": "test-ns",
        "QUERY": "IEA500I test",
    }
    for k, v in extra_env:
        env[k] = v
    return subprocess.run(
        ["sh", str(tmp_path / "scripts" / "airgap" / "smoke.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )


def test_smoke_dryrun_succeeds(smoke_tree):
    r = _run_smoke(smoke_tree, ("AIRGAP_DRYRUN", "1"))
    assert r.returncode == 0, r.stderr
    assert "[dryrun]" in r.stdout
    assert "healthz" in r.stdout
    assert "IEA500I test" in r.stdout


def test_smoke_defaults_namespace_to_mainframe_rag(smoke_tree):
    env = {
        "PATH": "/usr/bin:/bin",
        "AIRGAP_DRYRUN": "1",
    }
    r = subprocess.run(
        ["sh", str(smoke_tree / "scripts" / "airgap" / "smoke.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=smoke_tree,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "-n mainframe-rag" in r.stdout


def _clean_sysbin(tmp_path):
    clean_bin = tmp_path / "clean_sysbin"
    if not clean_bin.exists():
        clean_bin.mkdir(exist_ok=True)
        for bin_dir in ("/bin", "/usr/bin"):
            p_dir = Path(bin_dir)
            if not p_dir.is_dir():
                continue
            for item in p_dir.iterdir():
                if item.name not in ("kubectl", "oc") and not (clean_bin / item.name).exists():
                    try:
                        (clean_bin / item.name).symlink_to(item)
                    except OSError:
                        pass
    return clean_bin


def test_smoke_uses_oc_when_kubectl_missing(smoke_tree):
    # Setup oc stub only
    script = STUB_KC_TEMPLATE.format(health_exit=0, search_exit=0, default_exit=0)
    p = smoke_tree / "bin" / "oc"
    p.write_text(script)
    p.chmod(0o755)
    sysbin = _clean_sysbin(smoke_tree)
    r = _run_smoke(smoke_tree, ("PATH", f"{smoke_tree / 'bin'}:{sysbin}"))
    assert r.returncode == 0, r.stderr
    assert "Smoke query returned hits" in r.stdout


def test_smoke_clean_skip_on_empty_collection(smoke_tree):
    # Exit 3 from python container indicates empty collection -> skip
    _setup_stub(smoke_tree, health_exit=0, search_exit=3)
    r = _run_smoke(smoke_tree)
    assert r.returncode == 0, r.stderr
    assert "SKIP: nothing ingested yet" in r.stdout
    assert "INFRASTRUCTURE READY (Corpus not yet ingested)" in r.stdout


def test_smoke_success_when_hits_found(smoke_tree):
    _setup_stub(smoke_tree, health_exit=0, search_exit=0)
    r = _run_smoke(smoke_tree)
    assert r.returncode == 0, r.stderr
    assert "Smoke query returned hits" in r.stdout


def test_smoke_fails_on_search_error(smoke_tree):
    _setup_stub(smoke_tree, health_exit=0, search_exit=1)
    r = _run_smoke(smoke_tree)
    assert r.returncode == 1
    assert "search request failed" in r.stderr


def test_smoke_fails_on_degraded_healthz(smoke_tree):
    _setup_stub(smoke_tree, health_exit=1, search_exit=0)
    r = _run_smoke(smoke_tree)
    assert r.returncode == 1
    assert "FAIL: /healthz probe did not report ok" in r.stderr


def test_smoke_kc_env_override_respected(smoke_tree):
    # Ensure KC env override is used directly
    script = STUB_KC_TEMPLATE.format(health_exit=0, search_exit=0, default_exit=0)
    p = smoke_tree / "bin" / "custom-kc"
    p.write_text(script)
    p.chmod(0o755)
    r = _run_smoke(smoke_tree, ("KC", str(p)))
    assert r.returncode == 0, r.stderr
    assert "Smoke query returned hits" in r.stdout
