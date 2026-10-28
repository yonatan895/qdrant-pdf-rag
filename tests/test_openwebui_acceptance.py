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
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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
    pins = dict(line.split("=", 1) for line in (DIR / "pins.txt").read_text().splitlines()
                if line and not line.startswith("#"))
    assert set(pins) == {"NODE_IMAGE", "DEX_PIN", "SKOPEO_IMAGE"}  # the Open WebUI digest lives in images.txt only
    assert all("@sha256:" in v for v in pins.values())


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


def test_blocked_paths_need_the_allow_list_403_or_the_sidecars_own_nul_refusal():
    assert checks.classify_blocked(403, b"denied\n", "/api/v1/files/") == "proxy-403"
    assert checks.classify_blocked(403, b'{"detail":"Forbidden"}', "/x") == "HTTP 403"  # Open WebUI's own 403
    assert checks.classify_blocked(404, b"denied\n", "/x") == "HTTP 404"
    nginx_400 = b"<html><center>nginx</center></html>"
    # A 400 from nginx is no refusal by the allow-list; only the NUL path may use it.
    assert checks.classify_blocked(400, nginx_400, "/api/v1/files/") == "HTTP 400"
    assert checks.classify_blocked(400, nginx_400, "/api/v1/files%00") == "sidecar-nul-400"
    assert checks.classify_blocked(400, b'{"detail":"bad"}', "/api/v1/files%00") == "HTTP 400"
    ok = {"/api/v1/files/": "proxy-403", "/api/v1/files%00": "sidecar-nul-400"}
    assert checks.check_blocked(ok, 200).ok
    assert not checks.check_blocked({**ok, "/x": "HTTP 200"}, 200).ok
    assert not checks.check_blocked({**ok, "/y": "HTTP 400"}, 200).ok
    assert not checks.check_blocked(ok, 502).ok  # the allowed control path must still work
    assert not checks.check_blocked({}, 200).ok


def test_client_blocked_list_covers_the_proxy_acceptance_list():
    assert {"/api/v1/files/", "/api/v1/files%00", "/api/v1/%2566iles/", "/openai/chat/completions"} <= set(client.BLOCKED)
    assert len(set(client.BLOCKED)) == len(client.BLOCKED)


# ------------------------------------------------------------------ egress
DNS = ('[INFO] 10.244.0.9:41234 - 1 "A IN dex.localhost.owui.svc.cluster.local. udp 50 false 512" NXDOMAIN\n'
       '[INFO] 10.244.0.9:41235 - 2 "A IN rag-agent.owui.svc.cluster.local. udp 50 false 512" NOERROR\n'
       '[INFO] 10.244.0.7:41236 - 3 "A IN pypi.org. udp 50 false 512" NOERROR\n')


SEARCH = ["rag-kind-1.svc.cluster.local", "svc.cluster.local", "cluster.local"]


def test_dns_names_are_judged_for_the_pod_ips_only_and_matched_exactly():
    lines = DNS.splitlines()
    names = checks.dns_names(lines, {"10.244.0.9"})
    assert names == {"dex.localhost.owui.svc.cluster.local", "rag-agent.owui.svc.cluster.local"}
    search = ["owui.svc.cluster.local", "svc.cluster.local", "cluster.local"]
    assert checks.check_dns(names, checks.count_queries(lines, {"10.244.0.9"}), search).ok
    other = checks.dns_names(lines, {"10.244.0.7"})
    assert checks.check_dns(other, 1, search).detail["stray_names"] == ["pypi.org"]
    assert not checks.check_dns(other, 1, search).ok
    # Zero queries or no resolver search line means the log or the pod was wrong: not a pass.
    assert not checks.check_dns(set(), 0, search).ok
    assert not checks.check_dns({"dex.localhost"}, 1, []).ok
    # Substring matches are not allowed names (F4 of the #1107 review).
    for stray in ("dex.localhost.attacker.example", "rag-agent.attacker.example", "xdex.localhost",
                  "host.docker.internal"):
        assert not checks.check_dns({stray}, 1, SEARCH).ok, stray
    assert checks.check_dns({"dex.localhost", "rag-agent", "rag-agent.rag-kind-1.svc.cluster.local"}, 1, SEARCH).ok
    assert checks.parse_search_domains("nameserver 10.96.0.10\nsearch A.svc.cluster.local. cluster.local\n") == [
        "a.svc.cluster.local", "cluster.local"]


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


