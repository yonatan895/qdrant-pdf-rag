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

DOCS_FUNCTION = "mainframe_rag_docs"
# The docs Pipe's answer-depth entries as Open WebUI models (#1255), sorted as whoami lists them.
DOCS_MODELS = sorted(f"{DOCS_FUNCTION}.{entry}" for entry in ("standard", "quick", "thorough"))
DOCS_DEFAULT = f"{DOCS_FUNCTION}.standard"  # the chart's DEFAULT_MODELS
# restricted-v2 assigns UIDs from the namespace range; Kind stand-ins use this one.
UID_RANGE_START = 1000700000
# The names an Open WebUI pod may resolve: the IdP and the agent Service (search-domain
# expansions of those two included). Anything else is an egress attempt.
ALLOWED_DNS_BASES = ("dex.localhost", "rag-agent")


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
    """Each signed-in user sees exactly the Rosetta models (the docs Pipe's entries)."""
    bad = {u: m for u, m in users.items() if sorted(m) != DOCS_MODELS}
    return _v("docs-model-only", bool(users) and not bad, users=sorted(users), unexpected=bad)


def check_revoked_user(signed_in: bool, token_issued: bool) -> Verdict:
    """A user the IdP authenticates but whose only role value is in neither list gets no session."""
    return _v("role-revocation", not signed_in and not token_issued, signed_in=signed_in, token_issued=token_issued)


def check_groups(user_groups: dict[str, list[str]], expected: dict[str, str]) -> Verdict:
    """The IdP group claim is mapped to the Open WebUI group (per user)."""
    missing = {u: g for u, g in expected.items() if g not in user_groups.get(u, [])}
    return _v("group-claim", not missing, missing=missing)


TIMING_KEYS = ("path", "reply", "headers_s", "first_content_s", "last_content_s", "end_s", "max_gap_s",
               "saved_content_s", "done_s")


def check_answer(chat: dict, name: str = "answer-citations") -> Verdict:
    """One saved turn: finished, non-empty, with source events (citations as source chips).
    The client's timings ride along (seconds; with real models they are the measured
    first-token and answer times the release record compares with the CI envelope)."""
    ok = (bool(chat.get("done")) and chat.get("content_chars", 0) > 0 and chat.get("source_chips", 0) > 0
          and chat.get("source_documents", 0) > 0)
    return _v(name, ok, done=bool(chat.get("done")), content_chars=chat.get("content_chars", 0),
              status_events=chat.get("status_events", 0), source_chips=chat.get("source_chips", 0),
              source_documents=chat.get("source_documents", 0),
              **{k: chat[k] for k in TIMING_KEYS if k in chat})


def check_legacy_chat(listed: list[str], default_models: str | None, turn: dict) -> Verdict:
    """#1255 upgrade: a chat stored before the docs Pipe became a manifold names the bare
    Function id, which is no longer a model. On opening it v0.13.4 drops an unlisted id and
    selects the configured default (Chat.svelte loadChat, resolveDefaultModelIds), so the
    next turn answers only if the bare id is gone, the default is the listed default entry,
    and a turn on that stored chat, sent with the default, completes with sources. The
    chat is legacy by the model list it was created with; Open WebUI rewrites that list
    to the turn's model on the turn (observed live, #1273), so the list read afterwards
    does not decide. The frontend selection itself is browser behaviour (the pilot
    browser trace)."""
    default_ok = default_models == DOCS_DEFAULT and DOCS_DEFAULT in listed
    turn_ok = (turn.get("completion_status") == 200 and turn.get("chat_models_created") == [DOCS_FUNCTION]
               and turn.get("model") == DOCS_DEFAULT and check_answer(turn).ok)
    ok = DOCS_FUNCTION not in listed and default_ok and turn_ok
    return _v("legacy-chat-answers", ok, bare_id_listed=DOCS_FUNCTION in listed, default_ok=default_ok,
              turn_ok=turn_ok, done=bool(turn.get("done")), source_chips=turn.get("source_chips", 0),
              models_created=turn.get("chat_models_created"), models_after=turn.get("chat_models"))


