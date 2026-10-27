"""Unit tests for the answer-tier eval helpers (mainframe_rag.eval.answers):
verdict logic (judge), deterministic stratified sampling (select_sample),
and aggregation (summarize).

Hermetic: no Qdrant, no vLLM, no TestClient. Pure helpers are imported directly;
HTTP mode uses an inert transport or an original loopback HTTP producer. The
live model tier runs via `sh scripts/tools/run-task.sh eval:answers`, never as
part of plain pytest."""

from __future__ import annotations

import json
import logging

import pytest

from mainframe_rag.agent.answer import is_abstention, is_refusal
from mainframe_rag.eval.answers import (
    ZERO_HITS_ANSWER,
    AnswerCapture,
    answer_completeness,
    is_zero_hits_answer,
    judge,
    run_query,
    select_sample,
    summarize,
)


def _entry(**overrides) -> dict:
    base = {
        "id": "MSG-01",
        "query": "What does IEA500I report?",
        "query_class": "message_id",
        "expected_behavior": "answer",
        "expected_doc_ids": [],
    }
    base.update(overrides)
    return base


def _answer_payload(**overrides) -> dict:
    base = {
        "request_id": "r1",
        "answer": "LFAREA reserves frames above the bar.\nCitations:\nSA23-1380-70 ref, p. 1",
        "citations": ["SA23-1380-70 ref, p. 1"],
        "citations_inferred": False,
        "inferred_indices": [],
        "script": None,
    }
    base.update(overrides)
    return base


@pytest.fixture
def http_answer_eval(tmp_path, monkeypatch):
    """Run the actual CLI and HTTP client against an inert HTTP boundary."""
    import httpx2

    from mainframe_rag.eval import answers

    monkeypatch.chdir(tmp_path)
    real_client = httpx2.Client
    calls = []
    options = []

    def local_settings_forbidden():
        raise AssertionError("HTTP eval must not inspect the evaluator's local stack")

    monkeypatch.setattr(answers, "load_settings", local_settings_forbidden)

    def run(respond, entries=None, extra=()):
        golden = tmp_path / "original.jsonl"
        golden.write_text("".join(json.dumps(e) + "\n" for e in (entries or [_entry()])))

        def record(request):
            calls.append(request)
            return respond(request)

        def client(**kwargs):
            options.append(kwargs)
            return real_client(transport=httpx2.MockTransport(record), **kwargs)

        monkeypatch.setattr(httpx2, "Client", client)
        status = answers.main([
            "--agent-url", "http://deployed-agent:8087", "--golden", str(golden),
            "--all", "--out", str(tmp_path / "report.json"), *extra,
        ])
        report_path = tmp_path / "report.json"
        report = json.loads(report_path.read_text()) if report_path.exists() else None
        return status, report, calls, options

    return run


def test_http_answer_eval_scores_the_deployed_agent(http_answer_eval, tmp_path):
    import httpx2

    health = {"status": "ok", "qdrant": True, "embed": True,
              "representation": "compatible", "rerank": None}

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=health)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": []})
        assert request.url.path == "/v1/answer"
        return httpx2.Response(200, json=_answer_payload())

    status, report, calls, options = http_answer_eval(respond)
    assert status == 0
    assert [(r.method, str(r.url)) for r in calls] == [
        ("GET", "http://deployed-agent:8087/healthz"),
        ("POST", "http://deployed-agent:8087/v1/search"),
        ("POST", "http://deployed-agent:8087/v1/answer"),
    ]
    assert json.loads(calls[-1].content) == {"query": "What does IEA500I report?", "temperature": 0.0}
    assert report["metrics"]["queries"] == 1
    assert report["results"][0]["verdict"] == "pass"
    assert report["results"][0]["finish_reason"] is None
    assert report["deployment"] == {
        "agent_url": "http://deployed-agent:8087", "healthz": health,
        "git_sha": None, "image_digest": None,
    }
    assert report["representation"] == {"servable": True, "outcome": "compatible"}
    assert options == [{"base_url": "http://deployed-agent:8087", "timeout": 300.0,
                        "follow_redirects": False, "trust_env": False}]
    manifest = json.loads((tmp_path / "evals/runs/eval_answers_runs.jsonl").read_text())
    assert manifest["deployment"] == report["deployment"]
    assert manifest["git_sha"] == "unknown"
    assert manifest["settings_hash"] is None
    assert manifest["model_ids"] is None


@pytest.mark.parametrize("status,health,observed", [
    (503, {"status": "degraded", "qdrant": True, "representation": "pending"}, True),
    (200, {"status": "ok", "qdrant": True, "embed": False, "representation": "compatible"}, True),
    (200, {"status": "ok", "qdrant": "true", "representation": "compatible"}, False),
    (200, {"status": "ok", "qdrant": True}, False),
    (200, [], False),
    (401, {"code": "unauthorized", "message": "PRIVATE HEALTH DETAIL"}, False),
    (302, {"status": "ok", "qdrant": True, "representation": "compatible"}, True),
])
def test_http_answer_eval_refuses_nonservable_health(
    http_answer_eval, capsys, status, health, observed,
):
    import httpx2

    def respond(request):
        assert request.url.path == "/healthz"
        return httpx2.Response(status, json=health)

    entries = [_entry(), _entry(id="MSG-02", query="Original second case")]
    result, report, calls, _ = http_answer_eval(respond, entries)
    assert result == 2
    assert len(calls) == 1
    assert report["metrics"]["queries"] == 2
    assert report["metrics"]["judged"] == 0
    assert report["metrics"]["errors"] == 2
    assert [row["id"] for row in report["results"]] == ["MSG-01", "MSG-02"]
    assert report["representation"]["servable"] is False
    observed_health = report["deployment"]["healthz"]
    assert (observed_health is not None) is observed
    if observed:
        assert report["representation"]["outcome"] == health["representation"]
    assert "PRIVATE HEALTH DETAIL" not in json.dumps(report) + capsys.readouterr().err


@pytest.mark.parametrize("failure", ["connection", "json"])
def test_http_answer_eval_health_errors_are_fixed_and_keep_every_case(
    http_answer_eval, capsys, failure,
):
    import httpx2

    def respond(request):
        if failure == "connection":
            raise httpx2.ConnectError("PRIVATE HEALTH DETAIL", request=request)
        return httpx2.Response(200, text="PRIVATE HEALTH DETAIL")

    status, report, calls, _ = http_answer_eval(respond)
    assert status == 2
    assert report["metrics"]["errors"] == report["metrics"]["queries"] == 1
    assert report["deployment"]["healthz"] is None
    assert len(calls) == 1
    assert "PRIVATE HEALTH DETAIL" not in json.dumps(report) + capsys.readouterr().err


