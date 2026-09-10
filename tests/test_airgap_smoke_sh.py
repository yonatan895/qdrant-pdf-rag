"""scripts/airgap/smoke.sh fail-close and operability tests (issue #15).

Hermetic tests: tests dryrun preview, empty collection clean skip (exit code 3),
search success path, search failure path, and namespace validation.
"""

from pathlib import Path

import pytest

from tests.helpers_airgap import make_bin_tree, run_sh, write_stub

STUB_KC_TEMPLATE = """#!/bin/sh
# Check if this is the healthz probe, search query, or Jaeger trace poll.
# The trace arm comes first: the trace-poll script mentions v1.search, so a
# search arm would swallow it.
for arg in "$@"; do
    case "$arg" in
        *healthz*)
            exit {health_exit}
            ;;
        *traces*|*jaeger*)
            exit {trace_exit}
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
    *traces*|*jaeger*) exit {trace_exit} ;;
    *search*) exit {search_exit} ;;
esac
exit {default_exit}
"""


@pytest.fixture
def smoke_tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "smoke.sh"])
    return tmp_path


def _setup_stub(tmp_path, health_exit=0, search_exit=0, default_exit=0, trace_exit=0):
    script = STUB_KC_TEMPLATE.format(
        health_exit=health_exit, search_exit=search_exit, default_exit=default_exit,
        trace_exit=trace_exit,
    )
    for name in ("kubectl", "oc"):
        write_stub(tmp_path / "bin" / name, script)


def _run_smoke(tmp_path, *extra_env):
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "NAMESPACE": "test-ns",
        "QUERY": "IEA500I test",
    }
    for k, v in extra_env:
        env[k] = v
    return run_sh(tmp_path / "scripts" / "airgap" / "smoke.sh", env, tmp_path)


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
    r = run_sh(smoke_tree / "scripts" / "airgap" / "smoke.sh", env, smoke_tree)
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
    script = STUB_KC_TEMPLATE.format(health_exit=0, search_exit=0, default_exit=0, trace_exit=0)
    write_stub(smoke_tree / "bin" / "oc", script)
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
    script = STUB_KC_TEMPLATE.format(health_exit=0, search_exit=0, default_exit=0, trace_exit=0)
    p = write_stub(smoke_tree / "bin" / "custom-kc", script)
    r = _run_smoke(smoke_tree, ("KC", str(p)))
    assert r.returncode == 0, r.stderr
    assert "Smoke query returned hits" in r.stdout


def test_smoke_tracing_off_skips_trace_check(smoke_tree):
    _setup_stub(smoke_tree, health_exit=0, search_exit=0)
    r = _run_smoke(smoke_tree)
    assert r.returncode == 0, r.stderr
    assert "Tracing:       OFF (skipped — OTEL_EXPORTER_OTLP_ENDPOINT unset)" in r.stdout


def test_smoke_tracing_ok_when_span_landed(smoke_tree):
    _setup_stub(smoke_tree, health_exit=0, search_exit=0, trace_exit=0)
    r = _run_smoke(smoke_tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"))
    assert r.returncode == 0, r.stderr
    assert "Tracing:       OK (recent v1.search span in Jaeger)" in r.stdout


def test_smoke_tracing_fails_when_no_span_landed(smoke_tree):
    _setup_stub(smoke_tree, health_exit=0, search_exit=0, trace_exit=1)
    r = _run_smoke(smoke_tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"))
    assert r.returncode == 1
    assert "no v1.search span landed in Jaeger" in r.stderr


def test_smoke_tracing_skipped_on_empty_collection(smoke_tree):
    _setup_stub(smoke_tree, health_exit=0, search_exit=3)
    r = _run_smoke(smoke_tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"))
    assert r.returncode == 0, r.stderr
    assert "Tracing:       SKIPPED (nothing ingested — no request traced yet)" in r.stdout
