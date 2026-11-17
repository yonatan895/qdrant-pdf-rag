"""Site measurement for the answer eval (issue #1191): the `/v1/chat`
endpoint, served-corpus coverage, the text-free export, the RC client
reading the 2cd8f67d agent's wire, and the corpus coverage check.

The CLI runs over an inert HTTP boundary (`httpx2.MockTransport`) whose
chat frames come from the agent's own SSE formatters. Two tests drive the
real app in-process: the chat/answer parity check and nothing else. The
2cd8f67d shapes in tests/data/site_eval_2cd8f67d.json were captured from that
commit's real app with its own test doubles (synthetic data only)."""

from __future__ import annotations

import hashlib
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

    def run(respond, entries, extra=(), dataset=None):
        """`dataset` replaces the default `--golden <entries> --all` (the
        session runner reads its own file)."""
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
            "--agent-url", "http://deployed-agent:8087",
            *(["--golden", str(golden), "--all"] if dataset is None else dataset),
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


_HLASM = "GETMAIN R,LV=4096"


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_answer_rows_score_a_gold_string_in_the_script_like_one_in_the_answer(
        site_eval, transport):
    """Maintainer decision 2026-11-17 (#1285): a build whose parser moves an
    hlasm fence into `script` (the candidate) and one that leaves it in the
    answer (f117160c) give the same verdict on the same model output."""
    import httpx2

    def run(answer, script):
        def respond(request):
            if request.url.path == "/healthz":
                return httpx2.Response(200, json=HEALTH)
            if request.url.path == "/v1/search":
                return httpx2.Response(200, json={"hits": [{"doc_id": "SA22-0000-00"}]})
            if transport == "sse":
                return _sse(_answer_final(answer=answer, script=script,
                                          script_review_required=script is not None))
            return httpx2.Response(200, json={
                "answer": answer, "citations": [CITE], "citations_inferred": False,
                "inferred_indices": [], "verification_state": "accepted", "script": script,
                "script_review_required": script is not None, "request_id": "r1"})

        args = ["--stream"] if transport == "sse" else []
        _status, report, _, _ = site_eval(respond, [_entry(gold_must_contain=["GETMAIN"])], args)
        return report["results"][0]

    old = run(f"Obtain storage:\n\n```hlasm\n{_HLASM}\n```", None)
    new = run("Obtain storage:", _HLASM)
    assert old["verdict"] == new["verdict"] == "pass", (old["failures"], new["failures"])
    assert old["failures"] == new["failures"] == []


@pytest.mark.parametrize("agent_script", [None, _HLASM], ids=["old-agent", "new-agent"])
def test_chat_rows_score_a_fenced_gold_string_the_same_on_either_build(site_eval, agent_script):
    """On /v1/chat the body and the script both come from the evaluator's
    own parse of the streamed text, so the agent build's script field does
    not change the verdict."""
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": [{"doc_id": "SA22-0000-00"}]})
        body = chat_stream("Obtain storage:\n\n", f"```hlasm\n{_HLASM}\n```\n\n",
                           f"Citations:\n- {CITE}\n")
        if agent_script is not None:
            body = body.replace(b'"script": null', json.dumps({"script": agent_script})[1:-1].encode())
        return _sse(body)

    _status, report, _, _ = site_eval(respond, [_entry(gold_must_contain=["GETMAIN"])],
                                      ["--endpoint", "chat", "--stream"])
    row = report["results"][0]
    assert row["verdict"] == "pass", row["failures"]
    assert row["answer"] == "Obtain storage:"


_DEPTH_SUPPLIED = [
    {**_supplied(), "index": 1},
    {**_supplied(doc_id="SA22-0001-00"), "index": 2},
    {**_supplied(), "index": 9},
]


@pytest.mark.parametrize("endpoint,transport", [("answer", "json"), ("answer", "sse"), ("chat", "sse")])
@pytest.mark.parametrize("supplied,counts", [
    (_DEPTH_SUPPLIED, (3, 2)),
    ([], (0, 0)),
    (None, (None, None)),  # an agent that does not send the field: unknown
    ([{"index": 1, "citation": CITE}], (None, None)),  # no doc id: unknown, not a count
])
def test_rows_count_supplied_excerpts_and_their_manuals(site_eval, endpoint, transport, supplied,
                                                        counts):
    """Issue #1191: each answer row carries how many excerpts the agent
    supplied to the model and from how many distinct manuals, read off the
    response's `supplied_evidence` (counts only), on both endpoints."""
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": [{"doc_id": "SA22-0000-00", "cite": CITE}]})
        if endpoint == "chat":
            if supplied is None:
                # Strip the field from the agent's own finish chunk.
                return _sse(chat_stream("IEA500I reports a rejected command.", supplied=[])
                            .replace(b', "supplied_evidence": []', b""))
            return _sse(chat_stream("IEA500I reports a rejected command.", supplied=supplied))
        extra = {} if supplied is None else {"supplied_evidence": supplied}
        if transport == "sse":
            return _sse(_answer_final(**extra))
        return httpx2.Response(200, json={
            "answer": "IEA500I reports a rejected command.", "citations": [CITE],
            "citations_inferred": False, "inferred_indices": [], "verification_state": "accepted",
            "script": None, "script_review_required": False, "request_id": "r1", **extra})

    args = ["--endpoint", endpoint] + (["--stream"] if transport == "sse" else [])
    _status, report, export, _ = site_eval(respond, [_entry()], args)
    row = report["results"][0]
    if endpoint == "chat" and (supplied is None or (supplied and "doc_id" not in supplied[0])):
        assert row["verdict"] == "error"  # the chat body is parsed over the supplied doc ids
    else:
        assert row["verdict"] in ("pass", "fail"), row["failures"]
    assert (row.get("supplied_n"), row.get("supplied_doc_n")) == counts
    [exported] = json.loads(export)["rows"]
    assert (exported["supplied_n"], exported["supplied_doc_n"]) == counts


@pytest.mark.parametrize("extra,limit", [((), 16), (("--hit-limit", "8"), 8),
                                         (("--hit-limit", "16"), 16)])
def test_hit_limit_sets_the_sibling_pool_depth_and_is_recorded(site_eval, extra, limit):
    """Issue #1191: the sibling /v1/search pool (gold_retrieved, citation
    recall) asks for the deployed ANSWER_HIT_LIMIT the operator declares;
    default the agent default (16 since #1274, 8 before), and an undeclared
    run's params are unchanged."""
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": [{"doc_id": "SA22-0000-00", "cite": CITE}]})
        return _sse(_answer_final())

    status, report, export, calls = site_eval(respond, [_entry()], ["--stream", *extra])
    assert status == 0
    [search] = [c for c in calls if c.url.path == "/v1/search"]
    assert json.loads(search.content)["limit"] == limit
    for params in (report["params"], json.loads(export)["params"]):
        assert params.get("hit_limit") == (limit if extra else None)
        assert ("hit_limit" in params) == bool(extra)


@pytest.mark.parametrize("bad", ["0", "33", "16.0", "x"])
def test_hit_limit_takes_the_agent_bounds(site_eval, bad):
    with pytest.raises(SystemExit) as exc:
        site_eval(lambda r: pytest.fail("no request"), [_entry()], ["--hit-limit", bad])
    assert exc.value.code == 2


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