def test_http_answer_eval_retains_failed_answers_and_continues(http_answer_eval, capsys):
    import httpx2

    entries = [_entry(id=identity, query=query) for identity, query in (
        ("ERR-HTTP", "Original HTTP error case"),
        ("ERR-TIMEOUT", "Original timeout case"),
        ("ERR-JSON", "Original malformed case"),
        ("GOOD", "Original successful case"),
    )]

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json={"status": "ok", "qdrant": True,
                                             "representation": "compatible"})
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": []})
        query = json.loads(request.content)["query"]
        if query == "Original HTTP error case":
            return httpx2.Response(503, json={"code": "representation_unavailable",
                                              "message": "PRIVATE ANSWER DETAIL"})
        if query == "Original timeout case":
            raise httpx2.ReadTimeout("PRIVATE ANSWER DETAIL", request=request)
        if query == "Original malformed case":
            return httpx2.Response(200, text="PRIVATE ANSWER DETAIL")
        return httpx2.Response(200, json=_answer_payload())

    status, report, calls, _ = http_answer_eval(respond, entries)
    assert status == 1
    assert report["metrics"]["queries"] == 4
    assert report["metrics"]["errors"] == 3
    assert report["metrics"]["judged"] == 1
    assert [row["id"] for row in report["results"]] == ["ERR-HTTP", "ERR-TIMEOUT", "ERR-JSON", "GOOD"]
    assert [row["verdict"] for row in report["results"]] == ["error", "error", "error", "pass"]
    assert report["results"][0]["failures"] == ["HTTP 503 (representation_unavailable)"]
    assert report["results"][1]["failures"] == ["request error: ReadTimeout"]
    assert report["results"][2]["failures"] == ["malformed answer response"]
    assert len([r for r in calls if r.url.path == "/v1/answer"]) == 4
    assert "PRIVATE ANSWER DETAIL" not in json.dumps(report) + capsys.readouterr().err


_MALFORMED_ANSWER_CASES = [
    # A malformed /v1/answer body is a row error, never an abort and never a
    # pass: the next ordinary case still runs. Abstention rows are held to the
    # same completeness rule.
    ("answer", [], None),
    ("answer", "PRIVATE ANSWER DETAIL", None),
    ("answer", _answer_payload(answer={"PRIVATE ANSWER DETAIL": "not prose"}), None),
    ("answer", _answer_payload(citations=["Original p. 1", {"PRIVATE ANSWER DETAIL": "not a cite"}]), None),
    ("answer", _answer_payload(verification_state=[]), None),
    ("abstain", {}, "malformed answer response"),
    ("abstain", {"answer": "Original refusal", "citations": None}, "malformed answer response"),
    ("abstain", {"citations": []}, "malformed answer response"),
]


@pytest.mark.parametrize(("behavior", "payload", "failure"), _MALFORMED_ANSWER_CASES)
def test_http_answer_eval_malformed_answer_fields_cannot_abort_or_pass(
    http_answer_eval, capsys, behavior, payload, failure,
):
    import httpx2

    entries = [_entry(id="BAD", query="Original malformed response", expected_behavior=behavior),
               _entry(id="GOOD", query="Original next complete response")]

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json={"status": "ok", "qdrant": True,
                                             "representation": "compatible"})
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": []})
        if json.loads(request.content)["query"] == "Original malformed response":
            return httpx2.Response(200, json=payload)
        return httpx2.Response(200, json=_answer_payload())

    status, report, calls, _ = http_answer_eval(respond, entries)
    assert status == 1
    assert report["metrics"]["queries"] == 2
    assert report["metrics"]["errors"] == 1
    assert report["metrics"]["judged"] == 1
    assert [r["verdict"] for r in report["results"]] == ["error", "pass"]
    if failure is not None:
        assert report["results"][0]["failures"] == [failure]
    assert len([r for r in calls if r.url.path == "/v1/answer"]) == 2
    assert "PRIVATE ANSWER DETAIL" not in json.dumps(report) + capsys.readouterr().err


def test_http_answer_eval_numeric_overflow_retains_prior_and_next_cases(http_answer_eval):
    import httpx2

    entries = [_entry(id=identity, query=identity) for identity in ("FIRST", "OVERFLOW", "NEXT")]

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json={"status": "ok", "qdrant": True,
                                             "representation": "compatible"})
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": []})
        if json.loads(request.content)["query"] == "OVERFLOW":
            # Valid JSON numeric syntax can exceed the decoder's float range.
            return httpx2.Response(200, content=b'{"answer":"Original answer", "citations":[], "inferred_indices":[1e309]}')
        return httpx2.Response(200, json=_answer_payload())

    status, report, calls, _ = http_answer_eval(respond, entries)
    assert status == 1
    assert report["metrics"]["errors"] == 1
    assert report["metrics"]["judged"] == 2
    assert [row["verdict"] for row in report["results"]] == ["pass", "error", "pass"]
    assert report["results"][1]["failures"] == ["malformed inferred_indices in response"]
    assert len([request for request in calls if request.url.path == "/v1/answer"]) == 3


@pytest.mark.parametrize("hits", [
    ["PRIVATE SEARCH DETAIL"], {"PRIVATE SEARCH DETAIL": "not a hit list"},
    [{"doc_id": "A", "cite": "Original p. 1"}, {"doc_id": "B"}, 42],
])
def test_http_answer_eval_malformed_search_pool_keeps_the_next_case(
    http_answer_eval, capsys, hits,
):
    import httpx2

    entries = [_entry(id="BAD-POOL", query="Original malformed pool", expected_doc_ids=["A"]),
               _entry(id="GOOD", query="Original next ordinary request")]

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json={"status": "ok", "qdrant": True,
                                             "representation": "compatible"})
        if request.url.path == "/v1/search":
            bad_pool = json.loads(request.content)["query"] == "Original malformed pool"
            return httpx2.Response(200, json={"hits": hits if bad_pool else []})
        return httpx2.Response(200, json=_answer_payload())

    status, report, calls, _ = http_answer_eval(respond, entries)
    assert status == 0
    assert report["metrics"]["queries"] == report["metrics"]["judged"] == 2
    assert report["metrics"]["errors"] == 0
    assert [r["verdict"] for r in report["results"]] == ["pass", "pass"]
    assert report["results"][0]["hit_doc_ids"] is None
    assert "citation_recall" not in report["results"][0]
    assert len([r for r in calls if r.url.path == "/v1/answer"]) == 2
    assert "PRIVATE SEARCH DETAIL" not in json.dumps(report) + capsys.readouterr().err


