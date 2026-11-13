"""Tests for agent/tokenizer.py (vLLM /tokenize client and fallback)."""

import json
import logging
import threading

import httpx2
import pytest

from mainframe_rag.agent.tokenizer import (
    TOKENIZE_RETRY_AFTER_S,
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