# ------------------------------------------------------------------ slow model (#1118)
# The largest library default idle limit any hop on the path would fall back to if its
# setting were lost: nginx proxy_read_timeout (60 s; the OpenShift router's default
# server timeout is 30 s, httpx's 5 s). The paced stand-in must stay silent well past it.
DEFAULT_IDLE_S = 60
# Retrieval, prompt building and scheduling must fit between the paced answer and the
# agent's total request deadline, or the stand-in would test the deadline instead.
DEADLINE_MARGIN_S = 20


def check_paced_config(state: dict, deadline_s: float) -> Verdict:
    """The slow-model stand-in's settings (mock /healthz) against the agent's request deadline:
    a first token after twice the largest default idle limit, a stream longer than it, and the
    whole paced answer inside the deadline with a margin."""
    ttft_s = float(state.get("paced_ttft_ms") or 0) / 1000
    stream_s = float(state.get("paced_stream_ms") or 0) / 1000
    ok = (bool(state.get("paced_marker")) and ttft_s >= 2 * DEFAULT_IDLE_S and stream_s >= DEFAULT_IDLE_S
          and deadline_s > 0 and ttft_s + stream_s + DEADLINE_MARGIN_S <= deadline_s)
    return _v("paced-stand-in", ok, marker_set=bool(state.get("paced_marker")), ttft_s=ttft_s,
              stream_s=stream_s, agent_deadline_s=deadline_s)


def check_paced_chat(result: dict, ttft_s: float, stream_s: float) -> Verdict:
    """One answer from the paced stand-in through the router stand-in, the allow-list
    proxy, Open WebUI, the Pipe, the agent and the gateway: complete, cited, and timed
    so the slow first token and the long stream really happened on this path.

    ui-task    (the browser's path) the saved message: sources, first content no
               earlier than the first token, done no earlier than the whole answer;
    api-chat   (a saved chat over the API) the same saved message, and the one
               request that carried it stayed silent at least the first-token wait
               and lasted the whole answer;
    api-stream (OpenAI-compatible, no saved chat) the SSE reply: content, a cited
               line, finish stop and [DONE]; silent at least the first-token wait;
               last content no earlier than the whole answer."""
    path, total = result.get("path"), ttft_s + stream_s

    def at_least(key: str, bound: float) -> bool:
        value = result.get(key)
        return isinstance(value, (int, float)) and value >= bound

    ok = result.get("completion_status") == 200 and ttft_s > 0
    if path == "ui-task":
        ok = ok and check_answer(result).ok and at_least("saved_content_s", ttft_s) and at_least("done_s", total)
    elif path == "api-chat":
        ok = ok and check_answer(result).ok and at_least("max_gap_s", ttft_s) and at_least("end_s", total)
    elif path == "api-stream":
        ok = (ok and result.get("content_chars", 0) > 0 and result.get("cited_lines", 0) > 0
              and result.get("finish") == "stop" and result.get("done_frame") is True
              and at_least("first_content_s", ttft_s) and at_least("max_gap_s", ttft_s)
              and at_least("last_content_s", total))
    else:
        ok = False
    keep = (*TIMING_KEYS, "completion_status", "done", "content_chars", "source_chips", "source_documents",
            "cited_lines", "finish", "done_frame")
    return _v(f"paced-{path}", ok, ttft_s=ttft_s, stream_s=stream_s, **{k: result[k] for k in keep if k in result})


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
    """Restart on the RWO claim: chats, the Rosetta Function bytes and the model grant survived."""
    lost = sorted(set(before["chat_ids"]) - set(after["chat_ids"]))
    ok = (not lost and before["function_sha256"] == after["function_sha256"] and bool(after["function_sha256"])
          and after["models"] == DOCS_MODELS and after["pod_uid"] != before["pod_uid"])
    return _v("restart-persistence", ok, chats_before=len(before["chat_ids"]), chats_after=len(after["chat_ids"]),
              chats_lost=len(lost), function_unchanged=before["function_sha256"] == after["function_sha256"],
              docs_only_after=after["models"] == DOCS_MODELS, pod_replaced=after["pod_uid"] != before["pod_uid"])