def test_http_answer_eval_uses_a_real_http_agent_and_preserves_query_bytes(tmp_path, monkeypatch):
    """Socket boundary: an HTTP deployment, including a Route path prefix."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from mainframe_rag import manifest
    from mainframe_rag.eval import answers

    query = "Original question אב; $(touch SENTINEL) 'quoted'\nwith a second line"
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            observed.append(("GET", self.path, None))
            if self.path != "/route/healthz":
                self.send_error(404)
                return
            self.reply({"status": "ok", "qdrant": True, "embed": True,
                        "representation": "record_only_drift", "rerank": None})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observed.append(("POST", self.path, body))
            if self.path == "/route/v1/search":
                self.reply({"hits": []})
            elif self.path == "/route/v1/answer":
                if body["query"] == query:
                    self.reply(_answer_payload(answer="Original grounded answer."))
                else:
                    self.reply(_answer_payload(answer=ZERO_HITS_ANSWER, citations=[]))
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(answers, "load_settings", lambda: pytest.fail("remote eval touched local Settings"))
    monkeypatch.setattr(manifest, "get_git_sha", lambda: "e" * 40)
    golden = tmp_path / "original.jsonl"
    golden.write_text("\n".join(json.dumps(e) for e in [
        _entry(id="HTTP-ANSWER", query=query, gold_must_contain=["Original grounded"]),
        _entry(id="HTTP-REFUSAL", query="Original absent fixture", expected_behavior="abstain"),
    ]) + "\n")
    target = f"http://127.0.0.1:{server.server_port}/route"
    try:
        status = answers.main([
            "--agent-url", target + "/", "--golden", str(golden), "--all",
            "--temperature", "0.2", "--request-timeout", "12",
            "--out", str(tmp_path / "report.json"),
        ])
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert status == 0
    assert [(method, path) for method, path, _ in observed] == [
        ("GET", "/route/healthz"), ("POST", "/route/v1/search"),
        ("POST", "/route/v1/answer"), ("POST", "/route/v1/search"),
        ("POST", "/route/v1/answer"),
    ]
    assert observed[2][2] == {"query": query, "temperature": 0.2}
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["metrics"]["queries"] == 2
    assert report["metrics"]["errors"] == 0
    assert report["metrics"]["answer_n"] == report["metrics"]["abstain_n"] == 1
    assert report["metrics"]["answer_pass_rate"] == report["metrics"]["abstain_pass_rate"] == 1.0
    assert report["deployment"]["agent_url"] == target
    assert report["deployment"]["git_sha"] is None
    assert report["deployment"]["image_digest"] is None
    assert report["params"]["request_timeout"] == 12.0
    recorded = json.loads((tmp_path / "evals/runs/eval_answers_runs.jsonl").read_text())
    assert recorded["evaluator_git_sha"] == "e" * 40
    assert recorded["git_sha"] == "unknown"
    assert not (tmp_path / "SENTINEL").exists()


@pytest.mark.parametrize("value", ["0", "1801", "nan", "inf"])
def test_http_answer_eval_rejects_invalid_timeout_before_io(monkeypatch, value):
    from mainframe_rag.eval import answers

    monkeypatch.setattr(answers, "resolve_golden_paths", lambda *a: pytest.fail("invalid input reached dataset IO"))
    with pytest.raises(SystemExit) as exc:
        answers.main(["--agent-url", "http://agent:8080", "--request-timeout", value])
    assert exc.value.code == 2


@pytest.mark.parametrize(("raw", "expected"), [("1", 1.0), ("300", 300.0), ("950", 950.0), ("1800", 1800.0)])
def test_http_answer_eval_accepts_long_generation_timeout(raw, expected):
    """Issue #843: a client waiting out a 16384-token thinking reserve at ~22 tok/s needs more than 600 s."""
    from mainframe_rag.eval import answers

    assert answers._request_timeout(raw) == expected


@pytest.mark.parametrize("value", [
    "file:///tmp/agent", "http://user:private@agent",
    "http://agent?private=secret", "http://agent:bad",
])
def test_http_answer_eval_rejects_unsafe_target_before_io(monkeypatch, value):
    from mainframe_rag.eval import answers

    monkeypatch.setattr(answers, "resolve_golden_paths", lambda *a: pytest.fail("invalid input reached dataset IO"))
    with pytest.raises(SystemExit) as exc:
        answers.main(["--agent-url", value])
    assert exc.value.code == 2


# ----------------------------------------------------------------- refusal helper
def test_refusal_marker_battery() -> None:
    """Issues #135/#305: the eval's refusal verdicts and the agent's
    zero-citation rule use the same predicates — one helper, one marker
    list, one shape floor. Adversarial battery phrasings must fire it: the
    battery's 'no information regarding' refusal shipped 4 citations of
    real-but-unsupporting chunks because no marker caught it."""
    import mainframe_rag.eval.answers as ea

    assert ea.is_refusal is is_refusal
    assert ea.is_abstention is is_abstention
    assert is_refusal("No information regarding private keys is available in the excerpts.")
    assert is_refusal("There is no information about that parameter in the manuals provided.")
    assert is_refusal("The excerpts do not answer this question.")
    assert is_refusal("no manual excerpts carry dsn90221i")  # case-folded
    assert is_refusal("The EXCERPTS DO NOT CONTAIN the requested syntax.")
    assert is_refusal("**Not documented in the excerpts.**")  # wrapped markup
    assert not is_refusal("LFAREA reserves 64-bit frames above the bar.")
    assert not is_refusal("")


