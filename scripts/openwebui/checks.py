"""Open WebUI acceptance checks: the pure evaluators (#1070).

Every function takes plain data an executor collected from a live Kind
deployment (acceptance.py) and returns a `Verdict`. There is no I/O, no
cluster and no secret here, so each rule has a unit test in
tests/test_openwebui_acceptance.py. Verdict details carry ids, counts and
states, never chat text, passwords or tokens.

Owner: docs/openwebui-dev.md (acceptance lane), ADR-0007 conditions.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

DOCS_MODEL = "mainframe_rag_docs"
# restricted-v2 assigns UIDs from the namespace range; Kind stand-ins use this one.
UID_RANGE_START = 1000700000
# The names an Open WebUI pod may resolve: the IdP and the agent Service (search-domain
# expansions of those two included). Anything else is an egress attempt.
ALLOWED_DNS_BASES = ("dex.localhost", "rag-agent")
VECTOR_DIR_NAMES = ("vector_db", "chroma", "qdrant", "milvus")


@dataclass
class Verdict:
    name: str
    ok: bool
    detail: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        return {"check": self.name, "ok": self.ok, **self.detail}


def _v(name: str, ok: bool, **detail) -> Verdict:
    return Verdict(name, bool(ok), detail)


# ------------------------------------------------------------------ pod identity
def check_uid(id_output: str, expected_uid: int, image_uid: int) -> Verdict:
    """`id -u; id -g` output of the running container: the namespace's UID in group 0."""
    parts = id_output.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return _v("uid", False, reason="unparseable id output")
    uid, gid = int(parts[0]), int(parts[1])
    ok = uid == expected_uid and uid >= UID_RANGE_START and uid != image_uid and gid == 0
    return _v("uid", ok, uid=uid, gid=gid, expected_uid=expected_uid, image_uid=image_uid)


def check_pod_security(pod: dict) -> Verdict:
    """The Pod Security 'restricted' shape of the rendered pod, every container."""
    spec = pod.get("spec", {})
    psc = spec.get("securityContext", {})
    problems: list[str] = []
    if psc.get("runAsNonRoot") is not True:
        problems.append("pod runAsNonRoot is not true")
    if int(psc.get("runAsUser", 0)) < UID_RANGE_START:
        problems.append("pod runAsUser outside the namespace range")
    if (psc.get("seccompProfile") or {}).get("type") not in ("RuntimeDefault", "Localhost"):
        problems.append("no seccomp profile")
    if spec.get("hostNetwork") or spec.get("hostPID") or spec.get("hostIPC"):
        problems.append("host namespaces shared")
    names = []
    for c in spec.get("containers", []):
        names.append(c["name"])
        sc = c.get("securityContext", {})
        if sc.get("allowPrivilegeEscalation") is not False:
            problems.append(f"{c['name']}: privilege escalation allowed")
        if sc.get("privileged"):
            problems.append(f"{c['name']}: privileged")
        if "ALL" not in (sc.get("capabilities") or {}).get("drop", []):
            problems.append(f"{c['name']}: capabilities not dropped")
        if sc.get("readOnlyRootFilesystem") is not True:
            problems.append(f"{c['name']}: root filesystem writable")
    return _v("pod-security", not problems and bool(names), containers=sorted(names), problems=problems)


# ------------------------------------------------------------------ egress
def check_egress_probes(reachable_rc: int, blocked_rc: int) -> Verdict:
    """Exit codes of scripts/ci/egress_probe.py run inside the pod (0 = expectation met)."""
    return _v("egress-probes", reachable_rc == 0 and blocked_rc == 0,
              allowed_targets_reachable=reachable_rc == 0, other_targets_blocked=blocked_rc == 0)


_DNS_LINE = re.compile(r"\] (\d+\.\d+\.\d+\.\d+):\d+ - \d+ \"(\w+) IN (\S+)")


def dns_names(log_lines: list[str], pod_ips: set[str]) -> set[str]:
    """Names the given pod IPs asked CoreDNS for (query log lines of the `log` plugin)."""
    names: set[str] = set()
    for line in log_lines:
        match = _DNS_LINE.search(line)
        if match and match.group(1) in pod_ips:
            names.add(match.group(3).rstrip(".").lower())
    return names


def count_queries(log_lines: list[str], pod_ips: set[str]) -> int:
    return sum(1 for line in log_lines if (m := _DNS_LINE.search(line)) and m.group(1) in pod_ips)


def allowed_dns_names(search_domains: list[str]) -> set[str]:
    """The IdP and agent names as the pod's resolver asks for them: bare, then with each search domain."""
    return {base if not d else f"{base}.{d}" for base in ALLOWED_DNS_BASES for d in ["", *search_domains]}