def _np(name, selector, types=("Egress",)):
    return {"metadata": {"name": name}, "spec": {"podSelector": selector, "policyTypes": list(types)}}


def test_only_the_chart_policy_may_select_the_front_end_for_egress():
    labels = {"app": "rag-openwebui"}
    chart = _np("rag-openwebui", {"matchLabels": {"app": "rag-openwebui"}}, ("Ingress", "Egress"))
    lane = _np("rehearsal-default-deny-egress",
               {"matchExpressions": [{"key": "app", "operator": "NotIn", "values": ["rag-openwebui"]}]})
    probes = _np("kind-kubelet-probes", {"matchLabels": {"app": "rag-openwebui"}}, ("Ingress",))
    assert checks.check_policies_selecting(labels, [chart, lane, probes]).ok
    wide = _np("rehearsal-default-deny-egress", {})  # the pre-exclusion shape re-opens the pod
    bad = checks.check_policies_selecting(labels, [chart, wide])
    assert not bad.ok and bad.detail["egress_policies"] == ["rag-openwebui", "rehearsal-default-deny-egress"]
    assert not checks.check_policies_selecting(labels, [lane]).ok  # the chart policy must exist


def test_gated_deploy_never_pulls_and_uses_the_digest_pinned_image():
    lib = (DIR / "lib.sh").read_text()
    hashing = lib[lib.index("bcrypt_hashes() {"):]
    assert "--pull=never" in hashing and "owui_pinned" in hashing  # F2: no daemon pull behind the boundary
    for text in ((DIR / "client.py").read_text(), lib, (DIR / "acceptance.sh").read_text()):
        assert "seed" not in text.lower()  # the installer's real first install is what the lane proves
    assert "OWUI_SEED_FUNCTION" not in (ROOT / ".github" / "workflows" / "e2e.yml").read_text()


def test_a_user_in_neither_role_list_must_get_no_session():
    assert checks.check_revoked_user(False, False).ok
    assert not checks.check_revoked_user(True, True).ok
    assert not checks.check_revoked_user(False, True).ok  # a token cookie was issued
    other = client.USERS["other-user"][0]
    values = (DIR / "values.yaml").read_text()
    assert other not in values  # neither allowedRoles nor adminRoles contains it
    for line in values.splitlines():
        if "allowedRoles" in line or "adminRoles" in line:
            assert "*" not in line and "signed-in" not in line


def _owui_pinned(tmp_path, images_txt: str | None):
    """Run lib.sh's owui_pinned against a REPO whose images.txt is the given text (None: the real one)."""
    repo = ROOT
    if images_txt is not None:
        (tmp_path / "images.txt").write_text(images_txt)
        repo = tmp_path
    script = f"HERE={DIR}; REPO={repo}; . $HERE/lib.sh; owui_pinned"
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)


def test_the_lane_image_is_the_images_txt_row_and_fails_closed_while_pending(tmp_path):
    digest = "sha256:" + "ab" * 32
    row = f"ghcr.io/example/qdrant-pdf-rag-openwebui:v0.12.3-slim    {digest}\n"
    ok = _owui_pinned(tmp_path, "# c\nghcr.io/open-webui/open-webui:v0.12.3-slim  sha256:" + "cd" * 32 + "\n" + row)
    assert ok.returncode == 0 and ok.stdout.strip() == f"ghcr.io/example/qdrant-pdf-rag-openwebui@{digest}"
    pending = _owui_pinned(tmp_path, row.replace(digest, "sha256:PENDING"))
    assert pending.returncode != 0 and pending.stdout == "" and "OWUI_BUILD=1" in pending.stderr
    assert _owui_pinned(tmp_path, "# no row\n").returncode != 0

    # The real images.txt is the only digest source; a pending row fails closed.
    row = next(line.split() for line in (ROOT / "images.txt").read_text().splitlines()
               if "/qdrant-pdf-rag-openwebui:" in line and not line.startswith("#"))
    proc = _owui_pinned(None, None)
    if row[1] == "sha256:PENDING":
        assert proc.returncode != 0
    else:
        assert proc.stdout.strip() == f"{row[0].split(':')[0]}@{row[1]}"


