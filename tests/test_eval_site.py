"""Site measurement for the answer eval (issue #1191): the `/v1/chat`
endpoint, served-corpus coverage, the text-free export, and the RC client
reading the 2cd8f67d agent's wire.

The CLI runs over an inert HTTP boundary (`httpx2.MockTransport`) whose
chat frames come from the agent's own SSE formatters. Two tests drive the
real app in-process: the chat/answer parity check and nothing else. The
2cd8f67d shapes in tests/data/site_eval_2cd8f67d.json were captured from that
commit's real app with its own test doubles (synthetic data only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mainframe_rag.agent.sse import format_openai_chunk, format_openai_done, format_openai_error
from mainframe_rag.eval.follow_up import DEFAULT_FOLLOW_UP, FOLLOW_UPS

HEALTH = {"status": "ok", "qdrant": True, "embed": True,
          "representation": "compatible", "rerank": None}
CITE = "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"
CAPTURE = json.loads((Path(__file__).parent / "data" / "site_eval_2cd8f67d.json").read_text())


def _entry(**overrides) -> dict:
    base = {"id": "MSG-01", "query": "What does IEA500I report?", "query_class": "message_id",
            "expected_behavior": "answer", "expected_doc_ids": ["SA22-0000-00"],
            "expected_verification_state": "accepted"}
    base.update(overrides)
    return base


def _supplied(cite: str = CITE, doc_id: str = "SA22-0000-00", text: str = "excerpt") -> dict:
    return {"index": 1, "citation": cite, "doc_id": doc_id, "title": "Synthetic Reference",
            "heading": "Chapter 2 > IEA500I", "page_label": "1-6", "text": text}


def chat_stream(*deltas: str, citations=(CITE,), finish="stop", done=True, state="accepted",
                supplied=None, hits=None, inferred=False, indices=()) -> bytes:
    """A /v1/chat SSE body in the agent's own frame format: content deltas,
    the validated citations delta, the finish chunk with the metadata, [DONE]."""
    frames = [format_openai_chunk("chatcmpl-abc", "m", delta_content=d) for d in deltas]
    if citations:
        frames.append(format_openai_chunk(
            "chatcmpl-abc", "m",
            delta_content="\n\n**Citations:**\n" + "\n".join(f"- {c}" for c in citations)))
    if finish is not None:
        frames.append(format_openai_chunk("chatcmpl-abc", "m", finish_reason=finish, extra={
            "citations": list(citations), "citations_inferred": inferred,
            "inferred_indices": list(indices), "hits": hits or [],
            "verification_state": state, "script": None, "script_lang": None,
            "script_review_required": False,
            "supplied_evidence": [_supplied()] if supplied is None else supplied,
            "evidence_omitted": 0}))
    if done:
        frames.append(format_openai_done())
    return "".join(frames).encode()


def _answer_final(**overrides) -> bytes:
    final = {"type": "final", "request_id": "r1", "answer": "IEA500I reports a rejected command.",
             "citations": [CITE], "citations_inferred": False, "inferred_indices": [],
             "verification_state": "accepted", "script": None, "script_review_required": False,
             "hits": [], "finish_reason": "stop", "ttft_ms": 40,
             "usage": {"prompt_tokens": 900, "completion_tokens": 300,
                       "reasoning_tokens": 100, "total_tokens": 1200}}
    final.update(overrides)
    return f"event: final\ndata: {json.dumps(final)}\n\n".encode()


def _sse(body: bytes):
    import httpx2

    return httpx2.Response(200, content=body, headers={"Content-Type": "text/event-stream"})


@pytest.fixture
def site_eval(tmp_path, monkeypatch):
    """The real CLI over an inert HTTP boundary; returns status, report,
    export bytes and the recorded requests."""
    import httpx2

    from mainframe_rag.eval import answers

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(answers, "load_settings",
                        lambda: pytest.fail("remote eval touched local Settings"))
    real_client = httpx2.Client

    def run(respond, entries, extra=()):
        calls: list = []

        def record(request):
            calls.append(request)
            return respond(request)

        monkeypatch.setattr(httpx2, "Client", lambda **kw: real_client(
            transport=httpx2.MockTransport(record), **kw))
        golden = tmp_path / "original.jsonl"
        golden.write_text("".join(json.dumps(e) + "\n" for e in entries))
        for name in ("report.json", "export.json"):
            (tmp_path / name).unlink(missing_ok=True)
        status = answers.main([
            "--agent-url", "http://deployed-agent:8087", "--golden", str(golden), "--all",
            "--out", str(tmp_path / "report.json"), "--export", str(tmp_path / "export.json"),
            *extra,
        ])
        report = json.loads((tmp_path / "report.json").read_text())
        export = (tmp_path / "export.json").read_bytes()
        return status, report, export, calls

    return run


# ------------------------------------------------------------- /v1/chat


def test_chat_endpoint_needs_the_stream(site_eval):
    with pytest.raises(SystemExit) as exc:
        site_eval(lambda r: pytest.fail("no request"), [_entry()], ["--endpoint", "chat"])
    assert exc.value.code == 2


def test_chat_stream_is_judged_from_its_finish_chunk(site_eval):
    """The Pipe's request shape; the answer body is the streamed prose
    without the model's own citations block or the validated list; counts
    the chat stream does not carry stay unknown, never zero."""
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": [{"doc_id": "SA22-0000-00"}]})
        assert request.url.path == "/v1/chat"
        return _sse(chat_stream("IEA500I reports a rejected command.\n\n",
                                f"Citations:\n- {CITE}\n"))

    status, report, _, calls = site_eval(respond, [_entry()], ["--endpoint", "chat", "--stream"])
    assert status == 0
    assert json.loads(calls[-1].content) == {
        "messages": [{"role": "user", "content": "What does IEA500I report?"}],
        "stream": True, "temperature": 0.0}
    row = report["results"][0]
    assert row["verdict"] == "pass", row["failures"]
    assert row["answer"] == "IEA500I reports a rejected command."
    assert row["citations"] == [CITE] and row["verification_state"] == "accepted"
    assert row["finish_reason"] == "stop" and row["path"] == "llm"
    assert row["request_id"] == "abc"
    assert (row["prompt_tokens"], row["completion_tokens"], row["reasoning_tokens"],
            row["total_tokens"], row["ttft_ms"], row["decode_tps"]) == (None,) * 6
    assert isinstance(row["client_first_token_ms"], int)
    assert row["gold_retrieved"] is True
    assert report["params"]["endpoint"] == "chat" and report["params"]["transport"] == "sse"
    tokens = report["metrics"]["measurement"]["hardware_independent"]["tokens"]
    assert tokens["prompt_tokens"]["n"] == 0
    assert report["metrics"]["measurement"]["hardware_independent"]["truncation"]["n"] == 1


def test_chat_zero_hits_stream_is_the_zero_hits_path(site_eval):
    import httpx2

    canned = "No supporting manual excerpts were found for this question."

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": []})
        return _sse(chat_stream(canned, citations=(), state="insufficient_evidence", supplied=[]))

    status, report, _, _ = site_eval(
        respond, [_entry(expected_behavior="abstain", query_class="negative",
                         expected_verification_state="insufficient_evidence")],
        ["--endpoint", "chat", "--stream"])
    row = report["results"][0]
    assert status == 0 and row["verdict"] == "pass" and row["path"] == "zero_hits"


@pytest.mark.parametrize(("body", "failure"), [
    (chat_stream("partial", citations=(), finish=None, done=False).decode()
     + format_openai_error() + format_openai_done(), "stream error (upstream_error)"),
    (chat_stream("partial", citations=(), finish=None, done=False).decode()
     + format_openai_error("model_timeout", "x") + format_openai_done(), "stream error (model_timeout)"),
    (format_openai_error("Bearer sk-not-a-code", "x"), "stream error (?)"),
    (chat_stream("partial", done=False).decode(), "stream ended without final"),
    (chat_stream("partial", citations=(), finish=None).decode(), "stream ended without final"),
    ("data: {not json\n\n", "malformed answer stream"),
    ('data: {"choices": []}\n\n', "malformed answer stream"),
    (chat_stream("x", supplied=[{"index": "one"}]).decode(), "malformed answer stream"),
], ids=["stream-error-upstream", "stream-error-model-timeout", "stream-error-unknown-code",
        "finish-without-done", "done-without-finish", "malformed-json", "empty-choices",
        "malformed-supplied-evidence"])
def test_chat_stream_failures_are_error_rows_and_the_next_case_runs(site_eval, body, failure):
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": []})
        content = json.loads(request.content)["messages"][0]["content"]
        if content == "first":
            return _sse(body.encode())
        return _sse(chat_stream("IEA500I reports a rejected command."))

    status, report, _, _ = site_eval(
        respond, [_entry(id="A", query="first"), _entry(id="B", query="second")],
        ["--endpoint", "chat", "--stream"])
    first, second = report["results"]
    assert status == 1
    assert first["verdict"] == "error" and first["failures"] == [failure]
    assert second["verdict"] == "pass"


MODEL_OUTPUTS = {
    "explicit": f"IEA500I reports a rejected command.\n\nCitations:\n- {CITE}\n",
    "inferred": "IEA500I reports a rejected command [1].",
    "declined_with_cite": f"The excerpts do not answer this question.\n\nCitations:\n- {CITE}\n",
    "script": (f"Reissue it:\n```jcl\n//A EXEC PGM=ONE\n```\nThen check the log.\n\n"
               f"Sources:\n{CITE}\n"),
    "unvalidated_cite": "IEA500I reports a rejected command.\n\nCitations:\n- ZZ99-0000-00 Other, p. 9\n",
}


@pytest.mark.parametrize("output", sorted(MODEL_OUTPUTS))
def test_chat_and_answer_rows_agree_on_the_same_model_output(
    monkeypatch, servable_representation_gate, output,
):
    """Parity through the real app: one model output streamed by /v1/answer
    and by /v1/chat gives the same judged body, citations, state, finish
    reason and verdict, so the two endpoints measure the same answer."""
    from mainframe_rag.eval.answers import run_query
    from mainframe_rag.ports import TokenUsage
    from tests.helpers_agent_local import StreamStubLLM, agent_client, agent_hit

    hit = agent_hit(text="IEA500I reports a rejected command.")
    llm = StreamStubLLM((
        {"type": "token", "delta": MODEL_OUTPUTS[output], "ttft_ms": 5},
        {"type": "done", "finish_reason": "stop", "ttft_ms": 5,
         "usage": TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)},
    ))

    def search(*args, **kwargs):
        return [hit], "identifier", {"embed_ms": 1, "qdrant_ms": 1}

    keys = ("answer", "citations", "citations_inferred", "inferred_indices", "verification_state",
            "finish_reason", "path", "verdict", "failures", "warns", "abstention_zeroed")
    with agent_client(monkeypatch, llm=llm, search=search) as client:
        answer = run_query(client, _entry(), stream=True)
        chat = run_query(client, _entry(), stream=True, endpoint="chat")
    assert answer["verdict"] != "error", answer["failures"]
    assert {k: chat[k] for k in keys} == {k: answer[k] for k in keys}



@pytest.mark.parametrize(("condense", "gold_hit"), [
    pytest.param("false", False, id="off"), pytest.param("true", True, id="on")])
def test_two_turn_evidence_follows_the_real_agents_condense_setting(
    monkeypatch, servable_representation_gate, condense, gold_hit,
):
    """Through the real app (#1268): the agent accepts the Pipe-shaped turn-2
    body, and the follow-up's evidence hit moves with CHAT_CONDENSE_ENABLED
    because it is read from what the agent packed. The search double returns
    the gold doc only for a query that names the message id; the model double
    condenses the follow-up into such a query."""
    from mainframe_rag.eval.answers import run_follow_up, run_query
    from mainframe_rag.eval.follow_up import load_pipe
    from mainframe_rag.ports import ChatResult, TokenUsage
    from tests.helpers_agent_local import StreamStubLLM, agent_client, agent_hit

    gold = agent_hit(text="IEA500I reports a rejected command.")
    other = agent_hit(text="Generic recovery steps.", doc_id=OTHER_DOC, chunk_id="def456",
                      heading="Chapter 9 > Recovery")
    searched: list[str] = []

    def search(_qdrant, _embedder, _collection, query, **kwargs):
        searched.append(query)
        return [gold if "IEA500I" in query else other], "identifier", {"embed_ms": 1, "qdrant_ms": 1}

    class CondensingLLM(StreamStubLLM):
        def chat(self, *args, **kwargs):
            return ChatResult(content="IEA500I recovery procedure")

    llm = CondensingLLM((
        {"type": "token", "delta": "The command was rejected [1].", "ttft_ms": 5},
        {"type": "done", "finish_reason": "stop", "ttft_ms": 5,
         "usage": TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)},
    ))
    frames: list[str] = []
    with agent_client(monkeypatch, llm=llm, search=search,
                      env={"CHAT_CONDENSE_ENABLED": condense}) as client:
        opener = run_query(client, _entry(), stream=True, endpoint="chat", frames=frames,
                           evidence=True)
        follow = run_follow_up(client, _entry(), opener, frames, load_pipe()[0], 0.0)
    assert opener["verdict"] != "error" and follow["verdict"] != "error", follow["failures"]
    assert opener["evidence_gold_hit"] is True
    assert follow["evidence_gold_hit"] is gold_hit
    assert follow["evidence_doc_ids"] == (["SA22-0000-00"] if gold_hit else [OTHER_DOC])
    assert searched == [_entry()["query"], "IEA500I recovery procedure" if gold_hit
                        else FOLLOW_UPS["message_id"]]
    assert follow["prior_answer_chars"] > 0


# ------------------------------------------------------------- coverage


def _sources_page(doc_ids, cursor=None, build="b-1", title="Synthetic Guide"):
    return {"request_id": "x", "build_id": build, "next_cursor": cursor,
            "sources": [{"doc_id": d, "title": title, "source_revision": f"r|{d}"} for d in doc_ids]}


def _coverage_agent(sources):
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/sources":
            return sources(request)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": []})
        return _sse(_answer_final())

    return respond


COVERAGE_ENTRIES = [
    _entry(id="ALL", expected_doc_ids=["A-1", "B-1"]),
    _entry(id="SOME", expected_doc_ids=["A-1", "C-1"]),
    _entry(id="NONE", expected_doc_ids=["C-1"]),
    _entry(id="NA", expected_doc_ids=[], expected_behavior="abstain", query_class="negative",
           expected_verification_state=None),
]


def test_coverage_pages_the_served_sources_and_marks_each_row(site_eval):
    import httpx2

    def sources(request):
        cursor = request.url.params.get("cursor")
        assert request.url.params["limit"] == "200"
        if cursor is None:
            return httpx2.Response(200, json=_sources_page(["A-1"], cursor="ls1.next"))
        assert cursor == "ls1.next"
        return httpx2.Response(200, json=_sources_page(["B-1", "A-1"]))

    status, report, _, calls = site_eval(
        _coverage_agent(sources), COVERAGE_ENTRIES, ["--stream", "--coverage"])
    assert [r["coverage"] for r in report["results"]] == ["all", "some", "none", "not_applicable"]
    assert [r["expected_served"] for r in report["results"]] == [2, 1, 0, 0]
    assert report["coverage"] == {
        "status": "known", "reason": None, "build_id": "b-1", "build_id_end": "b-1",
        "pages": 2, "sources_n": 2,
        "rows": {"all": 1, "some": 1, "none": 1, "not_applicable": 1, "unknown": 0}}
    assert report["params"]["coverage"] is True
    # coverage never changes a verdict or the exit code
    assert [r["verdict"] for r in report["results"]][:3] == ["pass"] * 3
    assert status == 1  # the abstain row answers with citations: a real fail, not coverage
    paths = [c.url.path for c in calls]
    assert paths[:3] == ["/healthz", "/v1/sources", "/v1/sources"]
    assert paths[-1] == "/v1/sources"  # the end-of-run build check


def _unknown(kind):
    import httpx2

    def sources(request):
        if kind == "503":
            return httpx2.Response(503, json={"code": "sources_unavailable", "message": "m"})
        if kind == "404":
            return httpx2.Response(404, json={"code": "not_found", "message": "m"})
        if kind == "malformed":
            return httpx2.Response(200, json={"sources": "nope"})
        if kind == "transport":
            raise httpx2.ConnectError("refused")
        if kind == "build_changed":
            cursor = request.url.params.get("cursor")
            if cursor is None:
                return httpx2.Response(200, json=_sources_page(["A-1"], cursor="c2", build="b-1"))
            return httpx2.Response(200, json=_sources_page(["B-1"], build="b-2"))
        raise AssertionError(kind)

    return sources


@pytest.mark.parametrize(("kind", "reason"), [
    ("503", "http_503"), ("404", "http_404"), ("malformed", "malformed"),
    ("transport", "request_failed"), ("build_changed", "build_changed"),
])
def test_coverage_unknown_is_never_a_failure(site_eval, kind, reason):
    status, report, _, _ = site_eval(
        _coverage_agent(_unknown(kind)), [_entry(expected_doc_ids=["A-1"])],
        ["--stream", "--coverage"])
    assert status == 0
    assert report["results"][0]["verdict"] == "pass"
    assert report["results"][0]["coverage"] == "unknown"
    assert report["coverage"]["status"] == "unknown" and report["coverage"]["reason"] == reason
    assert report["coverage"]["rows"]["unknown"] == 1


def test_coverage_is_off_by_default(site_eval):
    import httpx2

    status, report, _, calls = site_eval(
        _coverage_agent(lambda r: httpx2.Response(500)), [_entry()], ["--stream"])
    assert status == 0 and "/v1/sources" not in [c.url.path for c in calls]
    assert "coverage" not in report and "coverage" not in report["results"][0]


# ------------------------------------------------------------- export

CANARIES = {
    "query": "CANARY-QUERY-7f3a",
    "gold": "CANARY 'GOLD': (9c1e)",
    "gold_not": "CANARY-FORBIDDEN-55d0",
    "ident": "CANARYIDENT42",
    "answer": "CANARY-ANSWER-b812",
    "cite": "CANARY-CITE-0a9d",
    "excerpt": "CANARY-EXCERPT-e4c7",
    "delta": "CANARY-DELTA-31f0",
    "finish": "canary_finish_x",
    "code": "canary_code_x",
    "title": "CANARY-TITLE-6b2b",
    "build": "CANARY BUILD (77aa)",
    "script": "CANARY-SCRIPT-d00d",
    "heading": "CANARY-HEADING-1c1c",
}

EXPORT_ROW_KEYS = {
    "id", "repeat", "query_class", "expected_behavior", "verdict", "failures", "warns",
    "verification_state", "expected_verification_state", "finish_reason", "path", "why",
    "citations_n", "citations_inferred", "inferred_n", "script_review_required",
    "abstention_zeroed", "prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens",
    "elapsed_ms", "ttft_ms", "llm_ms", "client_first_token_ms", "decode_tps", "gold_retrieved",
    "expected_doc_ids", "hit_doc_ids", "cited_doc_ids", "citation_precision", "citation_recall",
    "coverage", "expected_served",
}


def _canary_entries():
    c = CANARIES
    return [
        _entry(id="PASS", query=f"{c['query']} one", gold_must_contain=["rejected"],
               must_cite_identifier="IEA500I"),
        _entry(id="FAIL", query=f"{c['query']} two", gold_must_contain=[c["gold"]],
               gold_must_not_contain=[c["gold_not"]], must_cite_identifier=c["ident"]),
        _entry(id="HTTP", query=f"{c['query']} three"),
        _entry(id="STREAM", query=f"{c['query']} four"),
        _entry(id="ODD", query=f"{c['query']} five"),
    ]


def _canary_agent(endpoint):
    import httpx2

    c = CANARIES
    cite = f"SA22-0000-00 {c['cite']}, {c['heading']}, p. 1"
    hits = [{"doc_id": "SA22-0000-00", "text": c["excerpt"], "cite": cite, "heading": c["heading"]}]

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/sources":
            return httpx2.Response(200, json=_sources_page(["SA22-0000-00"], build=c["build"],
                                                           title=c["title"]))
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": hits})
        body = json.loads(request.content)
        query = body["messages"][0]["content"] if endpoint == "chat" else body["query"]
        answer = f"IEA500I {c['answer']} {c['gold_not']} rejected."
        if query.endswith("three"):
            return httpx2.Response(503, json={"code": c["code"], "message": c["answer"]})
        if query.endswith("four"):
            if endpoint == "chat":
                return _sse((format_openai_chunk("x", "m", delta_content=c["delta"])
                             + format_openai_error(c["code"], c["answer"])
                             + format_openai_done()).encode())
            return _sse(f"event: token\ndata: {json.dumps({'delta': c['delta']})}\n\n"
                        f"event: error\ndata: {json.dumps({'code': c['code']})}\n\n".encode())
        finish = c["finish"] if query.endswith("five") else "stop"
        if endpoint == "chat":
            return _sse(chat_stream(c["delta"] + " " + answer + f"\n```rexx\n{c['script']}\n```\n",
                                    citations=(cite,), finish=finish, hits=hits,
                                    supplied=[_supplied(cite=cite, text=c["excerpt"])]))
        return _sse(_answer_final(answer=answer, citations=[cite], finish_reason=finish,
                                  script=c["script"], hits=hits))

    return respond


@pytest.mark.parametrize("endpoint", ["answer", "chat"])
def test_export_carries_no_question_answer_citation_or_failure_text(site_eval, endpoint):
    _, report, export, _ = site_eval(
        _canary_agent(endpoint), _canary_entries(),
        ["--endpoint", endpoint, "--stream", "--coverage", "--repeats", "2"])
    raw = json.dumps(report)
    # The local report does hold the text: the canaries are live.
    assert CANARIES["query"] in raw and CANARIES["answer"] in raw and CANARIES["gold"] in raw
    text = export.decode("utf-8")
    for name, canary in CANARIES.items():
        assert canary not in text, name
        assert canary.lower() not in text.lower(), name
    data = json.loads(text)
    assert set(data) == {"schema", "timestamp", "evaluator_git_sha", "params", "metrics",
                         "healthz", "representation", "coverage", "rows"}
    rows = {(r["id"], r["repeat"]): r for r in data["rows"]}
    assert len(rows) == 10 and all(set(r) == EXPORT_ROW_KEYS for r in data["rows"])
    assert rows[("PASS", 1)]["verdict"] == "pass"
    assert rows[("FAIL", 2)]["failures"] == [
        "missing required substring", "forbidden substring present",
        "identifier not cited or mentioned"]
    assert rows[("HTTP", 1)]["failures"] == ["HTTP 503 (?)"]  # the code is not a stable one
    assert rows[("STREAM", 1)]["failures"] == ["stream error (?)"]
    assert rows[("ODD", 1)]["finish_reason"] == "other"
    assert rows[("PASS", 1)]["citations_n"] == 1 and rows[("PASS", 1)]["hit_doc_ids"] == ["SA22-0000-00"]
    assert rows[("PASS", 1)]["coverage"] == "all"
    assert data["coverage"]["build_id"] is None and data["coverage"]["status"] == "known"
    assert data["params"]["endpoint"] == endpoint and "agent_url" not in data["params"]
    assert data["metrics"]["by_failure"]["missing required substring"] == 2


def test_export_of_a_health_failure_holds_no_golden_text(site_eval):
    import httpx2

    def respond(request):
        return httpx2.Response(503, json={"status": "degraded", "qdrant": True,
                                          "representation": "pending"})

    status, report, export, _ = site_eval(respond, _canary_entries(), ["--stream"])
    assert status == 2 and CANARIES["query"] in json.dumps(report)
    text = export.decode()
    assert all(canary not in text for canary in CANARIES.values())
    rows = json.loads(text)["rows"]
    assert {tuple(r["failures"]) for r in rows} == {("agent health returned HTTP 503",)}
    assert all(set(r) == EXPORT_ROW_KEYS and r["repeat"] == 1 for r in rows)


@pytest.mark.parametrize(("failure", "label"), [
    ("empty answer body", "empty answer body"),
    ("only inferred citations (no explicit Citations: block)", "only inferred citations …"),
    ("trap answered: 3 validated citation(s)", "trap answered"),
    ("verification state 'unverified_draft' != expected 'accepted'",
     "verification state … != expected …"),
    ("verification state None != expected 'accepted'", "verification state None != expected …"),
    ("HTTP 422 (prompt_budget_exceeded)", "HTTP 422 (prompt_budget_exceeded)"),
    ("HTTP 500 (internal error text)", "HTTP 500 (?)"),
    ("stream error (model_timeout)", "stream error (model_timeout)"),
    ("request error: ConnectTimeout", "request error"),
    ("agent health returned HTTP 401", "agent health returned HTTP 401"),
    ("free text a future template forgot to quote", "other"),
], ids=["empty-body", "only-inferred", "trap-answered", "state-mismatch", "state-none",
        "http-known-code", "http-unknown-code", "stream-error-known-code", "request-error",
        "health-http", "free-text-other"])
def test_export_failure_labels_are_a_closed_set(failure, label):
    from mainframe_rag.eval.answers import export_failure_label

    assert export_failure_label(failure) == label


# ------------------------------------------------------------- 2cd8f67d wire


def _capture_agent(paths, sources="pages"):
    """Replay the 2cd8f67d responses for one stream path label."""
    import httpx2

    def respond(request):
        path = request.url.path
        if path == "/healthz":
            h = CAPTURE["healthz"]
            return httpx2.Response(h["status"], json=h["body"])
        if path == "/v1/sources":
            if sources == "none":
                c = CAPTURE["sources_no_catalog"]
                return httpx2.Response(c["status"], json=c["body"])
            cursor = request.url.params.get("cursor")
            pages = CAPTURE["sources_pages"]
            index = 0 if cursor is None else 1 + [p["body"]["next_cursor"] for p in pages].index(cursor)
            return httpx2.Response(pages[index]["status"], json=pages[index]["body"])
        if path == "/v1/search":
            return httpx2.Response(200, json={"hits": [{"doc_id": "SYN-0001-00"}]})
        name = f"{path.rsplit('/', 1)[1]}_{paths}"
        captured = CAPTURE[name]
        assert json.loads(request.content) == captured["request"]
        return httpx2.Response(captured["status"], content=captured["raw"].encode(),
                               headers={"Content-Type": captured["content_type"]})

    return respond


def _capture_entry(**overrides):
    query = CAPTURE["answer_hits"]["request"]["query"]
    return _entry(**{"id": "CAP", "query": query, "query_class": "syntax",
                     "expected_doc_ids": ["SYN-0001-00"], **overrides})


@pytest.mark.parametrize("endpoint", ["answer", "chat"])
def test_rc_client_reads_the_2cd8f67d_streams_and_sources(site_eval, endpoint):
    assert CAPTURE["source_sha"].startswith("2cd8f67d")
    status, report, export, _ = site_eval(
        _capture_agent("hits"), [_capture_entry()],
        ["--endpoint", endpoint, "--stream", "--coverage"])
    assert status == 0
    row = report["results"][0]
    assert row["verdict"] == "pass" and row["answer"] == "Step two runs the synthetic program [1]."
    assert row["finish_reason"] == "stop" and row["verification_state"] == "accepted"
    if endpoint == "answer":
        assert (row["prompt_tokens"], row["ttft_ms"]) == (900, 30)
    else:
        assert (row["prompt_tokens"], row["ttft_ms"]) == (None, None)
    assert report["coverage"]["status"] == "known"
    assert report["coverage"]["build_id"] == CAPTURE["sources_pages"][0]["body"]["build_id"]
    assert report["coverage"]["pages"] == len(CAPTURE["sources_pages"])
    assert row["coverage"] == "all"
    data = json.loads(export)
    assert all(set(r) == EXPORT_ROW_KEYS for r in data["rows"])
    assert data["healthz"] == CAPTURE["healthz"]["body"]


@pytest.mark.parametrize("endpoint", ["answer", "chat"])
def test_rc_client_reads_the_2cd8f67d_zero_hits_and_failure(site_eval, endpoint):
    _, report, _, _ = site_eval(
        _capture_agent("zero_hits", sources="none"),
        [_capture_entry(expected_behavior="abstain", query_class="negative",
                        expected_verification_state="insufficient_evidence")],
        ["--endpoint", endpoint, "--stream", "--coverage"])
    row = report["results"][0]
    assert row["verdict"] == "pass" and row["path"] == "zero_hits"
    assert report["coverage"]["status"] == "unknown"
    assert report["coverage"]["reason"] == "http_503" and row["coverage"] == "unknown"
    status, report, _, _ = site_eval(_capture_agent("failure"), [_capture_entry()],
                                     ["--endpoint", endpoint, "--stream"])
    assert status == 1
    assert report["results"][0]["failures"] == ["stream error (upstream_error)"]


# ------------------------------------------------------------- two turns (#1268)

TWO_TURN_ROW_KEYS = EXPORT_ROW_KEYS | {
    "turn", "prior_answer_chars", "evidence_doc_ids", "evidence_gold_hit",
    "evidence_cited_doc_ids", "evidence_citation_precision", "evidence_citation_recall",
}
OTHER_DOC = "SC28-9999-00"
OTHER_CITE = "SC28-9999-00 Other Reference, Chapter 9 > Something, p. 9-1"
TWO_TURN = ["--endpoint", "chat", "--stream", "--turns", "2", "--arm", "condense-off"]
ZERO_HITS = "No supporting manual excerpts were found for this question."


def _two_turn_agent(turns: dict[tuple[str, int], bytes], pool: dict[str, list[str]] | None = None):
    """A fake agent for two-turn sessions: the chat stream is chosen by the
    opener question and the number of messages; the search pool by query."""
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        body = json.loads(request.content)
        if request.url.path == "/v1/search":
            docs = (pool or {}).get(body["query"], [])
            return httpx2.Response(200, json={"hits": [{"doc_id": d} for d in docs]})
        assert request.url.path == "/v1/chat"
        messages = body["messages"]
        return _sse(turns[(messages[0]["content"], len(messages))])

    return respond


def _chat_bodies(calls) -> list[dict]:
    return [json.loads(c.content) for c in calls if c.url.path == "/v1/chat"]


def _pipe_session(turn_one: bytes, question: str, follow_up: str) -> tuple[str, list[dict]]:
    """Independent expectation: the real Open WebUI Pipe, streamed as Open
    WebUI consumes it, for the opener and then for [question, the answer it
    kept, follow-up]. Returns the kept answer and the agent bodies it sent."""
    import asyncio

    import httpx
    from integrations.openwebui import docs_pipe

    sent: list[dict] = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=turn_one, headers={"content-type": "text/event-stream"})

    async def emit(_event):
        return None

    async def turn(messages):
        pipe = docs_pipe.Pipe()
        pipe._transport = httpx.MockTransport(handler)
        result = await pipe.pipe({"model": "docs", "stream": True, "messages": messages},
                                 __user__={"id": "u1"}, __event_emitter__=emit,
                                 __task__=None, __metadata__={})
        return "".join([item["choices"][0]["delta"]["content"] async for item in result])

    kept = asyncio.run(turn([{"role": "user", "content": question}]))
    asyncio.run(turn([{"role": "user", "content": question},
                      {"role": "assistant", "content": kept},
                      {"role": "user", "content": follow_up}]))
    return kept, sent


def test_two_turn_bodies_are_what_the_pipe_sends(site_eval):
    """Turn 1 is the Pipe's request; turn 2 carries the answer as Open WebUI
    keeps it from the Pipe (the model's own citations section hidden, the
    validated list kept in the content) and the class follow-up. The eval's
    one addition is `temperature`, as on every chat row."""
    opener = _entry()
    turn_one = chat_stream("IEA500I reports a rejected command [1].\n\n",
                           f"Citations:\n- {CITE}\n")
    agent = _two_turn_agent({(opener["query"], 1): turn_one, (opener["query"], 3): chat_stream(
        "It means the command was rejected; reenter it [1].")})
    status, report, _, calls = site_eval(agent, [opener], TWO_TURN)
    assert status == 0, report["results"]
    kept, sent = _pipe_session(turn_one, opener["query"], FOLLOW_UPS["message_id"])
    bodies = _chat_bodies(calls)
    assert bodies == [{**sent[0], "temperature": 0.0}, {**sent[1], "temperature": 0.0}]
    assert bodies[1]["messages"][1] == {"role": "assistant", "content": kept}
    assert kept.endswith(f"\n\n**Citations:**\n- {CITE}") and kept.count("Citations:") == 1
    first, second = report["results"]
    assert (first["turn"], second["turn"]) == (1, 2)
    assert second["query"] == FOLLOW_UPS["message_id"] and second["verdict"] == "pass"
    assert second["prior_answer_chars"] == len(kept) and "prior_answer_chars" not in first


def test_turn_two_signals_come_from_supplied_evidence_not_the_search_pool(site_eval):
    """The follow-up is never searched on its own words. Its evidence hit is
    the agent's packed evidence: a pool full of gold cannot make a miss a
    hit, and an empty pool cannot hide a hit."""
    lost, gained = _entry(id="LOST", query="What does IEA500I report?"), _entry(
        id="GAINED", query="What does IEA501I report?")
    gold, other = [_supplied()], [_supplied(cite=OTHER_CITE, doc_id=OTHER_DOC)]
    agent = _two_turn_agent({
        (lost["query"], 1): chat_stream("Rejected [1].", supplied=gold),
        (lost["query"], 3): chat_stream("Other [1].", citations=(OTHER_CITE,), supplied=other),
        (gained["query"], 1): chat_stream("Other [1].", citations=(OTHER_CITE,), supplied=other),
        (gained["query"], 3): chat_stream("Rejected [1].", supplied=gold),
    }, pool={lost["query"]: ["SA22-0000-00"], gained["query"]: [OTHER_DOC]})
    status, report, export, calls = site_eval(agent, [lost, gained], TWO_TURN)
    searched = [json.loads(c.content)["query"] for c in calls if c.url.path == "/v1/search"]
    assert sorted(searched) == sorted([lost["query"], gained["query"]])
    rows = {(r["id"], r["turn"]): r for r in report["results"]}
    assert rows[("LOST", 1)]["evidence_gold_hit"] is True
    assert rows[("LOST", 2)]["evidence_gold_hit"] is False
    assert rows[("LOST", 2)]["evidence_doc_ids"] == [OTHER_DOC]
    assert rows[("LOST", 2)]["evidence_citation_recall"] == 0.0
    assert rows[("GAINED", 2)]["evidence_gold_hit"] is True
    assert rows[("GAINED", 2)]["evidence_cited_doc_ids"] == ["SA22-0000-00"]
    assert rows[("GAINED", 2)]["evidence_citation_precision"] == 1.0
    for row_id in ("LOST", "GAINED"):  # no pool for the follow-up
        assert rows[(row_id, 2)]["hit_doc_ids"] is None
        assert rows[(row_id, 2)]["gold_retrieved"] is None
    second = report["metrics"]["by_turn"]["2"]
    assert (second["evidence_n"], second["evidence_gold_hit"]) == (2, 1)
    assert second["vs_opener"] == {"kept": 0, "lost": 1, "gained": 1, "neither": 0, "unknown": 0}
    exported = {(r["id"], r["turn"]): r for r in json.loads(export)["rows"]}
    assert exported[("LOST", 2)]["evidence_gold_hit"] is False
    assert exported[("GAINED", 2)]["evidence_doc_ids"] == ["SA22-0000-00"]
    # The existing blocks cover the openers only.
    assert report["metrics"]["queries"] == 2 and status == 0


def test_only_answer_rows_with_gold_get_a_follow_up(site_eval):
    """Trap and gold-less rows end after turn 1; an opener that errored gives
    a turn-2 error row with a fixed label, so it is counted, not dropped."""
    import httpx2

    trap = _entry(id="TRAP", query="What does IEA999I report?", expected_behavior="abstain",
                  expected_doc_ids=[], expected_verification_state="insufficient_evidence")
    nogold = _entry(id="NOGOLD", query="What does IEA502I report?", expected_doc_ids=[])
    broken = _entry(id="BROKEN", query="What does IEA503I report?")

    def respond(request):
        if request.url.path == "/v1/chat" and b"IEA503I" in request.content:
            return httpx2.Response(503, json={"code": "overloaded", "message": "x"})
        return _two_turn_agent({
            (trap["query"], 1): chat_stream(ZERO_HITS, citations=(), supplied=[]),
            (nogold["query"], 1): chat_stream("Rejected [1]."),
        })(request)

    status, report, export, _ = site_eval(respond, [trap, nogold, broken], TWO_TURN)
    turns = sorted((r["id"], r["turn"]) for r in report["results"])
    assert turns == [("BROKEN", 1), ("BROKEN", 2), ("NOGOLD", 1), ("TRAP", 1)]
    exported = {(r["id"], r["turn"]): r for r in json.loads(export)["rows"]}
    assert exported[("BROKEN", 2)]["failures"] == ["opener not answered"]
    assert exported[("BROKEN", 2)]["verdict"] == "error"
    assert report["metrics"]["by_turn"]["2"]["by_failure"] == {"opener not answered": 1}
    assert status == 1


def test_two_turn_export_carries_no_question_answer_or_follow_up_text(site_eval):
    c = CANARIES
    opener = _entry(query=f"{c['query']} one")
    agent = _two_turn_agent({
        (opener["query"], 1): chat_stream(f"IEA500I {c['answer']} rejected [1]."),
        (opener["query"], 3): chat_stream(f"{c['delta']} again [1]."),
    })
    status, report, export, _ = site_eval(agent, [opener], [*TWO_TURN, "--repeats", "2"])
    raw = json.dumps(report)
    assert c["answer"] in raw and c["delta"] in raw and FOLLOW_UPS["message_id"] in raw
    text = export.decode("utf-8")
    for canary in (*CANARIES.values(), *FOLLOW_UPS.values(), DEFAULT_FOLLOW_UP, CITE):
        assert canary.lower() not in text.lower(), canary
    data = json.loads(text)
    assert len(data["rows"]) == 4 and all(set(r) == TWO_TURN_ROW_KEYS for r in data["rows"])
    params = data["params"]
    assert (params["turns"], params["arm"]) == (2, "condense-off")
    assert len(params["follow_ups_sha256"]) == 64 and len(params["pipe_sha256"]) == 64
    by_turn = data["metrics"]["by_turn"]
    assert by_turn["2"]["evidence_gold_hit_rate"] == 1.0
    assert by_turn["2"]["repeats"]["evidence_gold_hit_rate"]["range"] == 0
    assert data["metrics"]["queries"] == 2 and status == 0


def test_single_turn_run_is_unchanged_by_the_two_turn_mode(site_eval):
    """`--turns 1` is the default: same rows, same keys, no new params."""
    def normal(export: bytes) -> dict:
        data = json.loads(export)
        data.pop("timestamp")
        for row in data["rows"]:
            for key in ("elapsed_ms", "client_first_token_ms"):
                row.pop(key)
        return data

    agent = _two_turn_agent({(_entry()["query"], 1): chat_stream("Rejected [1].")})
    _, report, plain, _ = site_eval(agent, [_entry()], ["--endpoint", "chat", "--stream"])
    _, _, explicit, calls = site_eval(agent, [_entry()],
                                      ["--endpoint", "chat", "--stream", "--turns", "1"])
    assert normal(plain) == normal(explicit)
    data = json.loads(plain)
    assert not {"turns", "arm"} & set(data["params"]) and "by_turn" not in data["metrics"]
    assert all(set(r) == EXPORT_ROW_KEYS for r in data["rows"])
    assert "turn" not in report["results"][0] and len(_chat_bodies(calls)) == 1


@pytest.mark.parametrize("extra", [
    pytest.param(["--stream", "--turns", "2", "--arm", "a"], id="answer-endpoint"),
    pytest.param(["--endpoint", "chat", "--stream", "--turns", "2"], id="no-arm"),
    pytest.param(["--endpoint", "chat", "--stream", "--arm", "a"], id="arm-alone"),
    pytest.param(["--endpoint", "chat", "--stream", "--turns", "2", "--arm", "Off Arm"],
                 id="bad-arm"),
])
def test_two_turn_flags_are_refused_when_inconsistent(site_eval, extra):
    with pytest.raises(SystemExit) as exc:
        site_eval(lambda r: pytest.fail("no request"), [_entry()], extra)
    assert exc.value.code == 2


def test_two_turn_needs_the_pipe_in_the_evaluator_checkout(tmp_path, monkeypatch, capsys):
    """No Pipe file, no run: refused before any agent request (exit 2)."""
    import httpx2

    from mainframe_rag.eval import answers, follow_up

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(follow_up, "PIPE_PATH", tmp_path / "missing.py")
    real_client = httpx2.Client
    monkeypatch.setattr(httpx2, "Client", lambda **kw: real_client(transport=httpx2.MockTransport(
        lambda request: pytest.fail("no agent request without the Pipe")), **kw))
    golden = tmp_path / "g.jsonl"
    golden.write_text(json.dumps(_entry()) + "\n")
    assert answers.main(["--agent-url", "http://deployed-agent:8087", "--golden", str(golden),
                         *TWO_TURN]) == 2
    assert "FAIL: Open WebUI Pipe not loadable" in capsys.readouterr().err
    assert not (tmp_path / "evals").exists()


def test_a_pipe_failure_is_a_fixed_turn_two_error(site_eval, monkeypatch):
    """The Pipe is a boundary: if building the history fails, the follow-up
    is an error row with a fixed label and no turn-2 request is sent."""
    from mainframe_rag.eval import follow_up

    def broken(*args, **kwargs):
        raise RuntimeError("history text that must not reach the export")

    monkeypatch.setattr(follow_up, "kept_answer", broken)
    agent = _two_turn_agent({(_entry()["query"], 1): chat_stream("Rejected [1].")})
    status, _, export, calls = site_eval(agent, [_entry()], TWO_TURN)
    rows = {r["turn"]: r for r in json.loads(export)["rows"]}
    assert rows[2]["failures"] == ["follow-up history failed"] and rows[1]["verdict"] == "pass"
    assert len(_chat_bodies(calls)) == 1 and b"must not reach" not in export and status == 1


def test_two_turn_health_failure_records_openers_only(site_eval):
    """An unservable agent: every sampled row is a turn-1 error, no follow-up."""
    import httpx2

    def respond(request):
        assert request.url.path == "/healthz"
        return httpx2.Response(503, json={**HEALTH, "status": "degraded"})

    status, report, export, _ = site_eval(respond, [_entry()], TWO_TURN)
    assert status == 2 and [r["turn"] for r in json.loads(export)["rows"]] == [1]
    assert report["metrics"]["by_turn"]["2"]["n"] == 0