def check_function_hash(stored_sha256: str | None, repo_sha256: str) -> Verdict:
    """The installed Rosetta Function is byte-identical to the repository Pipe."""
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


# ------------------------------------------------------------------ operator lane (#1118 B3)
# The objects the chart renders for Open WebUI. After the K1 rollback (OPENWEBUI_ENABLED=false and
# airgap:deploy, docs/deploy.md) none may remain; the claim rag-openwebui-data is kept.
OPENWEBUI_OBJECTS = (
    "deployment.apps/rag-openwebui", "service/rag-openwebui", "route.route.openshift.io/rag-openwebui",
    "networkpolicy.networking.k8s.io/rag-openwebui", "configmap/rag-openwebui-pipe",
    "configmap/rag-openwebui-proxy", "configmap/rag-openwebui-idp-ca",
)


def check_pipe_absent_before_key(function_sha256: str | None, admin_role: str | None) -> Verdict:
    """Site procedure step 3: a deploy before the Secret has admin-api-key installs no Function, so its
    'docs Pipe NOT installed' notice is true, and the IdP admin role reaches Open WebUI (step 4 can run)."""
    ok = function_sha256 is None and admin_role == "admin"
    return _v("pipe-absent-before-key", ok, function_installed=function_sha256 is not None,
              admin_signed_in=admin_role == "admin")


def check_rollback_recorded(chat_ids: list[str], claim_uid: str) -> Verdict:
    """Before K1: there are chats to lose and a claim to keep, so the rollback checks can fail."""
    return _v("rollback-recorded", bool(chat_ids) and bool(claim_uid), chats=len(chat_ids), claim=bool(claim_uid))


def check_rollback_off(remaining: list[str], claim_before: str, claim_after: str, pods: int, console_ok: bool) -> Verdict:
    """K1 off: no Open WebUI object or pod remains, the claim is the same object, the console still answers."""
    kept = bool(claim_before) and claim_after == claim_before
    ok = not remaining and kept and pods == 0 and console_ok
    return _v("rollback-off", ok, remaining=sorted(remaining), claim_kept=kept, pods=pods, console_ok=console_ok)


def check_rollback_on(before: dict, after: dict, repo_sha256: str, next_chat_ok: bool) -> Verdict:
    """K1 on again: the same claim, every recorded chat, the grant and the repository Pipe are back,
    and the next ordinary chat answers."""
    lost = sorted(set(before["chat_ids"]) - set(after["chat_ids"]))
    kept = bool(before["claim_uid"]) and after["claim_uid"] == before["claim_uid"]
    ok = (bool(before["chat_ids"]) and not lost and kept and after["function_sha256"] == repo_sha256
          and after["models"] == DOCS_MODELS and next_chat_ok)
    return _v("rollback-on", ok, chats_before=len(before["chat_ids"]), chats_lost=len(lost), claim_kept=kept,
              pipe_is_repository=after["function_sha256"] == repo_sha256,
              docs_only=after["models"] == DOCS_MODELS, next_chat_answered=next_chat_ok)


# ------------------------------------------------------------------ team tools (#1077, ADR-0010 T4)
RATING_ACTION = "mainframe_rag_rating"  # provision_pipe.py ACTION_ID (with ratings on)
TEAM_TOOL_ID = "server:mcp:splunk"  # the chart's TOOL_SERVER_CONNECTIONS entry (info.id splunk)
TEAM_MODEL_PREFIX = "team."  # OPENAI_API_CONFIGS prefix_id