# ------------------------------------------------------------------ operator-deploy lane (#1118 B3)
def test_first_pass_proves_no_pipe_yet_and_an_admin_for_step_four():
    assert checks.check_pipe_absent_before_key(None, "admin").ok
    assert not checks.check_pipe_absent_before_key("a" * 64, "admin").ok  # the deploy's notice would be false
    assert not checks.check_pipe_absent_before_key(None, "user").ok  # the IdP admin role did not arrive
    assert not checks.check_pipe_absent_before_key(None, None).ok  # the admin could not sign in


def test_rollback_off_needs_every_object_gone_the_same_claim_and_the_console():
    assert checks.check_rollback_recorded(["a"], "pvc-1").ok
    assert not checks.check_rollback_recorded([], "pvc-1").ok  # nothing to lose: the later check could not fail
    assert not checks.check_rollback_recorded(["a"], "").ok
    assert checks.check_rollback_off([], "pvc-1", "pvc-1", 0, True).ok
    assert not checks.check_rollback_off(["route.route.openshift.io/rag-openwebui"], "pvc-1", "pvc-1", 0, True).ok
    assert not checks.check_rollback_off([], "pvc-1", "pvc-2", 0, True).ok  # recreated under the same name
    assert not checks.check_rollback_off([], "pvc-1", "", 0, True).ok  # deleted
    assert not checks.check_rollback_off([], "", "", 0, True).ok
    assert not checks.check_rollback_off([], "pvc-1", "pvc-1", 1, True).ok  # a pod still runs
    assert not checks.check_rollback_off([], "pvc-1", "pvc-1", 0, False).ok  # the console broke


def test_rollback_on_needs_the_kept_claim_its_chats_the_grant_the_pipe_and_a_next_chat():
    sha = "a" * 64
    before = {"chat_ids": ["a", "b"], "claim_uid": "pvc-1"}
    after = {"chat_ids": ["a", "b", "c"], "claim_uid": "pvc-1", "function_sha256": sha, "models": [checks.DOCS_MODEL]}
    assert checks.check_rollback_on(before, after, sha, True).ok
    for bad in ({"chat_ids": ["a"]}, {"claim_uid": "pvc-2"}, {"function_sha256": None},
                {"function_sha256": "b" * 64}, {"models": []}):
        assert not checks.check_rollback_on(before, {**after, **bad}, sha, True).ok, bad
    assert not checks.check_rollback_on(before, after, sha, False).ok
    assert not checks.check_rollback_on({**before, "chat_ids": []}, after, sha, True).ok


def test_rollback_off_list_is_every_chart_open_webui_object_but_the_kept_claim():
    rendered = set()
    for path in (ROOT / "charts" / "mainframe-rag" / "templates").glob("openwebui-*.yaml"):
        text = path.read_text()
        kind = re.search(r"^kind: (\w+)$", text, re.MULTILINE).group(1)
        name = re.search(r"^  name: (\S+)$", text, re.MULTILINE).group(1)
        rendered.add((kind, name))
    plural = {"Deployment": "deployment.apps", "Service": "service", "Route": "route.route.openshift.io",
              "NetworkPolicy": "networkpolicy.networking.k8s.io", "ConfigMap": "configmap",
              "PersistentVolumeClaim": "persistentvolumeclaim"}
    expected = {f"{plural[k]}/{n}" for k, n in rendered} - {"persistentvolumeclaim/rag-openwebui-data"}
    assert ("PersistentVolumeClaim", "rag-openwebui-data") in rendered
    assert set(checks.OPENWEBUI_OBJECTS) == expected


def _platform(ns="ns1"):
    text = (DIR / "kind-platform.yaml").read_text().replace("@OWUI_UID@", str(UID)).replace("@APP_NS@", ns)
    return [d for d in yaml.safe_load_all(text) if d]


