"""Tests for agent/tokenizer.py (vLLM /tokenize client and fallback)."""

import json
import logging
import threading

import httpx2
import pytest

from mainframe_rag.agent.tokenizer import (
    TOKENIZE_REFUSED_RETRY_AFTER_S,
    TOKENIZE_RETRY_AFTER_S,
    CompletionUsageTokenizer,
    FallbackTokenizer,
    VllmTokenizer,
    build_tokenizer,
    estimate_tokens,
)
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage
from tests.fakes import TokenizerPostFake


def test_fallback_estimator_counts_text_and_messages():
    assert estimate_tokens("") == 0
    assert estimate_tokens("Hello world, this is a test.") >= 6
    tok = FallbackTokenizer()
    messages = [
        ChatMessage(role="system", content="Be terse."),
        ChatMessage(role="user", content="IEA500I operator action"),
    ]
    assert tok.count_messages(messages) >= sum(tok.count_tokens(m.content) for m in messages)
    assert tok.count_messages([]) == 0


def test_vllm_tokenizer_calls_origin_root():
    """LLM_BASE_URL ends in /v1; vLLM serves /tokenize at the server root,
    so the request must strip /v1 and hit the origin (/v1/tokenize is 404)."""
    captured = {}
    client = TokenizerPostFake(
        count=7, extra={"max_model_len": 4096, "tokens": [1, 2, 3, 4, 5, 6, 7]}, capture=captured
    )

    tok = VllmTokenizer(
        base_url="http://mock-llm:8000/v1",
        model="mock-reasoning",
        client=client,
    )
    tokens = tok.count_tokens("Some query text to tokenize")
    assert tokens == 7
    assert captured["url"] == "http://mock-llm:8000/tokenize"
    assert captured["json"]["model"] == "mock-reasoning"
    bare = {}
    VllmTokenizer(
        base_url="http://mock-llm:8000", model="m", client=TokenizerPostFake(count=3, capture=bare)
    ).count_tokens("abc")
    assert bare["url"] == "http://mock-llm:8000/tokenize"


def test_tokenize_auth_header_follows_the_reasoning_key():
    """A gateway-guarded /tokenize sees the reasoning leg's key; keyless
    setups keep the pre-gateway shape (no header)."""
    keyless = {}
    VllmTokenizer(
        base_url="http://mock-llm:8000/v1", model="m", client=TokenizerPostFake(count=7, capture=keyless)
    ).count_tokens("abc")
    assert keyless["headers"] == {}
    captured = {}
    tok = build_tokenizer(
        Settings(
            llm_base_url="http://mock-llm:8000/v1",
            llm_model_reasoning="m",
            llm_api_key="sk-test-llm",
            _env_file=None,
        ),
        client=TokenizerPostFake(count=7, capture=captured),
    )
    assert tok.count_tokens("abc") == 7
    assert captured["headers"] == {"Authorization": "Bearer sk-test-llm"}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize("fake,transient", [
    (lambda status=status: TokenizerPostFake(count=0, status_code=status), False)
    for status in (404, 405, 501)
] + [
    (lambda: TokenizerPostFake(count=0, raises=RuntimeError("network down")), True),
    (lambda: TokenizerPostFake(count=None, extra={"unexpected": "shape"}), False),
], ids=["unserved-404", "unserved-405", "unserved-501",
        "transport-error", "malformed-200-body"])
def test_failed_tokenize_downgrades_with_one_warning(fake, transient, caplog):
    """A non-200 (e.g. LiteLLM without /tokenize), transport error or 200
    without a usable count must warn once, answer from the estimator, leave
    remote_confirmed False, and not re-attempt the endpoint while it is
    downgraded (count_messages included)."""
    client = fake()
    clock = _Clock()
    tok = VllmTokenizer(base_url="http://litellm:4000/v1", model="m", client=client, clock=clock)
    assert tok.remote_confirmed is False  # nothing counted yet
    with caplog.at_level(logging.WARNING, logger="agent.tokenizer"):
        first = tok.count_tokens("some text to count here")
        clock.now += TOKENIZE_RETRY_AFTER_S - 1
        second = tok.count_tokens("another text to count")
        messages = [ChatMessage(role="user", content="IEA500I operator action")]
        counted = tok.count_messages(messages)
    assert first >= 5 and second >= 4  # estimator answered, not zero
    assert counted == FallbackTokenizer().count_messages(messages)
    assert len(client.calls) == 1  # no repeated doomed RPCs inside the window
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert tok.remote_confirmed is False
    # After the window only a transient failure is probed again; an endpoint
    # that is not served, or answers without a count, stays pinned.
    clock.now += 2
    tok.count_tokens("a later text")
    assert len(client.calls) == (2 if transient else 1)