def check_gateway_start(log_text: str, ready: bool, templates: int) -> Verdict:
    """The gateway's own start-up line: its backend check passed against the fake Splunk, in bearer mode,
    and the pod is Ready (it serves only after the check)."""
    starts = []
    for line in log_text.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get("action") == "splunk_gateway_start":
            starts.append(doc)
    last = starts[-1] if starts else {}
    ok = (ready and last.get("decision") == "ok" and last.get("mode") == "bearer"
          and last.get("templates") == templates)
    return _v("team-gateway-start", ok, ready=ready, starts=len(starts), decision=last.get("decision"),
              mode=last.get("mode"), protocol=last.get("protocol"), templates=last.get("templates"))


def check_relogin(logins: dict[str, dict]) -> Verdict:
    """Both users sign in again after the team-tools re-render: the IdP CA now reaches Open WebUI only
    inside the complete bundle (SSL_CERT_FILE, no ca-trust init container; decision 5 on #1374)."""
    signed = {user: bool(result.get("signed_in")) for user, result in sorted(logins.items())}
    return _v("team-relogin", bool(signed) and all(signed.values()), signed_in=signed)


def _ids(models: list[dict]) -> list[str]:
    return sorted(str(m.get("id")) for m in models)


def _tool_ids(model: dict) -> list:
    return list((((model.get("info") or {}).get("meta") or {}).get("toolIds")) or [])


def check_team_model_admin_only(team_user_models: list[dict], admin_models: list[dict], team_model: str) -> Verdict:
    """Before the admin step, the connection model exists and only admins see it. With model_ids set,
    v0.13.4 lists the connection's models without asking LiteLLM, so this proves the registration, not
    the key or the path (check_team_chat does)."""
    ok = team_model in _ids(admin_models) and team_model not in _ids(team_user_models)
    return _v("team-model-admin-only-before-grant", ok, admin_sees=team_model in _ids(admin_models),
              team_user_sees=team_model in _ids(team_user_models))


def check_team_model_row(status: int, row: dict | None, group_id: str) -> Verdict:
    """The admin step (docs/deploy.md step 9) as stored: an override row of the connection model itself
    (no base model), the Splunk tool, native function calling, read for the team group only."""
    row = row or {}
    grants = sorted((g.get("principal_type"), g.get("principal_id"), g.get("permission"))
                    for g in row.get("access_grants") or [])
    ok = (status == 200 and row.get("base_model_id") is None
          and _tool_ids({"info": row}) == [TEAM_TOOL_ID]
          and (row.get("params") or {}).get("function_calling") == "native"
          and grants == [("group", group_id, "read")])
    return _v("team-model-row", ok, status=status, base_model=row.get("base_model_id"), tools=_tool_ids({"info": row}),
              function_calling=(row.get("params") or {}).get("function_calling"), grants=len(grants))


def check_t4_models(team_user_models: list[dict], docs_user_models: list[dict], team_model: str,
                    docs_models: list[str]) -> Verdict:
    """ADR-0010 T4 in /api/models: the team user sees the Docs models and the Team model with the Splunk
    tool; the docs user sees the Docs models only; no Docs model carries a tool."""
    team_ids, docs_ids = _ids(team_user_models), _ids(docs_user_models)
    team_entry = next((m for m in team_user_models if m.get("id") == team_model), {})
    docs_with_tools = sorted({str(m.get("id")) for m in team_user_models + docs_user_models
                              if m.get("id") in docs_models and _tool_ids(m)})
    ok = (team_ids == sorted([*docs_models, team_model]) and docs_ids == sorted(docs_models)
          and _tool_ids(team_entry) == [TEAM_TOOL_ID] and not docs_with_tools)
    return _v("t4-models", ok, team_user_models=team_ids, docs_user_models=docs_ids,
              team_model_tools=_tool_ids(team_entry), docs_models_with_tools=docs_with_tools)


