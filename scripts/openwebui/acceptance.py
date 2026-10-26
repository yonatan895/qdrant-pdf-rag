"""Open WebUI acceptance executor (#1070, #1073): runs the checks against a live Kind deployment.

    python3 -I acceptance.py STATE_DIR        (via acceptance.sh run)

Everything here is collection: kubectl calls into the Open WebUI pod, HTTP
calls through the router stand-in as the signed-in test users, and reads of
the pod's claim. Every judgement is a pure function in checks.py (unit-tested).
Output is one JSON document of check names, states, counts and ids: never a
password, token, key, question or answer. Exit 0 only when every check passed.

Environment (set by acceptance.sh): KUBECTL, KIND_NAME, APP_NS, OWUI_URL, DEX_ISSUER.
The agent behind the Docs Pipe is the one the e2e Kind lane already runs
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
            status, body = self.s.raw("docs-user", "POST", path)
            results[path] = checks.classify_blocked(status, body)
        self.add(checks.check_blocked(results, self.s.api("docs-user", "GET", "/api/models")[0]))

    def data_dir_after_use(self) -> None:
        self.add(checks.check_data_dir(self.inspect()))

    def console(self, label: str) -> None:
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
            self.add(checks.Verdict(f"console-alongside:{label}", False, {"reason": "console request failed"}))
            return
        verdict = checks.check_console(int(parts[0]), str(parts[1]), int(parts[2]))
        verdict.name = f"console-alongside:{label}"
        self.add(verdict)

    def egress_probes(self) -> None:
        """Real sockets from inside the pod. Last, because the blocked targets add DNS names of their own."""
        probe = read_bytes(PROBE)
        pod = pod_info()["name"]

        def run(mode: str, *targets: str) -> int:
            return kc("exec", "-i", pod, "-c", "openwebui", "--", "python3", "-", mode, *targets,
                      stdin=probe).returncode

        reach = run("reachable", "rag-agent:8080", client.DEX_ISSUER.split("/")[2])
        # One probe per target, so a failure names the target that connected.
        # The e2e boundary policy admits every pod of the lane's namespace and the API server for all
        # pods there (unioned with the chart's policy), so only targets outside that are asserted:
        # public addresses and a listening pod in another namespace (the router stand-in, port 8443).
        connected = [t for t in ("1.1.1.1:443", "8.8.8.8:443", "router.owui-router.svc.cluster.local:8443")
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
        repo_sha = read_text(os.path.join(self.state, "render", "pipe.sha256")).strip()
        self.add(checks.check_function_hash(before["function_sha256"], repo_sha))
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
        self.pod_identity()
        self.add(self._first_answer())
        self.blocked_paths()
        self.data_dir_after_use()
        self.console("before-restart")
        self.c1_and_restart()
        self.retention_purge()
        self.collect_dns(pod_info()["ip"])  # the replacement pod, after all use
        self.add(checks.check_dns(self.dns_names, self.dns_queries))
        self.log_scan()
        self.console("after-restart")
        self.egress_probes()
        return checks.overall(self.verdicts)

    def _first_answer(self) -> checks.Verdict:
        return checks.check_answer(self.ask(self.canary1))


def main(argv: list[str]) -> int:
    run = Acceptance(argv[0])
    try:
        ok = run.run()
    except Exception as exc:  # noqa: BLE001 - report the class, never the text (it may carry upstream bytes)
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        run.verdicts.append(checks.Verdict("executor", False, {
            "error": type(exc).__name__, "where": f"{os.path.basename(frame.filename)}:{frame.lineno}"}))
        ok = False
    print(json.dumps({"ok": ok, "checks": [v.as_json() for v in run.verdicts]}, sort_keys=True, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