def parse_search_domains(resolv_conf: str) -> list[str]:
    for line in resolv_conf.splitlines():
        parts = line.split()
        if parts and parts[0] == "search":
            return [p.strip(".").lower() for p in parts[1:]]
    return []


def check_dns(names: set[str], queries_seen: int, search_domains: list[str]) -> Verdict:
    """Every name resolved by the Open WebUI pod is the IdP or the agent Service, exactly, bare or with one of
    the pod's own search domains appended (not a substring: dex.localhost.attacker.example is stray)."""
    allowed = allowed_dns_names(search_domains)
    stray = sorted(n for n in names if n not in allowed)
    # No query at all (or no resolver search line read) means the log or the pod was wrong, not zero egress.
    return _v("dns-egress", queries_seen > 0 and bool(search_domains) and not stray, distinct_names=len(names),
              queries=queries_seen, search_domains=len(search_domains), stray_names=stray)


def _selects(selector: dict, labels: dict) -> bool:
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for e in selector.get("matchExpressions") or []:
        key, op, values = e.get("key"), e.get("operator"), e.get("values") or []
        have = key in labels
        if op == "In" and not (have and labels[key] in values):
            return False
        if op == "NotIn" and have and labels[key] in values:
            return False
        if op == "Exists" and not have:
            return False
        if op == "DoesNotExist" and have:
            return False
    return True


def check_policies_selecting(pod_labels: dict, policies: list[dict], chart_policy: str = "rag-openwebui") -> Verdict:
    """Names of the NetworkPolicies that govern the pod's egress: exactly the chart's, nothing else."""
    egress = sorted(p["metadata"]["name"] for p in policies
                    if "Egress" in (p.get("spec", {}).get("policyTypes") or [])
                    and _selects(p["spec"].get("podSelector") or {}, pod_labels))
    return _v("egress-policy-owner", egress == [chart_policy], egress_policies=egress)


# ------------------------------------------------------------------ DATA_DIR
def check_data_dir(report: dict) -> Verdict:
    """inspect_data.py's report after use: no vector store, no upload, no retrieval rows."""
    vector = report.get("vector_store_paths", [])
    uploads = report.get("uploads", 0)
    vectorish = {t: n for t, n in report.get("vector_like_tables", {}).items() if n}
    rows = {t: n for t, n in report.get("retrieval_content_rows", {}).items() if n}
    has_db = any(f[0] == "webui.db" for f in report.get("data_dir_files", []))
    ok = has_db and not vector and uploads == 0 and not vectorish and not rows
    return _v("data-dir", ok, files=len(report.get("data_dir_files", [])), vector_store_paths=vector,
              uploads=uploads, vector_like_rows=vectorish, retrieval_content_rows=rows, has_db=has_db)


# ------------------------------------------------------------------ sign-in and models
def check_models(users: dict[str, list[str]]) -> Verdict:
    """Each signed-in user sees exactly the Docs model."""
    bad = {u: m for u, m in users.items() if m != [DOCS_MODEL]}
    return _v("docs-model-only", bool(users) and not bad, users=sorted(users), unexpected=bad)


def check_revoked_user(signed_in: bool, token_issued: bool) -> Verdict:
    """A user the IdP authenticates but whose only role value is in neither list gets no session."""
    return _v("role-revocation", not signed_in and not token_issued, signed_in=signed_in, token_issued=token_issued)


def check_groups(user_groups: dict[str, list[str]], expected: dict[str, str]) -> Verdict:
    """The IdP group claim is mapped to the Open WebUI group (per user)."""
    missing = {u: g for u, g in expected.items() if g not in user_groups.get(u, [])}
    return _v("group-claim", not missing, missing=missing)


def check_answer(chat: dict, name: str = "answer-citations") -> Verdict:
    """One saved turn: finished, non-empty, with source events (citations as source chips)."""
    ok = (bool(chat.get("done")) and chat.get("content_chars", 0) > 0 and chat.get("source_chips", 0) > 0
          and chat.get("source_documents", 0) > 0)
    return _v(name, ok, done=bool(chat.get("done")), content_chars=chat.get("content_chars", 0),
              status_events=chat.get("status_events", 0), source_chips=chat.get("source_chips", 0),
              source_documents=chat.get("source_documents", 0))


BLOCKED_OK = ("proxy-403", "sidecar-nul-400")


def check_blocked(results: dict[str, str], allowed_control: int) -> Verdict:
    """Every listed path is refused by the pod's allow-list sidecar; the control path still works."""
    not_blocked = {p: r for p, r in sorted(results.items()) if r not in BLOCKED_OK}
    return _v("blocked-paths", bool(results) and not not_blocked and allowed_control == 200,
              paths=len(results), classes=sorted(set(results.values())), not_blocked=not_blocked,
              allowed_control=allowed_control)


