"""scripts/airgap/smoke.sh fail-close and operability tests (issue #15).

Hermetic tests: tests dryrun preview, empty collection clean skip (exit code 3),
search success path, search failure path, and namespace validation.
"""

import contextlib
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
                    with contextlib.suppress(OSError):
                        (clean_bin / item.name).symlink_to(item)
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


def test_smoke_tracing_off_sentinel_skips_trace_check(smoke_tree):
    _setup_stub(smoke_tree, health_exit=0, search_exit=0)
    r = _run_smoke(smoke_tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "off"))
    assert r.returncode == 0, r.stderr
    assert "Tracing:       OFF (disabled)" in r.stdout


def test_smoke_tracing_on_by_default_runs_check(smoke_tree):
    # Unset endpoint now resolves to the in-cluster Jaeger: the trace check
    # must run (and fail closed when no span landed).
    _setup_stub(smoke_tree, health_exit=0, search_exit=0, trace_exit=1)
    r = _run_smoke(smoke_tree)
    assert r.returncode == 1
    assert "no v1.search span landed in Jaeger" in r.stderr


def test_smoke_tracing_ok_when_span_landed(smoke_tree):
    _setup_stub(smoke_tree, health_exit=0, search_exit=0, trace_exit=0)
    r = _run_smoke(smoke_tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"))
    assert r.returncode == 0, r.stderr
    assert "Tracing:       OK (recent v1.search span in Jaeger)" in r.stdout


def test_smoke_tracing_custom_query_url_reaches_pod(smoke_tree):
    # The Jaeger query API URL is operator-overridable: prove the custom
    # value is what the trace poll passes to the pod.
    url_log = smoke_tree / "trace-url.log"
    script = f"""#!/bin/sh
for arg in "$@"; do
    case "$arg" in
        *obs-ui*) echo "$arg" > {url_log}; exit 0 ;;
    esac
done
cat >/dev/null
exit 0
"""
    for name in ("kubectl", "oc"):
        write_stub(smoke_tree / "bin" / name, script)
    r = _run_smoke(smoke_tree, ("JAEGER_QUERY_URL", "http://obs-ui:16686"))
    assert r.returncode == 0, r.stderr
    assert "obs-ui:16686" in url_log.read_text()


def test_smoke_tracing_skipped_on_empty_collection(smoke_tree):
    _setup_stub(smoke_tree, health_exit=0, search_exit=3)
    r = _run_smoke(smoke_tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"))
    assert r.returncode == 0, r.stderr
    assert "Tracing:       SKIPPED (nothing ingested — no request traced yet)" in r.stdout


def test_smoke_external_collector_never_claims_trace_arrival(smoke_tree):
    # Issue #568: a disabled bundled Jaeger skips the query; the report must
    # say arrival is NOT VERIFIED and must not claim a collector was checked.
    _setup_stub(smoke_tree, health_exit=0, search_exit=0, trace_exit=1)
    r = _run_smoke(smoke_tree, ("JAEGER_ENABLED", "false"),
                   ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.platform:4318"))
    assert r.returncode == 0, r.stderr
    assert "trace arrival NOT VERIFIED" in r.stdout
    assert "Tracing:       OK" not in r.stdout
    assert "external collector —" not in r.stdout




# --- backend-neutral trace arrival (issue #956) ------------------------------
# The stub kubectl runs the real scripts/trace_arrival.py (what the agent image
# ships) against a loopback fake, so query, auth and exit mapping are real.
import base64
import sys

from tests.helpers_airgap import REPO, FakeHttpServer

OO_USER, OO_PASSWORD = "ops@example.invalid", "p:ss w0rd/+="
OO_BASIC = "Basic " + base64.b64encode(f"{OO_USER}:{OO_PASSWORD}".encode()).decode()


def _stub_running_trace_module(tmp_path):
    script = f"""#!/bin/sh
for arg in "$@"; do
    case "$arg" in
        /app/scripts/trace_arrival.py)
            while [ "$1" != "$arg" ]; do shift; done
            shift
            exec {sys.executable} {REPO / 'scripts' / 'trace_arrival.py'} "$@"
            ;;
    esac
done
while read -r _line; do :; done
exit 0
"""
    for name in ("kubectl", "oc"):
        write_stub(tmp_path / "bin" / name, script)


def _oo_server(present=True):
    def respond(request):
        if request["headers"].get("authorization") != OO_BASIC:
            return 401, {}
        return 200, {"hits": [{"trace_id": "t"}] if present else []}
    return FakeHttpServer(respond)


def _oo_env(server, **extra):
    env = {"TRACE_BACKEND": "openobserve", "TRACE_QUERY_URL": server.url, "TRACE_QUERY_USER": OO_USER,
           "TRACE_QUERY_PASSWORD": OO_PASSWORD, "TRACE_TIMEOUT": "1", **extra}
    return list(env.items())


def test_smoke_openobserve_arrival_found(smoke_tree):
    _stub_running_trace_module(smoke_tree)
    server = _oo_server()
    try:
        r = _run_smoke(smoke_tree, *_oo_env(server))
    finally:
        server.close()
    assert r.returncode == 0, r.stderr
    assert "Tracing:       OK (recent v1.search span in OpenObserve)" in r.stdout
    request = server.requests[0]
    assert (request["method"], request["path"], request["query"]) == (
        "POST", "/api/default/_search", {"type": ["traces"]})
    assert request["body"]["query"]["sql"] == (
        "SELECT trace_id FROM \"default\" WHERE operation_name = 'v1.search'"
        " AND service_name = 'mainframe-rag-agent'")
    assert OO_PASSWORD not in r.stdout + r.stderr


def test_smoke_openobserve_arrival_not_found_fails_closed(smoke_tree):
    _stub_running_trace_module(smoke_tree)
    server = _oo_server(present=False)
    try:
        r = _run_smoke(smoke_tree, *_oo_env(server))
    finally:
        server.close()
    assert r.returncode == 1
    assert "no v1.search span landed in OpenObserve within 1s" in r.stderr
    assert "Tracing:       OK" not in r.stdout


def test_smoke_openobserve_auth_failure_is_fixed_message(smoke_tree):
    _stub_running_trace_module(smoke_tree)
    server = _oo_server()
    try:
        r = _run_smoke(smoke_tree, *_oo_env(server, TRACE_QUERY_PASSWORD="wrong-secret"))
    finally:
        server.close()
    assert r.returncode == 1
    assert "trace backend refused the credentials" in r.stderr
    assert "wrong-secret" not in r.stdout + r.stderr and OO_PASSWORD not in r.stdout + r.stderr
    assert len(server.requests) == 1  # no retry loop on a refusal


@pytest.mark.parametrize("env, message", [
    ([("TRACE_BACKEND", "openobserve")], "TRACE_BACKEND=openobserve requires TRACE_QUERY_URL"),
    ([("TRACE_BACKEND", "openobserve"), ("TRACE_QUERY_URL", "http://o2:5080")],
     "TRACE_BACKEND=openobserve requires TRACE_QUERY_USER and TRACE_QUERY_PASSWORD"),
    ([("TRACE_BACKEND", "tempo")], "TRACE_BACKEND must be jaeger or openobserve"),
])
def test_smoke_trace_backend_misconfiguration_fails_closed(smoke_tree, env, message):
    _setup_stub(smoke_tree)
    r = _run_smoke(smoke_tree, *env)
    assert r.returncode == 1
    assert f"FAIL: {message}" in r.stderr


def test_smoke_default_backend_is_jaeger_with_todays_query(smoke_tree):
    _stub_running_trace_module(smoke_tree)

    def respond(request):
        if request["path"] == "/api/services":
            return 200, {"data": ["mainframe-rag-agent"]}
        return 200, {"data": [{"spans": [{}, {}, {}]}]}
    server = FakeHttpServer(respond)
    try:
        r = _run_smoke(smoke_tree, ("JAEGER_QUERY_URL", server.url))
    finally:
        server.close()
    assert r.returncode == 0, r.stderr
    assert "traced service=mainframe-rag-agent spans=3" in r.stdout
    assert "Tracing:       OK (recent v1.search span in Jaeger)" in r.stdout
    assert [(q["path"], q["query"]) for q in server.requests] == [
        ("/api/services", {}),
        ("/api/traces", {"service": ["mainframe-rag-agent"], "operation": ["v1.search"]})]


def test_smoke_default_dryrun_text_unchanged(smoke_tree):
    r = _run_smoke(smoke_tree, ("AIRGAP_DRYRUN", "1"))
    assert "python3 - '... poll http://jaeger:16686 for a v1.search trace ...'" in r.stdout