@pytest.mark.parametrize("failure", [
    httpx2.Response(503),
    httpx2.Response(502),
    httpx2.Response(429),
    httpx2.Response(408),
    httpx2.ReadTimeout("slow"),
    httpx2.ConnectError("refused"),
], ids=["503", "502", "429", "408", "read-timeout", "connect-error"])
def test_transient_failure_recovers_after_the_retry_window(failure, caplog):
    """Issue #835: one transient failure no longer disables verified counting
    for the life of the process. Inside the window the estimator answers with
    no remote call; the first count after it probes, and a success restores
    remote, confirmed counts for the counts that follow."""
    replies: list = [failure]

    def respond(request):
        reply = replies.pop(0) if replies else httpx2.Response(200, json={"count": 41})
        if isinstance(reply, Exception):
            raise reply
        return reply

    calls = []
    clock = _Clock()
    messages = [ChatMessage(role="user", content="Original synthetic question")]
    estimate = FallbackTokenizer().count_messages(messages)
    transport = httpx2.MockTransport(lambda request: (calls.append(request), respond(request))[1])
    with httpx2.Client(transport=transport) as client, caplog.at_level(logging.INFO, logger="agent.tokenizer"):
        tok = VllmTokenizer("http://synthetic/v1", "reasoning", client=client, clock=clock)
        assert tok.count_messages(messages) == estimate
        assert tok.remote_confirmed is False
        clock.now += TOKENIZE_RETRY_AFTER_S - 0.5
        assert tok.count_messages(messages) == estimate
        assert len(calls) == 1
        clock.now += 1
        assert tok.count_messages(messages) == 41
        assert tok.remote_confirmed is True
        assert tok.count_messages(messages) == 41  # next ordinary count, no window
        assert tok.remote_confirmed is True
    assert len(calls) == 3
    assert [r.levelname for r in caplog.records if r.name == "agent.tokenizer"] == ["WARNING", "INFO"]


def test_a_refused_count_opens_a_long_window_and_a_probe_after_it_resumes(caplog):
    """#1214: a 401 is not evidence the endpoint is unserved. The estimator
    carries the load for the long window, then one probe resumes remote
    counting: a rotated key must not cost the process its tokenizer."""
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx2.Response(401)
        return httpx2.Response(200, json={"count": 41})

    message = ChatMessage(role="user", content="IEF001A synthetic")
    estimate = FallbackTokenizer().count_messages([message])
    clock = _Clock()
    with (
        caplog.at_level(logging.INFO, logger="agent.tokenizer"),
        httpx2.Client(transport=httpx2.MockTransport(respond)) as client,
    ):
        tok = VllmTokenizer("http://synthetic/v1", "reasoning", client=client, clock=clock)
        assert tok.count_messages([message]) == estimate
        assert tok._pinned is False
        assert tok.remote_confirmed is False
        assert len(calls) == 1
        # Inside the long window the estimator serves, without traffic.
        clock.now += TOKENIZE_REFUSED_RETRY_AFTER_S - 1
        assert tok.count_messages([message]) == estimate
        assert len(calls) == 1
        # After the window one probe is allowed, exactly like the short one.
        clock.now += 1
        assert tok.count_messages([message]) == 41
        assert tok.remote_confirmed is True
        assert len(calls) == 2
        # And remote counting stays resumed: no window left, the next
        # count goes to the endpoint as a normal call, not as a probe.
        assert tok.count_messages([message]) == 41
        assert tok._retry_at is None
        assert len(calls) == 3
    warning = [rec for rec in caplog.records if rec.name == "agent.tokenizer" and rec.levelname == "WARNING"]
    assert len(warning) == 1
    assert "401" in warning[0].getMessage()
    assert [r.levelname for r in caplog.records if r.name == "agent.tokenizer"] == ["WARNING", "INFO"]


