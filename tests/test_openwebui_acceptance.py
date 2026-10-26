"""Open WebUI dev environment and acceptance lane (#1070): the pure rules and the script invariants.

checks.py holds every judgement the acceptance executor makes, so each rule is
exercised here with synthetic inputs, including the counterexample that must
fail. The shell invariants (name-constrained local CA, lock message, pins) run
the real functions with openssl; the cluster itself is exercised by the e2e
workflow's openwebui lane, not here.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "scripts" / "openwebui"


def _load(name):
    sys.path.insert(0, str(DIR))
    try:
        spec = importlib.util.spec_from_file_location(f"owui_{name}", DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(DIR))


checks = _load("checks")
client = _load("client")

GOOD_ANSWER = {"done": True, "content_chars": 120, "status_events": 2, "source_chips": 3, "source_documents": 3}
UID = 1000712345


def test_trial_directory_is_gone_and_pins_are_complete():
    assert not (ROOT / "scripts" / "ci" / "openwebui-trial").exists()
    pins = dict(line.split("=", 1) for line in (DIR / "pins.env").read_text().splitlines()
                if line and not line.startswith("#"))
    assert set(pins) == {"NODE_IMAGE", "OWUI_BASE", "DEX_PIN", "SKOPEO_IMAGE"}
    assert all("@sha256:" in v for v in pins.values())
    # The Containerfile base and the pin file name the same upstream digest.
    assert pins["OWUI_BASE"].split("@")[1] in (ROOT / "images" / "openwebui" / "Containerfile").read_text()


def test_dev_environment_reads_the_working_tree_not_git_refs():
    dev = (DIR / "dev.sh").read_text() + (DIR / "lib.sh").read_text()
    assert "CHART_REF" not in dev and "PIPE_REF" not in dev and "git archive" not in dev
    assert "integrations/openwebui/docs_pipe.py" in dev and "charts/mainframe-rag" in dev


def test_lock_conflict_is_reported_not_silent():
    dev = (DIR / "dev.sh").read_text()
    assert "exec flock" not in dev
    assert "flock -E 99" in dev and "is held by another job" in dev


# ------------------------------------------------------------------ identity and pod
def test_uid_must_be_the_namespace_uid_in_group_zero():
    assert checks.check_uid(f"{UID}\n0\n", UID, 1000).ok
    assert not checks.check_uid("1000\n0\n", UID, 1000).ok  # the image's own USER
    assert not checks.check_uid(f"{UID}\n1000\n", UID, 1000).ok  # not group 0
    assert not checks.check_uid("0\n0\n", 0, 1000).ok  # root, even when "expected"
    assert not checks.check_uid("garbage", UID, 1000).ok


def _pod(**over):
    container = {"name": "openwebui", "securityContext": {
        "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}}
    pod = {"spec": {"securityContext": {"runAsNonRoot": True, "runAsUser": UID,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [container]}}
    for path, value in over.items():
        node = pod
        *head, last = path.split(".")
        for part in head:
            node = node[int(part)] if part.isdigit() else node[part]
        node[last] = value
    return pod


def test_pod_security_restricted_shape():
    assert checks.check_pod_security(_pod()).ok
    assert not checks.check_pod_security(_pod(**{"spec.securityContext.runAsNonRoot": False})).ok
    assert not checks.check_pod_security(_pod(**{"spec.containers.0.securityContext.privileged": True})).ok
    assert not checks.check_pod_security(_pod(**{"spec.containers.0.securityContext.readOnlyRootFilesystem": False})).ok
    assert not checks.check_pod_security(_pod(**{"spec.containers.0.securityContext.capabilities": {"drop": []}})).ok
    assert not checks.check_pod_security({"spec": {"containers": []}}).ok


def test_models_groups_and_sso_only():
    assert checks.check_models({"a": [checks.DOCS_MODEL], "b": [checks.DOCS_MODEL]}).ok
    assert not checks.check_models({"a": [checks.DOCS_MODEL, "other"]}).ok
    assert not checks.check_models({"a": []}).ok and not checks.check_models({}).ok
    assert checks.check_groups({"a": ["internal-team"]}, {"a": "internal-team"}).ok
    assert not checks.check_groups({"a": []}, {"a": "internal-team"}).ok
    assert checks.check_refusals(403, False, 403).ok
    assert not checks.check_refusals(200, False, 403).ok  # signup open
    assert not checks.check_refusals(403, True, 403).ok  # login form on
    assert not checks.check_refusals(403, None, 403).ok  # unknown is not off


# ------------------------------------------------------------------ answers and paths
def test_answer_needs_source_events_with_documents():
    assert checks.check_answer(GOOD_ANSWER).ok
    assert not checks.check_answer({**GOOD_ANSWER, "source_chips": 0}).ok
    assert not checks.check_answer({**GOOD_ANSWER, "source_documents": 0}).ok
    assert not checks.check_answer({**GOOD_ANSWER, "done": False}).ok
    assert not checks.check_answer({"chat_create": 500}).ok
    assert checks.check_answer(GOOD_ANSWER, name="x").name == "x"


def test_blocked_paths_need_the_proxy_403_body():
    assert checks.classify_blocked(403, b"denied\n") == "proxy-403"
    assert checks.classify_blocked(403, b'{"detail":"Forbidden"}') == "HTTP 403"  # Open WebUI's own 403 is not the proxy
    assert checks.classify_blocked(404, b"denied\n") == "HTTP 404"
    assert checks.classify_blocked(400, b"<center>nginx</center>") == "proxy-400"
    assert checks.classify_blocked(400, b'{"detail":"bad"}') == "HTTP 400"  # Open WebUI's own 400 is not the proxy
    ok = {"/api/v1/files/": "proxy-403", "/api/v1/files%00": "proxy-400"}
    assert checks.check_blocked(ok, 200).ok
    assert not checks.check_blocked({**ok, "/x": "HTTP 200"}, 200).ok
    assert not checks.check_blocked(ok, 502).ok  # the allowed control path must still work
    assert not checks.check_blocked({}, 200).ok


def test_client_blocked_list_covers_the_proxy_acceptance_list():
    assert {"/api/v1/files/", "/api/v1/files%00", "/api/v1/%2566iles/", "/openai/chat/completions"} <= set(client.BLOCKED)
    assert len(set(client.BLOCKED)) == len(client.BLOCKED)


# ------------------------------------------------------------------ egress
DNS = ('[INFO] 10.244.0.9:41234 - 1 "A IN dex.localhost.owui.svc.cluster.local. udp 50 false 512" NXDOMAIN\n'
       '[INFO] 10.244.0.9:41235 - 2 "A IN rag-agent.owui.svc.cluster.local. udp 50 false 512" NOERROR\n'
       '[INFO] 10.244.0.7:41236 - 3 "A IN pypi.org. udp 50 false 512" NOERROR\n')


def test_dns_names_are_judged_for_the_pod_ips_only():
    lines = DNS.splitlines()
    names = checks.dns_names(lines, {"10.244.0.9"})
    assert names == {"dex.localhost.owui.svc.cluster.local", "rag-agent.owui.svc.cluster.local"}
    assert checks.check_dns(names, checks.count_queries(lines, {"10.244.0.9"})).ok
    other = checks.dns_names(lines, {"10.244.0.7"})
    assert checks.check_dns(other, 1).detail["stray_names"] == ["pypi.org"]
    assert not checks.check_dns(other, 1).ok
    # Zero queries means the log or the IPs were wrong: not a pass.
    assert not checks.check_dns(set(), 0).ok
    # The Ollama port probe of the 2026-10-24 trial must be caught.
    assert not checks.check_dns({"host.docker.internal"}, 1).ok


def test_egress_probe_exit_codes():
    assert checks.check_egress_probes(0, 0).ok
    assert not checks.check_egress_probes(0, 1).ok  # something outside the allow-list connected
    assert not checks.check_egress_probes(1, 0).ok  # the agent or the IdP is unreachable


# ------------------------------------------------------------------ DATA_DIR
CLEAN = {"data_dir_files": [["webui.db", 4096]], "vector_store_paths": [], "uploads": 0,
         "vector_like_tables": {}, "retrieval_content_rows": {"file": 0, "memory": 0}}


def test_data_dir_clean_and_each_counterexample():
    assert checks.check_data_dir(CLEAN).ok
    assert not checks.check_data_dir({**CLEAN, "vector_store_paths": ["vector_db"]}).ok
    assert not checks.check_data_dir({**CLEAN, "uploads": 1}).ok
    assert not checks.check_data_dir({**CLEAN, "vector_like_tables": {"document_chunk": 2}}).ok
    assert not checks.check_data_dir({**CLEAN, "retrieval_content_rows": {"file": 1}}).ok
    assert not checks.check_data_dir({}).ok  # an empty report (the inspector failed) is not a pass


def test_inspect_data_flags_a_vector_directory(tmp_path, monkeypatch):
    import sqlite3
    sqlite3.connect(tmp_path / "webui.db").execute("CREATE TABLE chat (id TEXT)").connection.close()
    (tmp_path / "vector_db").mkdir()
    (tmp_path / "vector_db" / "chroma.sqlite3").write_bytes(b"x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    proc = subprocess.run([sys.executable, "-I", str(DIR / "inspect_data.py"), "CANARY"],
                          capture_output=True, text=True, check=False)
    report = json.loads(proc.stdout)
    assert proc.returncode == 1 and any("vector_db" in p for p in report["vector_store_paths"])
    assert not checks.check_data_dir(report).ok


# ------------------------------------------------------------------ restart, C1, S2, S4
def test_admin_edit_must_take_effect_not_persist_and_revert():
    assert checks.check_admin_edit_reverts("8h", "4w", "8h", 0).ok
    assert not checks.check_admin_edit_reverts("8h", "8h", "8h", 0).ok  # the edit never took effect: proves nothing
    assert not checks.check_admin_edit_reverts("8h", "4w", "4w", 0).ok  # survived the restart
    assert not checks.check_admin_edit_reverts("8h", "4w", "8h", 1).ok  # reached the database


def _snap(**over):
    base = {"chat_ids": ["a", "b"], "function_sha256": "f" * 64, "models": [checks.DOCS_MODEL], "pod_uid": "u1"}
    return {**base, **over}


def test_persistence_needs_a_new_pod_and_nothing_lost():
    assert checks.check_persistence(_snap(), _snap(pod_uid="u2", chat_ids=["a", "b", "c"])).ok
    assert not checks.check_persistence(_snap(), _snap(pod_uid="u1")).ok  # no restart happened
    assert not checks.check_persistence(_snap(), _snap(pod_uid="u2", chat_ids=["a"])).ok
    assert not checks.check_persistence(_snap(), _snap(pod_uid="u2", function_sha256="0" * 64)).ok
    assert not checks.check_persistence(_snap(), _snap(pod_uid="u2", models=[])).ok
    assert checks.check_function_hash("a" * 64, "a" * 64).ok and not checks.check_function_hash(None, "a" * 64).ok


PURGE_OK = {"consecutive_failures": 0, "last_success": 1760000000, "last_counts": {"chats": 1}}


def _purge(**over):
    args = {"old_chat_present_after": False, "fresh_chat_present_after": True, "orphan_upload_present_after": False,
            "canary_files_after": [], "purge_status": PURGE_OK, "next_chat": GOOD_ANSWER}
    return checks.check_purge(**{**args, **over})


def test_purge_removes_aged_data_keeps_fresh_and_the_next_chat_works():
    assert _purge().ok
    assert not _purge(old_chat_present_after=True).ok
    assert not _purge(fresh_chat_present_after=False).ok  # deleted too much
    assert not _purge(orphan_upload_present_after=True).ok  # rows gone, bytes left
    assert not _purge(canary_files_after=["webui.db-wal"]).ok  # text still on the claim
    assert not _purge(purge_status={"consecutive_failures": 2, "last_success": 1}).ok
    assert not _purge(next_chat={**GOOD_ANSWER, "source_chips": 0}).ok  # cleanup broke the next operation
    assert checks.parse_purge_status("not json") == {} and checks.parse_purge_status("[1]") == {}


def test_log_scan_finds_every_canary_and_never_prints_it():
    logs = {"owui": "GET /health 200\n", "agent": "retrieval ok count=3\n"}
    canaries = {"q": "ZQ123abc", "pw": "s3cretvalue"}
    assert checks.scan_logs(logs, canaries).ok
    leaked = checks.scan_logs({**logs, "agent": "query=ZQ123abc"}, canaries)
    assert not leaked.ok and leaked.detail["leaks"] == {"agent": ["q"]}
    assert "ZQ123abc" not in json.dumps(leaked.as_json())
    assert not checks.scan_logs({}, canaries).ok  # nothing collected is not a pass
    assert not checks.scan_logs({"owui": ""}, canaries).ok


def test_console_and_overall():
    assert checks.check_console(200, "text/html; charset=utf-8", 200).ok
    assert not checks.check_console(404, "application/json", 200).ok  # ui disabled
    assert not checks.check_console(200, "text/html", 503).ok
    assert checks.overall([checks.Verdict("a", True)]) and not checks.overall([])
    assert not checks.overall([checks.Verdict("a", True), checks.Verdict("b", False)])


# ------------------------------------------------------------------ certificates (N2 of #1095)
@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is required")
def test_local_ca_is_name_constrained(tmp_path):
    script = f"""