def test_platform_stand_ins_are_the_route_api_and_the_uid_assignment_for_the_front_end_only():
    docs = {d["kind"]: d for d in _platform()}
    assert sorted(docs) == ["CustomResourceDefinition", "MutatingAdmissionPolicy", "MutatingAdmissionPolicyBinding"]
    crd = docs["CustomResourceDefinition"]["spec"]
    assert docs["CustomResourceDefinition"]["metadata"]["name"] == "routes.route.openshift.io"
    assert crd["group"] == "route.openshift.io" and crd["names"]["kind"] == "Route" and crd["scope"] == "Namespaced"
    assert [(v["name"], v["served"], v["storage"]) for v in crd["versions"]] == [("v1", True, True)]
    tls = crd["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["tls"]["properties"]
    assert "reencrypt" in tls["termination"]["enum"]
    policy = docs["MutatingAdmissionPolicy"]["spec"]
    assert policy["matchConstraints"]["resourceRules"] == [
        {"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE"], "resources": ["pods"]}]
    assert [c["expression"] for c in policy["matchConditions"]] == [(
        "has(object.metadata.labels) && 'app' in object.metadata.labels"
        " && object.metadata.labels['app'] == 'rag-openwebui'")]
    expr = policy["mutations"][0]["applyConfiguration"]["expression"]
    assert f"runAsUser: {UID}" in expr and "runAsGroup: 0" in expr and f"fsGroup: {UID}" in expr
    assert UID >= checks.UID_RANGE_START
    binding = docs["MutatingAdmissionPolicyBinding"]["spec"]
    assert binding["policyName"] == docs["MutatingAdmissionPolicy"]["metadata"]["name"]
    assert binding["matchResources"]["namespaceSelector"] == {"matchLabels": {"kubernetes.io/metadata.name": "ns1"}}


def test_platform_stand_ins_never_reach_a_product_path():
    product = [*(ROOT / "charts").rglob("*"), *(ROOT / "scripts" / "airgap").rglob("*"), ROOT / "images.txt",
               ROOT / "airgap.env.example"]
    for path in product:
        if path.is_file() and path.suffix not in (".tgz",):
            text = path.read_text(errors="replace")
            assert "kind-platform" not in text and "rehearsal-stand-in" not in text, path


def _shell(script, tmp_path, env=None):
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False,
                          env={**os.environ, **(env or {})}, cwd=tmp_path)


HTPASSWD_STUB = """#!/bin/sh
# stub: argv is recorded, the password must arrive on stdin
echo "$*" >> "$ARGV_LOG"
pw=$(cat)
[ -n "$pw" ] || exit 9
[ "${STUB_EMPTY:-0}" = 1 ] && exit 0
printf '%s:$2y$10$%s\\n' "$3" "$(printf %s "$pw" | sha256sum | cut -c1-53)"
"""