def test_a_refused_400_does_not_pin_the_estimator_for_the_process():
    """#1214: one unusual payload must not disable remote counting for the
    life of the process: every long window ends with another probe."""
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(400, json={"error": "synthetic refused payload"})

    message = ChatMessage(role="user", content="IEF001A synthetic")
    estimate = FallbackTokenizer().count_messages([message])
    clock = _Clock()
    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tok = VllmTokenizer("http://synthetic/v1", "reasoning", client=client, clock=clock)
        assert tok.count_messages([message]) == estimate
        assert len(calls) == 1
        for _ in range(3):
            for _ in range(4):  # inside the window: estimator only
                assert tok.count_messages([message]) == estimate
            clock.now += TOKENIZE_REFUSED_RETRY_AFTER_S
            # The window expires: one probe, refused again, window reopened.
            assert tok.count_messages([message]) == estimate
            assert tok._pinned is False
    assert len(calls) == 4  # the first refusal plus one probe per window


def test_a_late_success_that_was_not_the_probe_keeps_the_window():
    """#1214: a count whose request was already in flight when a refusal
    opened the window may keep its own result, but it must not clear that
    window: the next probe stays reserved for after the window passes."""
    calls = []
    first_started = threading.Event()
    release_first = threading.Event()

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(timeout=5.0)
            return httpx2.Response(200, json={"count": 41})
        return httpx2.Response(400)

    message = ChatMessage(role="user", content="IEF001A synthetic")
    estimate = FallbackTokenizer().count_messages([message])
    clock = _Clock()
    results = {}

    def counted_as(key):
        results[key] = tok.count_messages([message])

    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tok = VllmTokenizer("http://synthetic/v1", "reasoning", client=client, clock=clock)
        in_flight = threading.Thread(target=counted_as, args=("in_flight",))
        in_flight.start()
        assert first_started.wait(timeout=5.0)
        refused = threading.Thread(target=counted_as, args=("refused",))
        refused.start()
        refused.join(timeout=5.0)
        assert not refused.is_alive()
        release_first.set()
        in_flight.join(timeout=5.0)
        assert not in_flight.is_alive()
        assert results["refused"] == estimate
        assert results["in_flight"] == 41
        assert len(calls) == 2
        # The success that was never the probe left the refusal window live.
        assert tok._retry_at == clock.now + TOKENIZE_REFUSED_RETRY_AFTER_S
        assert tok.count_messages([message]) == estimate
        assert len(calls) == 2
        clock.now += TOKENIZE_REFUSED_RETRY_AFTER_S
        assert tok.count_messages([message]) == estimate
        assert len(calls) == 3


def test_failed_probe_reopens_the_window_without_extra_calls():
    """An outage that outlives the window costs one probe per window, never
    one call per count."""
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(503)

    clock = _Clock()
    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tok = VllmTokenizer("http://synthetic/v1", "reasoning", client=client, clock=clock)
        for _ in range(3):
            for _ in range(5):
                tok.count_tokens("synthetic text")
                assert tok.remote_confirmed is False
            clock.now += TOKENIZE_RETRY_AFTER_S
    assert len(calls) == 3


def test_confirmation_belongs_to_the_counting_thread():
    """A probe that succeeds on another thread must not certify a count this
    thread took from the estimator: budget_verified reads remote_confirmed
    right after its own count."""
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(503) if len(calls) == 1 else httpx2.Response(200, json={"count": 9})

    clock = _Clock()
    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tok = VllmTokenizer("http://synthetic/v1", "reasoning", client=client, clock=clock)
        tok.count_tokens("synthetic text")  # fails, opens the window
        tok.count_tokens("synthetic text")  # estimator inside the window
        assert tok.remote_confirmed is False
        clock.now += TOKENIZE_RETRY_AFTER_S
        other: dict = {}

        def probe() -> None:
            other["count"] = tok.count_tokens("synthetic text")
            other["confirmed"] = tok.remote_confirmed

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join()
        assert other == {"count": 9, "confirmed": True}
        assert tok.remote_confirmed is False  # this thread's last count was estimated
        assert tok.count_tokens("synthetic text") == 9
        assert tok.remote_confirmed is True