# ---------------------------------------------------------------- judge: answer
# One verdict table: each row is a distinct judge branch with its own expected
# verdict and (for a fail) the fixed failure text callers see.
_JUDGE_CASES = {
    "grounded-passes": ({}, "LFAREA is set in IEASYSxx.", ["SA23-1380-70 ref, p. 1"], {}, "pass", None),
    "zero-citations-fail": ({}, "LFAREA is set in IEASYSxx.", [], {}, "fail", "zero validated citations"),
    # Issue #269: cites mapped from bare bracket markers are never grounded.
    "inferred-citations-fail": ({}, "LFAREA is set in IEASYSxx [1].", ["SA23-1380-70 ref, p. 1"],
                                {"citations_inferred": True}, "fail", "only inferred citations"),
    "explicit-refusal-fails": ({}, "The excerpts do not cover LFAREA.", ["SA23-1380-70 ref, p. 1"],
                               {}, "fail", "explicit refusal"),
    "empty-body-fails": ({}, "   ", ["SA23-1380-70 ref, p. 1"], {}, "fail", "empty answer body"),
    # Issue #305: a grounded partial answer with a refusal-marked scope caveat
    # has a non-refusal clause and is NOT an abstention.
    "grounded-scope-caveat-passes": (
        {}, ("LFAREA is set in IEASYSxx and controls the real storage reserved for 1 MB and 2 GB "
             "pages; the documented forms cover xM, xG, xT, and x% values. The excerpts do not contain "
             "the 2.4, 2.5, and 3.1 variants. For the release shown here the syntax is complete, and the "
             "INCLUDE1MAFC and 1M=/2G= rules document how to size the reservation for each page type."),
        ["SA23-1380-70 ref, p. 1"], {}, "pass", None),
    "gold-substring-casefolded-fails": ({"gold_must_contain": ["JES2", "JES3"]}, "JES2 does X.",
                                        ["SA32-0990-02 ref, p. 3"], {}, "fail", "JES3"),
    "gold-substring-casefolded-passes": ({"gold_must_contain": ["jes2"]}, "JES2 does X.", ["c1"],
                                         {}, "pass", None),
    "gold-forbidden-substring-fails": ({"gold_must_not_contain": ["SETROPTS NO"]},
                                       "Run SETROPTS NO...", ["c1"], {}, "fail", "forbidden substring"),
    # Seed semantics: NEG-01 demands the literal refusal phrase; the agent's
    # fixed zero-hits wording does not satisfy it (deliberate strictness).
    "abstain-gold-phrase-pin-fails": (
        {"id": "NEG-01", "expected_behavior": "abstain", "gold_must_contain": ["excerpts do not answer"]},
        ZERO_HITS_ANSWER, [], {}, "fail", "excerpts do not answer"),
    # run_query suppresses gold checks on the canned zero-hits message.
    "zero-hits-abstain-skips-gold": (
        {"expected_behavior": "abstain", "gold_must_contain": ["IEA500I"],
         "must_cite_identifier": "IEA500I"}, ZERO_HITS_ANSWER, [], {"judge_gold": False}, "pass", None),
    # The gold suppression must not mask the retrieval gap.
    "zero-hits-answer-still-fails": ({"id": "DOC-01", "gold_must_contain": ["SA23-1380"]},
                                     ZERO_HITS_ANSWER, [], {"judge_gold": False}, "fail", "explicit refusal"),
    "must-cite-identifier-passes": ({"must_cite_identifier": "IEA794I"}, "See the message text.",
                                    ["SA38-0673-70, IEA794I, p. 5"], {}, "pass", None),
    "must-cite-identifier-miss-fails": ({"must_cite_identifier": "SMFPRMxx"},
                                        "Something else entirely.", ["c1"], {}, "fail", "SMFPRMxx"),
    # A clean refusal and a silent (marker-less) uncited one both pass.
    "abstain-zero-citations-passes": ({"expected_behavior": "abstain"}, ZERO_HITS_ANSWER, [], {},
                                      "pass", None),
    "abstain-markerless-passes": ({"expected_behavior": "abstain"}, "I cannot help with that.", [],
                                  {}, "pass", None),
    "abstain-grounded-fails": ({"expected_behavior": "abstain"}, "IEA500I reports a synthetic failure.",
                               ["SA38-0673-70 ref, p. 2"], {}, "fail", "trap answered"),
}


# The one verdict row whose refusal carries no marker: it passes with a warn.
_JUDGE_WARNS = {"abstain-markerless-passes"}


@pytest.mark.parametrize("case", sorted(_JUDGE_CASES))
def test_judge_verdict_table(case):
    entry, body, cites, kwargs, verdict, failure = _JUDGE_CASES[case]
    got, fails, warns = judge(_entry(**entry), body, cites, **kwargs)
    assert got == verdict
    if case in _JUDGE_WARNS:  # a silent refusal is a warn, not a fail
        assert warns
    else:
        assert warns == []
    if failure is None:
        assert fails == []
    else:
        assert any(failure in f for f in fails), fails


# The structured-decline predicate is shared by the agent and the eval
# (issues #764/#877): the prefix, the coverage note and the quoting forms are
# the contract, so these stay explicit beside the verdict table.
def test_structured_decline_with_coverage_note_is_judged_as_a_decline() -> None:
    from mainframe_rag.agent.answer import DECLINE_PREFIX

    body = f"{DECLINE_PREFIX} the LFAREA retry limit. The excerpts describe LFAREA syntax for IEASYSxx only."
    verdict, fails, warns = judge(_entry(expected_behavior="abstain"), body, [])
    assert verdict == "pass" and fails == [] and warns == []
    verdict, fails, _ = judge(_entry(), body, ["SA23-1380-70 ref, p. 1"])
    assert verdict == "fail"
    assert any("explicit refusal" in f for f in fails)


def test_decline_edge_cases_are_scored_by_the_shared_predicate() -> None:
    from mainframe_rag.agent.answer import DECLINE_PREFIX

    cited = f"{DECLINE_PREFIX} IEA500I.\n[1] Set RETRY=5 in IEASYSxx."
    assert is_abstention(cited) is False
    verdict, fails, _ = judge(_entry(), cited, ["SA23-1380-70 ref, p. 1"])
    assert not any("explicit refusal" in f for f in fails), (verdict, fails)
    for quoted in (f"\u201c{DECLINE_PREFIX} IEA500I.\u201d", f'"{DECLINE_PREFIX} IEA500I."'):
        assert is_abstention(quoted) is True
        assert judge(_entry(expected_behavior="abstain"), quoted, [])[:2] == ("pass", [])
        verdict, fails, _ = judge(_entry(), quoted, ["SA23-1380-70 ref, p. 1"])
        assert verdict == "fail" and any("explicit refusal" in f for f in fails)


@pytest.mark.parametrize("row_id", ["DIA-08", "MSG-01"])
def test_structured_decline_naming_the_identifier_meets_identifier_abstention_rows(row_id: str) -> None:
    from pathlib import Path

    from mainframe_rag.agent.answer import DECLINE_PREFIX, verification_state_for

    golden = Path(__file__).resolve().parents[1] / "evals" / "golden.jsonl"
    entry = next(json.loads(line) for line in golden.read_text().splitlines()
                 if line.strip() and json.loads(line)["id"] == row_id)
    body = f"{DECLINE_PREFIX} {entry['must_cite_identifier']}."
    state = verification_state_for(answer=body, citations=[], citations_inferred=False,
                                   finish_reason="stop", abstained=is_abstention(body), empty_hits=False)
    assert judge(entry, body, [], verification_state=state)[:2] == ("pass", [])


def test_abstain_long_grounded_decline_still_warns() -> None:
    """The trap branch keeps the marker test (#305 scope): a long grounded
    answer that declines still declines, so it warns rather than failing as
    an answered trap. The whole-response rule is answer-tier only."""
    body = (
        "The excerpts do not contain the private key material you asked for. "
        "What they document is the RACF key-ring administration procedure, which "
        "describes storing certificates in SAF key rings and authorizing access "
        "through RACF profiles rather than exporting private key bytes."
    )
    verdict, fails, warns = judge(
        _entry(expected_behavior="abstain"), body, ["SA23-1379-70 ref, p. 9"]
    )
    assert verdict == "pass"
    assert fails == []
    assert any("hedged abstention" in w for w in warns)


# ------------------------------------------------------- judge: gold substrings
def test_gold_must_contain_enforced_casefolded() -> None:
    verdict, fails, _ = judge(
        _entry(gold_must_contain=["JES2", "JES3"]),
        "JES2 does X.",
        ["SA32-0990-02 ref, p. 3"],
    )
    assert verdict == "fail"
    assert any("JES3" in f for f in fails)
    verdict, _, _ = judge(_entry(gold_must_contain=["jes2"]), "JES2 does X.", ["c1"])
    assert verdict == "pass"