def test_operator_hasher_reads_each_password_on_stdin_and_gives_dex_one_2b_hash_per_user(tmp_path):
    stub = tmp_path / "bin" / "htpasswd"
    stub.parent.mkdir()
    stub.write_text(HTPASSWD_STUB)
    stub.chmod(0o755)
    state = tmp_path / "state"
    script = f"""
set -eu
HERE={DIR}; REPO={ROOT}; STATE={state}; PYH="python3 -I {DIR}/client.py"
die() {{ echo "$*" >&2; exit 1; }}
. $HERE/lib.sh
$PYH secrets "$STATE" >/dev/null
OWUI_HASHER=htpasswd bcrypt_hashes
"""
    env = {"PATH": f"{stub.parent}:{os.environ['PATH']}", "ARGV_LOG": str(tmp_path / "argv.log")}
    proc = _shell(script, tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    hashes = (state / "hashes.txt").read_text().split()
    assert len(hashes) == len(client.USERS) and all(h.startswith("$2b$10$") for h in hashes)
    assert (state / "dex-config.yaml").read_text().count("$2b$10$") == len(client.USERS)
    argv = (tmp_path / "argv.log").read_text()
    for user in client.USERS:
        assert (state / f"{user}.password").read_text().strip() not in argv  # never on argv
    assert argv.splitlines() == [f"-niBC 10 {u}" for u in client.USERS]
    env["STUB_EMPTY"] = "1"
    bad = _shell(script, tmp_path, env)
    assert bad.returncode != 0 and "one bcrypt hash per user" in bad.stderr


def _acceptance(tmp_path, *args, env=None):
    base = {"KIND_NAME": "b3-none", "APP_NS": "ns1", "OWUI_STATE": str(tmp_path / "state"),
            "KUBECTL": str(tmp_path / "no-kubectl")}
    return subprocess.run(["sh", str(DIR / "acceptance.sh"), *args], capture_output=True, text=True, check=False,
                          env={**os.environ, **base, **(env or {})})


def test_site_env_is_a_complete_site_configuration_the_product_validator_accepts(tmp_path):
    proc = _acceptance(tmp_path, "operator", "site-env")
    assert proc.returncode == 0, proc.stderr
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    spec = importlib.util.spec_from_file_location("b3_model_config", ROOT / "scripts" / "airgap" / "model_config.py")
    model_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(model_config)
    config = model_config.openwebui_config(values)
    assert config["enabled"] is True and config["host"] == "owui.localhost"
    # The chart's redirect (https://HOST/oauth/oidc/callback, no port) is the one the test IdP allows.
    assert f"https://{config['host']}" == client.OWUI_URL
    assert config["oidc"]["providerUrl"] == client.DEX_ISSUER + "/.well-known/openid-configuration"
    roles = set(config["oidc"]["allowedRoles"].split(",")) | set(config["oidc"]["adminRoles"].split(","))
    assert client.USERS["other-user"][0] not in roles  # the revocation check keeps its counterexample
    assert config["oidc"]["adminRoles"] == client.USERS[client.ADMIN][0]
    example = (ROOT / "airgap.env.example").read_text()
    assert all(f"# {key}=" in example for key in values), "every key is a documented operator key"
    assert values["OPENWEBUI_IDP_CA_FILE"] == str(tmp_path / "state" / "pki" / "ca.crt")


def test_operator_mode_refuses_a_port_the_chart_cannot_render_and_a_missing_workspace(tmp_path):
    proc = _acceptance(tmp_path, "operator", "prepare", env={"OWUI_WORKSPACE": str(ROOT), "OWUI_PORT": "8443"})
    assert proc.returncode != 0 and "OWUI_PORT=443" in proc.stderr
    proc = _acceptance(tmp_path, "operator", "prepare", env={"OWUI_WORKSPACE": str(tmp_path)})
    assert proc.returncode != 0 and "no chart Pipe" in proc.stderr
    assert _acceptance(tmp_path, "operator", "bogus").returncode == 2


def _route_json(tmp_path, *sets):
    proc = subprocess.run(["helm", "template", "rag", str(ROOT / "charts" / "mainframe-rag"), "--namespace", "ns1",
                           "-f", str(ROOT / "charts" / "mainframe-rag" / "ci" / "openwebui-site-values.yaml"),
                           "--set", "openwebui.host=owui.localhost", *sets,
                           "--show-only", "templates/openwebui-route.yaml"],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    path = tmp_path / "route.json"
    path.write_text(json.dumps(yaml.safe_load(proc.stdout)))
    return path


def _router(tmp_path, route):
    out = tmp_path / "ca.crt"
    script = f'HERE={DIR}; . {DIR}/lib.sh; route_for_router {route} owui.localhost {out}'
    return _shell(script, tmp_path), out


def test_router_stand_in_takes_host_ca_and_timeout_from_the_charts_route(tmp_path):
    proc, out = _router(tmp_path, _route_json(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "300"
    assert "FAKE-TEST-CA-NOT-A-CERTIFICATE" in out.read_text()
    proc, _ = _router(tmp_path, _route_json(tmp_path, "--set", "route.timeoutSeconds=120"))
    assert proc.stdout.strip() == "120"


def test_router_stand_in_refuses_a_route_it_would_serve_differently(tmp_path):
    good = json.loads(_route_json(tmp_path).read_text())
    for mutate in (lambda r: r["spec"].__setitem__("host", "other.localhost"),
                   lambda r: r["spec"]["tls"].__setitem__("termination", "edge"),
                   lambda r: r["spec"]["to"].__setitem__("name", "rag-agent"),
                   lambda r: r["spec"]["tls"].pop("destinationCACertificate"),
                   lambda r: r["metadata"]["annotations"].clear()):
        route = json.loads(json.dumps(good))
        mutate(route)
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(route))
        (tmp_path / "ca.crt").unlink(missing_ok=True)
        proc, out = _router(tmp_path, path)
        assert proc.returncode != 0 and "cannot serve Route rag-openwebui" in proc.stderr
        assert not out.exists()


def _kind_job():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "e2e.yml").read_text())["jobs"]["kind-live-rehearsal"]


GATED = "sh rehearsal-infra/scripts/ci/egress_gate.sh run bash -e {0}"
OPERATOR_IF = "matrix.lane == 'openwebui-operator'"


def test_operator_lane_runs_the_site_commands_behind_the_gate_and_never_applies_chart_objects():
    job = _kind_job()
    assert "openwebui-operator" in job["strategy"]["matrix"]["lane"]
    # Empty is unset for common.sh: the lane's airgap.env decides; every other lane opts out.
    assert job["env"]["OPENWEBUI_ENABLED"] == "${{ matrix.lane != 'openwebui-operator' && 'false' || '' }}"
    steps = job["steps"]
    names = [s.get("name", "") for s in steps]
    boundary = names.index("Close the network boundary (block public egress, prove it)")
    own = [(i, s) for i, s in enumerate(steps) if s.get("if") == OPERATOR_IF]
    text = "\n".join(s["run"] for _, s in own)
    assert "acceptance.sh deploy" not in text and "helm " not in text and "kubectl apply" not in text
    assert text.count("sh scripts/tools/run-task.sh airgap:pipeline") == 1
    assert text.count("sh scripts/tools/run-task.sh airgap:deploy") == 3
    order = []
    for i, step in own:
        order += re.findall(r"(?:acceptance\.sh operator|\$OWUI) ([a-z-]+)", step["run"])
        if "operator images" in step["run"]:
            assert i < boundary and "shell" not in step
        else:
            assert i > boundary and step["shell"] == GATED, step["name"]
    assert order == ["images", "site-env", "prepare", "admin", "run", "record", "off", "on"]
    pipeline = next(s for _, s in own if "airgap:pipeline" in s["run"])
    assert names.index(pipeline["name"]) > names.index(next(s["name"] for _, s in own if "operator prepare" in s["run"]))
    # The chart-objects lane's deploy never runs in the operator lane, nor does the --skip-load pipeline.
    for step in steps:
        if "acceptance.sh deploy" in step.get("run", ""):
            assert step.get("if") == "matrix.lane == 'openwebui'", step["name"]
    skip_load = next(s for s in steps if s.get("name", "").startswith("airgap-pipeline LIVE"))
    assert "pipeline.sh --skip-load" in skip_load["run"] and skip_load["if"] == "matrix.lane != 'openwebui-operator'"


def test_only_the_operator_lane_cluster_serves_the_alpha_admission_api(tmp_path):
    job = _kind_job()
    step = next(s for s in job["steps"] if s.get("name") == "Start authenticated TLS registry + Kind")
    code = step["run"].split("<<'PY_KIND'\n", 1)[1].split("\nPY_KIND", 1)[0]
    for lane in job["strategy"]["matrix"]["lane"]:
        out = tmp_path / f"{lane}.json"
        proc = subprocess.run([sys.executable, "-", str(out), "/trust", lane], input=code, text=True,
                              capture_output=True, check=False)
        assert proc.returncode == 0, proc.stderr
        config = json.loads(out.read_text())
        if lane == "openwebui-operator":
            assert config["featureGates"] == {"MutatingAdmissionPolicy": True}
            assert config["runtimeConfig"] == {"admissionregistration.k8s.io/v1alpha1": "true"}
            assert [n["role"] for n in config["nodes"]] == ["control-plane"]  # acceptance.sh's NODE
        else:
            assert "featureGates" not in config and "runtimeConfig" not in config, lane