@pytest.mark.parametrize("count", ["tokens", "messages"])
def test_a_later_estimated_count_clears_this_threads_confirmation(count):
    """A thread whose previous count was remote must read unconfirmed when
    its next count comes from the estimator (another thread's failure opened
    the window in between)."""
    replies = [200, 503]

    def respond(request):
        status = replies.pop(0)
        return httpx2.Response(status, json={"count": 9})

    messages = [ChatMessage(role="user", content="synthetic question")]

    def counted(tok):
        return tok.count_tokens("synthetic text") if count == "tokens" else tok.count_messages(messages)

    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tok = VllmTokenizer("http://synthetic/v1", "reasoning", client=client, clock=_Clock())
        assert counted(tok) == 9
        assert tok.remote_confirmed is True
        worker = threading.Thread(target=tok.count_tokens, args=("other text",))
        worker.start()
        worker.join()  # fails with 503 and opens the window
        assert counted(tok) != 9
        assert tok.remote_confirmed is False


def test_concurrent_counts_at_expiry_share_one_probe():
    """When the window expires, the first count claims the probe; counts on
    other threads keep the estimator instead of each paying the timeout."""
    release = threading.Event()
    entered = threading.Event()
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx2.Response(503)
        entered.set()
        release.wait(5)
        return httpx2.Response(200, json={"count": 9})

    clock = _Clock()
    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tok = VllmTokenizer("http://synthetic/v1", "reasoning", client=client, clock=clock)
        tok.count_tokens("synthetic text")
        clock.now += TOKENIZE_RETRY_AFTER_S
        worker = threading.Thread(target=tok.count_tokens, args=("synthetic text",))
        worker.start()
        assert entered.wait(5)
        assert tok.count_tokens("synthetic text") == estimate_tokens("synthetic text")
        assert tok.remote_confirmed is False
        release.set()
        worker.join()
    assert len(calls) == 2


def test_build_tokenizer_dispatch():
    s_empty = Settings(_env_file=None)
    assert isinstance(build_tokenizer(s_empty), FallbackTokenizer)

    s_vllm = Settings(
        llm_base_url="http://localhost:8000/v1",
        llm_model_reasoning="mock-model",
        _env_file=None,
    )
    assert isinstance(build_tokenizer(s_vllm), VllmTokenizer)


@pytest.mark.parametrize("count", [-1, True, "12"])
def test_malformed_count_cannot_verify_even_with_tokens(count):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx2.Response(200, json={"count": count, "tokens": [1, 2]})
    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tokenizer = VllmTokenizer("http://synthetic/v1", "reasoning", client=client)
        messages = [ChatMessage(role="user", content="synthetic question")]
        expected = FallbackTokenizer().count_messages(messages)
        assert tokenizer.count_messages(messages) == expected
        assert not tokenizer.remote_confirmed
        assert tokenizer.count_messages(messages) == expected
        assert len(calls) == 1


def test_chat_tokenizer_matches_inference_template_flags():
    captured = {}
    tokenizer = VllmTokenizer("http://synthetic/v1", "reasoning",
                              client=TokenizerPostFake(count=12, capture=captured))
    messages = [ChatMessage(role="user", content="synthetic question")]
    assert tokenizer.count_messages(messages) == 12
    assert captured["json"] == {"model": "reasoning", "messages": [messages[0].model_dump()],
                                "add_generation_prompt": True, "add_special_tokens": False}


@pytest.mark.parametrize("effort,count", [("low", 45), ("none", 17), (None, 57)])
def test_chat_token_count_uses_selected_request_template(effort, count):
    """Model-specific counts differ; omitted effort cannot verify a low prompt."""
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        selected = body.get("chat_template_kwargs", {}).get("reasoning_effort")
        return httpx2.Response(200, json={"count": {"low": 45, "medium": 15, "xhigh": 57, "none": 17, None: 57}[selected]})

    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tokenizer = VllmTokenizer("http://synthetic/v1", "reasoning", client=client)
        messages = [ChatMessage(role="user", content="Return the word READY")]
        assert tokenizer.count_messages(messages, reasoning_effort=effort) == count
        assert tokenizer.remote_confirmed
    expected = {"reasoning_effort": effort, "enable_thinking": effort != "none"} if effort else None
    assert bodies[0].get("chat_template_kwargs") == expected