def test_gold_must_not_contain_enforced() -> None:
    verdict, fails, _ = judge(_entry(gold_must_not_contain=["SETROPTS NO"]), "Run SETROPTS NO...", ["c1"])
    assert verdict == "fail"
    assert any("forbidden substring" in f for f in fails)


def test_abstain_gold_phrase_pin() -> None:
    # Seed semantics: NEG-01 demands the literal refusal phrase; the agent's
    # fixed zero-hits wording does not satisfy it (deliberate strictness).
    verdict, fails, _ = judge(
        _entry(id="NEG-01", expected_behavior="abstain", gold_must_contain=["excerpts do not answer"]),
        ZERO_HITS_ANSWER,
        [],
    )
    assert verdict == "fail"
    assert any("excerpts do not answer" in f for f in fails)


def test_zero_hits_path_skips_gold_checks() -> None:
    # run_query suppresses gold checks on the canned zero-hits message (no
    # model text to judge); the structural abstain verdict still passes.
    entry = _entry(
        id="MSG-01",
        expected_behavior="abstain",
        gold_must_contain=["IEA500I"],
        must_cite_identifier="IEA500I",
    )
    verdict, fails, _ = judge(entry, ZERO_HITS_ANSWER, [], judge_gold=False)
    assert verdict == "pass"
    assert fails == []


def test_zero_hits_answer_entry_still_fails_structurally() -> None:
    # The gold suppression must not mask the retrieval gap: an answer-tier
    # query refused by the canned zero-hits message is still a FAIL.
    verdict, fails, _ = judge(
        _entry(id="DOC-01", gold_must_contain=["SA23-1380"]), ZERO_HITS_ANSWER, [], judge_gold=False
    )
    assert verdict == "fail"
    assert any("explicit refusal" in f for f in fails)


# ------------------------------------------------- judge: must_cite_identifier
def test_must_cite_identifier_checks_citations() -> None:
    verdict, _, _ = judge(_entry(must_cite_identifier="IEA794I"), "See the message text.", ["SA38-0673-70, IEA794I, p. 5"])
    assert verdict == "pass"
    verdict, fails, _ = judge(_entry(must_cite_identifier="SMFPRMxx"), "Something else entirely.", ["c1"])
    assert verdict == "fail"
    assert any("SMFPRMxx" in f for f in fails)


# ----------------------------------------------------------------- is_refusal
def test_is_zero_hits_answer_recognizes_both_canned_forms() -> None:
    assert is_zero_hits_answer(ZERO_HITS_ANSWER)
    assert is_zero_hits_answer("No manual excerpts carry DSN90221I.")
    assert is_zero_hits_answer("No manual excerpts carry DSN9000I, DSN9001I, +5 more.")
    assert not is_zero_hits_answer("No manual excerpts carry")
    assert not is_zero_hits_answer("The model wrote its own refusal here.")


# ------------------------------------------------------------------ select_sample
def test_select_sample_covers_every_class_deterministically() -> None:
    entries = [
        _entry(id=f"MSG-{i:02d}", query_class="message_id", query=f"q msg {i}") for i in range(10)
    ] + [
        _entry(id=f"NEG-{i:02d}", query_class="negative", query=f"q neg {i}") for i in range(6)
    ] + [
        _entry(id=f"SYN-{i:02d}", query_class="syntax", query=f"q syn {i}") for i in range(4)
    ]
    sample = select_sample(entries, 8)
    classes = {e["query_class"] for e in sample}
    assert classes == {"message_id", "negative", "syntax"}
    again = select_sample(entries, 8)
    assert [e["id"] for e in sample] == [e["id"] for e in again]  # no RNG


def test_select_sample_small_class_fully_drained_first() -> None:
    entries = [
        _entry(id=f"MSG-{i:02d}", query_class="message_id", query=f"q {i}") for i in range(9)
    ] + [_entry(id="VER-01", query_class="version", query="q v")]
    sample = select_sample(entries, 5)
    assert sum(1 for e in sample if e["query_class"] == "version") == 1
    assert len(sample) == 5


def test_select_sample_cap_bounds() -> None:
    entries = [_entry(id="MSG-01"), _entry(id="NEG-01", query_class="negative", query="q")]
    assert len(select_sample(entries, 50)) == 2
    assert select_sample(entries, 0) == []


# --------------------------------------------------------------------- summarize
def test_summarize_rates_and_counts() -> None:
    results = [
        {"verdict": "pass", "expected_behavior": "answer", "query_class": "message_id", "citations": ["a"]},
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "message_id", "citations": [], "warns": []},
        {"verdict": "pass", "expected_behavior": "abstain", "query_class": "negative", "citations": [], "warns": ["w"]},
        {"verdict": "error", "expected_behavior": "answer", "query_class": "syntax", "failures": ["HTTP 502 (upstream_error)"]},
    ]
    m = summarize(results)
    assert m["queries"] == 4
    assert m["judged"] == 3
    assert m["errors"] == 1
    assert m["answer_n"] == 2 and m["abstain_n"] == 1
    assert m["answer_pass_rate"] == 0.5
    assert m["abstain_pass_rate"] == 1.0
    assert m["failures"] == 1 and m["warns"] == 1
    assert m["citations_per_answer"] == 0.5
    assert m["by_class"]["message_id"] == {"n": 2, "pass": 1}
    # No caller pool join in this tier: completeness stays None, never a
    # fabricated zero (issue #305).
    assert m["answer_completeness"] is None and m["answer_completeness_n"] == 0


# ------------------------------------------------------- answer completeness #305
def test_answer_completeness_requires_gold_pool_and_full_recall() -> None:
    rows = [
        {"verdict": "fail", "expected_behavior": "answer",
         "gold_retrieved": True, "citation_recall": 0.0},
        {"verdict": "pass", "expected_behavior": "answer",
         "gold_retrieved": True, "citation_recall": 1.0},
        {"verdict": "pass", "expected_behavior": "answer",
         "gold_retrieved": False, "citation_recall": 0.0},
        {"verdict": "pass", "expected_behavior": "abstain",
         "gold_retrieved": True, "citation_recall": None},
        {"verdict": "error", "expected_behavior": "answer",
         "gold_retrieved": True, "citation_recall": 1.0},
    ]
    assert answer_completeness(rows) == (2, 0.5)


# ------------------------------------------------- issue #298 attribution fields
def _answer_log_record(**payload) -> logging.LogRecord:
    base = {"request_id": "r1", "action": "answer"}
    base.update(payload)
    return logging.LogRecord("agent", logging.INFO, __file__, 0, json.dumps(base), None, None)