@pytest.mark.parametrize(("env", "gold_hit"), [
    # Both modes off: the behaviour before #1274's default flip.
    pytest.param({"CHAT_CONDENSE_ENABLED": "false", "CHAT_FOLLOWUP_ANCHOR_ENABLED": "false"}, False,
                 id="off"),
    # Condense on with the anchor unset (#1274): the anchor default yields.
    pytest.param({"CHAT_CONDENSE_ENABLED": "true"}, True, id="on"),
    # Nothing set (#1274): anchored, the opener is searched as a second leg.
    pytest.param({}, True, id="default-anchored"),
])
def test_two_turn_evidence_follows_the_real_agents_condense_setting(
    monkeypatch, servable_representation_gate, env, gold_hit,
):
    """Through the real app (#1268): the agent accepts the Pipe-shaped turn-2
    body, and the follow-up's evidence hit moves with the follow-up mode
    because it is read from what the agent packed. The search double returns
    the gold doc only for a query that names the message id; the model double
    condenses the follow-up into such a query, and the anchored leg searches
    the opener, which names it."""
    from mainframe_rag.eval.answers import run_follow_ups, run_query
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
    for name in ("CHAT_CONDENSE_ENABLED", "CHAT_FOLLOWUP_ANCHOR_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    with agent_client(monkeypatch, llm=llm, search=search, env=env) as client:
        opener = run_query(client, _entry(), stream=True, endpoint="chat", frames=frames,
                           evidence=True)
        [follow] = run_follow_ups(client, _entry(), opener, frames, load_pipe()[0], 0.0)
    assert opener["verdict"] != "error" and follow["verdict"] != "error", follow["failures"]
    assert opener["evidence_gold_hit"] is True
    assert follow["evidence_gold_hit"] is gold_hit
    expected = {"off": [FOLLOW_UPS["message_id"]], "on": ["IEA500I recovery procedure"],
                "default-anchored": [FOLLOW_UPS["message_id"], _entry()["query"]]}
    mode = ("off" if env.get("CHAT_FOLLOWUP_ANCHOR_ENABLED") == "false"
            else "on" if env else "default-anchored")
    assert searched == [_entry()["query"], *expected[mode]]
    if mode != "default-anchored":
        assert follow["evidence_doc_ids"] == (["SA22-0000-00"] if gold_hit else [OTHER_DOC])
    else:
        assert "SA22-0000-00" in follow["evidence_doc_ids"]
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
    "coverage", "expected_served", "supplied_n", "supplied_doc_n",
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


@pytest.mark.parametrize("endpoint", ["answer", "chat"])
def test_single_turn_export_does_not_follow_the_prior_turn_default(site_eval, monkeypatch, endpoint):
    """Issue #1274: the --prior-cap default follows the agent's
    chat_max_prior_turn_chars default (1000 -> 4000). A single-turn run
    reads neither: its requests and its export, timings aside, are the same
    under the old and the new default, and carry no prior_cap."""
    from mainframe_rag.config import Settings

    def run():
        _, _, export, calls = site_eval(_canary_agent(endpoint), _canary_entries(),
                                        ["--endpoint", endpoint, "--stream", "--coverage"])
        data = json.loads(export)
        for row in data["rows"]:
            for key in ("elapsed_ms", "ttft_ms", "llm_ms", "client_first_token_ms", "decode_tps"):
                row.pop(key)
        data.pop("timestamp")
        data["params"].pop("generated_at", None)
        return data, [(c.method, str(c.url), c.content) for c in calls]

    field = Settings.model_fields["chat_max_prior_turn_chars"]
    assert field.default == 4000
    new_export, new_calls = run()
    monkeypatch.setattr(field, "default", 1000)
    old_export, old_calls = run()
    assert "prior_cap" not in new_export["params"]
    assert "by_turn" not in new_export["metrics"]
    assert old_calls == new_calls
    assert json.dumps(old_export, sort_keys=True) == json.dumps(new_export, sort_keys=True)


@pytest.mark.parametrize("endpoint", ["answer", "chat"])
def test_export_carries_the_coding_breakdown_without_the_question(site_eval, endpoint):
    """Issue #1276: the coding class is derived from the question on the
    evaluator side and leaves the gap as counts only."""
    query = f"Write a REXX exec for {CANARIES['query']} one"
    _, report, export, _ = site_eval(
        _canary_agent(endpoint), [_entry(id="COD", query=query, query_class="syntax"),
                                  _entry(id="LOOK", query=f"{CANARIES['query']} one")],
        ["--endpoint", endpoint, "--stream"])
    text = export.decode("utf-8")
    assert CANARIES["query"] not in text and "REXX" not in text
    coding = json.loads(text)["metrics"]["coding"]
    assert coding == report["metrics"]["coding"]
    assert coding["n"] == 1 and coding["judged"] == 1 and coding["answer_n"] == 1


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
    ("HTTP 422 (unsupported_image)", "HTTP 422 (unsupported_image)"),
    ("stream error (model_timeout)", "stream error (model_timeout)"),
    ("request error: ConnectTimeout", "request error"),
    ("agent health returned HTTP 401", "agent health returned HTTP 401"),
    ("free text a future template forgot to quote", "other"),
], ids=["empty-body", "only-inferred", "trap-answered", "state-mismatch", "state-none",
        "http-known-code", "http-unknown-code", "http-image-code", "stream-error-known-code", "request-error",
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
    assert (second["citation_precision_n"], second["citation_precision_mean"]) == (2, 0.5)
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


# ------------------------------------------------------- follow-up kinds (#1268)

KIND_ROW_KEYS = TWO_TURN_ROW_KEYS | {
    "follow_up_kind", "switch_id", "history_over_cap", "citation_list_cut",
    "restate_declined", "gold_n", "gold_found_n", "gold_split",
}
KINDS = [*TWO_TURN, "--follow-up", "all"]


def _kind_agent(turns: dict[tuple[str, str | None], bytes]):
    """A fake agent for follow-up kinds: the chat stream is chosen by the
    opener question and the follow-up text (None for the opener itself)."""
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        body = json.loads(request.content)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": []})
        messages = body["messages"]
        follow = messages[-1]["content"] if len(messages) == 3 else None
        return _sse(turns[(messages[0]["content"], follow)])

    return respond


def _kind_rows(report) -> dict[tuple[str, int, str | None], dict]:
    return {(r["id"], r["turn"], r.get("follow_up_kind")): r for r in report["results"]}


def test_every_kind_branches_off_the_same_opener_history(site_eval):
    """One turn-1 call per row, then one turn-2 call per kind in session
    order, all over the same [question, kept answer]: anaphoric asks the
    class follow-up, switch the partner's own question, restate and summary
    the class-B wordings. The partner is also asked as an opener."""
    from mainframe_rag.eval.follow_up import RESTATE_FOLLOW_UPS

    first = _entry(id="A", query="What does IEA500I report?", gold_must_contain=["rejected"])
    second = _entry(id="B", query="What does IEA501I report?", expected_doc_ids=[OTHER_DOC],
                    expected_verification_state=None)
    other = [_supplied(cite=OTHER_CITE, doc_id=OTHER_DOC)]
    a_turn = chat_stream("Rejected [1].")
    b_turn = chat_stream("Other [1].", citations=(OTHER_CITE,), supplied=other)
    agent = _kind_agent({
        (first["query"], None): a_turn, (second["query"], None): b_turn,
        (first["query"], FOLLOW_UPS["message_id"]): chat_stream("Reenter it [1]."),
        (first["query"], second["query"]): b_turn,
        (first["query"], RESTATE_FOLLOW_UPS["restate"]): chat_stream("It was rejected [1]."),
        (first["query"], RESTATE_FOLLOW_UPS["summary"]): chat_stream("- rejected [1]."),
        (second["query"], FOLLOW_UPS["message_id"]): b_turn,
        (second["query"], first["query"]): a_turn,
    })
    status, report, _, calls = site_eval(agent, [first, second], KINDS)
    bodies = _chat_bodies(calls)
    asked = [(b["messages"][0]["content"],
              b["messages"][-1]["content"] if len(b["messages"]) == 3 else None) for b in bodies]
    assert asked == [
        (first["query"], None), (first["query"], FOLLOW_UPS["message_id"]),
        (first["query"], second["query"]), (first["query"], RESTATE_FOLLOW_UPS["restate"]),
        (first["query"], RESTATE_FOLLOW_UPS["summary"]),
        (second["query"], None), (second["query"], FOLLOW_UPS["message_id"]),
        (second["query"], first["query"]),
    ]
    histories = {json.dumps(b["messages"][:2]) for b in bodies[1:5]}
    assert len(histories) == 1  # every kind sees the same kept answer
    rows = _kind_rows(report)
    assert rows[("A", 2, "switch")]["switch_id"] == "B"
    assert rows[("B", 2, "switch")]["switch_id"] == "A"
    assert rows[("A", 1, None)]["follow_up_kind"] is None
    by_turn = report["metrics"]["by_turn"]
    assert set(by_turn["2"]) == {"anaphoric", "switch", "restate", "summary"}
    assert by_turn["2"]["restate"]["skipped"] == {"no_gold_strings": 1}  # B has none
    assert by_turn["2"]["switch"]["n"] == 2 and "vs_opener" not in by_turn["2"]["switch"]
    timing = by_turn["2"]["anaphoric"]["timing"]
    assert timing["elapsed_ms"]["n"] == 2 and timing["client_first_token_ms"]["n"] == 2
    assert timing["ttft_ms"] is None and timing["total_tokens"] is None
    params = report["params"]
    assert params["follow_up"] == ["anaphoric", "switch", "restate", "summary"]
    assert (params["prior_cap"], params["switch_pairs_n"]) == (4000, 2)  # #1274: was 1000
    assert status == 0, [r["failures"] for r in report["results"]]


def test_an_explicit_anaphoric_kind_sends_what_the_default_sends(site_eval):
    opener = _entry()
    agent = _two_turn_agent({(opener["query"], 1): chat_stream("Rejected [1]."),
                             (opener["query"], 3): chat_stream("Reenter it [1].")})
    _, plain, _, plain_calls = site_eval(agent, [opener], TWO_TURN)
    _, tagged, export, tagged_calls = site_eval(agent, [opener],
                                                [*TWO_TURN, "--follow-up", "anaphoric"])
    assert _chat_bodies(plain_calls) == _chat_bodies(tagged_calls)
    assert [r["follow_up_kind"] for r in json.loads(export)["rows"]] == [None, "anaphoric"]
    assert "follow_up_kind" not in plain["results"][1]
    assert set(tagged["metrics"]["by_turn"]["2"]) == {"anaphoric"}


@pytest.mark.parametrize(("docs", "expected"), [
    pytest.param([["X"], ["Y"], ["Z"]], {"A": "B", "B": "C", "C": "A"}, id="next"),
    pytest.param([["X"], ["X", "Q"], ["Y"]], {"A": "C", "B": "C", "C": "A"}, id="skip-shared"),
    pytest.param([["X"], ["X"]], {}, id="all-shared"),
    pytest.param([["X"]], {}, id="alone"),
])
def test_switch_pairs_are_deterministic_and_never_share_a_gold_doc(docs, expected):
    from mainframe_rag.eval.follow_up import switch_pairs, switch_pairs_sha256

    rows = [{"id": name, "expected_doc_ids": d} for name, d in zip("ABC", docs, strict=False)]
    assert switch_pairs(rows) == expected
    assert switch_pairs([dict(r) for r in rows]) == expected
    ids = [r["id"] for r in rows]
    assert switch_pairs_sha256(ids, expected) == switch_pairs_sha256(ids, switch_pairs(rows))
    # A different pairing is a different identity.
    assert switch_pairs_sha256(["A", "B", "C"], {"A": "B"}) != switch_pairs_sha256(
        ["A", "B", "C"], {"A": "C"})


def test_switch_is_scored_against_the_same_question_asked_alone(site_eval):
    """Harm from history: the switched-to question with its full gold. B
    alone passes on its gold evidence; after A's history the agent packs A's
    doc and declines, so pass, evidence and state are lost. A alone misses
    its gold doc, after B's history it finds it: gained. Each switch is
    compared with its asked question alone, never with its opener."""
    first = _entry(id="A")
    second = _entry(id="B", query="What does IEA501I report?", expected_doc_ids=[OTHER_DOC],
                    gold_must_contain=["other"])
    other = [_supplied(cite=OTHER_CITE, doc_id=OTHER_DOC)]
    refusal = chat_stream("Nothing in the supplied excerpts answers this question about IEA501I.",
                          citations=(), state="insufficient_evidence")
    agent = _kind_agent({
        (first["query"], None): chat_stream("Other [1].", citations=(OTHER_CITE,),
                                            supplied=other),
        (second["query"], None): chat_stream("Other [1].", citations=(OTHER_CITE,),
                                             supplied=other),
        (first["query"], second["query"]): refusal,
        (second["query"], first["query"]): chat_stream("Rejected [1]."),
    })
    status, report, export, _ = site_eval(agent, [first, second],
                                          [*TWO_TURN, "--follow-up", "switch"])
    switched = _kind_rows(report)[("A", 2, "switch")]
    assert switched["verdict"] == "fail" and switched["evidence_gold_hit"] is False
    single = report["metrics"]["by_turn"]["2"]["switch"]["vs_single"]
    assert single["pass"] == {"kept": 1, "lost": 1, "gained": 0, "neither": 0, "unknown": 0}
    assert single["evidence_gold_hit"] == {"kept": 0, "lost": 1, "gained": 1, "neither": 0,
                                           "unknown": 0}
    assert single["verification_state"] == {"same": 1, "changed": 1, "unknown": 0}
    assert single["verification_moves"] == {"accepted>insufficient_evidence": 1}
    assert {(r["id"], r["switch_id"]) for r in json.loads(export)["rows"]
            if r["follow_up_kind"] == "switch"} == {("A", "B"), ("B", "A")}
    assert status == 1


def test_restate_is_scored_on_the_openers_gold_and_declines_are_counted(site_eval):
    """Class B: only after a passed opener with gold strings; the verdict is
    the opener's gold substrings alone (an uncited restatement is judged on
    them); the zero-hits path and the structured decline are declines; a
    failed opener is skipped and counted, never a row."""
    from mainframe_rag.eval.answers import _annotate_history, judge_restate
    from mainframe_rag.eval.follow_up import RESTATE_FOLLOW_UPS

    kept = _entry(id="KEPT", gold_must_contain=["rejected", "reenter"])
    failed = _entry(id="FAILED", query="What does IEA502I report?", gold_must_contain=["missing"])
    agent = _kind_agent({
        (kept["query"], None): chat_stream("Rejected; reenter it [1]."),
        (failed["query"], None): chat_stream("Rejected [1]."),
        (kept["query"], RESTATE_FOLLOW_UPS["restate"]): chat_stream("It was rejected.",
                                                                    citations=()),
        (kept["query"], RESTATE_FOLLOW_UPS["summary"]): chat_stream(ZERO_HITS, citations=(),
                                                                    supplied=[]),
    })
    status, report, export, calls = site_eval(agent, [kept, failed],
                                              [*TWO_TURN, "--follow-up", "restate,summary"])
    assert len(_chat_bodies(calls)) == 4
    rows = _kind_rows(report)
    restate, summary = rows[("KEPT", 2, "restate")], rows[("KEPT", 2, "summary")]
    assert (restate["verdict"], restate["gold_n"], restate["gold_found_n"]) == ("fail", 2, 1)
    assert restate["restate_declined"] is False
    assert summary["restate_declined"] is True and summary["gold_found_n"] == 0
    by_turn = report["metrics"]["by_turn"]["2"]
    assert by_turn["restate"]["skipped"] == {"opener_not_passed": 1}
    assert by_turn["restate"]["restate"]["declines"] == 0
    assert by_turn["summary"]["restate"]["declines"] == 1
    assert by_turn["restate"]["by_failure"] == {"missing required substring": 1}
    exported = {(r["id"], r["follow_up_kind"]): r for r in json.loads(export)["rows"]}
    assert exported[("KEPT", "restate")]["failures"] == ["missing required substring"]
    assert exported[("KEPT", "summary")]["restate_declined"] is True
    # The structured decline is a decline too, by the agent's own predicate.
    row = {"answer": "Nothing in the supplied excerpts answers this question about your "
                     "last answer.", "path": "llm", "citations": [], "warns": []}
    judge_restate(row, kept)
    assert row["restate_declined"] is True and row["verdict"] == "fail"
    # A decline that happens to name every gold string is still no restatement.
    named = {"answer": "Nothing in the supplied excerpts answers this question about "
                       "rejected reenter.", "path": "llm", "citations": [], "warns": []}
    judge_restate(named, kept)
    assert (named["verdict"], named["gold_n"], named["gold_found_n"]) == ("fail", 2, 0)
    assert named["failures"] == ["follow-up declined"]
    _annotate_history(named, kept, "restate", {"messages": [
        {"role": "user", "content": kept["query"]},
        {"role": "assistant", "content": "Rejected; reenter it."},
        {"role": "user", "content": RESTATE_FOLLOW_UPS["restate"]}]}, 1000)
    assert named["gold_split"]["before_cut"] == {"n": 2, "found": 0}
    assert exported[("KEPT", "summary")]["failures"] == [
        "missing required substring", "missing required substring", "follow-up declined"]
    assert status == 1


_CUT_1000 = ({"n": 1, "found": 1}, {"n": 1, "found": 0})
_KEPT_4000 = ({"n": 2, "found": 1}, {"n": 0, "found": 0})


@pytest.mark.parametrize(("cap", "cut", "split", "citations"), [
    pytest.param(["--prior-cap", "1000"], 1000, _CUT_1000, "cut", id="cap1000"),
    pytest.param(["--prior-cap", "4000"], 4000, _KEPT_4000, "kept", id="cap4000"),
    # #1274: the default follows the agent default, 4000 (was 1000).
    pytest.param([], 4000, _KEPT_4000, "kept", id="default"),
])
def test_restate_gold_is_split_at_the_declared_prior_turn_cut(site_eval, cap, cut, split, citations):
    """A long opener answer: one gold string in the kept head, one past the
    declared cut. The restatement has only the first; its evidence holds the
    gold doc (the doc-level proxy). Exposure flags the cut citation list."""
    from mainframe_rag.eval.follow_up import RESTATE_FOLLOW_UPS

    opener = _entry(gold_must_contain=["rejected", "lategold"])
    agent = _kind_agent({
        (opener["query"], None): chat_stream("Rejected " + "x" * 1200 + " lategold [1]."),
        (opener["query"], RESTATE_FOLLOW_UPS["restate"]): chat_stream("It was rejected [1]."),
    })
    _, report, export, _ = site_eval(agent, [opener],
                                     [*TWO_TURN, "--follow-up", "restate", *cap])
    row = _kind_rows(report)[("MSG-01", 2, "restate")]
    assert (row["gold_split"]["before_cut"], row["gold_split"]["after_cut"]) == split
    assert row["history_over_cap"] is (cut == 1000) and row["citation_list_cut"] == citations
    block = report["metrics"]["by_turn"]["2"]["restate"]
    assert block["restate"]["gold_split"]["before_cut"]["in_evidence"] == split[0]
    assert block["exposure"]["over_cap"] == (1 if cut == 1000 else 0)
    assert block["prior_answer_chars"]["cut_chars"] == cut
    assert report["params"]["prior_cap"] == cut
    assert json.loads(export)["rows"][1]["gold_split"]["after_cut"] == split[1]


@pytest.mark.parametrize(("view", "expected"), [
    pytest.param("short\n\n**Citations:**\n- a", (False, "kept"), id="kept"),
    pytest.param("x" * 990 + "\n\n**Citations:**\n- a", (True, "partial"), id="partial"),
    pytest.param("x" * 1000 + "\n\n**Citations:**\n- a", (True, "cut"), id="cut"),
    pytest.param("x" * 1200, (True, "absent"), id="absent"),
])
def test_history_exposure_places_the_citation_list_against_the_cap(view, expected):
    from mainframe_rag.eval.follow_up import history_exposure

    assert history_exposure(view, 1000) == expected


@pytest.mark.parametrize(("cap", "tail"), [
    pytest.param(200, "y" * 200, id="cut200"), pytest.param(1000, "y" * 1000, id="cut1000"),
    pytest.param(1000, "", id="pasted"),
])
def test_history_view_is_what_the_agents_prompt_builder_keeps(cap, tail):
    """The evaluator's model of the prior-turn cut against the agent's own
    `build_chat_messages`: the same kept text, a string the evaluator places
    before the cut is in the prompt, one after it is not, and one only in a
    pasted excerpts block (dropped by the agent) is not in the history."""
    from mainframe_rag.agent.answer import EXCERPTS_HEADER, build_chat_messages
    from mainframe_rag.config import Settings
    from mainframe_rag.eval.follow_up import gold_positions, history_view
    from mainframe_rag.ports import ChatMessage
    from tests.helpers_agent_local import agent_hit

    sent = ("  \nheadgold " + tail + " tailgold\n\n**Citations:**\n- c\n"
            + EXCERPTS_HEADER + "\n[1] pastedgold\n")
    settings = Settings(_env_file=None, llm_base_url="http://llm.internal/v1",
                        llm_model_reasoning="test-reasoning-model", embed_mode="hash",
                        allow_hash_mode=True, chat_max_prior_turn_chars=cap)
    prepared = build_chat_messages(
        [ChatMessage(role="user", content="An earlier question"),
         ChatMessage(role="assistant", content=sent),
         ChatMessage(role="user", content="And then?")], [agent_hit()], settings=settings)
    prompt = next(m.content for m in prepared.messages if m.role == "assistant")
    view = history_view(sent)
    assert prompt == (view if len(view) <= cap else view[:cap] + " ... [history truncated]")
    positions = gold_positions(view, ["HEADGOLD", "tailgold", "pastedgold"], cap)
    assert positions == ["before_cut", "after_cut" if tail else "before_cut", "not_in_history"]
    assert "headgold" in prompt and ("tailgold" in prompt) is (not tail)
    assert "pastedgold" not in prompt


def test_kind_export_carries_no_question_answer_gold_or_follow_up_text(site_eval):
    from mainframe_rag.eval.follow_up import RESTATE_FOLLOW_UPS, every_follow_up_text

    c = CANARIES
    first = _entry(id="A", query=f"{c['query']} one", gold_must_contain=[c["gold"]])
    second = _entry(id="B", query=f"{c['query']} two", expected_doc_ids=[OTHER_DOC])
    answer = chat_stream(f"{c['answer']} {c['gold']} [1].")
    turns = {(q, f): answer for q in (first["query"], second["query"])
             for f in (None, FOLLOW_UPS["message_id"], *RESTATE_FOLLOW_UPS.values())}
    turns.update({(first["query"], second["query"]): answer,
                  (second["query"], first["query"]): answer})
    _, report, export, _ = site_eval(_kind_agent(turns), [first, second],
                                     [*KINDS, "--repeats", "2"])
    raw = json.dumps(report)
    assert c["gold"] in raw and RESTATE_FOLLOW_UPS["summary"] in raw
    text = export.decode("utf-8")
    for canary in (*CANARIES.values(), *every_follow_up_text(), CITE):
        assert canary.lower() not in text.lower(), canary
    data = json.loads(text)
    assert all(set(r) == KIND_ROW_KEYS for r in data["rows"])
    # Per repeat: 2 openers, 2 anaphoric, 2 switch, restate and summary for A only.
    assert len(data["rows"]) == 2 * (2 + 2 + 2 + 1 + 1)
    assert len(data["params"]["restate_follow_ups_sha256"]) == 64
    assert len(data["params"]["switch_pairs_sha256"]) == 64


@pytest.mark.parametrize("extra", [
    pytest.param(["--endpoint", "chat", "--stream", "--follow-up", "all"], id="kind-alone"),
    pytest.param([*TWO_TURN, "--prior-cap", "4000"], id="cap-alone"),
    pytest.param([*TWO_TURN, "--follow-up", "anaphoric,other"], id="bad-kind"),
    pytest.param([*KINDS, "--prior-cap", "100"], id="cap-range"),
])
def test_follow_up_flags_are_refused_when_inconsistent(site_eval, extra):
    with pytest.raises(SystemExit) as exc:
        site_eval(lambda r: pytest.fail("no request"), [_entry()], extra)
    assert exc.value.code == 2


def test_kind_health_failure_records_openers_with_the_kind_keys(site_eval):
    import httpx2

    def respond(request):
        return httpx2.Response(503, json={**HEALTH, "status": "degraded"})

    status, report, export, _ = site_eval(respond, [_entry()], KINDS)
    rows = json.loads(export)["rows"]
    assert status == 2 and [r["turn"] for r in rows] == [1]
    assert set(rows[0]) == KIND_ROW_KEYS and rows[0]["follow_up_kind"] is None
    assert report["metrics"]["by_turn"]["2"]["switch"]["n"] == 0


def test_an_errored_opener_and_a_lone_row_are_counted_per_kind(site_eval, tmp_path):
    """An errored opener: anaphoric and switch get a fixed `opener not
    answered` row (switch then compares as unknown), class B is skipped as
    `opener_not_passed`. A row without a partner is a `no_partner` skip. The
    summary renders the turn x kind tables."""
    import httpx2

    broken = _entry(id="BROKEN", query="What does IEA503I report?", gold_must_contain=["x"])
    fine = _entry(id="FINE", expected_doc_ids=[OTHER_DOC])
    lone = _entry(id="LONE", query="What does IEA504I report?", expected_doc_ids=[OTHER_DOC])

    def respond(request):
        if request.url.path == "/v1/chat" and b"IEA503I" in request.content:
            return httpx2.Response(503, json={"code": "overloaded", "message": "x"})
        return _kind_agent({(fine["query"], None): chat_stream("Rejected [1]."),
                            (fine["query"], FOLLOW_UPS["message_id"]): chat_stream("Again [1]."),
                            (fine["query"], broken["query"]): chat_stream("Rejected [1].")})(request)

    summary = tmp_path / "summary.md"
    status, report, _, _ = site_eval(respond, [broken, fine],
                                     [*KINDS, "--summary", str(summary)])
    rows = _kind_rows(report)
    for kind in ("anaphoric", "switch"):
        assert rows[("BROKEN", 2, kind)]["failures"] == ["opener not answered"]
    by_turn = report["metrics"]["by_turn"]["2"]
    assert by_turn["restate"]["skipped"] == {"opener_not_passed": 1, "no_gold_strings": 1}
    assert by_turn["switch"]["vs_single"]["pass"]["unknown"] == 2  # one errored side each
    text = summary.read_text()
    assert "### switch: turn-2 pass is" in text and "- skipped: " in text and status == 1
    _, alone, _, _ = site_eval(_kind_agent({(lone["query"], None): chat_stream("Rejected [1].")}),
                               [lone], [*TWO_TURN, "--follow-up", "switch"])
    assert alone["metrics"]["by_turn"]["2"]["switch"]["skipped"] == {"no_partner": 1}
    assert alone["params"]["switch_pairs_n"] == 0


def test_a_switch_row_covers_the_docs_of_the_question_it_asked(site_eval):
    """Coverage of a switch row is its asked row's expected docs, served or
    not, never its opener's (the two share no doc by construction)."""
    import httpx2

    first = _entry(id="A")
    second = _entry(id="B", query="What does IEA501I report?", expected_doc_ids=[OTHER_DOC])
    chat = _kind_agent({(q, f): chat_stream("Rejected [1].")
                        for q in (first["query"], second["query"])
                        for f in (None, first["query"], second["query"])})

    def respond(request):
        if request.url.path == "/v1/sources":
            return httpx2.Response(200, json=_sources_page([OTHER_DOC]))
        return chat(request)

    _, _, export, _ = site_eval(respond, [first, second],
                                [*TWO_TURN, "--follow-up", "switch", "--coverage"])
    rows = {(r["id"], r["turn"]): r for r in json.loads(export)["rows"]}
    assert (rows[("A", 1)]["coverage"], rows[("A", 2)]["coverage"]) == ("none", "all")
    assert (rows[("B", 1)]["coverage"], rows[("B", 2)]["coverage"]) == ("all", "none")


# ------------------------------------------------- corpus coverage check (#1191)
# `python -m mainframe_rag.eval.coverage` compares the served catalog with the
# wishlist and reports only wishlist items, statuses and public form numbers.
# All numbers and titles below are synthetic.

PRIVATE_IDS = ("acme-jes2-runbook", "acme-pd-notes", "kb201234", "OPS01-0042", "SITE-SECRET-01")
SYNTHETIC_WISHLIST = {"schema": "corpus_wishlist/1", "items": [
    {"id": "num", "category": "hardware_language_reference", "title": "Numbered Principles",
     "priority": 1, "match": "doc_number", "doc_numbers": ["SA99-0001"],
     "title_keywords": [["numbered principles"]]},
    {"id": "miss", "category": "hlasm_library", "title": "Missing Reference",
     "priority": 2, "match": "doc_number", "doc_numbers": ["SA99-0002"]},
    {"id": "amb", "category": "language_tool_library", "title": "Assembler Guide",
     "priority": 2, "match": "title", "title_keywords": [["synthetic", "assembler guide"]]},
    {"id": "one", "category": "language_tool_library", "title": "Toolkit Handbook",
     "priority": 3, "match": "title", "title_keywords": [["toolkit handbook"]]},
    {"id": "many", "category": "redbook", "title": "Synthetic Series",
     "priority": 1, "match": "title", "title_keywords": [["synthetic series"]],
     "expect_many": True},
    {"id": "rb-jes2", "category": "redbook", "title": "JES2 Redbooks",
     "priority": 2, "match": "title", "title_keywords": [["jes2"]],
     "doc_id_prefixes": ["SG24", "REDP"], "expect_many": True},
    {"id": "pd", "category": "practitioner", "title": "Problem Determination",
     "priority": 3, "match": "title", "title_keywords": [["problem determination"]],
     "expect_many": True},
    {"id": "renum", "category": "language_tool_library", "title": "Renumbered Handbook",
     "priority": 2, "match": "doc_number", "doc_numbers": ["SA99-0005"],
     "title_keywords": [["toolkit handbook"]]},
    {"id": "rp", "category": "redbook", "title": "Redpaper Handbook",
     "priority": 3, "match": "title", "title_keywords": [["redpaper handbook"]]},
]}
SYNTHETIC_CATALOG = [
    ("SA99-0001-13", "Numbered Principles of Operation, Thirteenth Edition"),
    ("SA99-0001-12", "Numbered Principles of Operation, Twelfth Edition"),
    ("SC99-1111-00", "Synthetic Assembler Guide"),
    ("SC99-2222-01", "Synthetic Assembler Guide for Experts"),
    ("GC99-0003-01", "Toolkit Handbook"),
    ("SG24-9001-00", "Synthetic Series Volume 1"),
    ("SG24-9002-00", "Synthetic Series Volume 2"),
    ("sg249999", "JES2 Synthetic Redbook"),
    ("REDP-9004-00", "Redpaper Handbook"),
    ("REDP-9004-01", "Redpaper Handbook, Second Edition"),
    ("acme-jes2-runbook", "ACME JES2 Runbook"),
    ("acme-pd-notes", "ACME Problem Determination Notes"),
    ("kb201234", "KB Problem Determination Checklist"),
    # A site file named like a form number (ingest keeps that shape).
    ("OPS01-0042", "OPS Problem Determination Handbook"),
    ("SITE-SECRET-01", "Secret Site Procedures"),
]


def _catalog_agent(entries, per_page=200, build="b-1"):
    import httpx2

    def respond(request):
        assert request.url.path == "/v1/sources"
        cursor = request.url.params.get("cursor")
        start = 0 if cursor is None else int(cursor.removeprefix("ls1.p"))
        chunk = entries[start:start + per_page]
        nxt = f"ls1.p{start + per_page}" if start + per_page < len(entries) else None
        return httpx2.Response(200, json={
            "request_id": "x", "build_id": build, "next_cursor": nxt,
            "sources": [{"doc_id": d, "title": t, "source_revision": f"r|{d}"} for d, t in chunk]})

    return respond


@pytest.fixture
def corpus_check(tmp_path, monkeypatch, capsys):
    """The real coverage CLI over an inert HTTP boundary; returns status,
    stdout, stderr, the parsed --out file (or None) and the requests."""
    import httpx2

    from mainframe_rag.eval import coverage

    real_client = httpx2.Client

    def run(respond, wishlist=SYNTHETIC_WISHLIST):
        calls: list = []

        def record(request):
            calls.append(request)
            return respond(request)

        monkeypatch.setattr(httpx2, "Client", lambda **kw: real_client(
            transport=httpx2.MockTransport(record), **kw))
        args = ["--agent-url", "http://deployed-agent:8087"]
        if wishlist is not None:
            path = tmp_path / "wishlist.json"
            path.write_text(json.dumps(wishlist))
            args += ["--wishlist", str(path)]
        out = tmp_path / "coverage.json"
        out.unlink(missing_ok=True)
        status = coverage.main([*args, "--out", str(out)])
        captured = capsys.readouterr()
        data = json.loads(out.read_text()) if out.exists() else None
        return status, captured.out, captured.err, data, calls

    return run


def test_coverage_check_reports_present_missing_and_ambiguous(corpus_check):
    status, out, _, data, _ = corpus_check(_catalog_agent(SYNTHETIC_CATALOG))
    assert status == 0
    assert data["schema"] == "corpus_coverage/1"
    rows = {r["id"]: r for r in data["items"]}
    assert list(rows) == [i["id"] for i in SYNTHETIC_WISHLIST["items"]]
    # Two editions of one form number are one present document.
    assert rows["num"] == {"id": "num", "title": "Numbered Principles",
                           "category": "hardware_language_reference", "priority": 1,
                           "status": "present", "matched_by": "doc_number",
                           "doc_ids": ["SA99-0001-12", "SA99-0001-13"], "other_matches": 0}
    assert (rows["miss"]["status"], rows["miss"]["matched_by"], rows["miss"]["doc_ids"]) == (
        "missing", None, [])
    # Two form-number families behind one single-book title: ambiguous. A
    # title-matched id is named only as a Redbook/Redpaper number, since a
    # form-number shape alone can be a site file name; the rest are counted.
    assert (rows["amb"]["status"], rows["amb"]["doc_ids"], rows["amb"]["other_matches"]) == (
        "ambiguous", [], 2)
    assert (rows["one"]["status"], rows["one"]["matched_by"], rows["one"]["doc_ids"],
            rows["one"]["other_matches"]) == ("present", "title", [], 1)
    assert (rows["many"]["status"], rows["many"]["doc_ids"]) == (
        "present", ["SG24-9001-00", "SG24-9002-00"])
    # Two Redpaper editions are one family.
    assert (rows["rp"]["status"], rows["rp"]["doc_ids"]) == (
        "present", ["REDP-9004-00", "REDP-9004-01"])
    # The prefix keeps the site's own JES2 runbook out of the Redbook item.
    assert (rows["rb-jes2"]["status"], rows["rb-jes2"]["doc_ids"],
            rows["rb-jes2"]["other_matches"]) == ("present", ["sg249999"], 0)
    # Site documents (a stem with digits, a file named like a form number)
    # are counted, never named.
    assert (rows["pd"]["status"], rows["pd"]["doc_ids"], rows["pd"]["other_matches"]) == (
        "present", [], 3)
    # A numbered item whose number is absent but whose title keywords hit
    # another book is ambiguous, never present.
    assert (rows["renum"]["status"], rows["renum"]["matched_by"], rows["renum"]["doc_ids"],
            rows["renum"]["other_matches"]) == ("ambiguous", "title", [], 1)
    assert data["counts"] == {"present": 6, "missing": 1, "ambiguous": 2}
    lines = out.splitlines()
    assert any(line.split()[:3] == ["num", "1", "present"] for line in lines)
    assert any(line.split()[:3] == ["miss", "2", "missing"] for line in lines)
    assert any(line.split()[:3] == ["amb", "2", "ambiguous"] for line in lines)


def test_coverage_check_never_reports_what_the_wishlist_did_not_name(corpus_check):
    """Unmatched entries, title-matched private entries, the build id and
    every catalog title stay out of stdout, stderr and the export."""
    status, out, err, data, _ = corpus_check(_catalog_agent(SYNTHETIC_CATALOG, build="build-77"))
    assert status == 0
    blob = out + err + json.dumps(data)
    for doc_id in PRIVATE_IDS:
        assert doc_id not in blob
    wishlist_titles = {i["title"] for i in SYNTHETIC_WISHLIST["items"]}
    for _, title in SYNTHETIC_CATALOG:
        if title not in wishlist_titles:
            assert title not in blob
    assert "build-77" not in blob


def test_coverage_check_pages_the_whole_catalog(corpus_check):
    status, _, _, data, calls = corpus_check(_catalog_agent(SYNTHETIC_CATALOG, per_page=4))
    assert status == 0
    assert [c.url.params.get("cursor") for c in calls] == [None, "ls1.p4", "ls1.p8", "ls1.p12"]
    assert {c.url.params.get("limit") for c in calls} == {"200"}
    rows = {r["id"]: r for r in data["items"]}
    assert rows["rb-jes2"]["status"] == "present"  # second page
    assert rows["pd"]["other_matches"] == 3  # third and fourth pages


def test_coverage_check_prints_the_table_when_the_export_cannot_be_written(
        tmp_path, monkeypatch, capsys):
    import httpx2

    from mainframe_rag.eval import coverage

    real_client = httpx2.Client
    monkeypatch.setattr(httpx2, "Client", lambda **kw: real_client(
        transport=httpx2.MockTransport(_catalog_agent(SYNTHETIC_CATALOG)), **kw))
    wishlist = tmp_path / "wishlist.json"
    wishlist.write_text(json.dumps(SYNTHETIC_WISHLIST))
    status = coverage.main(["--agent-url", "http://deployed-agent:8087",
                            "--wishlist", str(wishlist), "--out", str(tmp_path)])
    captured = capsys.readouterr()
    assert status == 2
    assert any(line.split()[:3] == ["num", "1", "present"] for line in captured.out.splitlines())
    assert captured.err == "error: the --out file cannot be written\n"


def test_coverage_check_matches_by_number_without_a_title(corpus_check):
    import httpx2

    def respond(request):
        return httpx2.Response(200, json={"request_id": "x", "build_id": "b-1", "next_cursor": None,
                                          "sources": [{"doc_id": "SA99-0001-13"}]})

    status, _, _, data, _ = corpus_check(respond)
    assert status == 0
    assert data["items"][0]["status"] == "present"


def _unreadable(kind):
    import httpx2

    def respond(request):
        if kind == "no_catalog":
            return httpx2.Response(503, json={"code": "sources_unavailable", "message": "m"})
        if kind == "malformed":
            return httpx2.Response(200, json={"sources": "nope"})
        if kind == "transport":
            raise httpx2.ConnectError("refused")
        build = "b-1" if request.url.params.get("cursor") is None else "b-2"
        return httpx2.Response(200, json={
            "request_id": "x", "build_id": build,
            "next_cursor": "ls1.p1" if build == "b-1" else None,
            "sources": [{"doc_id": "SA99-0001-13", "title": "t"}]})

    return respond


@pytest.mark.parametrize(("kind", "reason"), [
    ("no_catalog", "http_503"), ("malformed", "malformed"),
    ("transport", "request_failed"), ("build_changed", "build_changed"),
])
def test_coverage_check_fails_closed_when_the_catalog_is_unreadable(corpus_check, kind, reason):
    status, out, err, data, _ = corpus_check(_unreadable(kind))
    assert status == 1
    assert out == "" and data is None  # no partial table, no export
    assert err == f"error: the agent's source catalog could not be read ({reason})\n"


def test_coverage_check_refuses_an_invalid_wishlist_before_any_request(corpus_check):
    bad = {"schema": "corpus_wishlist/1", "items": [
        {"id": "x", "category": "hlasm_library", "title": "X", "priority": 1,
         "match": "title", "doc_numbers": ["SA99-0001"]}]}
    status, out, err, data, calls = corpus_check(_catalog_agent(SYNTHETIC_CATALOG), wishlist=bad)
    assert status == 2 and out == "" and data is None and calls == []
    assert err.startswith("error: invalid wishlist: ")


def test_coverage_check_reads_the_2cd8f67d_catalog(corpus_check):
    """The site's current agent: its captured catalog pages carry titles."""
    status, out, err, data, calls = corpus_check(_capture_agent("hits"))
    assert status == 0
    assert len(calls) == len(CAPTURE["sources_pages"])
    assert {r["status"] for r in data["items"]} == {"missing"}
    guide = {"schema": "corpus_wishlist/1", "items": [
        {"id": "guide", "category": "practitioner", "title": "A Guide", "priority": 1,
         "match": "title", "title_keywords": [["synthetic guide"]]}]}
    status, out, err, data, _ = corpus_check(_capture_agent("hits"), wishlist=guide)
    assert status == 0
    assert data["items"] == [{"id": "guide", "title": "A Guide", "category": "practitioner",
                              "priority": 1, "status": "present", "matched_by": "title",
                              "doc_ids": [], "other_matches": 1}]
    blob = out + err + json.dumps(data)
    for page in CAPTURE["sources_pages"]:
        for source in page["body"]["sources"]:
            assert source["doc_id"] not in blob and source["title"] not in blob


def test_shipped_wishlist_is_valid_and_runs_by_default(corpus_check):
    from mainframe_rag.eval import coverage

    items = coverage.load_wishlist(coverage.DEFAULT_WISHLIST)
    ids = [i["id"] for i in items]
    assert {i["category"] for i in items} == set(coverage.CATEGORIES)
    numbers = [n for i in items for n in i.get("doc_numbers", [])]
    assert len(numbers) == len(set(numbers))
    status, _, _, data, _ = corpus_check(
        _catalog_agent([("SITE-SECRET-01", "Secret Site Procedures")]), wishlist=None)
    assert status == 0
    assert [r["id"] for r in data["items"]] == ids
    assert {r["status"] for r in data["items"]} == {"missing"}


# ------------------------------------------------------------- image arms (#1287)

IMAGE_ROW_KEYS = EXPORT_ROW_KEYS | {
    "image_arm", "image_n", "image_bytes", "image_reads_n", "image_read_failed", "image_cer_bucket"}
IMAGE_CHAT = ["--endpoint", "chat", "--stream", "--image-arm"]


def _image_stream(reads=None) -> bytes:
    """A chat stream whose finish chunk carries the agent's own image fields
    (`reads`: one ImageRead, or None for a failed read, per image; None for
    a finish chunk without image fields), inside `choices[0]` like every
    other finish field."""
    from mainframe_rag.agent.images import TurnImages

    image = {} if reads is None else TurnImages(images=(), typed="",
                                                reads=list(reads)).final_fields()
    frames = [format_openai_chunk("chatcmpl-abc", "m", delta_content="IEA500I is rejected.\n\n"),
              format_openai_chunk("chatcmpl-abc", "m",
                                  delta_content=f"\n\n**Citations:**\n- {CITE}"),
              format_openai_chunk("chatcmpl-abc", "m", finish_reason="stop", extra={
                  "citations": [CITE], "citations_inferred": False, "inferred_indices": [],
                  "hits": [], "verification_state": "accepted", "script": None,
                  "script_lang": None, "script_review_required": False,
                  "supplied_evidence": [_supplied()], "evidence_omitted": 0, **image}),
              format_openai_done()]
    return "".join(frames).encode()


def _read(screen: str, description: str = ""):
    from mainframe_rag.agent.images import ImageRead

    return ImageRead(screen_text=screen, description=description)


def _shown(screen: str, description: str = "") -> str:
    """`image_reads[].text` as the agent builds it."""
    return _read(screen, description).shown


def _image_agent(read_for):
    """`read_for(chat_body)` -> the SSE bytes of that chat call."""
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": [{"doc_id": "SA22-0000-00"}]})
        assert request.url.path == "/v1/chat"
        return _sse(read_for(json.loads(request.content)))

    return respond


def _image_parts(body: dict) -> tuple[list[str], list[bytes]]:
    import base64

    (message,) = body["messages"]
    texts = [p["text"] for p in message["content"] if p["type"] == "text"]
    images = []
    for part in message["content"]:
        if part["type"] == "image_url":
            prefix, _, data = part["image_url"]["url"].partition(",")
            assert prefix == "data:image/png;base64"
            images.append(base64.b64decode(data, validate=True))
    return texts, images


def _chat_calls(calls) -> list[dict]:
    return [json.loads(c.content) for c in calls if c.url.path == "/v1/chat"]


def test_levenshtein_and_cer_buckets_by_hand():
    from mainframe_rag.eval.image_arm import cer_bucket, character_error_rate, levenshtein

    assert levenshtein("KITTEN", "SITTING") == 3
    assert levenshtein("", "ABC") == levenshtein("ABC", "") == 3
    assert levenshtein("FLAW", "LAWN") == 2 and levenshtein("SAME", "SAME") == 0
    # Case and layout are the panel's; then one substitution in 20 characters.
    assert character_error_rate("IEA500I REPORTS WHAT", "iea500i\nreports  whaT") == 0
    assert character_error_rate("IEA500I REPORTS WHAT", "IEA500I REPORTS WHAX") == 0.05
    assert character_error_rate("", "anything") is None
    assert [cer_bucket(v) for v in (0, 0.0, 0.049, 0.05, 0.19, 0.2, 0.49, 0.5, 3.0)] == [
        "0", "0", "<0.05", "<0.2", "<0.2", "<0.5", "<0.5", ">=0.5", ">=0.5"]
    assert {cer_bucket(v) for v in (None, True, "0", float("nan"), -1)} == {"none"}


def test_cer_scores_the_agents_screen_text_not_its_description():
    from mainframe_rag.eval.image_arm import character_error_rate

    reference = "IEA500I REPORTS WHAT"
    assert character_error_rate(reference, _shown("IEA500I REPORTS WHAT", "A panel.")) == 0
    # A read with no screen text misses every drawn character.
    assert character_error_rate(reference, _shown("", "A panel.")) == 1


def test_image_arms_ask_the_same_rows_three_ways(site_eval):
    """text: today's body; image: one PNG data URL alone; image+ask: the
    same image after the fixed typed question. Panels are 80 columns wide
    whatever the question's length."""
    from mainframe_rag.eval.image_arm import IMAGE_ASK

    entries = [_entry(), _entry(id="LONG", query="Why does IEA500I appear " * 8)]

    def agent():
        return _image_agent(lambda body: _image_stream([_read("WHAT DOES IEA500I REPORT?")]))

    _, _, _, plain = site_eval(agent(), entries, ["--endpoint", "chat", "--stream"])
    bodies, samples = {}, []
    for arm in ("text", "image", "image+ask"):
        status, report, _, calls = site_eval(agent(), entries, [*IMAGE_CHAT, arm])
        assert status == 0, arm
        bodies[arm] = _chat_calls(calls)
        samples.append(report["params"]["sample"])
        assert (report["params"]["image_arm"], report["params"]["image_columns"]) == (arm, 80)
    assert samples[0] == samples[1] == samples[2]
    assert bodies["text"] == _chat_calls(plain)
    widths = set()
    for image_body, ask_body in zip(bodies["image"], bodies["image+ask"], strict=True):
        texts, (png,) = _image_parts(image_body)
        assert texts == [] and png.startswith(b"\x89PNG\r\n\x1a\n")
        widths.add(int.from_bytes(png[16:20], "big"))
        assert _image_parts(ask_body) == ([IMAGE_ASK], [png])
        assert image_body["stream"] is True and image_body["temperature"] == 0.0
    assert len(widths) == 1


def test_image_rows_record_the_read_and_its_cer(site_eval):
    reads = {
        "EXACT": [_read("WHAT DOES IEA500I REPORT?", "A panel.")],
        "GARBLED": [_read("XXXX")],
        "FAILED": [None],
        "OLD": None,
    }
    entries = [_entry(id=key) for key in reads]
    streams = iter(reads.values())

    status, report, export, _ = site_eval(
        _image_agent(lambda body: _image_stream(next(streams))), entries, [*IMAGE_CHAT, "image"])
    assert status == 0
    rows = {r["id"]: r for r in json.loads(export)["rows"]}
    assert all(set(r) == IMAGE_ROW_KEYS for r in rows.values())
    assert [(rows[k]["image_cer_bucket"], rows[k]["image_reads_n"], rows[k]["image_read_failed"])
            for k in reads] == [("0", 1, False), (">=0.5", 1, False), ("none", 0, True),
                                ("none", None, False)]
    assert {r["image_arm"] for r in rows.values()} == {"image"}
    assert {r["image_n"] for r in rows.values()} == {1}
    assert all(r["image_bytes"] > 0 for r in rows.values())
    assert report["metrics"]["image"] == {
        "arm": "image", "n": 4, "read_failed": 1, "reads_missing": 2,
        "cer": {"0": 1, "<0.05": 0, "<0.2": 0, "<0.5": 0, ">=0.5": 1, "none": 2}}


def test_image_export_carries_no_question_image_or_transcript(site_eval):
    marker = "CANARY-TRANSCRIPT-5e11"
    entries = [_entry(id="ASK", query=f"{CANARIES['query']} what now")]

    def read_for(body):
        return _image_stream([_read(f"{CANARIES['query']} WHAT NOW {marker}", marker)])

    for arm in ("image", "image+ask"):
        _, report, export, calls = site_eval(_image_agent(read_for), entries, [*IMAGE_CHAT, arm])
        (body,) = _chat_calls(calls)
        encoded = body["messages"][0]["content"][-1]["image_url"]["url"].partition(",")[2]
        assert len(encoded) > 200
        local, text = json.dumps(report), export.decode("utf-8")
        assert CANARIES["query"] in local  # the local report holds the question
        assert marker not in local and encoded[:40] not in local  # transcript, image: never kept
        for needle in (CANARIES["query"], marker, encoded[:40], encoded[-40:], "base64",
                       "WHAT NOW", "what now"):
            assert needle.lower() not in text.lower(), needle
        (row,) = json.loads(text)["rows"]
        assert set(row) == IMAGE_ROW_KEYS and row["image_cer_bucket"] in ("<0.5", ">=0.5")


def test_text_arm_rows_carry_the_image_keys_and_no_image(site_eval):
    status, report, export, _ = site_eval(
        _image_agent(lambda body: _image_stream()), [_entry()], [*IMAGE_CHAT, "text"])
    (row,) = json.loads(export)["rows"]
    assert status == 0 and set(row) == IMAGE_ROW_KEYS
    assert (row["image_arm"], row["image_n"], row["image_bytes"], row["image_reads_n"],
            row["image_cer_bucket"]) == ("text", 0, 0, None, "none")
    assert report["metrics"]["image"]["cer"] is None


def test_image_arm_health_failure_rows_carry_the_image_keys(site_eval):
    import httpx2

    status, _, export, _ = site_eval(lambda r: httpx2.Response(503, json={}), [_entry()],
                                     [*IMAGE_CHAT, "image"])
    (row,) = json.loads(export)["rows"]
    assert status == 2 and set(row) == IMAGE_ROW_KEYS
    assert (row["image_arm"], row["image_n"], row["image_cer_bucket"]) == ("image", None, "none")


@pytest.mark.parametrize("extra", [
    ["--stream", "--image-arm", "image"],
    ["--endpoint", "chat", "--stream", "--turns", "2", "--arm", "a", "--image-arm", "image"],
    ["--endpoint", "chat", "--stream", "--image-arm", "photo"],
])
def test_image_arm_flags_are_refused_when_inconsistent(site_eval, extra):
    with pytest.raises(SystemExit) as exc:
        site_eval(lambda r: pytest.fail("no request"), [_entry()], extra)
    assert exc.value.code == 2


def test_image_arm_refuses_the_frozen_holdout_before_any_read(tmp_path, monkeypatch, capsys):
    """VENUE=rc adds the holdout to the default golden set; the image arm
    refuses that set before reading a row or sending a request."""
    import httpx2

    from mainframe_rag.eval import answers

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VENUE", "rc")
    monkeypatch.setattr(httpx2, "Client", lambda **kw: pytest.fail("no request"))
    monkeypatch.setattr(answers, "load_golden_records_with_identity",
                        lambda *a, **k: pytest.fail("no read"))
    status = answers.main(["--agent-url", "http://deployed-agent:8087", "--endpoint", "chat",
                           "--stream", "--image-arm", "image+ask"])
    assert status == 2
    assert "--image-arm needs an explicit non-holdout --golden" in capsys.readouterr().err


# ------------------------------------------- graded capture (#1191 PR 1)
# Stage attribution needs what the agent packed and retrieved for this very
# answer. Rows keep that as text-free metadata (doc, physical pages, char
# range, a text hash); the excerpt text only on request, in the private
# report. Headings, titles, citation strings and hit text are never copied.


def _excerpt(**overrides) -> dict:
    from mainframe_rag.agent.answer import SuppliedExcerpt

    c = CANARIES
    base = {"index": 1, "citation": f"SA22-0000-00 {c['cite']}, {c['heading']}, p. 6",
            "doc_id": "SA22-0000-00", "title": c["title"], "heading": c["heading"],
            "page_label": "1-6", "pdf_page_start": 6, "pdf_page_end": 7, "start_char": 10,
            "end_char": 40, "source_chars": 50, "truncated_start": True, "truncated_end": True,
            "text": c["excerpt"]}
    base.update(overrides)
    return SuppliedExcerpt(**base).model_dump()


def _hit(**overrides) -> dict:
    from mainframe_rag.retrieve.query import SearchHit

    c = CANARIES
    base = {"chunk_id": "chunk-1", "score": 0.75, "cite": f"SA22-0000-00 {c['cite']}, p. 6",
            "heading": c["heading"], "text": c["excerpt"], "doc_id": "SA22-0000-00",
            "title": c["title"], "page_label": "1-6", "chunk_type": "message",
            "message_ids": ("IEA500I",), "page_start": 5, "page_end": 6, "rerank_score": 0.5}
    base.update(overrides)
    return SearchHit(**base).model_dump(mode="json")


# Expected projections, written out by hand (not derived from the code).
EXCERPT_META = {
    "index": 1, "doc_id": "SA22-0000-00", "page_label": "1-6", "pdf_page_start": 6,
    "pdf_page_end": 7, "start_char": 10, "end_char": 40, "source_chars": 50,
    "truncated_start": True, "truncated_end": True, "text_clipped": False,
    "duplicate_citation": False, "cited": False,
}
# The hit's 0-based page span in the supplied-evidence convention (1-based).
HIT_META = {"chunk_id": "chunk-1", "doc_id": "SA22-0000-00", "chunk_type": "message",
            "page_label": "1-6", "pdf_page_start": 6, "pdf_page_end": 7, "score": 0.75,
            "rerank_score": 0.5}


def _capture_respond(transport: str, omitted: int = 2):
    import httpx2

    supplied, hits = [_excerpt()], [_hit()]

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": [{"doc_id": "SA22-0000-00"}]})
        if transport == "chat":
            body = chat_stream("IEA500I reports a rejected command [1].", supplied=supplied,
                               hits=hits)
            # The finish chunk's own `evidence_omitted`, as the agent sends it.
            return _sse(body.replace(b'"evidence_omitted": 0',
                                     f'"evidence_omitted": {omitted}'.encode()))
        final = {"supplied_evidence": supplied, "evidence_omitted": omitted}
        if transport == "sse":
            return _sse(_answer_final(hits=hits, **final))
        # The buffered /v1/answer body carries no hits (AnswerResponse).
        return httpx2.Response(200, json={
            "request_id": "r1", "answer": "IEA500I reports a rejected command.",
            "citations": [CITE], "verification_state": "accepted", "script": None, **final})

    return respond


CAPTURE_ARGS = {"json": [], "sse": ["--stream"], "chat": ["--endpoint", "chat", "--stream"]}


@pytest.mark.parametrize("transport", ["json", "sse", "chat"])
def test_rows_keep_packed_evidence_and_final_hits_as_text_free_metadata(site_eval, transport):
    status, report, _, _ = site_eval(_capture_respond(transport), [_entry()],
                                     CAPTURE_ARGS[transport])
    row = report["results"][0]
    assert status == 0 and row["verdict"] == "pass", row["failures"]
    expected = {**EXCERPT_META,
                "text_sha256": hashlib.sha256(CANARIES["excerpt"].encode("utf-8")).hexdigest()}
    assert row["supplied_evidence_meta"] == [expected]
    assert row["evidence_omitted"] == 2
    assert row["final_hits"] == (None if transport == "json" else [HIT_META])
    assert "supplied_evidence_text" not in row
    captured = json.dumps([row["supplied_evidence_meta"], row["final_hits"]])
    for name in ("excerpt", "heading", "title", "cite"):
        assert CANARIES[name] not in captured, name


def test_excerpt_text_is_kept_only_on_request_and_never_exported(site_eval):
    status, report, export, _ = site_eval(_capture_respond("chat"), [_entry()],
                                          [*CAPTURE_ARGS["chat"], "--keep-evidence-text"])
    row = report["results"][0]
    assert status == 0 and row["supplied_evidence_text"] == [
        {"index": 1, "text": CANARIES["excerpt"]}]
    assert report["params"]["keep_evidence_text"] is True
    text = export.decode("utf-8")
    for name in ("excerpt", "heading", "title", "cite"):
        assert CANARIES[name] not in text, name
    assert all(set(r) == EXPORT_ROW_KEYS for r in json.loads(text)["rows"])


def test_capture_from_an_agent_without_the_fields_is_unknown_not_empty(site_eval):
    """A pre-#635 agent sends no supplied evidence and no hits: unknown
    (None), never a known empty pack."""
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": []})
        final = json.loads(_answer_final().decode().split("data: ", 1)[1])
        final.pop("hits")
        return _sse(f"event: final\ndata: {json.dumps(final)}\n\n".encode())

    status, report, _, _ = site_eval(respond, [_entry()], ["--stream"])
    row = report["results"][0]
    assert status == 0, row["failures"]
    assert (row["supplied_evidence_meta"], row["evidence_omitted"], row["final_hits"]) == (
        None, None, None)


@pytest.mark.parametrize("bad", [
    {"index": "1"}, {"doc_id": None}, {"pdf_page_start": "6"}, {"text": 7},
], ids=["index", "doc", "page", "text"])
def test_a_malformed_supplied_entry_makes_the_capture_unknown(site_eval, bad):
    """Observational only: a malformed entry voids the capture (None) and
    never fails the row."""
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": []})
        entry = {**_excerpt(), **bad}
        return _sse(_answer_final(supplied_evidence=[entry], evidence_omitted=0, hits=[_hit()]))

    status, report, _, _ = site_eval(respond, [_entry()], ["--stream"])
    row = report["results"][0]
    assert status == 0 and row["verdict"] == "pass"
    assert row["supplied_evidence_meta"] is None and row["final_hits"] == [HIT_META]


# Spec review F1 (#1279): the "Cited" stage maps the answer's citations to
# the packed excerpts. Two excerpts can share a doc and a page under different
# headings, so doc and page alone cannot say which one was cited; each excerpt
# carries `cited`, never the citation line itself.
LINE_A = "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 6"
LINE_B = "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA501I, p. 6"


def _cited_row(site_eval, transport: str, supplied: list, **final) -> dict:
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": []})
        if transport == "chat":
            return _sse(chat_stream("IEA500I reports a rejected command [2].",
                                    supplied=supplied, **final))
        return _sse(_answer_final(supplied_evidence=supplied, evidence_omitted=0, **final))

    _, report, export, _ = site_eval(respond, [_entry()], CAPTURE_ARGS[transport])
    row = report["results"][0]
    assert row["verdict"] in ("pass", "fail"), row["failures"]
    assert b'"cited"' not in export and b"IEA501I" not in export
    return row


def test_each_packed_excerpt_says_whether_its_citation_was_given(site_eval):
    """Same doc, same page, different headings: only the excerpt whose line
    the answer cites is cited. A validated citation names a line, and the
    agent and the Open WebUI Pipe map it to every excerpt carrying that line,
    so a repeated line (`duplicate_citation`) is cited on each excerpt."""
    supplied = [_excerpt(index=1, citation=LINE_A, duplicate_citation=True),
                _excerpt(index=2, citation=LINE_B),
                _excerpt(index=3, citation=LINE_A, duplicate_citation=True)]
    row = _cited_row(site_eval, "sse", supplied, citations=[LINE_A])
    meta = row["supplied_evidence_meta"]
    assert [(e["doc_id"], e["pdf_page_start"]) for e in meta] == [("SA22-0000-00", 6)] * 3
    assert [e["cited"] for e in meta] == [True, False, True]
    assert LINE_A not in json.dumps(meta) and "IEA501I" not in json.dumps(meta)


def test_an_inferred_citation_marks_its_prompt_index_and_never_guesses(site_eval):
    """Inferred citations come from bracket indices (`inferred_indices`).
    The agent records an index only when its line is not listed yet, so an
    answer bracketing [2] and [1], two excerpts on one line, lists only 2:
    excerpt 2 is cited, excerpt 1 is unknown (null, it may or may not have
    been bracketed), and an excerpt on a line not listed is not cited."""
    supplied = [_excerpt(index=1, citation=LINE_A, duplicate_citation=True),
                _excerpt(index=2, citation=LINE_A, duplicate_citation=True),
                _excerpt(index=3, citation=LINE_B)]
    row = _cited_row(site_eval, "chat", supplied, citations=(LINE_A,), inferred=True,
                     indices=(2,))
    assert [e["cited"] for e in row["supplied_evidence_meta"]] == [None, True, False]


@pytest.mark.parametrize(("supplied", "final"), [
    ([{**_excerpt(), "citation": None}], {"citations": [LINE_A]}),
    ([_excerpt(citation=LINE_A)],
     {"citations": [LINE_A], "citations_inferred": True, "inferred_indices": []}),
], ids=["excerpt-without-line", "inferred-without-indices"])
def test_cited_is_unknown_when_the_mapping_cannot_be_read(site_eval, supplied, final):
    """An excerpt with no citation line, or an inferred line with no index
    recorded for the excerpt: `cited` is null (unknown), never guessed, and
    the rest of the capture is kept."""
    row = _cited_row(site_eval, "sse", supplied, **final)
    assert [e["cited"] for e in row["supplied_evidence_meta"]] == [None]
    assert row["supplied_evidence_meta"][0]["doc_id"] == "SA22-0000-00"


# --------------------------------- fixed-history /v1/chat sessions (#1191)


def _session(**overrides) -> dict:
    base = {"id": "GTU-01", "query_class": "message_id", "expected_behavior": "answer",
            "expected_doc_ids": ["SA22-0000-00"],
            "turns": [{"role": "user", "content": "What does IEA500I report?"},
                      {"role": "assistant", "content": "It reports a rejected command."},
                      {"role": "user", "content": "And how do I resolve it?"}]}
    base.update(overrides)
    return base


def _write_sessions(tmp_path, sessions, name="sessions.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(s) + "\n" for s in sessions))
    return path


def _pipe_body(turns: list[dict]) -> dict:
    """Independent expectation: the body the real Open WebUI Pipe sends for
    this conversation, captured at its HTTP boundary."""
    import asyncio

    import httpx
    from integrations.openwebui import docs_pipe

    sent: list[dict] = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=chat_stream("ok [1]."),
                              headers={"content-type": "text/event-stream"})

    async def emit(_event):
        return None

    async def run():
        pipe = docs_pipe.Pipe()
        pipe._transport = httpx.MockTransport(handler)
        result = await pipe.pipe({"model": "docs", "stream": True, "messages": turns},
                                 __user__={"id": "u1"}, __event_emitter__=emit,
                                 __task__=None, __metadata__={})
        return [item async for item in result]

    asyncio.run(run())
    assert len(sent) == 1
    return sent[0]


def _session_agent():
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        assert request.url.path == "/v1/chat", "a session never searches on its own"
        return _sse(chat_stream("Reenter the command [1].", supplied=[_excerpt()],
                                hits=[_hit()]))

    return respond


SESSION_ARGS = ["--endpoint", "chat", "--stream"]
SESSION_ROW_KEYS = (TWO_TURN_ROW_KEYS - {"turn"}) | {"session_turns"}


def test_a_session_sends_its_fixed_history_as_the_pipe_does(site_eval, tmp_path):
    session = _session()
    path = _write_sessions(tmp_path, [session])
    status, report, _, calls = site_eval(_session_agent(), [], SESSION_ARGS,
                                         dataset=["--sessions", str(path)])
    assert status == 0, report["results"]
    assert _chat_bodies(calls) == [{**_pipe_body(session["turns"]), "temperature": 0.0}]
    row = report["results"][0]
    assert row["id"] == "GTU-01" and row["query"] == "And how do I resolve it?"
    assert row["verdict"] == "pass" and row["session_turns"] == 3
    assert row["prior_answer_chars"] == len("It reports a rejected command.")
    assert row["evidence_doc_ids"] == ["SA22-0000-00"] and row["evidence_gold_hit"] is True
    assert row["supplied_evidence_meta"][0]["pdf_page_start"] == 6
    assert report["params"]["sessions"] == {
        "name": "sessions.jsonl", "n": 1, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert report["params"]["pipe_sha256"]


def test_every_session_runs_once_in_file_order(site_eval, tmp_path):
    """Sessions are authored rows, never sampled: more than the default
    sample size (24) all run, in the file's order."""
    ids = [f"GTU-{n:02d}" for n in range(30, 0, -1)]
    path = _write_sessions(tmp_path, [_session(id=i) for i in ids])
    status, report, _, calls = site_eval(_session_agent(), [], SESSION_ARGS,
                                         dataset=["--sessions", str(path)])
    assert status == 0 and [r["id"] for r in report["results"]] == ids
    assert len(_chat_bodies(calls)) == 30
    assert report["params"]["sample"]["mode"] == "sessions"


def test_two_turn_follow_ups_keep_excerpt_text_when_asked(site_eval):
    opener = _entry()
    agent = _two_turn_agent({(opener["query"], 1): chat_stream("Rejected [1].", supplied=[_excerpt()]),
                             (opener["query"], 3): chat_stream("Reenter it [1].",
                                                               supplied=[_excerpt()])})
    status, report, _, _ = site_eval(agent, [opener], [*TWO_TURN, "--keep-evidence-text"])
    assert status == 0
    assert [r["supplied_evidence_text"] for r in report["results"]] == [
        [{"index": 1, "text": CANARIES["excerpt"]}]] * 2


def test_a_session_export_carries_no_turn_text(site_eval, tmp_path):
    c = CANARIES
    session = _session(turns=[{"role": "user", "content": c["query"]},
                              {"role": "assistant", "content": c["answer"]},
                              {"role": "user", "content": c["delta"]}])
    path = _write_sessions(tmp_path, [session])
    status, report, export, _ = site_eval(_session_agent(), [], SESSION_ARGS,
                                          dataset=["--sessions", str(path)])
    assert status == 0 and c["delta"] in json.dumps(report)  # live canary
    text = export.decode("utf-8")
    for name in ("query", "answer", "delta", "excerpt", "heading"):
        assert c[name] not in text, name
    assert all(set(r) == SESSION_ROW_KEYS for r in json.loads(text)["rows"])


@pytest.mark.parametrize("turns", [
    [{"role": "assistant", "content": "a"}, {"role": "user", "content": "q"}],
    [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
    [{"role": "user", "content": "q"}, {"role": "user", "content": "q2"}],
    [{"role": "user", "content": "q"}, {"role": "system", "content": "s"},
     {"role": "user", "content": "q2"}],
    [{"role": "user", "content": " "}],
    [],
], ids=["leading-answer", "ends-on-answer", "two-users", "system", "blank-text", "no-turns"])
def test_a_malformed_session_is_refused_before_any_request(tmp_path, capsys, turns):
    from mainframe_rag.eval import answers

    path = _write_sessions(tmp_path, [_session(turns=turns)])
    status = answers.main(["--agent-url", "http://deployed-agent:8087", "--sessions", str(path),
                           *SESSION_ARGS])
    assert status == 2 and "FAIL: session" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["holdout.jsonl", "release_set.jsonl"], ids=["holdout", "release"])
def test_sessions_refuse_the_protected_sets_before_any_read(tmp_path, monkeypatch, capsys, name):
    from mainframe_rag.eval import answers

    path = tmp_path / name
    path.write_text("not read\n")
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("protected set was read")
                        if self.name == name else real(self))
    status = answers.main(["--agent-url", "http://deployed-agent:8087", "--sessions", str(path),
                           *SESSION_ARGS])
    assert status == 2 and "FAIL:" in capsys.readouterr().err


@pytest.mark.parametrize(("content", "message"), [
    (json.dumps({"record": "manifest", "set_id": "x"}).encode() + b"\n",
     "FAIL: release-set records are not a dev dataset"),
    (b"\n", "FAIL: sessions file has no sessions"),
    ((json.dumps(_session(id="A")) + "\n" + json.dumps(_session(id="A")) + "\n").encode(),
     "FAIL: session line 2: missing or duplicate id"),
    (json.dumps(_session(expected_doc_ids="SA22-0000-00")).encode() + b"\n",
     "FAIL: session line 1: expected_doc_ids"),
    (b"\xff\xfe\n", "FAIL: session file is not UTF-8"),
    (None, "FAIL: dev-only dataset is unreadable"),
], ids=["release-record", "empty", "duplicate-id", "doc-ids", "not-utf8", "unreadable"])
def test_a_sessions_file_that_is_not_dev_sessions_is_refused(tmp_path, capsys, content, message):
    """A renamed release-set copy (by content), an empty file, duplicate ids,
    a malformed doc-id list, undecodable bytes and an unreadable path (a
    directory): exit 2 before any request, a fixed message with no text."""
    from mainframe_rag.eval import answers

    path = tmp_path / "sessions.jsonl"
    if content is None:
        path.mkdir()
    else:
        path.write_bytes(content)
    status = answers.main(["--agent-url", "http://deployed-agent:8087", "--sessions", str(path),
                           *SESSION_ARGS])
    err = capsys.readouterr().err
    assert status == 2 and err.strip().splitlines()[-1] == message
    assert "IEA500I" not in err and "rejected command" not in err


def test_a_malformed_hit_makes_final_hits_unknown(site_eval):
    import httpx2

    def respond(request):
        if request.url.path == "/healthz":
            return httpx2.Response(200, json=HEALTH)
        if request.url.path == "/v1/search":
            return httpx2.Response(200, json={"hits": []})
        return _sse(_answer_final(supplied_evidence=[_excerpt()], evidence_omitted=0,
                                  hits=[{**_hit(), "page_start": "5"}]))

    status, report, _, _ = site_eval(respond, [_entry()], ["--stream"])
    row = report["results"][0]
    assert status == 0 and row["verdict"] == "pass"
    assert row["final_hits"] is None and row["supplied_evidence_meta"] is not None


@pytest.mark.parametrize("extra", [
    ["--stream"], [*SESSION_ARGS, "--golden", "x.jsonl"], [*SESSION_ARGS, "--all"],
    [*SESSION_ARGS, "--subset", "8"], [*TWO_TURN],
], ids=["answer-endpoint", "golden", "all", "subset", "two-turn"])
def test_session_flags_are_refused_when_inconsistent(tmp_path, extra):
    from mainframe_rag.eval import answers

    path = _write_sessions(tmp_path, [_session()])
    with pytest.raises(SystemExit) as exc:
        answers.main(["--agent-url", "http://deployed-agent:8087", "--sessions", str(path),
                      *extra])
    assert exc.value.code == 2


def test_a_history_the_pipe_cannot_fit_is_a_fixed_error_row(site_eval, tmp_path):
    """A last turn larger than the Pipe's body cap means the Pipe makes no
    call: an error row with a fixed label, and no request."""
    from integrations.openwebui import docs_pipe

    turns = [{"role": "user", "content": "x" * (docs_pipe.DEFAULT_MAX_BODY_CHARS + 1)}]
    path = _write_sessions(tmp_path, [_session(turns=turns)])
    status, report, export, calls = site_eval(_session_agent(), [], SESSION_ARGS,
                                              dataset=["--sessions", str(path)])
    row = report["results"][0]
    assert status == 1 and row["verdict"] == "error"
    assert row["failures"] == ["session request not built"]
    assert not _chat_bodies(calls)
    assert json.loads(export)["rows"][0]["failures"] == ["session request not built"]


def test_image_arm_and_sessions_are_refused_together(capsys):
    """Train 2026-11-17c (#1287 x #1191): the image arm draws dev golden
    questions and --sessions is its own dataset, so the pair is a usage
    error before any read or request."""
    from mainframe_rag.eval import answers

    with pytest.raises(SystemExit) as exc:
        answers.main(["--agent-url", "http://agent.invalid", "--endpoint", "chat", "--stream",
                      "--sessions", "sessions.jsonl", "--image-arm", "image"])
    assert exc.value.code == 2
    assert "--image-arm draws dev golden questions: drop --sessions" in capsys.readouterr().err