def test_chat_token_counts_do_not_retain_another_requests_effort():
    calls = []

    def respond(request):
        selected = json.loads(request.content).get("chat_template_kwargs", {}).get("reasoning_effort")
        calls.append(selected)
        return httpx2.Response(200, json={"count": {"low": 45, "medium": 15, None: 57}[selected]})

    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tokenizer = VllmTokenizer("http://synthetic/v1", "reasoning", client=client)
        messages = [ChatMessage(role="user", content="Return the word READY")]
        assert tokenizer.count_messages(messages, reasoning_effort="low") == 45
        assert tokenizer.count_messages(messages, reasoning_effort="medium") == 15
        assert tokenizer.count_messages(messages, reasoning_effort="low") == 45
        assert tokenizer.count_messages(messages) == 57
    assert calls == ["low", "medium", "low", None]


def test_effort_specific_count_failure_still_pins_unverified_estimation():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(503)

    messages = [ChatMessage(role="user", content="Original synthetic question")]
    fallback = FallbackTokenizer()
    expected = fallback.count_messages(messages)
    assert fallback.count_messages(messages, reasoning_effort="xhigh") == expected
    assert not fallback.remote_confirmed
    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tokenizer = VllmTokenizer("http://synthetic/v1", "reasoning", client=client)
        assert tokenizer.count_messages(messages, reasoning_effort="low") == expected
        assert tokenizer.count_messages(messages, reasoning_effort="medium") == expected
        assert not tokenizer.remote_confirmed
    assert len(requests) == 1


@pytest.mark.parametrize(("transport", "extra", "effort", "expected"), [
    # Default transport, nothing configured: the pre-#1193 count arguments.
    ("top_level", {}, "low", {"reasoning_effort": "low", "enable_thinking": True}),
    ("top_level", {}, None, None),
    # A configured template argument is sent with the generation, so it is
    # counted, and wins over the implied thinking flag.
    ("top_level", {"chat_template_kwargs": {"enable_thinking": False, "budget": 512}}, "low",
     {"reasoning_effort": "low", "enable_thinking": False, "budget": 512}),
    # Template transport: exactly the generation's chat_template_kwargs.
    ("chat_template", {}, "high", {"reasoning_effort": "high"}),
    ("chat_template", {"chat_template_kwargs": {"thinking": True}}, "high",
     {"thinking": True, "reasoning_effort": "high"}),
    ("chat_template", {"chat_template_kwargs": {"thinking": True}}, None, {"thinking": True}),
    # Effort not sent: only what the operator configured, a fixed effort included.
    ("none", {}, "high", None),
    ("none", {"chat_template_kwargs": {"reasoning_effort": "minimal", "enable_thinking": False}}, "high",
     {"reasoning_effort": "minimal", "enable_thinking": False}),
    # Body fields outside the template never reach /tokenize.
    ("none", {"top_k": 20}, "high", None),
])
def test_chat_token_count_renders_the_generations_template_arguments(transport, extra, effort, expected):
    """Issue #1193: the count and the generation come from one ModelRequestArgs,
    so the generation's own chat_template_kwargs is the independent oracle."""
    from mainframe_rag.agent.answer import _chat_body

    settings = Settings(_env_file=None, llm_base_url="http://synthetic/v1", llm_model_reasoning="reasoning",
                        llm_extra_body=json.dumps(extra), llm_effort_transport=transport)
    captured = {}
    tokenizer = build_tokenizer(settings, TokenizerPostFake(count=12, capture=captured))
    messages = [ChatMessage(role="user", content="synthetic question")]
    assert tokenizer.count_messages(messages, reasoning_effort=effort) == 12
    assert captured["json"].get("chat_template_kwargs") == expected
    assert set(captured["json"]) - {"chat_template_kwargs"} == {
        "model", "messages", "add_generation_prompt", "add_special_tokens"}
    generation = _chat_body("reasoning", [m.model_dump() for m in messages], effort, 0.2,
                            stream=True, args=settings.model_request_args())
    if transport != "top_level":
        assert generation.get("chat_template_kwargs") == expected
    else:
        # Top-level effort is mapped by the server; configured arguments match.
        assert generation.get("chat_template_kwargs") == extra.get("chat_template_kwargs")
        assert generation.get("reasoning_effort") == effort


