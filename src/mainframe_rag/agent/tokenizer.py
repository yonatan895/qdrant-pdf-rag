"""Tokenizer accounting for model context budgeting (issue #20).

Token counting against vLLM's /tokenize endpoint with a deterministic
in-process fallback for air-gapped operations, offline testing, and CI.

The endpoint lives at the SERVER ORIGIN: LLM_BASE_URL is .../v1, and vLLM
serves /tokenize next to /v1, not under it (a LiteLLM front may not expose
it at all). A failed call is logged as a warning and the instance answers
from the in-process estimator. An endpoint that is not served or answers
without a usable count (404/405/501, a 200 without a count) pins the
estimator for the life of the instance. A transient failure (transport
error, timeout, 408/429/5xx) downgrades only for TOKENIZE_RETRY_AFTER_S;
a refusal (400/401/403, any other 4xx, an unexpected 3xx) downgrades for
the longer TOKENIZE_REFUSED_RETRY_AFTER_S without pinning. After either
window one call probes the endpoint again, and a probe that works resumes
remote counting (issue #835, #1214). Never a silent per-call fallback,
never repeated doomed RPCs. Callers plan prompt packing
with the estimator and use the real tokenizer only to verify the packed
prompt (see answer.build_messages).

A gateway without /tokenize can still count (#1266): CompletionUsageTokenizer
sends the packed request as a non-streaming chat completion with
`max_tokens=1`, built by the generation's own body builder, and reads
`usage.prompt_tokens`, the server's template-aware count. It follows the same
pin and retry rules. `LLM_TOKEN_COUNT_SOURCE` selects the source
(build_tokenizer); docs/agent.md#token-count-sources owns the contract.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx2

from mainframe_rag.config import bearer_auth_headers
from mainframe_rag.model_args import DEFAULT_MODEL_REQUEST_ARGS, ModelRequestArgs, chat_request_body
from mainframe_rag.ports import ChatMessage

if TYPE_CHECKING:
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import Tokenizer

log = logging.getLogger("agent.tokenizer")

_TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)

# Issue #835: after a transient /tokenize failure the estimator answers for
# this long, then one call probes the endpoint again. An outage therefore
# stalls at most one request per window by the call timeout.
TOKENIZE_RETRY_AFTER_S = 60.0
# Issue #1214: the endpoint answered but refused this kind of count request.
# That is not permanent either: a long window passes before the next probe.
TOKENIZE_REFUSED_RETRY_AFTER_S = 600.0
# The endpoint answered but is not served: no window can make it usable.
_ABSENT_STATUSES = frozenset({404, 405, 501})


def estimate_tokens(text: str) -> int:
    """Deterministic token estimation fallback approximating BPE tokenization."""
    if not text:
        return 0
    return max(1, len(_TOKEN_PATTERN.findall(text)))


class FallbackTokenizer:
    """In-process tokenizer for air-gapped / offline testing or fallback."""

    # Estimator-only: verification through this tokenizer is reported as
    # estimated, never confirmed (issue #368).
    remote_confirmed = False

    def count_tokens(self, text: str) -> int:
        return estimate_tokens(text)

    def count_messages(
        self, messages: list[ChatMessage], *, reasoning_effort: str | None = None
    ) -> int:
        return sum(estimate_tokens(f"{m.role}\n{m.content}") for m in messages)


class _RemoteCount:
    """The pin and retry state machine one remote count source runs (#835,
    #1214), shared by VllmTokenizer and CompletionUsageTokenizer.

    A failure logs one warning and answers from the in-process estimator, and
    the outcome decides how long that lasts. An endpoint that is not served
    (404, 405, 501) or a 200 that carries no usable count pins the estimator
    for the life of this instance, so a gateway without the route costs
    exactly one failed request per process, never per answer. A transient
    failure (transport error, timeout, 408, 429, 5xx other than 501)
    downgrades for ``retry_after_s``. Any other refusal — 400, 401, 403,
    another 4xx, an unexpected 3xx — opens the longer
    ``refused_retry_after_s`` window instead of pinning, because one unusual
    payload or a key that rotates must not cost remote counting for the rest
    of the process. After either window the first count claims one probe, and
    success resumes remote counting (issue #835, #1214).

    ``remote_confirmed`` describes the calling thread's most recent count,
    not the instance: a probe that succeeds on another thread must never
    certify a count this thread took from the estimator. Log lines carry the
    source label, the URL and a fixed reason, never request content.
    """

    # Names the source in the fixed log lines.
    _label = "remote token count"

    def __init__(
        self,
        *,
        timeout_s: float,
        client: httpx2.Client | None,
        api_key: str | None,
        retry_after_s: float,
        refused_retry_after_s: float,
        clock: Callable[[], float],
    ) -> None:
        self._timeout_s = timeout_s
        self._client = client
        self._api_key = api_key
        self._fallback = FallbackTokenizer()
        self._retry_after_s = retry_after_s
        self._refused_retry_after_s = refused_retry_after_s
        self._clock = clock
        # One tokenizer serves concurrent turns on worker threads: the lock
        # guards the downgrade state, and the per-thread flag records whether
        # that thread's last count came from the endpoint.
        self._lock = threading.Lock()
        self._pinned = False
        self._retry_at: float | None = None
        self._local = threading.local()

    @property
    def remote_confirmed(self) -> bool:
        """True when this thread's most recent count came from the server
        (issue #368): only then does a prompt-size verdict confirm
        compliance. Estimator counts, and a thread that has not counted,
        read False."""
        return bool(getattr(self._local, "remote", False))

    def _remote_count(
        self, url: str, payload: dict[str, Any], parse: Callable[[Any], int | None]
    ) -> int | None:
        """One count call, or None for the estimator: the endpoint is pinned
        off, inside a retry window, or this call failed. ``parse`` maps a 200
        body to a count, or None when it carries no usable one."""
        with self._lock:
            if self._pinned:
                return None
            retry_at = self._retry_at
            probing = retry_at is not None
            if retry_at is not None:
                now = self._clock()
                if now < retry_at:
                    return None
                # Claim the probe: concurrent counts keep the estimator
                # instead of each paying the call timeout.
                self._retry_at = now + self._retry_after_s
        headers = bearer_auth_headers(self._api_key)
        try:
            if self._client is not None:
                resp = self._client.post(url, json=payload, timeout=self._timeout_s, headers=headers)
            else:
                resp = httpx2.post(url, json=payload, timeout=self._timeout_s, headers=headers)
            status = resp.status_code
            if status == 200:
                count = parse(resp.json())
                if count is not None:
                    self._recovered(url, probing)
                    return count
                # A served endpoint that answers 200 without a count never
                # grew this route: pin, even though the status is not 404.
                self._downgrade(url, "no usable token count in response", mode="absent")
                return None
            # Three outcomes: an endpoint that is not served pins, a
            # transient failure gets the short window, and any other
            # refusal (400, 401, 403, other 4xx, unexpected 3xx) is only
            # refused for now and gets the long window without pinning.
            if status in _ABSENT_STATUSES:
                self._downgrade(url, f"status {status}", mode="absent")
            elif status in (408, 429) or status >= 500:
                self._downgrade(url, f"status {status}", mode="transient")
            else:
                self._downgrade(url, f"status {status}", mode="refused")
        except Exception as exc:  # noqa: BLE001
            self._downgrade(url, type(exc).__name__, mode="transient")
        return None

    def _recovered(self, url: str, probing: bool) -> None:
        with self._lock:
            # Only the probe that was waiting on the window closes it. A
            # late success from a call that started before the downgrade
            # must not clear a window that is still running, otherwise the
            # pinned window would silently reopen on every in-flight call.
            if probing or self._retry_at is None:
                self._retry_at = None
        self._local.remote = True
        if probing:
            log.info("%s at %s answers again; remote token counting resumed", self._label, url)

    def _downgrade(self, url: str, reason: str, *, mode: str) -> None:
        if mode == "absent":
            with self._lock:
                self._pinned = True
            log.warning(
                "%s unusable at %s (%s); pinning in-process token estimation",
                self._label,
                url,
                reason,
            )
            return
        if mode == "refused":
            retry_after = self._refused_retry_after_s
            with self._lock:
                self._retry_at = self._clock() + retry_after
            log.warning(
                "%s refused at %s (%s); in-process token estimation "
                "until the next probe in %.0f s",
                self._label,
                url,
                reason,
                retry_after,
            )
            return
        with self._lock:
            self._retry_at = self._clock() + self._retry_after_s
        log.warning(
            "%s failed at %s (%s); in-process token estimation for %.0f s",
            self._label,
            url,
            reason,
            self._retry_after_s,
        )


def _tokenize_count(data: Any) -> int | None:
    """A /tokenize body's count: `count` when it is a non-negative int,
    else the length of `tokens` when no `count` key exists."""
    if isinstance(data, dict):
        if type(data.get("count")) is int and data["count"] >= 0:
            return int(data["count"])
        if "count" not in data and isinstance(data.get("tokens"), list):
            return len(data["tokens"])
    return None


def usage_prompt_tokens(data: Any) -> int | None:
    """A chat completion's `usage.prompt_tokens` when it is a positive int
    (a rendered chat template is never empty). The completion content is
    never read."""
    usage = data.get("usage") if isinstance(data, dict) else None
    count = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    if type(count) is int and count > 0:
        return count
    return None


class VllmTokenizer(_RemoteCount):
    """Counts tokens via vLLM's /tokenize endpoint at the server origin,
    under the pin and retry rules of ``_RemoteCount`` (issue #835, #1214)."""

    _label = "vLLM /tokenize"

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float = 5.0,
        client: httpx2.Client | None = None,
        api_key: str | None = None,
        retry_after_s: float = TOKENIZE_RETRY_AFTER_S,
        refused_retry_after_s: float = TOKENIZE_REFUSED_RETRY_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
        request_args: ModelRequestArgs = DEFAULT_MODEL_REQUEST_ARGS,
    ) -> None:
        super().__init__(
            timeout_s=timeout_s,
            client=client,
            api_key=api_key,
            retry_after_s=retry_after_s,
            refused_retry_after_s=refused_retry_after_s,
            clock=clock,
        )
        self._base_url = base_url.rstrip("/")
        # vLLM serves /tokenize at the server root; /v1/tokenize is a 404.
        self._origin = self._base_url.removesuffix("/v1")
        self._model = model
        # The generation's own arguments (#1193): the count must render the
        # template the generation renders, extra-body arguments included.
        self._request_args = request_args

    def count_tokens(self, text: str) -> int:
        self._local.remote = False
        if not text:
            return 0
        count = self._remote_count(
            f"{self._origin}/tokenize", {"model": self._model, "prompt": text}, _tokenize_count
        )
        if count is not None:
            return count
        return self._fallback.count_tokens(text)

    def count_messages(
        self, messages: list[ChatMessage], *, reasoning_effort: str | None = None
    ) -> int:
        """Chat-template-aware count: what /tokenize reports for the message
        list is what actually consumes max_model_len."""
        self._local.remote = False
        if not messages:
            return 0
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [m.model_dump() for m in messages],
            "add_generation_prompt": True,
            "add_special_tokens": False,
        }
        # /tokenize takes the template arguments explicitly, so it gets the
        # ones this generation sends (#1193): on the default top-level
        # transport the server forwards the selected effort and enables
        # thinking; on the others exactly the request's chat_template_kwargs,
        # extra-body arguments included. Keep them local to this call: one
        # tokenizer serves concurrent turns.
        template_kwargs = self._request_args.template_kwargs(reasoning_effort)
        if template_kwargs:
            payload["chat_template_kwargs"] = template_kwargs
        count = self._remote_count(f"{self._origin}/tokenize", payload, _tokenize_count)
        if count is not None:
            return count
        return self._fallback.count_messages(messages, reasoning_effort=reasoning_effort)


# The count completion decodes one token; its content is never read.
COUNT_MAX_TOKENS = 1


def count_request_body(
    model: str,
    serialized: list[dict[str, Any]],
    reasoning_effort: str | None,
    temperature: float | None,
    args: ModelRequestArgs = DEFAULT_MODEL_REQUEST_ARGS,
) -> dict[str, Any]:
    """The completion-usage count body (#1266): the generation's own body
    from `chat_request_body`, non-streaming, with `max_tokens=1`."""
    return chat_request_body(
        model, serialized, reasoning_effort, temperature,
        stream=False, max_tokens=COUNT_MAX_TOKENS, args=args,
    )


class CompletionUsageTokenizer(_RemoteCount):
    """Counts a packed prompt by sending it as a non-streaming chat
    completion with ``max_tokens=1`` and reading ``usage.prompt_tokens``
    (issue #1266), for a gateway that serves no /tokenize.

    The body comes from ``model_args.chat_request_body``, the generation's
    own builder, with the same model, messages, effort transport, extra body
    and temperature: it differs from the sent generation only in
    ``max_tokens`` and the streaming flags, so the count renders exactly the
    template the generation renders. Only whole-prompt verification sends it
    (``count_messages``, answer.build_messages); ``count_tokens`` prices
    loose text for packing and always answers from the estimator, so a
    packing loop never sends a completion. Pin and retry rules are those of
    ``_RemoteCount``; ``LLM_TOKENIZE_TIMEOUT_S`` bounds each call.
    """

    _label = "completion usage count"
    # Capability (#1266): the prompt builder passes the request's own
    # temperature so the counted body equals the sent one. Duck-typed like
    # ``supports_max_tokens``, so other tokenizers keep their signature.
    counts_request_temperature = True

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float = 5.0,
        client: httpx2.Client | None = None,
        api_key: str | None = None,
        retry_after_s: float = TOKENIZE_RETRY_AFTER_S,
        refused_retry_after_s: float = TOKENIZE_REFUSED_RETRY_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
        request_args: ModelRequestArgs = DEFAULT_MODEL_REQUEST_ARGS,
        temperature: float | None = None,
    ) -> None:
        super().__init__(
            timeout_s=timeout_s,
            client=client,
            api_key=api_key,
            retry_after_s=retry_after_s,
            refused_retry_after_s=refused_retry_after_s,
            clock=clock,
        )
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._request_args = request_args
        # LLM_TEMPERATURE: what the generation sends when the request names
        # no temperature of its own.
        self._temperature = temperature

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        client: httpx2.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> CompletionUsageTokenizer:
        if not settings.llm_base_url or not settings.llm_model_reasoning:
            raise ValueError("completion usage counts need LLM_BASE_URL and LLM_MODEL_REASONING")
        return cls(
            base_url=settings.llm_base_url,
            model=settings.llm_model_reasoning,
            timeout_s=settings.llm_tokenize_timeout_s,
            client=client,
            api_key=settings.llm_api_key,
            clock=clock,
            request_args=settings.model_request_args(),
            temperature=settings.llm_temperature,
        )

    def count_tokens(self, text: str) -> int:
        """Loose text is priced by the estimator, never by a completion."""
        self._local.remote = False
        return self._fallback.count_tokens(text)

    def count_messages(
        self,
        messages: list[ChatMessage],
        *,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
    ) -> int:
        self._local.remote = False
        if not messages:
            return 0
        body = count_request_body(
            self._model,
            [m.model_dump() for m in messages],
            reasoning_effort,
            temperature if temperature is not None else self._temperature,
            self._request_args,
        )
        count = self._remote_count(self._url, body, usage_prompt_tokens)
        if count is not None:
            return count
        return self._fallback.count_messages(messages, reasoning_effort=reasoning_effort)


# Fixed synthetic prompt for the `auto` startup check: no request content.
_STARTUP_CHECK_MESSAGES = [ChatMessage(role="user", content="token count source check")]


def _vllm_tokenizer(
    settings: Settings, client: httpx2.Client | None, clock: Callable[[], float] = time.monotonic
) -> VllmTokenizer:
    assert settings.llm_base_url and settings.llm_model_reasoning
    return VllmTokenizer(
        base_url=settings.llm_base_url,
        model=settings.llm_model_reasoning,
        timeout_s=settings.llm_tokenize_timeout_s,
        client=client,
        api_key=settings.llm_api_key,
        clock=clock,
        request_args=settings.model_request_args(),
    )


class AutoTokenizer:
    """`LLM_TOKEN_COUNT_SOURCE=auto` (#1266): /tokenize while it is not
    pinned as not served, then the completion-usage count.

    The choice is made per count, not once: /tokenize in a transient or
    refused window answers from the estimator, exactly as the `tokenize`
    source does, and keeps re-probing; the moment its own rules pin it (404,
    405, 501, a 200 without a count), the same count moves on to the
    completion, which then follows its own pin and retry rules. A gateway
    that was down at startup therefore cannot leave the process on the
    estimator for life. The startup check only front-loads that discovery
    and names the source in one log line."""

    counts_request_temperature = True

    def __init__(self, tokenize: VllmTokenizer, usage: CompletionUsageTokenizer) -> None:
        self._tokenize = tokenize
        self._usage = usage
        self._local = threading.local()

    @property
    def remote_confirmed(self) -> bool:
        """This thread's most recent count came from a server (#368)."""
        return bool(getattr(self._local, "remote", False))

    def source(self) -> str:
        """The source the next count asks: tokenize, completion_usage, or
        estimate when both are pinned."""
        if not self._tokenize._pinned:
            return "tokenize"
        return "estimate" if self._usage._pinned else "completion_usage"

    def count_tokens(self, text: str) -> int:
        count = self._tokenize.count_tokens(text) if not self._tokenize._pinned else estimate_tokens(text)
        self._local.remote = not self._tokenize._pinned and self._tokenize.remote_confirmed
        return count

    def count_messages(
        self,
        messages: list[ChatMessage],
        *,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
    ) -> int:
        if not self._tokenize._pinned:
            count = self._tokenize.count_messages(messages, reasoning_effort=reasoning_effort)
            if self._tokenize.remote_confirmed or not self._tokenize._pinned:
                self._local.remote = self._tokenize.remote_confirmed
                return count
        count = self._usage.count_messages(
            messages, reasoning_effort=reasoning_effort, temperature=temperature
        )
        self._local.remote = self._usage.remote_confirmed
        return count


def _select_auto(
    settings: Settings, client: httpx2.Client | None, clock: Callable[[], float] = time.monotonic
) -> AutoTokenizer:
    """`auto` (#1266): one warn-only startup count, never a refusal to start,
    on a fixed synthetic message; it asks /tokenize and, only when that is
    pinned as not served, the completion count. One log line names the
    source the next count will ask."""
    auto = AutoTokenizer(
        _vllm_tokenizer(settings, client, clock),
        CompletionUsageTokenizer.from_settings(settings, client, clock),
    )
    auto.count_messages(_STARTUP_CHECK_MESSAGES, reasoning_effort=settings.llm_reasoning_effort_simple)
    confirmed = auto.remote_confirmed
    # The check's own thread must not read as a confirmed request count.
    auto._local.remote = False
    source = auto.source()
    if source == "estimate":
        log.warning("token count source: estimator (neither /tokenize nor completion usage counts)")
    else:
        log.info(
            "token count source: %s (%s at startup)",
            source,
            "confirmed" if confirmed else "retry window",
        )
    return auto


def build_tokenizer(
    settings: Settings,
    client: httpx2.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Tokenizer:
    """Builds the prompt counter LLM_TOKEN_COUNT_SOURCE selects (#1266) for
    the served reasoning model; without one, the estimator. Only `auto`
    sends anything here (its startup check); the lifespan runs this off the
    event loop."""
    source = settings.llm_token_count_source
    if source == "estimate" or not (settings.llm_base_url and settings.llm_model_reasoning):
        return FallbackTokenizer()
    if source == "completion_usage":
        return CompletionUsageTokenizer.from_settings(settings, client, clock)
    if source == "auto":
        return _select_auto(settings, client, clock)
    return _vllm_tokenizer(settings, client, clock)
