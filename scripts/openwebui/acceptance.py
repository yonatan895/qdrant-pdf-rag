"""Open WebUI acceptance executor (#1070, #1073): runs the checks against a live Kind deployment.

    python3 -I acceptance.py STATE_DIR [PHASE]   (via acceptance.sh run, or acceptance.sh operator ...)

PHASE is `checks` (default: the full check list) or, in the operator-deploy lane (#1118 B3),
`first-pass` (before the admin API key), `record`, `rollback-off` and `rollback-on` (K1).

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
            "models": [client.DOCS_MODEL],
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


def main(argv: list[str]) -> int:
    run = Acceptance(argv[0])
    phases = {"checks": run.run, "first-pass": run.first_pass, "record": run.record,
              "rollback-off": run.rollback_off, "rollback-on": run.rollback_on}
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