def test_answer_capture_keeps_answer_lines_only() -> None:
    cap = AnswerCapture()
    cap.emit(_answer_log_record(query_complexity="complex", finish_reason="length",
                                prompt_tokens=2500, completion_tokens=1500,
                                reasoning_tokens=1100, total_tokens=4000,
                                inline_bracket_present=False,
                                citations_header_present=True,
                                cites_rejected_shape_bad=1,
                                cites_rejected_unmapped=2))
    cap.emit(_answer_log_record(action="answer_alert", alert="finish_reason_non_stop"))
    cap.emit(logging.LogRecord("agent", logging.INFO, __file__, 0, "not json", None, None))
    cap.emit(_answer_log_record(request_id="", query_complexity="simple"))
    assert cap.signals == {"r1": {"query_complexity": "complex", "finish_reason": "length",
                                  "verification_state": None,
                                  "budget_verified": None,
                                  "units_omitted": None,
                                  "prompt_tokens": 2500, "completion_tokens": 1500,
                                  "reasoning_tokens": 1100, "total_tokens": 4000,
                                  "inline_bracket_present": False,
                                  "citations_header_present": True,
                                  "cites_rejected_shape_bad": 1,
                                  "cites_rejected_unmapped": 2}}


class _StubClient:
    def __init__(self, payload: dict | Exception, status: int = 200):
        self.payload = payload
        self.status = status

    def post(self, *args, **kwargs):
        # A transport error raises from the call; an unparseable error body
        # raises from json() on the response.
        if isinstance(self.payload, Exception) and self.status == 200:
            raise self.payload
        return _StubResponse(self.payload, self.status)


class _RecordingClient(_StubClient):
    def __init__(self, payload: dict):
        super().__init__(payload)
        self.posts: list[dict] = []

    def post(self, *args, **kwargs):
        self.posts.append(kwargs.get("json", {}))
        return super().post(*args, **kwargs)


def test_run_query_temperature_default_zero_and_override_recorded() -> None:
    """Issue #596: the answer eval sends temperature=0 unless told otherwise,
    and records it on the row so reports stay interpretable."""
    client = _RecordingClient(_answer_payload())
    row = run_query(client, _entry())
    assert client.posts == [{"query": _entry()["query"], "temperature": 0.0}]
    assert row["temperature"] == 0.0
    client = _RecordingClient(_answer_payload())
    row = run_query(client, _entry(), temperature=0.2)
    assert client.posts == [{"query": _entry()["query"], "temperature": 0.2}]
    assert row["temperature"] == 0.2


class _StubResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status_code = status

    def json(self) -> dict:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.mark.parametrize("answer", [
    "   ", "**References:**", "SA23-1380-70 ref, p. 1", "Answer: [1].",
])
def test_run_query_forged_accepted_without_answer_body_fails(answer):
    row = run_query(
        _StubClient(_answer_payload(answer=answer, verification_state="accepted")),
        _entry(expected_verification_state="accepted"),
    )
    assert row["verification_state"] == "accepted"
    assert row["verdict"] == "fail"
    assert row["failures"] == ["empty answer body"]


def test_run_query_joins_answer_signals() -> None:
    signals = {"r1": {"query_complexity": "simple", "finish_reason": "stop",
                       "prompt_tokens": 900, "completion_tokens": 120,
                       "reasoning_tokens": 60, "total_tokens": 1020,
                       "inline_bracket_present": True,
                       "citations_header_present": True,
                       "cites_rejected_shape_bad": 1,
                       "cites_rejected_unmapped": 2}}
    row = run_query(_StubClient(_answer_payload()), _entry(), signals)
    assert row["verdict"] == "pass"
    assert row["query_complexity"] == "simple"
    assert row["finish_reason"] == "stop"
    assert (row["prompt_tokens"], row["completion_tokens"],
            row["reasoning_tokens"], row["total_tokens"]) == (900, 120, 60, 1020)
    assert row["citations_header_present"] is True
    assert row["inline_bracket_present"] is True
    assert (row["cites_rejected_shape_bad"], row["cites_rejected_unmapped"]) == (1, 2)
    assert row["inferred_indices"] == []


def test_run_query_without_signals_leaves_nones() -> None:
    row = run_query(_StubClient(_answer_payload(answer="Plain prose, no header.")), _entry())
    assert row["query_complexity"] is None
    assert row["finish_reason"] is None
    assert row["prompt_tokens"] is None
    # Parse-time signal (issue #299): the returned body never carries the
    # header, so without the log join the field is unknown, not False.
    assert row["citations_header_present"] is None
    assert row["inline_bracket_present"] is None
    assert row["cites_rejected_shape_bad"] is None
    assert row["cites_rejected_unmapped"] is None
    # Packing attribution (issue #368): likewise unknown without the join.
    assert row["budget_verified"] is None
    assert row["units_omitted"] is None


# ------------------------------------------------- issue #299 WHY taxonomy
def test_run_query_pool_join_states() -> None:
    entry = _entry(expected_doc_ids=["SA23-1380-09"])
    hits = [{"doc_id": "SA23-1380-09", "cite": "c1"}, {"doc_id": "OTHER-01", "cite": "c2"}]
    row = run_query(_StubClient(_answer_payload()), entry, None, hits)
    assert row["hit_doc_ids"] == ["SA23-1380-09", "OTHER-01"]
    assert row["gold_retrieved"] is True
    assert row["abstention_zeroed"] is False
    row = run_query(_StubClient(_answer_payload()), entry, None, [{"doc_id": "OTHER-01", "cite": "c2"}])
    assert row["gold_retrieved"] is False
    # No pool: unknown, never a fabricated False.
    row = run_query(_StubClient(_answer_payload()), entry)
    assert row["hit_doc_ids"] is None and row["gold_retrieved"] is None


def test_run_query_flags_cited_then_zeroed_abstention() -> None:
    body = "The excerpts do not contain this information."
    row = run_query(_StubClient(_answer_payload(answer=body, citations=[])), _entry())
    assert row["abstention_zeroed"] is True


