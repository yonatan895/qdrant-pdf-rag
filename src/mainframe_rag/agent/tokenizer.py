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


class VllmTokenizer:
    """Counts tokens via vLLM's /tokenize endpoint at the server origin.

    A failure logs one warning and answers from the in-process estimator, and
    the outcome decides how long that lasts. An endpoint that is not served
    (404, 405, 501) or a 200 that carries no usable count pins the estimator
    for the life of this instance, so a LiteLLM front without /tokenize costs
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
    certify a count this thread took from the estimator.
    """

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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # vLLM serves /tokenize at the server root; /v1/tokenize is a 404.
        self._origin = self._base_url.removesuffix("/v1")
        self._model = model
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
        """True when this thread's most recent count came from /tokenize
        (issue #368): only then does a prompt-size verdict confirm
        compliance. Estimator counts, and a thread that has not counted,
        read False."""
        return bool(getattr(self._local, "remote", False))

    def count_tokens(self, text: str) -> int:
        self._local.remote = False
        if not text:
            return 0
        count = self._remote_count({"model": self._model, "prompt": text})
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
        if reasoning_effort:
            # vLLM chat completions forward the selected effort and enable
            # thinking. /tokenize needs these explicit nested template kwargs.
            # Keep them local to this call: one tokenizer serves concurrent turns.
            payload["chat_template_kwargs"] = {
                "reasoning_effort": reasoning_effort, "enable_thinking": reasoning_effort != "none",
            }
        count = self._remote_count(payload)
        if count is not None:
            return count
        return self._fallback.count_messages(messages, reasoning_effort=reasoning_effort)

    def _remote_count(self, payload: dict[str, Any]) -> int | None:
        """One /tokenize call, or None for the estimator: the endpoint is
        pinned off, inside a retry window, or this call failed."""
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
        url = f"{self._origin}/tokenize"
        headers = bearer_auth_headers(self._api_key)
        try:
            if self._client is not None:
                resp = self._client.post(url, json=payload, timeout=self._timeout_s, headers=headers)
            else:
                resp = httpx2.post(url, json=payload, timeout=self._timeout_s, headers=headers)
            status = resp.status_code
            if status == 200:
                data = resp.json()
                if isinstance(data, dict):
                    count = None
                    if type(data.get("count")) is int and data["count"] >= 0:
                        count = data["count"]
                    elif "count" not in data and isinstance(data.get("tokens"), list):
                        count = len(data["tokens"])
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
            log.info("vLLM /tokenize at %s answers again; remote token counting resumed", url)

    def _downgrade(self, url: str, reason: str, *, mode: str) -> None:
        if mode == "absent":
            with self._lock:
                self._pinned = True
            log.warning(
                "vLLM /tokenize unusable at %s (%s); pinning in-process token estimation",
                url,
                reason,
            )
            return
        if mode == "refused":
            retry_after = self._refused_retry_after_s
            with self._lock:
                self._retry_at = self._clock() + retry_after
            log.warning(
                "vLLM /tokenize refused at %s (%s); in-process token estimation "
                "until the next probe in %.0f s",
                url,
                reason,
                retry_after,
            )
            return
        with self._lock:
            self._retry_at = self._clock() + self._retry_after_s
        log.warning(
            "vLLM /tokenize failed at %s (%s); in-process token estimation for %.0f s",
            url,
            reason,
            self._retry_after_s,
        )


def build_tokenizer(
    settings: Settings, client: httpx2.Client | None = None
) -> Tokenizer:
    """Builds a tokenizer configured for the served reasoning model."""
    if settings.llm_base_url and settings.llm_model_reasoning:
        return VllmTokenizer(
            base_url=settings.llm_base_url,
            model=settings.llm_model_reasoning,
            timeout_s=settings.llm_tokenize_timeout_s,
            client=client,
            api_key=settings.llm_api_key,
        )
    return FallbackTokenizer()