# ---------------------------------------------------------------------------
# Completion-usage counts (issue #1266): a gateway without /tokenize still
# reports `usage.prompt_tokens` for a 1-token chat completion.
# ---------------------------------------------------------------------------

_COUNT_SETTINGS = {"llm_base_url": "http://synthetic/v1", "llm_model_reasoning": "reasoning"}


def _usage_reply(prompt_tokens=321, content="never read"):
    return httpx2.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 1, "total_tokens": 322},
    })


def _usage_counter(respond, clock=None, **settings):
    calls: list = []

    def record(request):
        calls.append(request)
        reply = respond(request)
        if isinstance(reply, Exception):
            raise reply
        return reply

    client = httpx2.Client(transport=httpx2.MockTransport(record))
    resolved = Settings(_env_file=None, **{**_COUNT_SETTINGS, "llm_token_count_source": "completion_usage",
                                         **settings})
    counter = CompletionUsageTokenizer.from_settings(resolved, client, clock=clock or _Clock())
    return counter, calls, client


def test_completion_usage_count_reads_prompt_tokens_not_content():
    counter, calls, client = _usage_counter(lambda request: _usage_reply(321))
    messages = [ChatMessage(role="user", content="synthetic question")]
    with client:
        assert counter.count_messages(messages, reasoning_effort="low") == 321
        assert counter.remote_confirmed is True
    assert [str(call.url) for call in calls] == ["http://synthetic/v1/chat/completions"]
    body = json.loads(calls[0].content)
    assert body["max_tokens"] == 1 and "stream" not in body and "stream_options" not in body
    assert body["messages"] == [m.model_dump() for m in messages]


def test_completion_usage_text_counts_never_send_a_request():
    """Packing (fit_message) counts text in a loop: those stay estimator-only,
    so the completion is sent once per verification, never per packing step."""
    counter, calls, client = _usage_counter(lambda request: _usage_reply())
    with client:
        assert counter.count_tokens("synthetic text to price") == estimate_tokens("synthetic text to price")
        assert counter.remote_confirmed is False
    assert calls == []


def test_completion_usage_count_sends_the_reasoning_key():
    counter, calls, client = _usage_counter(lambda request: _usage_reply(), llm_api_key="sk-test-llm")
    with client:
        counter.count_messages([ChatMessage(role="user", content="q")])
    assert calls[0].headers["authorization"] == "Bearer sk-test-llm"


@pytest.mark.parametrize("reply", [
    httpx2.Response(404), httpx2.Response(405), httpx2.Response(501),
    httpx2.Response(200, json={"choices": []}),
    httpx2.Response(200, json={"usage": {}}),
    httpx2.Response(200, json={"usage": {"prompt_tokens": 0}}),
    httpx2.Response(200, json={"usage": {"prompt_tokens": -3}}),
    httpx2.Response(200, json={"usage": {"prompt_tokens": True}}),
    httpx2.Response(200, json={"usage": {"prompt_tokens": "12"}}),
    httpx2.Response(200, json={"usage": {"prompt_tokens": 12.0}}),
    httpx2.Response(200, json=["not", "an", "object"]),
], ids=["404", "405", "501", "no-usage", "empty-usage", "zero", "negative", "bool", "string", "float",
        "list"])
def test_unusable_completion_usage_pins_the_estimator(reply, caplog):
    """No usable prompt_tokens is a gateway that cannot count this way: one
    warning with a fixed message, then the estimator for the process."""
    clock = _Clock()
    counter, calls, client = _usage_counter(lambda request: reply, clock=clock)
    secret = "synthetic-private-question"
    messages = [ChatMessage(role="user", content=secret)]
    with client, caplog.at_level(logging.WARNING, logger="agent.tokenizer"):
        assert counter.count_messages(messages) == FallbackTokenizer().count_messages(messages)
        clock.now += TOKENIZE_REFUSED_RETRY_AFTER_S + 1
        assert counter.count_messages(messages) == FallbackTokenizer().count_messages(messages)
        assert counter.remote_confirmed is False
    assert len(calls) == 1 and counter._pinned is True
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "pinning in-process token estimation" in warnings[0]
    assert secret not in warnings[0]