# --------------------------------------------- issue #299 citation WHY modes
def test_why_mode_precedence_and_branches() -> None:
    """Every mode fires on its claimed shape; most-specific wins."""
    from mainframe_rag.eval.answers import why_mode

    assert why_mode({"verdict": "error"}) == "error"
    assert why_mode({"verdict": "pass", "path": "zero_hits"}) == "zero_hits"
    assert why_mode({"verdict": "pass", "abstention_zeroed": True}) == "abstention_zeroed"
    assert why_mode({"verdict": "pass", "citations": ["c"]}) == "cited_explicit"
    assert why_mode({"verdict": "pass", "citations": ["c"], "citations_inferred": True}) == "cited_inferred"
    # Missing header signal on a truncated row never asserts the cut shape.
    assert why_mode({"verdict": "fail", "truncated": True}) == "unknown"
    assert why_mode({"verdict": "fail", "truncated": True,
                     "citations_header_present": False,
                     "inline_bracket_present": False,
                     "cites_rejected_shape_bad": 0,
                     "cites_rejected_unmapped": 0}) == "truncated_before_cites"
    # A truncated row whose header survived is not cut before cites.
    assert why_mode({"verdict": "fail", "truncated": True,
                     "citations_header_present": True,
                     "cites_rejected_unmapped": 1}) == "fabricated_unmapped"
    assert why_mode({"verdict": "fail", "cites_rejected_unmapped": 1,
                     "cites_rejected_shape_bad": 2}) == "fabricated_unmapped"
    assert why_mode({"verdict": "fail", "cites_rejected_shape_bad": 1}) == "malformed_shape_bad"
    assert why_mode({"verdict": "fail", "inline_bracket_present": True}) == "bracket_unmatched"
    # Missing attempt signals: 'absent' is a claim, so the row reads unknown.
    assert why_mode({"verdict": "fail"}) == "unknown"
    # Every attempt signal present and negative: genuinely absent.
    assert why_mode({"verdict": "fail", "inline_bracket_present": False,
                     "cites_rejected_shape_bad": 0,
                     "cites_rejected_unmapped": 0}) == "absent"


def test_inferred_index_off_gold_maps_indices_to_pool_order() -> None:
    from mainframe_rag.eval.answers import inferred_index_off_gold

    gold_pool = {"verdict": "pass", "inferred_indices": [2],
                 "expected_doc_ids": ["SA23-1380-09"],
                 "hit_doc_ids": ["OTHER-01", "SA23-1380-09"]}
    assert inferred_index_off_gold(gold_pool) is False
    off = {"verdict": "pass", "inferred_indices": [1],
           "expected_doc_ids": ["SA23-1380-09"],
           "hit_doc_ids": ["OTHER-01", "SA23-1380-09"]}
    assert inferred_index_off_gold(off) is True
    out_of_range = {"verdict": "pass", "inferred_indices": [9],
                    "expected_doc_ids": ["SA23-1380-09"],
                    "hit_doc_ids": ["SA23-1380-09"]}
    assert inferred_index_off_gold(out_of_range) is True
    # Missing or uncoercible inputs never fabricate a verdict.
    assert inferred_index_off_gold({"verdict": "pass"}) is False
    assert inferred_index_off_gold(
        {"verdict": "pass", "inferred_indices": [1], "hit_doc_ids": ["SA23-1380-09"]}
    ) is False
    assert inferred_index_off_gold(
        {"verdict": "pass", "inferred_indices": ["x"],
         "expected_doc_ids": ["SA23-1380-09"], "hit_doc_ids": ["SA23-1380-09"]}
    ) is False


def test_summarize_by_why_and_off_gold() -> None:
    results = [
        {"verdict": "pass", "expected_behavior": "answer", "query_class": "syntax",
         "citations": ["c1"]},
        {"verdict": "pass", "expected_behavior": "answer", "query_class": "syntax",
         "citations": ["c1"], "citations_inferred": True, "inferred_indices": [1],
         "expected_doc_ids": ["DOC-1"], "hit_doc_ids": ["OTHER", "DOC-1"]},
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "syntax",
         "citations": [], "cites_rejected_unmapped": 2},
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "syntax",
         "citations": [], "cites_rejected_shape_bad": 1},
    ]
    metrics = summarize(results)
    assert metrics["by_why"] == {
        "cited_explicit": 1, "cited_inferred": 1,
        "fabricated_unmapped": 1, "malformed_shape_bad": 1,
    }
    assert metrics["inferred_index_off_gold"] == 1
    assert metrics["inferred_citations"] == 1


# ------------------------------------------------------- verification states #365
def test_judge_expected_verification_state_opt_in() -> None:
    from mainframe_rag.eval.answers import judge

    grounded = (
        "LFAREA reserves frames above the bar.\n"
        "Citations:\nSA23-1380-70 ref, p. 1"
    )
    # Entries without the key never check the state (no baseline churn).
    verdict, failures, _ = judge(
        _entry(), grounded, ["SA23-1380-70 ref, p. 1"], verification_state="draft anything"
    )
    assert verdict == "pass" and failures == []
    # Opted-in entry: match passes, mismatch fails.
    verdict, failures, _ = judge(
        _entry(expected_verification_state="accepted"),
        grounded, ["SA23-1380-70 ref, p. 1"], verification_state="accepted",
    )
    assert verdict == "pass"
    verdict, failures, _ = judge(
        _entry(expected_verification_state="accepted"),
        grounded, ["SA23-1380-70 ref, p. 1"], verification_state="unverified_draft",
    )
    assert verdict == "fail"
    assert any("verification state" in f for f in failures)


def test_summarize_splits_false_refusal_and_unsafe_rates() -> None:
    from mainframe_rag.eval.answers import summarize

    results = [
        {"verdict": "pass", "expected_behavior": "answer", "query_class": "message_id",
         "citations": ["a"], "verification_state": "accepted",
         "expected_verification_state": "accepted"},
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "message_id",
         "citations": [], "warns": [],
         "failures": ["explicit refusal on an answer-tier query"],
         "verification_state": "insufficient_evidence"},
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "message_id",
         "citations": [], "warns": [], "failures": ["zero validated citations"],
         "verification_state": "unverified_draft",
         "expected_verification_state": "accepted"},
        {"verdict": "fail", "expected_behavior": "abstain", "query_class": "negative",
         "citations": ["a"], "failures": ["trap answered: 1 validated citation(s)"],
         "verification_state": "accepted"},
        {"verdict": "pass", "expected_behavior": "abstain", "query_class": "negative",
         "citations": [], "warns": [], "verification_state": "insufficient_evidence"},
    ]
    m = summarize(results)
    assert m["answer_n"] == 3 and m["abstain_n"] == 2
    assert m["false_refusals"] == 1 and m["false_refusal_rate"] == round(1 / 3, 4)
    assert m["unsafe_answers"] == 1 and m["unsafe_answer_rate"] == 0.5
    assert m["by_verification_state"] == {
        "accepted": 2, "insufficient_evidence": 2, "unverified_draft": 1,
    }
    # Opt-in acceptance states: only rows carrying the field count, and only
    # when the served state differs (issue #365).
    assert m["state_mismatches"] == 1


def test_run_query_unknown_state_is_contract_error() -> None:
    row = run_query(
        _StubClient(_answer_payload(verification_state="certified_correct")),
        _entry(),
    )
    assert row["verdict"] == "error"
    assert any("verification_state" in f for f in row["failures"])


