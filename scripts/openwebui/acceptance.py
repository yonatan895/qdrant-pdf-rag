"""Open WebUI acceptance executor (#1070, #1073): runs the checks against a live Kind deployment.

    python3 -I acceptance.py STATE_DIR [PHASE]   (via acceptance.sh run, or acceptance.sh operator ...)

PHASE is `checks` (default: the full check list), `team-tools` (after acceptance.sh team-tools,
#1077: the Splunk gateway and ADR-0010 T4) or, in the operator-deploy lane (#1118 B3),
`first-pass` (before the admin API key), `record`, `rollback-off` and `rollback-on` (K1), and
`paced`: one question on the three Open WebUI paths at once against the slow-model stand-in
(scripts/mock_vllm.py MOCK_PACED_*; CI stand-in models only).

Everything here is collection: kubectl calls into the Open WebUI pod, HTTP
calls through the router stand-in as the signed-in test users, and reads of
the pod's claim. Every judgement is a pure function in checks.py (unit-tested).
Output is one JSON document of check names, states, counts and ids: never a
password, token, key, question or answer. Exit 0 only when every check passed.

Environment (set by acceptance.sh): KUBECTL, KIND_NAME, APP_NS, OWUI_URL, DEX_ISSUER.
The agent behind the Rosetta Pipe is the one the e2e Kind lane already runs
(Service rag-agent, test gateway with mock models); nothing here starts a model.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import checks
import client

HERE = os.path.dirname(os.path.abspath(__file__))
KUBECTL = os.environ.get("KUBECTL", "kubectl")
KIND_NAME = os.environ["KIND_NAME"]
NS = os.environ["APP_NS"]
DATA = "/app/backend/data"
QUESTION = "Explain the IEA500I operator message."  # the lane's corpus answers this (application_contracts.py)
RETENTION_DAYS = 30  # values.yaml openwebui.retentionDays
IMAGE_UID = 1000  # the derived image's USER; the pod must not run as it
EDIT_KEY = "JWT_EXPIRES_IN"
EDIT_VALUE = "4w"
PROBE = os.path.join(HERE, "..", "ci", "egress_probe.py")
INSPECT = os.path.join(HERE, "inspect_data.py")
# Team tools (#1077): acceptance.sh team-tools renders these (deploy/gateways/splunk-gateway.example.json).
TOOL_CALL = os.path.join(HERE, "team_tool_call.py")
TEAM_GROUP = "internal-team"
TEAM_REASONING_MODEL = "mock-reasoning"
TEAM_TEMPLATES = ["errors_by_host", "job_runs"]
# ADR-0010 T4: registration comes from the environment, so these stay behind the proxy's 403.
TEAM_PROXY_CLOSED = ("/api/v1/configs/tool_servers", "/api/v1/configs/tool_servers/verify", "/openai/config/update",
                     "/api/v1/tasks/config/update", "/api/v1/tools/id/server:mcp:splunk/specs",
                     "/api/v1/tools/id/server:mcp:splunk/mcp/app/rpc", "/api/v1/tools/create")
SPECS_CODE = (
    "import json, sys, urllib.error, urllib.request\n"
    "token = sys.stdin.read().strip()\n"
    "req = urllib.request.Request('http://127.0.0.1:8080/api/v1/tools/id/server:mcp:splunk/specs',\n"
    "                             headers={'Authorization': 'Bearer ' + token})\n"
    "try:\n"
    "    with urllib.request.urlopen(req, timeout=60) as r:\n"
    "        status, body = r.status, json.load(r)\n"
    "except urllib.error.HTTPError as e:\n"
    "    status, body = e.code, None\n"
    "specs = body.get('specs') if isinstance(body, dict) else None\n"
    "print(json.dumps({'status': status, 'names': sorted(s.get('name') for s in specs or [])}))\n")


# ------------------------------------------------------------------ cluster access
def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def kc(*args: str, ns: str | None = NS, stdin: bytes | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    cmd = [KUBECTL, "--context", f"kind-{KIND_NAME}"]
    if ns:
        cmd += ["-n", ns]
    return subprocess.run(cmd + list(args), input=stdin, capture_output=True, timeout=timeout, check=False)


def kc_out(*args: str, **kw) -> str:
    proc = kc(*args, **kw)
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl {args[0]} failed (rc {proc.returncode})")
    return proc.stdout.decode("utf-8", "replace")


def pod_info() -> dict:
    doc = json.loads(kc_out("get", "pod", "-l", "app=rag-openwebui", "-o", "json"))
    pods = [p for p in doc["items"] if not p["metadata"].get("deletionTimestamp")]
    if len(pods) != 1:
        raise RuntimeError(f"expected one rag-openwebui pod, found {len(pods)}")
    pod = pods[0]
    return {"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
            "ip": pod["status"].get("podIP", ""), "spec": pod}


def owui_exec(container: str, argv: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    pod = pod_info()["name"]
    flags = ["-i"] if stdin is not None else []
    return kc("exec", *flags, pod, "-c", container, "--", *argv, stdin=stdin)


def py_exec(container: str, code: str, *args: str) -> subprocess.CompletedProcess:
    return owui_exec(container, ["python3", "-c", code, *args])


def agent_exec(code: str) -> subprocess.CompletedProcess:
    return kc("exec", "-i", "deploy/rag-agent", "--", "python3", "-", stdin=code.encode())


def claim_uid() -> str:
    """The uid of the kept claim, so a recreated claim of the same name never passes as kept."""
    proc = kc("get", "pvc", "rag-openwebui-data", "--ignore-not-found", "-o", "jsonpath={.metadata.uid}")
    return proc.stdout.decode().strip() if proc.returncode == 0 else ""


def mock_health() -> dict:
    """The model stand-in's own /healthz, read inside its pod: the settings it actually runs with."""
    code = ("import json, urllib.request as u\n"
            "print(json.dumps(json.load(u.urlopen('http://127.0.0.1:8000/healthz', timeout=10))))\n")
    proc = kc("exec", "deploy/vllm-mock", "--", "python3", "-c", code)
    try:
        state = json.loads(proc.stdout.decode())
    except ValueError:
        return {}
    return state if proc.returncode == 0 and isinstance(state, dict) else {}