def check_t4_tools(team_status: int, team_tools: list[dict], docs_status: int, docs_tools: list[dict]) -> Verdict:
    """ADR-0010 T4 in /api/v1/tools/ (through the allow-list proxy): the tool server is listed for the team
    group's member only."""
    team_ids, docs_ids = _ids(team_tools), _ids(docs_tools)
    ok = team_status == 200 and docs_status == 200 and TEAM_TOOL_ID in team_ids and TEAM_TOOL_ID not in docs_ids
    return _v("t4-tools", ok, team_status=team_status, docs_status=docs_status, team_user_tools=team_ids,
              docs_user_tools=docs_ids)


def check_mcp_handshake(team: dict, docs: dict, templates: list[str]) -> Verdict:
    """Open WebUI's own MCP handshake (GET /api/v1/tools/id/server:mcp:splunk/specs, connect_mcp_server:
    the access check, the expanded bearer key, the SDK's discover-then-initialize) as each user: the team
    user gets the gateway's template tools, the docs user is refused (404, access denied)."""
    ok = team.get("status") == 200 and team.get("names") == sorted(templates) and docs.get("status") == 404
    return _v("team-mcp-handshake", ok, team_status=team.get("status"), team_tools=team.get("names"),
              docs_status=docs.get("status"))


def check_tool_call(result: dict, templates: list[str]) -> Verdict:
    """A template tool called with Open WebUI's MCP client from inside its pod (url and key from its own
    TOOL_SERVER_CONNECTIONS): rows come back framed as untrusted, redacted, through both policies."""
    ok = (result.get("key_expanded") is True and result.get("tools") == sorted(templates)
          and isinstance(result.get("rows"), int) and result["rows"] > 0 and result.get("framed") is True
          and result.get("redacted") is True and result.get("raw_secret_seen") is False)
    return _v("team-tool-call", ok, **{k: result.get(k) for k in
                                         ("auth_type", "key_expanded", "tools", "rows", "framed", "redacted",
                                          "raw_secret_seen", "error")})


def check_team_chat(team: dict, docs_status: int) -> Verdict:
    """The Team model answers the team user through LiteLLM with Open WebUI's own key (the shared api-key
    beside it is no LiteLLM key), through the CA bundle and the model egress rule; the docs user cannot use
    it (v0.13.4 answers a failed model-access check with 400 "Model not found")."""
    ok = (team.get("completion_status") == 200 and team.get("content_chars", 0) > 0 and bool(team.get("done_frame"))
          and docs_status in (400, 401, 403, 404))
    return _v("team-model-chat", ok, team_status=team.get("completion_status"),
              team_content_chars=team.get("content_chars"), team_done=team.get("done_frame"), docs_status=docs_status)


def check_t4_closed(proxy: dict[str, str], function_ids: list[str], user_model_create: int) -> Verdict:
    """ADR-0010 T4, unchanged surfaces with team tools on: the proxy still answers its own 403 for the
    tool-server, connection and task APIs and the tool routes beyond the listing; the only Functions are
    the repository's (the docs Pipe, and the rating Action when ratings are on); a user cannot create a
    model."""
    open_paths = sorted(path for path, result in proxy.items() if result != "proxy-403")
    ok = (bool(proxy) and not open_paths and DOCS_FUNCTION in function_ids
          and set(function_ids) <= {DOCS_FUNCTION, RATING_ACTION} and user_model_create in (401, 403))
    return _v("t4-closed-surfaces", ok, paths=len(proxy), not_refused=open_paths, functions=function_ids,
              user_model_create=user_model_create)


def check_team_network(probes: dict[str, int]) -> Verdict:
    """Real sockets (scripts/ci/egress_probe.py exit codes, 0 = expectation met) from three pods: a pod outside
    Open WebUI cannot reach the gateway but reaches LiteLLM in the same namespace (control); Open WebUI
    reaches the gateway and LiteLLM, not Splunk; the gateway reaches Splunk, not LiteLLM or the agent."""
    failed = sorted(name for name, rc in probes.items() if rc != 0)
    return _v("team-network", bool(probes) and not failed, probes=len(probes), failed=failed)


def overall(verdicts: list[Verdict]) -> bool:
    return bool(verdicts) and all(v.ok for v in verdicts)