@pytest.mark.parametrize("failure", [
    httpx2.Response(503), httpx2.Response(429), httpx2.Response(408),
    httpx2.ReadTimeout("slow"), httpx2.ConnectError("refused"),
], ids=["503", "429", "408", "read-timeout", "connect-error"])
def test_transient_completion_count_failure_recovers_after_the_window(failure, caplog):
    """#835 semantics: the estimator answers inside the window, one probe
    after it, and a success restores confirmed counts."""
    replies = [failure]
    clock = _Clock()
    counter, calls, client = _usage_counter(
        lambda request: replies.pop(0) if replies else _usage_reply(77), clock=clock)
    messages = [ChatMessage(role="user", content="synthetic-private-question")]
    estimate = FallbackTokenizer().count_messages(messages)
    with client, caplog.at_level(logging.INFO, logger="agent.tokenizer"):
        assert counter.count_messages(messages) == estimate and not counter.remote_confirmed
        clock.now += TOKENIZE_RETRY_AFTER_S - 0.5
        assert counter.count_messages(messages) == estimate
        assert len(calls) == 1
        clock.now += 1
        assert counter.count_messages(messages) == 77 and counter.remote_confirmed
        assert counter.count_messages(messages) == 77
    assert len(calls) == 3
    records = [r for r in caplog.records if r.name == "agent.tokenizer"]
    assert [r.levelname for r in records] == ["WARNING", "INFO"]
    assert all("synthetic-private-question" not in r.getMessage() for r in records)


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_refused_completion_count_opens_the_long_window(status):
    """#1214 semantics: a refusal (a prompt over the served window answers
    400 here) is not a missing route: the long window, then one probe."""
    replies = [httpx2.Response(status)]
    clock = _Clock()
    counter, calls, client = _usage_counter(
        lambda request: replies.pop(0) if replies else _usage_reply(55), clock=clock)
    messages = [ChatMessage(role="user", content="q")]
    with client:
        assert counter.count_messages(messages) == FallbackTokenizer().count_messages(messages)
        clock.now += TOKENIZE_REFUSED_RETRY_AFTER_S - 1
        counter.count_messages(messages)
        assert len(calls) == 1 and counter._pinned is False
        clock.now += 1
        assert counter.count_messages(messages) == 55
    assert len(calls) == 2


@pytest.mark.parametrize(("transport", "extra"), [
    ("top_level", {}),
    ("top_level", {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20}),
    ("chat_template", {"chat_template_kwargs": {"enable_thinking": True}}),
    ("none", {"chat_template_kwargs": {"reasoning_effort": "minimal"}, "repetition_penalty": 1.05}),
])
@pytest.mark.parametrize("effort", ["low", "xhigh", None])
@pytest.mark.parametrize("temperature", [0.2, 0.9])
def test_completion_count_body_is_the_generation_body_without_its_limits(transport, extra, effort, temperature):
    """The count is built by the generation's own body builder, so it can
    never render another template: the counted body equals the streamed
    generation body minus `max_tokens` and the streaming flags."""
    from mainframe_rag.model_args import chat_request_body

    counter, calls, client = _usage_counter(
        lambda request: _usage_reply(), llm_effort_transport=transport, llm_extra_body=json.dumps(extra))
    messages = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="q")]
    with client:
        counter.count_messages(messages, reasoning_effort=effort, temperature=temperature)
    counted = json.loads(calls[0].content)
    sent = chat_request_body("reasoning", [m.model_dump() for m in messages], effort, temperature,
                             stream=True, max_tokens=4096, args=counter._request_args)
    limits = {"max_tokens", "stream", "stream_options"}
    assert {k: v for k, v in counted.items() if k not in limits} == {
        k: v for k, v in sent.items() if k not in limits}
    assert counted["max_tokens"] == 1 and not limits - {"max_tokens"} & counted.keys()