def classify_blocked(status: int, body: bytes, path: str = "") -> str:
    """The allow-list's own answer is 403 'denied'. The one other accepted refusal is the sidecar itself
    rejecting a NUL byte in the decoded path (nginx's 400 page, asked on the pod's own listener, so the
    router stand-in cannot be the hop that answered); tests/test_openwebui_proxy_nginx.py owns that case."""
    if status == 403 and body == b"denied\n":
        return "proxy-403"
    if status == 400 and path.endswith("%00") and b"<center>nginx</center>" in body:
        return "sidecar-nul-400"
    return f"HTTP {status}"


def check_refusals(signup_status: int, login_form_enabled: object, password_signin_status: int) -> Verdict:
    """Signup and the password login form are off (single sign-on is the only way in)."""
    ok = signup_status != 200 and login_form_enabled is False and password_signin_status != 200
    return _v("sso-only", ok, signup_status=signup_status, login_form_enabled=login_form_enabled,
              password_signin_status=password_signin_status)


# ------------------------------------------------------------------ restart and C1
def check_admin_edit_reverts(before: object, edited: object, after: object, config_rows: int) -> Verdict:
    """C1: a live admin edit took effect, never reached the database, and a restart restored the pinned value."""
    ok = edited != before and after == before and config_rows == 0
    return _v("admin-edit-reverts", ok, edit_took_effect=edited != before, restored_after_restart=after == before,
              config_rows=config_rows)


def check_persistence(before: dict, after: dict) -> Verdict:
    """Restart on the RWO claim: chats, the Docs Function bytes and the model grant survived."""
    lost = sorted(set(before["chat_ids"]) - set(after["chat_ids"]))
    ok = (not lost and before["function_sha256"] == after["function_sha256"] and bool(after["function_sha256"])
          and after["models"] == [DOCS_MODEL] and after["pod_uid"] != before["pod_uid"])
    return _v("restart-persistence", ok, chats_before=len(before["chat_ids"]), chats_after=len(after["chat_ids"]),
              chats_lost=len(lost), function_unchanged=before["function_sha256"] == after["function_sha256"],
              docs_only_after=after["models"] == [DOCS_MODEL], pod_replaced=after["pod_uid"] != before["pod_uid"])


def check_function_hash(stored_sha256: str | None, repo_sha256: str) -> Verdict:
    """The installed Docs Function is byte-identical to the repository Pipe."""
    return _v("pipe-hash", stored_sha256 == repo_sha256, match=stored_sha256 == repo_sha256)


# ------------------------------------------------------------------ retention purge (S2)
def check_purge(old_chat_present_after: bool, fresh_chat_present_after: bool, orphan_upload_present_after: bool,
                canary_files_after: list[str], purge_status: dict, next_chat: dict) -> Verdict:
    """S2: the aged chat and the orphan upload are gone from the claim, the fresh chat survives, and the next
    ordinary question works (the purge left a database the app can use)."""
    # The sidecar may run between the backdating and our run; the claim's state decides, the counters
    # only have to say the last run succeeded and nothing is owed.
    counters_ok = purge_status.get("consecutive_failures") == 0 and bool(purge_status.get("last_success"))
    answered = check_answer(next_chat).ok
    ok = (not old_chat_present_after and fresh_chat_present_after and not orphan_upload_present_after
          and not canary_files_after and counters_ok and answered)
    return _v("retention-purge", ok, aged_chat_gone=not old_chat_present_after,
              fresh_chat_kept=fresh_chat_present_after, orphan_upload_gone=not orphan_upload_present_after,
              canary_files_after=len(canary_files_after), purge_counters_ok=counters_ok,
              next_chat_answered=answered)


def parse_purge_status(text: str) -> dict:
    """.mainframe-rag-purge.json next to the database; unreadable means not ok."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ logs (S4)
def scan_logs(logs: dict[str, str], canaries: dict[str, str]) -> Verdict:
    """No canary (question text, password, key) occurs in any container log; counts only."""
    hits: dict[str, list[str]] = {}
    for source, text in logs.items():
        found = sorted(label for label, needle in canaries.items() if needle and needle in text)
        if found:
            hits[source] = found
    scanned = sum(len(t) for t in logs.values())
    return _v("log-canary", bool(logs) and scanned > 0 and not hits, sources=sorted(logs), bytes_scanned=scanned,
              canaries=len(canaries), leaks=hits)


# ------------------------------------------------------------------ console
def check_console(ui_status: int, ui_content_type: str, healthz_status: int) -> Verdict:
    """The operator console (/ui) and the agent health endpoint still answer next to Open WebUI."""
    ok = ui_status == 200 and ui_content_type.startswith("text/html") and healthz_status == 200
    return _v("console-alongside", ok, ui_status=ui_status, healthz_status=healthz_status)


def overall(verdicts: list[Verdict]) -> bool:
    return bool(verdicts) and all(v.ok for v in verdicts)