set -eu
HERE={DIR}; REPO={ROOT}; STATE={tmp_path}; APP_NS=owui; OWUI_HOST=owui.localhost; DEX_HOST=dex.localhost
say() {{ :; }}; die() {{ echo "$*" >&2; exit 1; }}
. $HERE/lib.sh
pki
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj /CN=example.com \\
    -keyout $STATE/x.key -out $STATE/x.csr 2>/dev/null
printf 'subjectAltName=DNS:example.com\\nextendedKeyUsage=serverAuth\\n' > $STATE/x.ext
openssl x509 -req -in $STATE/x.csr -CA $STATE/pki/ca.crt -CAkey $STATE/pki/ca.key -CAcreateserial -days 5 \\
    -extfile $STATE/x.ext -out $STATE/x.crt 2>/dev/null
if openssl verify -CAfile $STATE/pki/ca.crt $STATE/x.crt >/dev/null 2>&1; then echo VERIFIED; else echo REFUSED; fi
"""
    proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "REFUSED"


# ------------------------------------------------------------------ test IdP config
def test_dex_config_uses_the_issuer_and_one_hash_per_user(tmp_path, capsys):
    (tmp_path / "secrets.json").write_text(json.dumps({"webui": "w", "client": "c"}))
    hashes = tmp_path / "hashes.txt"
    hashes.write_text("\n".join(f"$2y$10$h{i}" for i in range(len(client.USERS))))
    client.cmd_dex_config(str(tmp_path), str(hashes))
    text = capsys.readouterr().out
    assert f"issuer: {client.DEX_ISSUER}" in text
    assert text.count("staticPasswords") == 1 and text.count("userID:") == len(client.USERS)
    hashes.write_text("one")
    with pytest.raises(SystemExit):
        client.cmd_dex_config(str(tmp_path), str(hashes))
