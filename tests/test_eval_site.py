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
])
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
])
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