def agent_deadline_s() -> float:
    """REQUEST_DEADLINE_S of the deployed agent container (0: none, as Settings defaults)."""
    doc = json.loads(kc_out("get", "deploy", "rag-agent", "-o", "json"))
    for container in doc["spec"]["template"]["spec"]["containers"]:
        for env in container.get("env") or []:
            if env.get("name") == "REQUEST_DEADLINE_S":
                try:
                    return float(env.get("value") or 0)
                except ValueError:
                    return 0.0
    return 0.0


def logs_of(*target: str, ns: str | None = NS) -> str:
    proc = kc("logs", *target, "--all-containers", "--tail=-1", ns=ns)
    return proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")


# ------------------------------------------------------------------ collection
class Acceptance:
    def __init__(self, state: str) -> None:
        self.state = state
        self.s = client.Session(state)
        self.verdicts: list[checks.Verdict] = []
        self.canary1 = "ZQ" + secrets.token_hex(6)
        self.canary2 = "ZQ" + secrets.token_hex(6)
        self.logs: dict[str, str] = {}
        self.dns_names: set[str] = set()
        self.dns_queries = 0

    def add(self, verdict: checks.Verdict) -> None:
        self.verdicts.append(verdict)
        print(json.dumps(verdict.as_json(), sort_keys=True), file=sys.stderr, flush=True)

    # -------------------------------------------------------------- helpers
    def question(self, canary: str) -> str:
        return f"{QUESTION} (ref {canary})"

    def sign_in_all(self) -> dict[str, dict]:
        out = {}
        for user in ("team-user", "docs-user", "owui-admin"):
            login = self.s.login(user)
            out[user] = {**login, **(self.s.whoami(user) if login.get("signed_in") else {})}
        return out

    def ask(self, canary: str) -> dict:
        return self.s.chat("docs-user", client.DOCS_MODEL, self.question(canary))

    def inspect(self, canary: str | None = None) -> dict:
        args = ["python3", "-", canary] if canary else ["python3", "-"]
        proc = owui_exec("openwebui", args, stdin=read_bytes(INSPECT))
        try:
            return json.loads(proc.stdout.decode())
        except ValueError:
            return {}

    def snapshot_logs(self, label: str) -> None:
        pod = pod_info()["name"]
        self.logs[f"{label}:openwebui-pod"] = logs_of(pod)
        self.logs[f"{label}:agent"] = logs_of("deploy/rag-agent")
        self.logs[f"{label}:router"] = logs_of("deploy/router", ns="owui-router")
        self.logs[f"{label}:dex"] = logs_of("deploy/dex", ns="owui-idp")

    def collect_dns(self, ip: str) -> None:
        """Names (and the number of queries) the pod with this IP asked CoreDNS for, so far."""
        proc = kc("logs", "-l", "k8s-app=kube-dns", "--tail=-1", ns="kube-system")
        lines = proc.stdout.decode("utf-8", "replace").splitlines()
        self.dns_names |= checks.dns_names(lines, {ip})
        self.dns_queries += checks.count_queries(lines, {ip})

    # -------------------------------------------------------------- the checks
    def revocation(self) -> None:
        login = self.s.login("other-user")
        issued = os.path.exists(self.s._token_path("other-user"))
        self.add(checks.check_revoked_user(bool(login.get("signed_in")), issued))

    def identity(self, users: dict[str, dict]) -> None:
        self.add(checks.check_models({u: users[u].get("models", []) for u in ("team-user", "docs-user")}))
        self.add(checks.check_groups({u: users[u].get("groups", []) for u in ("team-user", "docs-user")},
                                     {u: client.USERS[u][0] for u in ("team-user", "docs-user")}))
        signup = self.s.api(None, "POST", "/api/v1/auths/signup",
                            {"name": "x", "email": "x@trial.test", "password": "x" * 16})[0]
        config = self.s.api(None, "GET", "/api/config")[1] or {}
        signin = self.s.api(None, "POST", "/api/v1/auths/signin",
                            {"email": "docs-user@trial.test", "password": "not-the-password"})[0]
        self.add(checks.check_refusals(signup, (config.get("features") or {}).get("enable_login_form"), signin))

    def pod_identity(self) -> None:
        pod = pod_info()
        self.add(checks.check_pod_security(pod["spec"]))
        uid_file = os.path.join(self.state, "uid-openwebui.txt")
        expected = int(read_text(uid_file).strip())
        proc = owui_exec("openwebui", ["sh", "-c", "id -u; id -g"])
        self.add(checks.check_uid(proc.stdout.decode(), expected, IMAGE_UID))

    def blocked_paths(self) -> None:
        results = {}
        for path in client.BLOCKED:
            if path.endswith("%00"):
                # Asked on the pod's own listener (the sidecar), so no other nginx hop can be the answer.
                status, body = self.sidecar_post(path)
            else:
                status, body = self.s.raw("docs-user", "POST", path)
            results[path] = checks.classify_blocked(status, body, path)
        self.add(checks.check_blocked(results, self.s.api("docs-user", "GET", "/api/models")[0]))

    def sidecar_post(self, path: str) -> tuple[int, bytes]:
        code = ("import http.client, ssl, sys\n"
                "c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE\n"
                "h = http.client.HTTPSConnection('127.0.0.1', 8443, timeout=15, context=c)\n"
                "h.request('POST', sys.argv[1], body=b'{}')\n"
                "r = h.getresponse()\n"
                "sys.stdout.write(str(r.status) + '\\n')\n"
                "sys.stdout.flush()\n"
                "sys.stdout.buffer.write(r.read())\n")
        proc = py_exec("openwebui", code, path)
        head, _, body = proc.stdout.partition(b"\n")
        return (int(head) if head.strip().isdigit() else 0), body

    def data_dir_after_use(self) -> None:
        self.add(checks.check_data_dir(self.inspect()))

    def console(self, label: str) -> checks.Verdict:
        code = ("import urllib.request as u\n"
                "r = u.urlopen('http://localhost:8080/ui', timeout=15)\n"
                "h = u.urlopen('http://localhost:8080/healthz', timeout=15)\n"
                "import json\n"
                "print(json.dumps([r.status, r.headers.get('content-type', ''), h.status]))\n")
        proc = agent_exec(code)
        try:
            parts = json.loads(proc.stdout.decode())
        except ValueError:
            parts = []
        if proc.returncode != 0 or len(parts) != 3:
            verdict = checks.Verdict(f"console-alongside:{label}", False, {"reason": "console request failed"})
        else:
            verdict = checks.check_console(int(parts[0]), str(parts[1]), int(parts[2]))
            verdict.name = f"console-alongside:{label}"
        self.add(verdict)
        return verdict

    def network_policies(self) -> None:
        """Only the chart's policy may select the Open WebUI pod for egress (a lane policy would be unioned in)."""
        pod = pod_info()["spec"]["metadata"].get("labels", {})
        docs = json.loads(kc_out("get", "networkpolicy", "-o", "json"))["items"]
        self.add(checks.check_policies_selecting(pod, docs))

    def egress_probes(self) -> None:
        """Real sockets from inside the pod. Last, because the blocked targets add DNS names of their own."""
        probe = read_bytes(PROBE)
        pod = pod_info()["name"]

        def run(mode: str, *targets: str) -> int:
            return kc("exec", "-i", pod, "-c", "openwebui", "--", "python3", "-", mode, *targets,
                      stdin=probe).returncode

        reach = run("reachable", "rag-agent:8080", client.DEX_ISSUER.split("/")[2])
        # One probe per target, so a failure names the target that connected.
        # The lane's boundary policy excludes this pod, so only the chart's policy governs it: every
        # target besides the agent and the IdP must fail, in-cluster ones included.
        connected = [t for t in ("1.1.1.1:443", "8.8.8.8:443", "kubernetes.default.svc:443", "qdrant:6333",
                                 "test-gateway:4000", "router.owui-router.svc.cluster.local:8443")
                     if run("blocked", t) != 0]
        verdict = checks.check_egress_probes(reach, 1 if connected else 0)
        verdict.detail["connected_outside_allow_list"] = connected
        self.add(verdict)

    def c1_and_restart(self) -> None:
        before = {
            "chat_ids": self.s.chat_ids("docs-user"),
            "function_sha256": self.s.function_sha256(),
            "models": checks.DOCS_MODELS,
            "pod_uid": pod_info()["uid"],
        }
        self.add(checks.check_function_hash(before["function_sha256"], self.repo_sha()))
        original = self.s.admin_config_value(EDIT_KEY)
        edit_status = self.s.admin_config_set(EDIT_KEY, EDIT_VALUE)
        edited = self.s.admin_config_value(EDIT_KEY) if edit_status == 200 else original
        rows_proc = py_exec("openwebui", (
            "import sqlite3\n"
            f"c = sqlite3.connect('file:{DATA}/webui.db?mode=ro', uri=True)\n"
            "try:\n    print(c.execute('SELECT COUNT(*) FROM config').fetchone()[0])\n"
            "except sqlite3.Error:\n    print(0)\n"))
        config_rows = int(rows_proc.stdout.decode().strip() or -1)
        self.snapshot_logs("before-restart")
        self.collect_dns(pod_info()["ip"])  # the pod about to be replaced
        # Restart: the Recreate rollout replaces the pod on the same RWO claim.
        kc("rollout", "restart", "deploy/rag-openwebui")
        kc("rollout", "status", "deploy/rag-openwebui", "--timeout=600s", timeout=700)
        self.wait_ready()
        self.sign_in_all()
        after = {
            "chat_ids": self.s.chat_ids("docs-user"),
            "function_sha256": self.s.function_sha256(),
            "models": self.s.whoami("docs-user").get("models", []),
            "pod_uid": pod_info()["uid"],
        }
        reverted = self.s.admin_config_value(EDIT_KEY)
        self.add(checks.check_admin_edit_reverts(original, edited, reverted, config_rows))
        self.add(checks.check_persistence(before, after))
        verdict = checks.check_answer(self.ask(self.canary1 + "r"))
        verdict.name = "restart-next-chat"
        self.add(verdict)

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                if self.s.login("owui-admin").get("signed_in"):
                    return
            except OSError:
                pass
            time.sleep(5)
        raise RuntimeError("Open WebUI did not accept a sign-in after the restart")

    def retention_purge(self) -> None:
        aged = self.ask(self.canary2)
        fresh = self.ask(self.canary1 + "f")
        old_id, fresh_id = aged.get("chat_id", ""), fresh.get("chat_id", "")
        long_ago = int(time.time()) - (RETENTION_DAYS + 10) * 86400
        py_exec("chat-purge", (
            "import sqlite3, sys\n"
            f"c = sqlite3.connect('{DATA}/webui.db', timeout=30)\n"
            "c.execute('UPDATE chat SET created_at = ?, updated_at = ? WHERE id = ?', (int(sys.argv[1]), int(sys.argv[1]), sys.argv[2]))\n"
            "c.commit()\n"), str(long_ago), old_id)
        orphan = f"{DATA}/uploads/acceptance-orphan.bin"
        py_exec("openwebui", (
            "import os, sys, time\n"
            "p = sys.argv[1]\nos.makedirs(os.path.dirname(p), exist_ok=True)\n"
            "open(p, 'wb').write(b'x' * 17)\nt = time.time() - 3 * 86400\nos.utime(p, (t, t))\n"), orphan)
        pod = pod_info()["name"]
        kc("exec", pod, "-c", "chat-purge", "--", "python3", "/opt/mainframe-rag/openwebui/purge_chats.py",
           "--days", str(RETENTION_DAYS), "--data-dir", DATA, "--once")
        status_text = kc("exec", pod, "-c", "chat-purge", "--", "cat", f"{DATA}/.mainframe-rag-purge.json").stdout.decode()
        orphan_left = kc("exec", pod, "-c", "chat-purge", "--", "ls", orphan).returncode == 0
        canary_files = self.inspect(self.canary2).get("canary_files") or []
        next_chat = self.ask(self.canary1 + "n")
        self.add(checks.check_purge(
            old_chat_present_after=self.s.chat_status("docs-user", old_id) == 200,
            fresh_chat_present_after=self.s.chat_status("docs-user", fresh_id) == 200,
            orphan_upload_present_after=orphan_left, canary_files_after=canary_files,
            purge_status=checks.parse_purge_status(status_text), next_chat=next_chat))

    def log_scan(self) -> None:
        self.snapshot_logs("after")
        canaries = {"question-canary-1": self.canary1, "question-canary-2": self.canary2}
        for user in client.USERS:
            canaries[f"password:{user}"] = read_text(os.path.join(self.state, f"{user}.password")).strip()
        secret = json.loads(read_text(os.path.join(self.state, "secrets.json")))
        canaries["webui-secret-key"], canaries["oidc-client-secret"] = secret["webui"], secret["client"]
        self.add(checks.scan_logs(self.logs, canaries))

    # -------------------------------------------------------------- the run
    def run(self) -> bool:
        users = self.sign_in_all()
        self.identity(users)
        self.revocation()
        self.pod_identity()
        self.add(self._first_answer())
        self.add(self.legacy_chat())
        self.blocked_paths()
        self.data_dir_after_use()
        self.console("before-restart")
        self.c1_and_restart()
        self.retention_purge()
        self.collect_dns(pod_info()["ip"])  # the replacement pod, after all use
        resolv = owui_exec("openwebui", ["cat", "/etc/resolv.conf"]).stdout.decode("utf-8", "replace")
        self.add(checks.check_dns(self.dns_names, self.dns_queries, checks.parse_search_domains(resolv)))
        self.log_scan()
        self.console("after-restart")
        self.network_policies()
        self.egress_probes()
        return checks.overall(self.verdicts)

    def _first_answer(self) -> checks.Verdict:
        return checks.check_answer(self.ask(self.canary1))

    def legacy_chat(self) -> checks.Verdict:
        """#1255: a chat stored with the pre-manifold model id, continued with the model the
        frontend resolves for it (the served default_models)."""
        listed = self.s.whoami("docs-user").get("models", [])
        status, config = self.s.api("docs-user", "GET", "/api/config")
        default = (config or {}).get("default_models") if status == 200 else None
        turn = self.s.chat("docs-user", default or client.DOCS_MODEL, self.question(self.canary1 + "l"),
                           chat_models=[client.DOCS_FUNCTION])
        return checks.check_legacy_chat(listed, default, turn)

    # -------------------------------------------------------------- team tools (#1077, ADR-0010 T4)
    def team_tools(self) -> bool:
        """After acceptance.sh team-tools re-rendered Open WebUI with the Splunk gateway (bearer mode)
        against the fake Splunk. The admin step of docs/deploy.md step 9 is done through the admin API
        here; at a site it is the admin UI (a site check)."""
        team_model = checks.TEAM_MODEL_PREFIX + TEAM_REASONING_MODEL
        gateway_ready = kc("rollout", "status", "deploy/rag-splunk-gateway", "--timeout=60s").returncode == 0
        self.add(checks.check_gateway_start(logs_of("deploy/rag-splunk-gateway"), gateway_ready, len(TEAM_TEMPLATES)))
        logins = {user: self.s.login(user) for user in ("team-user", "docs-user", client.ADMIN)}
        self.add(checks.check_relogin(logins))
        self.add(checks.check_team_model_admin_only(self.models("team-user"), self.models(client.ADMIN), team_model))
        group_id = self.s.group_ids().get(TEAM_GROUP, "")
        self.add(self.team_model_row(team_model, group_id))
        self.add(checks.check_t4_models(self.models("team-user"), self.models("docs-user"), team_model,
                                        sorted(m for m, _name in client.DOCS_MODELS)))
        team_status, team_tools = self.s.api("team-user", "GET", "/api/v1/tools/")
        docs_status, docs_tools = self.s.api("docs-user", "GET", "/api/v1/tools/")
        self.add(checks.check_t4_tools(team_status, team_tools if isinstance(team_tools, list) else [],
                                       docs_status, docs_tools if isinstance(docs_tools, list) else []))
        self.add(checks.check_mcp_handshake(self.specs("team-user"), self.specs("docs-user"), TEAM_TEMPLATES))
        self.add(checks.check_tool_call(self.tool_call(), TEAM_TEMPLATES))
        team_chat = self.s.stream_completion("team-user", team_model, "Say hello.")
        docs_chat = self.s.stream_completion("docs-user", team_model, "Say hello.")
        self.add(checks.check_team_chat(team_chat, int(docs_chat.get("completion_status") or 0)))
        self.add(self.t4_closed(team_model))
        self.add(checks.check_team_network(self.team_probes()))
        self.add(self.team_log_scan())
        return checks.overall(self.verdicts)

    def t4_closed(self, team_model: str) -> checks.Verdict:
        proxy = {}
        for path in TEAM_PROXY_CLOSED:
            status, body = self.s.raw(client.ADMIN, "POST", path)
            proxy[path] = "proxy-403" if status == 403 and body == b"denied\n" else f"HTTP {status}"
        status, functions = self.s.api(client.ADMIN, "GET", "/api/v1/functions/")
        ids = sorted(f.get("id") for f in functions) if status == 200 and isinstance(functions, list) else []
        create, _ = self.s.api("team-user", "POST", "/api/v1/models/create", {
            "id": "team-user-model", "base_model_id": team_model, "name": "user model",
            "meta": {"toolIds": [checks.TEAM_TOOL_ID]}, "params": {}})
        return checks.check_t4_closed(proxy, ids, create)

    def models(self, user: str) -> list[dict]:
        status, body = self.s.api(user, "GET", "/api/models")
        return list((body or {}).get("data") or []) if status == 200 and isinstance(body, dict) else []

    def team_model_row(self, team_model: str, group_id: str) -> checks.Verdict:
        """docs/deploy.md step 9: edit the connection model itself (an override row, no base model)."""
        status, _created = self.s.api(client.ADMIN, "POST", "/api/v1/models/create", {
            "id": team_model, "base_model_id": None, "name": "Team \N{EN DASH} Splunk",
            "meta": {"description": "Team model with the Splunk tool (Kind acceptance)",
                     "toolIds": [checks.TEAM_TOOL_ID]},
            "params": {"function_calling": "native"},
            "access_grants": [{"principal_type": "group", "principal_id": group_id, "permission": "read"}]})
        if status != 200:
            return checks.Verdict("team-model-row", False, {"create_status": status})
        status, row = self.s.api(client.ADMIN, "GET", f"/api/v1/models/model?id={team_model}")
        return checks.check_team_model_row(status, row if isinstance(row, dict) else None, group_id)

    def specs(self, user: str) -> dict:
        """GET .../server:mcp:splunk/specs on the container port from inside the pod (the allow-list proxy
        admits only /api/v1/tools/), as the user (the token on stdin, never argv)."""
        with open(os.path.join(self.state, "sessions", f"{user}.token"), "rb") as fh:
            token = fh.read()
        proc = owui_exec("openwebui", ["python3", "-c", SPECS_CODE], stdin=token)
        try:
            return json.loads(proc.stdout.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"status": None, "rc": proc.returncode}

    def tool_call(self) -> dict:
        proc = owui_exec("openwebui", ["python3", "-", "errors_by_host"], stdin=read_bytes(TOOL_CALL))
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("TEAM_TOOL_CALL "):
                try:
                    return json.loads(line.removeprefix("TEAM_TOOL_CALL "))
                except ValueError:
                    break
        return {"error": f"no result line (rc {proc.returncode})"}

    def team_probes(self) -> dict[str, int]:
        """egress_probe.py from the intruder, Open WebUI and the gateway pods; one target per probe."""
        probe = read_bytes(PROBE)
        gateway_ip = kc_out("get", "pod", "-l", "app=rag-splunk-gateway",
                            "-o", "jsonpath={.items[0].status.podIP}").strip()
        splunk = "fake-splunk.owui-splunk.svc:8089"

        def run(target: list[str], mode: str, dest: str) -> int:
            return kc("exec", "-i", *target, "--", "python3", "-", mode, dest, stdin=probe).returncode

        intruder = ["pod/team-tools-intruder"]
        owui = [pod_info()["name"], "-c", "openwebui"]
        gateway = ["deploy/rag-splunk-gateway", "-c", "gateway"]
        return {
            "intruder->gateway-pod-ip:blocked": run(intruder, "blocked", f"{gateway_ip}:8080") if gateway_ip else 1,
            "intruder->gateway-service:blocked": run(intruder, "blocked", "rag-splunk-gateway:8080"),
            # Control: the lane's default-deny egress lets the intruder reach this namespace's pods,
            # the gateway's among them, so only the gateway's own ingress policy refuses it.
            "intruder->litellm:reachable (control)": run(intruder, "reachable", "test-gateway:4000"),
            "openwebui->gateway:reachable": run(owui, "reachable", "rag-splunk-gateway:8080"),
            "openwebui->litellm:reachable": run(owui, "reachable", "test-gateway:4000"),
            "openwebui->fake-splunk:blocked": run(owui, "blocked", splunk),
            "gateway->fake-splunk:reachable": run(gateway, "reachable", splunk),
            "gateway->litellm:blocked": run(gateway, "blocked", "test-gateway:4000"),
            "gateway->agent:blocked": run(gateway, "blocked", "rag-agent:8080"),
            # The lane's default-deny egress no longer selects the gateway: its own policy must hold
            # in the cluster (a public address is also refused by the runner's gate, so it proves nothing).
            "gateway->kubernetes-api:blocked": run(gateway, "blocked", "kubernetes.default.svc:443"),
        }

    def team_log_scan(self) -> checks.Verdict:
        """No team-tools secret value in the gateway, fake Splunk or Open WebUI logs."""
        team = os.path.join(self.state, "team")
        canaries = {name: read_text(os.path.join(team, name)).strip()
                    for name in ("splunk-mcp-token", "bearer-secret", "openwebui-llm-api-key")}
        logs, unread = {}, []
        for label, target, ns in (("gateway", "deploy/rag-splunk-gateway", NS),
                                  ("fake-splunk", "deploy/fake-splunk", "owui-splunk"),
                                  ("openwebui-pod", pod_info()["name"], NS)):
            proc = kc("logs", target, "--all-containers", "--tail=-1", ns=ns)
            if proc.returncode != 0:
                unread.append(label)  # an error message is not a scanned log
            logs[label] = proc.stdout.decode("utf-8", "replace")
        verdict = checks.scan_logs(logs, canaries)
        if unread:
            verdict.ok = False
            verdict.detail["unreadable"] = unread
        verdict.name = "team-log-canary"
        return verdict

    # -------------------------------------------------------------- operator lane (#1118 B3)
    def repo_sha(self) -> str:
        return read_text(os.path.join(self.state, "render", "pipe.sha256")).strip()

    def first_pass(self) -> bool:
        """After the first airgap:deploy, before the Secret holds admin-api-key."""
        login = self.s.login(client.ADMIN)
        role = self.s.whoami(client.ADMIN).get("role") if login.get("signed_in") else None
        self.add(checks.check_pipe_absent_before_key(self.s.function_sha256(), role))
        return checks.overall(self.verdicts)

    def record(self) -> bool:
        """Before the rollback: the docs user's chats and the claim, kept for rollback-on."""
        self.s.login("docs-user")
        before = {"chat_ids": self.s.chat_ids("docs-user"), "claim_uid": claim_uid()}
        client._private_write(os.path.join(self.state, "rollback-before.json"), json.dumps(before))
        self.add(checks.check_rollback_recorded(before["chat_ids"], before["claim_uid"]))
        return checks.overall(self.verdicts)

    def rollback_off(self) -> bool:
        """After OPENWEBUI_ENABLED=false and airgap:deploy: Helm removed the objects, kept the claim."""
        before = json.loads(read_text(os.path.join(self.state, "rollback-before.json")))
        deadline = time.monotonic() + 180  # a removed Deployment's pod terminates after the deploy returns
        while True:
            pods = json.loads(kc_out("get", "pod", "-l", "app=rag-openwebui", "-o", "json"))["items"]
            if not pods or time.monotonic() > deadline:
                break
            time.sleep(5)
        remaining = []
        for obj in checks.OPENWEBUI_OBJECTS:
            proc = kc("get", obj, "--ignore-not-found", "-o", "name")
            if proc.returncode != 0 or proc.stdout.strip():
                remaining.append(obj)  # an unreadable object counts as present (fail closed)
        console = self.console("rollback-off")
        self.add(checks.check_rollback_off(remaining, before["claim_uid"], claim_uid(), len(pods), console.ok))
        return checks.overall(self.verdicts)

    def rollback_on(self) -> bool:
        """After OPENWEBUI_ENABLED=true and airgap:deploy again: the next ordinary use on the kept claim."""
        before = json.loads(read_text(os.path.join(self.state, "rollback-before.json")))
        self.wait_ready()
        self.s.login("docs-user")
        after = {"chat_ids": self.s.chat_ids("docs-user"), "claim_uid": claim_uid(),
                 "function_sha256": self.s.function_sha256(),
                 "models": self.s.whoami("docs-user").get("models", [])}
        next_chat = checks.check_answer(self.ask(self.canary1 + "k"), name="rollback-next-chat")
        self.add(next_chat)
        self.add(checks.check_rollback_on(before, after, self.repo_sha(), next_chat.ok))
        return checks.overall(self.verdicts)

    # -------------------------------------------------------------- copy-out and restore (#1376)
    def copy_later(self) -> bool:
        """After the copy-out and the next airgap:deploy (B's stand-in): an answered chat the copy
        does not hold, kept for restored."""
        before = json.loads(read_text(os.path.join(self.state, "rollback-before.json")))
        self.wait_ready()
        self.s.login("docs-user")
        chat = checks.check_answer(self.ask(self.canary1 + "w"), name="copy-later-chat")
        later = sorted(set(self.s.chat_ids("docs-user")) - set(before["chat_ids"]))
        client._private_write(os.path.join(self.state, "copy-later.json"), json.dumps({"chat_ids": later}))
        self.add(chat)
        self.add(checks.check_copy_later(later, chat.ok))
        return checks.overall(self.verdicts)

    def restored(self) -> bool:
        """After the restore and A's airgap:deploy: what rollback-on proves after K1, and the chat
        written after the copy-out is gone."""
        later = json.loads(read_text(os.path.join(self.state, "copy-later.json")))["chat_ids"]
        self.rollback_on()
        self.add(checks.check_restored(later, self.s.chat_ids("docs-user")))
        return checks.overall(self.verdicts)

    # -------------------------------------------------------------- slow model (#1118)
    def paced(self) -> bool:
        """The slow-model stand-in: a first token after a long silence, then a long stream.
        The same marked question goes down the browser's path, the API path for a saved
        chat and the OpenAI-compatible stream at once, so the phase takes one answer's time."""
        state = mock_health()
        config = checks.check_paced_config(state, agent_deadline_s())
        self.add(config)
        if not config.ok:
            return False
        ttft_s, stream_s = config.detail["ttft_s"], config.detail["stream_s"]
        if not self.s.login("docs-user").get("signed_in"):
            raise RuntimeError("docs-user sign-in failed")
        question = f"{self.question(self.canary1 + 'p')} {state['paced_marker']}"
        runs = {
            "ui-task": lambda: self.s.chat("docs-user", client.DOCS_MODEL, question,
                                           session_id="paced-" + secrets.token_hex(8)),
            "api-chat": lambda: self.s.chat("docs-user", client.DOCS_MODEL, question),
            "api-stream": lambda: self.s.stream_completion("docs-user", client.DOCS_MODEL, question),
        }
        with ThreadPoolExecutor(len(runs)) as pool:
            futures = {path: pool.submit(run) for path, run in runs.items()}
        for path, future in futures.items():
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - the class only, never the text
                self.add(checks.Verdict(f"paced-{path}", False, {"error": type(exc).__name__}))
                continue
            self.add(checks.check_paced_chat({"path": path, **result}, ttft_s, stream_s))
        return checks.overall(self.verdicts)


def main(argv: list[str]) -> int:
    run = Acceptance(argv[0])
    phases = {"checks": run.run, "first-pass": run.first_pass, "record": run.record,
              "rollback-off": run.rollback_off, "rollback-on": run.rollback_on, "paced": run.paced,
              "copy-later": run.copy_later, "restored": run.restored,
              "team-tools": run.team_tools}
    phase = argv[1] if len(argv) > 1 else "checks"
    if phase not in phases:
        print(f"unknown phase {phase!r}; one of {', '.join(phases)}", file=sys.stderr)
        return 2
    try:
        ok = phases[phase]()
    except Exception as exc:  # noqa: BLE001 - report the class, never the text (it may carry upstream bytes)
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        run.verdicts.append(checks.Verdict("executor", False, {
            "error": type(exc).__name__, "where": f"{os.path.basename(frame.filename)}:{frame.lineno}"}))
        ok = False
    print(json.dumps({"ok": ok, "checks": [v.as_json() for v in run.verdicts]}, sort_keys=True, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