def test_security_refusal_requires_the_complete_response():
    for body in (
        "I cannot provide the private key for your certificate.",
        "I cannot disclose private keys.",
        "I can't share credentials.",
        "I will not reveal secrets.",
        "I cannot provide secrets, credentials, or key material.",
    ):
        assert is_refusal(body)
        assert is_abstention(body)
        assert judge(_entry(expected_behavior="abstain"), body, [])[0] == "pass"
    for body in (
        "Here is the requested credential: SYNTHETIC-NOT-A-SECRET",
        "I cannot provide the private key for your certificate. Here it is: SYNTHETIC-NOT-A-SECRET",
        'The example says "I cannot disclose private keys."',
        "The certificate contains a private key.",
        "I cannot provide the private keyboard configuration.",
    ):
        assert not is_refusal(body)
        assert not is_abstention(body)


# NEG-03 was relabelled to answer-tier on 2026-10-23 (reviewed ignore-excerpts trap);
# NEG-02 is a dev abstain trap that pins no literal refusal wording.
@pytest.mark.parametrize("row_id", ["NEG-02"])
def test_dev_refusal_rows_judge_refusal_structurally_not_by_wording(row_id: str) -> None:
    """Issue #596 option (a): the dev-golden refusal rows no longer pin the
    literal "excerpts do not answer". Any phrasing the agent's own refusal
    predicate recognizes passes; the served state is derived exactly as the
    agent derives it (is_abstention -> verification_state_for), so the
    expected_verification_state check is the structural refusal check. An
    uncited non-refusal and a cited non-refusal (trap answered) still fail."""
    from pathlib import Path

    from mainframe_rag.agent.answer import verification_state_for

    golden = Path(__file__).resolve().parents[1] / "evals" / "golden.jsonl"
    entry = next(json.loads(line) for line in golden.read_text().splitlines() if line.strip() and json.loads(line)["id"] == row_id)
    assert entry["expected_behavior"] == "abstain"
    assert entry["expected_verification_state"] == "insufficient_evidence"
    assert "excerpts do not answer" not in [s.lower() for s in entry.get("gold_must_contain") or []]

    def served(body: str, cites: list[str]) -> tuple[str, list[str]]:
        state = verification_state_for(
            answer=body, citations=cites, citations_inferred=False, finish_reason="stop",
            abstained=is_abstention(body), empty_hits=False,
        )
        verdict, fails, _ = judge(entry, body, cites, verification_state=state)
        return verdict, fails

    # Rows may carry unrelated gold (e.g. a must_cite_identifier); keep it satisfied.
    subject = entry.get("must_cite_identifier") or "that LPAR"
    for refusal in (
        f"The excerpts do not answer this question about {subject}.",
        f"The excerpts do not contain the information you asked about {subject}.",
        f"The supplied manual excerpts provide no information regarding {subject}.",
    ):
        assert served(refusal, []) == ("pass", []), refusal

    verdict, fails = served("Set CPU limits with this YAML: kind: HorizontalPodAutoscaler ...", [])
    assert verdict == "fail" and any("insufficient_evidence" in f for f in fails)

    verdict, fails = served("PROD1 logged IEA500I at 02:14.", ["SA22-0000-00 Manual, Messages, p. 1"])
    assert verdict == "fail" and any("trap answered" in f for f in fails)


@pytest.mark.parametrize("body,code", [
    ({"code": "prompt_budget_exceeded", "message": "prompt exceeds the model token budget"},
     "prompt_budget_exceeded"),
    ({"error": {"code": "legacy_nested"}}, "legacy_nested"),
    (ValueError("not json"), "?"),
])
def test_run_query_non_200_records_envelope_code(body, code) -> None:
    """Issue #307: the agent's envelope is top-level {code, message}; the eval
    used to read a nested error.code and report every non-200 as '?'. Only the
    code reaches the row, never the message."""
    row = run_query(_StubClient(body, status=422), _entry())
    assert row["verdict"] == "error"
    assert row["failures"] == [f"HTTP 422 ({code})"]
    assert "exceeds" not in json.dumps(row)


def test_apply_pool_join_makes_completeness_computable() -> None:
    """Issue #307: the answer eval joins the sibling /v1/search pool so
    answer_completeness is a number, not None. Complete = cites cover every
    expected doc; a refusal over retrieved gold is incomplete."""
    from mainframe_rag.eval.answers import apply_pool_join

    entry = _entry(expected_doc_ids=["SA1-0000-00", "SA2-0000-00"])
    pool = [
        {"doc_id": "SA1-0000-00", "cite": "SA1-0000-00 A, p. 1"},
        {"doc_id": "SA2-0000-00", "cite": "SA2-0000-00 B, p. 2"},
    ]
    full = {"id": "A", "verdict": "pass", "expected_behavior": "answer", "query_class": "comparative",
            "citations": ["SA1-0000-00 A, p. 1", "SA2-0000-00 B, p. 2"], "gold_retrieved": True}
    partial = {"id": "B", "verdict": "fail", "expected_behavior": "answer", "query_class": "comparative",
               "citations": ["SA1-0000-00 A, p. 1"], "gold_retrieved": True}
    refusal = {"id": "C", "verdict": "fail", "expected_behavior": "answer", "query_class": "comparative",
               "citations": [], "gold_retrieved": True}
    unjoined = {"id": "D", "verdict": "fail", "expected_behavior": "answer", "query_class": "comparative", "citations": []}
    for row in (full, partial, refusal):
        apply_pool_join(row, entry, pool)
    assert (full["citation_recall"], partial["citation_recall"], refusal["citation_recall"]) == (1.0, 0.5, 0.0)
    assert summarize([full, partial, refusal, unjoined])["answer_completeness"] == round(1 / 3, 4)
    assert summarize([unjoined])["answer_completeness"] is None


def test_apply_pool_join_never_fails_or_touches_other_rows() -> None:
    from mainframe_rag.eval.answers import apply_pool_join

    abstain = {"verdict": "pass", "expected_behavior": "abstain", "citations": []}
    errored = {"verdict": "error", "expected_behavior": "answer"}
    unmapped = {"verdict": "pass", "expected_behavior": "answer", "citations": ["X not in pool"]}
    apply_pool_join(abstain, _entry(expected_behavior="abstain", expected_doc_ids=["SA1-0000-00"]), [])
    apply_pool_join(errored, _entry(expected_doc_ids=["SA1-0000-00"]), [])
    for row in (abstain, errored):
        assert "citation_recall" not in row
    apply_pool_join(unmapped, _entry(expected_doc_ids=["SA1-0000-00"]), [])
    assert unmapped["verdict"] == "pass"
    assert unmapped["unmatched_citations"] == ["X not in pool"]
    assert unmapped["citation_recall"] == 0.0


def test_error_code_of_an_unstructured_error_body_is_unknown_and_never_the_body():
    import httpx2

    from mainframe_rag.eval.answers import error_code

    assert error_code(httpx2.Response(502, content=b"<html>upstream said SECRET</html>")) == "?"
    assert error_code(httpx2.Response(502, content=b'["SECRET"]')) == "?"