def test_completion_count_without_a_request_temperature_sends_the_configured_one():
    counter, calls, client = _usage_counter(lambda request: _usage_reply(), llm_temperature=0.7)
    with client:
        counter.count_messages([ChatMessage(role="user", content="q")])
    assert json.loads(calls[0].content)["temperature"] == 0.7


# Source selection (#1266). The default keeps the pre-#1266 behaviour.

def _routes(tokenize, completion):
    """Mock gateway: /tokenize and /v1/chat/completions answer as told."""
    calls: list[str] = []

    def respond(request):
        calls.append(request.url.path)
        reply = tokenize if request.url.path == "/tokenize" else completion
        if isinstance(reply, Exception):
            raise reply
        return reply

    return calls, httpx2.Client(transport=httpx2.MockTransport(respond))


@pytest.mark.parametrize(("source", "expected"), [
    ("tokenize", VllmTokenizer), ("completion_usage", CompletionUsageTokenizer), ("estimate", FallbackTokenizer),
])
def test_build_tokenizer_selects_the_configured_source_without_traffic(source, expected):
    calls, client = _routes(httpx2.Response(200, json={"count": 9}), _usage_reply(9))
    with client:
        tok = build_tokenizer(Settings(_env_file=None, **_COUNT_SETTINGS, llm_token_count_source=source), client)
    assert type(tok) is expected and calls == []


def test_default_source_is_tokenize_with_estimator_fallback():
    assert type(build_tokenizer(Settings(_env_file=None, **_COUNT_SETTINGS))) is VllmTokenizer
    assert type(build_tokenizer(Settings(_env_file=None))) is FallbackTokenizer
    assert type(build_tokenizer(Settings(_env_file=None, llm_token_count_source="completion_usage"))) is (
        FallbackTokenizer)


_AUTO_CASES = {
    # /tokenize served: it wins, no completion is sent.
    "tokenize-served": (httpx2.Response(200, json={"count": 9}), _usage_reply(), VllmTokenizer,
                        ["/tokenize"]),
    # /tokenize not served, completion usage confirmed at startup.
    "usage-confirmed": (httpx2.Response(404), _usage_reply(), CompletionUsageTokenizer,
                        ["/tokenize", "/v1/chat/completions"]),
    # Neither: the estimator, for the process.
    "neither": (httpx2.Response(404), httpx2.Response(200, json={"usage": {}}), FallbackTokenizer,
                ["/tokenize", "/v1/chat/completions"]),
    # /tokenize unreachable right now (transient): keep it with its retry
    # window, exactly as the tokenize source behaves today.
    "tokenize-transient": (httpx2.ConnectError("down"), _usage_reply(), VllmTokenizer, ["/tokenize"]),
    # /tokenize absent and the completion failed transiently: the counter
    # keeps its retry window; nothing is pinned by a startup blip.
    "usage-transient": (httpx2.Response(404), httpx2.Response(503), CompletionUsageTokenizer,
                        ["/tokenize", "/v1/chat/completions"]),
}


@pytest.mark.parametrize("case", list(_AUTO_CASES))
def test_auto_source_startup_check(case, caplog):
    tokenize, completion, expected, paths = _AUTO_CASES[case]
    calls, client = _routes(tokenize, completion)
    with client, caplog.at_level(logging.INFO, logger="agent.tokenizer"):
        tok = build_tokenizer(Settings(_env_file=None, **_COUNT_SETTINGS, llm_token_count_source="auto"), client)
    assert type(tok) is expected and calls == paths
    assert tok.remote_confirmed is False  # a request thread has not counted yet
    selected = [r.getMessage() for r in caplog.records if "token count source" in r.getMessage()]
    assert len(selected) == 1


def test_auto_selected_usage_counter_counts_on_a_request_thread():
    calls, client = _routes(httpx2.Response(404), _usage_reply(64))
    with client:
        tok = build_tokenizer(Settings(_env_file=None, **_COUNT_SETTINGS, llm_token_count_source="auto"), client)
        result = {}
        worker = threading.Thread(target=lambda: result.update(
            n=tok.count_messages([ChatMessage(role="user", content="q")]), ok=tok.remote_confirmed))
        worker.start()
        worker.join()
    assert result == {"n": 64, "ok": True}
    assert calls == ["/tokenize", "/v1/chat/completions", "/v1/chat/completions"]
