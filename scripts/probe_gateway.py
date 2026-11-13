#!/usr/bin/env python3
"""Gateway readiness probe for the LiteLLM cutover.

Reads Settings from the environment (the same keys the agent and ingest
use, including the `*_API_KEY` knobs) and exercises each configured model
leg directly: embed (`/models` + `/embeddings` dimension check), reasoning
(`/models` + `/chat/completions`, streaming optional), rerank (`/v1/score`
vs `/v1/rerank` reachability — prints the recommended
`RERANK_ENDPOINT_ORDER`), tokenizer (chat-template-aware `/tokenize`; `--require-tokenizer` makes
an absent or malformed route fail acceptance rather than allow estimation).

Response shapes mirror the client contracts they diagnose (`embed.py`,
`answer.py`, `rerank.py`, `tokenizer.py` own the parsing; this script only
asks "does the leg answer usably"). 401/403 answers name the missing
virtual-key knob explicitly.

Exit 0 when every required leg answers (embed always; reasoning when
`LLM_MODEL_REASONING` is set; rerank when `RERANK_ENABLED=true`); exit 1
otherwise. Run from inside the cluster, on the same network as the agent:

    kubectl -n mainframe-rag exec deploy/rag-agent -- \
        python3 /app/scripts/probe_gateway.py

Argument probe (`--probe-args`, issue #1193): the served model and its chat
template are platform-owned, so which request arguments make it think more
or less is discovered on the running site instead of assumed. One fixed
synthetic prompt is sent once under each candidate argument set (built-in
candidates, plus `--candidates-file` / `--candidate NAME=JSON`), and one
JSON row per candidate reports the HTTP status, finish reason, prompt /
completion / reasoning tokens, whether a separate reasoning field came
back, time to first token, decode tokens per second and elapsed time. A
row never carries the prompt, the answer, reasoning text, a key or
upstream error text: only counts, timings, fixed labels and the candidate's
own arguments. A candidate the gateway refuses is a finding (its status),
not a probe failure; the mode exits 1 only when the `baseline` candidate
(no extra arguments, a reserved name) ran and did not complete. The winning arguments go into
`LLM_EXTRA_BODY` / `LLM_EFFORT_TRANSPORT`.

    kubectl -n mainframe-rag exec deploy/rag-agent -- \
        python3 /app/scripts/probe_gateway.py --probe-args
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mainframe_rag.config import Settings, bearer_auth_headers, load_settings
from mainframe_rag.model_args import parse_body_fields

PROBE_TEXT = "gateway readiness probe"
PROBE_TIMEOUT_S = 30.0
PROBE_MAX_TOKENS = 16
PROBE_MAX_TOKENS_CAP = 4096

# Argument probe (#1193). The prompt is original, fixed and needs a few
# reasoning steps, so a thinking argument shows up in tokens and timing.
ARG_PROBE_PROMPT = (
    "A batch window opens at 06:10 and one job starts every 45 minutes from then. "
    "List each start time before 09:00, then give the number of starts on a final line."
)
ARG_PROBE_MAX_TOKENS = 512
ARG_PROBE_TIMEOUT_S = 120.0
ARG_PROBE_MAX_CANDIDATES = 64
_CANDIDATE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}")
# Reasoning arrives in a separate field under either name, by server version.
_REASONING_FIELDS = ("reasoning_content", "reasoning")


def completion_budget(value: str) -> int:
    """Diagnostic completion limit; never changes application answer budgets."""
    if not value.isascii() or not value.isdecimal() or len(value) > 4:
        raise argparse.ArgumentTypeError("max tokens must be an integer from 1 to 4096")
    try:
        budget = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max tokens must be an integer from 1 to 4096") from None
    if not 1 <= budget <= PROBE_MAX_TOKENS_CAP:
        raise argparse.ArgumentTypeError("max tokens must be an integer from 1 to 4096")
    return budget


def _auth_hint(status: int, knob: str) -> str:
    if status in (401, 403):
        return f"HTTP {status} — virtual key rejected, check {knob}"
    return f"HTTP {status}"


def check_models(label: str, base_url: str, api_key: str | None, timeout: float) -> tuple[str, str]:
    """GET {base}/models: names the served ids (verifies model names +
    auth in one call). Never fails the run alone — each leg judges itself."""
    try:
        resp = httpx2.get(
            f"{base_url.rstrip('/')}/models",
            timeout=timeout,
            headers=bearer_auth_headers(api_key),
        )
    except (httpx2.HTTPError, OSError) as exc:
        return "fail", f"{label} /models unreachable: {exc}"
    if resp.status_code in (401, 403):
        return "fail", f"{label} /models {_auth_hint(resp.status_code, 'key for ' + label)}"
    if resp.status_code != 200:
        return "fail", f"{label} /models HTTP {resp.status_code}"
    try:
        ids = [m.get("id") for m in resp.json().get("data", []) if isinstance(m, dict)]
    except ValueError:
        return "fail", f"{label} /models returned non-JSON"
    return "ok", f"{label} /models: {', '.join(i for i in ids if i) or '(no ids)'}"


def check_embeddings(settings: Settings, timeout: float) -> tuple[str, str]:
    """POST /embeddings + dimension match against DENSE_DIM (fail-closed:
    ingest refuses dim mismatches, so the probe must too)."""
    try:
        base_url, model = settings.require_embed()
    except RuntimeError as exc:
        return "fail", f"embed not configured: {exc}"
    try:
        resp = httpx2.post(
            f"{base_url.rstrip('/')}/embeddings",
            json={"model": model, "input": [PROBE_TEXT]},
            timeout=timeout,
            headers=bearer_auth_headers(settings.embed_api_key),
        )
    except (httpx2.HTTPError, OSError) as exc:
        return "fail", f"embed /embeddings unreachable: {exc}"
    if resp.status_code in (401, 403):
        return "fail", f"embed /embeddings {_auth_hint(resp.status_code, 'EMBED_API_KEY')}"
    if resp.status_code != 200:
        return "fail", f"embed /embeddings HTTP {resp.status_code}"
    try:
        vec = resp.json()["data"][0]["embedding"]
    except (ValueError, KeyError, IndexError, TypeError):
        return "fail", "embed /embeddings returned no vector"
    want = settings.require_dense_dim()
    if len(vec) != want:
        return "fail", f"embed dim {len(vec)} != DENSE_DIM {want} (ingest would refuse)"
    return "ok", f"embed /embeddings: model={model} dim={len(vec)}"


def check_chat(
    settings: Settings, timeout: float, stream: bool, max_tokens: int = PROBE_MAX_TOKENS
) -> tuple[str, str]:
    """POST /chat/completions (+ optional SSE [DONE] check). Skipped when no
    reasoning model is configured — answers stay disabled by design then."""
    if not settings.llm_base_url or not settings.llm_model_reasoning:
        return "skip", "reasoning not configured (/v1/answer stays disabled)"
    base_url, model = settings.llm_base_url, settings.llm_model_reasoning
    headers = bearer_auth_headers(settings.llm_api_key)
    # Match the application's simple-query reasoning contract, including the
    # configured effort transport and extra body fields (#1193): unset, this
    # is the top-level `reasoning_effort` it always sent.
    request_args = settings.model_request_args()
    effort = settings.llm_reasoning_effort_simple
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "temperature": 0,
        "max_tokens": max_tokens,
        **request_args.effort_fields(effort),
        **request_args.extra_fields(effort),
    }
    try:
        resp = httpx2.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=body,
            timeout=timeout,
            headers=headers,
        )
    except (httpx2.HTTPError, OSError) as exc:
        return "fail", f"chat /chat/completions unreachable: {exc}"
    if resp.status_code in (401, 403):
        return "fail", f"chat /chat/completions {_auth_hint(resp.status_code, 'LLM_API_KEY')}"
    if resp.status_code != 200:
        return "fail", f"chat /chat/completions HTTP {resp.status_code}"
    try:
        choices = resp.json()["choices"]
    except (ValueError, KeyError, TypeError):
        return "fail", "chat /chat/completions returned no choices"
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], dict)
        or choices[0].get("finish_reason") != "stop"
        or not isinstance(choices[0].get("message"), dict)
        or not isinstance(choices[0]["message"].get("content"), str)
        or not choices[0]["message"]["content"].strip()
    ):
        return "fail", "chat /chat/completions returned no successfully completed answer"
    detail = f"chat /chat/completions: model={model} finish={choices[0].get('finish_reason')}"
    if not stream:
        return "ok", detail
    try:
        saw_done = False
        saw_finish = False
        saw_content = False
        with httpx2.stream(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            json={**body, "stream": True},
            timeout=timeout,
            headers=headers,
        ) as stream_resp:
            stream_resp.raise_for_status()
            for line in stream_resp.iter_lines():
                line = line.strip()
                if line == "event: error":
                    return "fail", detail + "; streaming error event"
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    saw_done = True
                    break
                try:
                    frame = json.loads(payload)
                except ValueError:
                    return "fail", detail + "; streaming non-JSON frame"
                if not isinstance(frame, dict) or "error" in frame:
                    return "fail", detail + "; streaming error or invalid frame"
                chunks = frame.get("choices")
                if not isinstance(chunks, list):
                    return "fail", detail + "; streaming missing choices"
                # OpenAI usage-only frames legitimately carry choices: [].
                if not chunks and isinstance(frame.get("usage"), dict):
                    continue
                if len(chunks) != 1 or not isinstance(chunks[0], dict):
                    return "fail", detail + "; streaming invalid choices"
                chunk = chunks[0]
                delta = chunk.get("delta")
                if not isinstance(delta, dict) or saw_finish:
                    return "fail", detail + "; streaming invalid delta or data after finish"
                content = delta.get("content")
                if content is not None and not isinstance(content, str):
                    return "fail", detail + "; streaming invalid content"
                saw_content = saw_content or bool(content and content.strip())
                finish = chunk.get("finish_reason")
                if finish is not None:
                    if finish != "stop":
                        return "fail", detail + "; streaming unsuccessful finish"
                    saw_finish = True
    except (httpx2.HTTPError, OSError) as exc:
        return "fail", detail + f"; streaming FAILED ({exc})"
    if not saw_done:
        return "fail", detail + "; streaming has no [DONE]"
    if not saw_finish or not saw_content:
        return "fail", detail + "; streaming has no successfully completed answer"
    return "ok", detail + "; streaming finish + [DONE] ok"


def _leg_ok(
    label: str,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    valid: Any,
) -> tuple[bool, str]:
    """One rerank leg: True when it answers 200 with a usable shape."""
    try:
        resp = httpx2.post(url, json=payload, timeout=timeout, headers=headers)
    except (httpx2.HTTPError, OSError) as exc:
        return False, f"{label} unreachable ({exc})"
    if resp.status_code in (401, 403):
        return False, f"{label} {_auth_hint(resp.status_code, 'RERANK_API_KEY')}"
    if resp.status_code != 200:
        return False, f"{label} HTTP {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return False, f"{label} non-JSON"
    if not valid(data):
        return False, f"{label} unusable shape"
    return True, f"{label} ok"


def check_rerank(settings: Settings, timeout: float) -> tuple[str, str, str | None]:
    """Probe both scoring legs independently and recommend the order.
    Skipped unless reranking is enabled. Returns (status, detail,
    recommended order or None)."""
    if not settings.rerank_enabled:
        return "skip", "rerank disabled (RERANK_ENABLED=false)", None
    base_url = settings.rerank_base_url or settings.embed_base_url
    if not base_url:
        return "fail", "rerank enabled but no RERANK_BASE_URL (or EMBED_BASE_URL)", None
    base = base_url.rstrip("/")
    headers = bearer_auth_headers(settings.rerank_api_key)
    score_url = f"{base}/score" if base.endswith("/v1") else f"{base}/v1/score"
    rerank_url = f"{base}/rerank" if base.endswith("/v1") else f"{base}/v1/rerank"
    score_ok, score_detail = _leg_ok(
        "/v1/score",
        score_url,
        {"model": settings.rerank_model, "text_1": PROBE_TEXT, "text_2": [PROBE_TEXT, "other"]},
        headers,
        timeout,
        lambda d: isinstance(d, dict)
        and isinstance(d.get("data"), list)
        and len(d["data"]) == 2,
    )
    rerank_ok, rerank_detail = _leg_ok(
        "/v1/rerank",
        rerank_url,
        {"model": settings.rerank_model, "query": PROBE_TEXT, "documents": [PROBE_TEXT, "other"]},
        headers,
        timeout,
        lambda d: isinstance(d, dict)
        and isinstance(d.get("results"), list)
        and len(d["results"]) == 2,
    )
    detail = f"rerank: {score_detail}; {rerank_detail}"
    if score_ok:
        return "ok", detail, "score_first"
    if rerank_ok:
        return "ok", detail, "rerank_first"
    return "fail", detail, None


def check_tokenize(settings: Settings, timeout: float, required: bool = False) -> tuple[str, str]:
    """POST chat messages to /tokenize; optionally require exact remote counts.
    Without required=True, unavailable routes retain estimator compatibility."""
    if not settings.llm_base_url or not settings.llm_model_reasoning:
        return ("fail" if required else "skip"), "tokenizer not configured (no reasoning model)"
    origin = settings.llm_base_url.rstrip("/").removesuffix("/v1")
    try:
        resp = httpx2.post(
            f"{origin}/tokenize",
            json={"model": settings.llm_model_reasoning,
                  "messages": [{"role": "user", "content": PROBE_TEXT}],
                  "add_generation_prompt": True, "add_special_tokens": False},
            timeout=timeout,
            headers=bearer_auth_headers(settings.llm_api_key),
        )
    except (httpx2.HTTPError, OSError):
        return ("fail" if required else "ok"), "tokenize unreachable — estimator fallback"
    if resp.status_code != 200:
        return ("fail" if required else "ok"), f"tokenize HTTP {resp.status_code} — estimator fallback"
    try:
        data = resp.json()
    except ValueError:
        return ("fail" if required else "ok"), "tokenize non-JSON — estimator fallback"
    if isinstance(data, dict) and (
        (type(data.get("count")) is int and data["count"] > 0)
        or ("count" not in data and isinstance(data.get("tokens"), list) and data["tokens"])
    ):
        return "ok", "tokenize served"
    return ("fail" if required else "ok"), "tokenize unusable shape — estimator fallback"


def builtin_candidates(settings: Settings) -> list[tuple[str, dict[str, Any]]]:
    """Generic guesses, no model assumed: no arguments, what the application
    is configured to send, each served effort on both transports, and the two
    common template thinking switches. A template that ignores an argument
    answers like `baseline`; a gateway that refuses it shows its status."""
    args = settings.model_request_args()
    effort = settings.llm_reasoning_effort_simple
    candidates: list[tuple[str, dict[str, Any]]] = [
        ("baseline", {}),
        ("configured", {**args.effort_fields(effort), **args.extra_fields(effort)}),
    ]
    for served in settings.reasoning_efforts():
        candidates.append((f"effort-top-{served}", {"reasoning_effort": served}))
        candidates.append((f"effort-template-{served}", {"chat_template_kwargs": {"reasoning_effort": served}}))
    for key in ("enable_thinking", "thinking"):
        for label, value in (("on", True), ("off", False)):
            candidates.append((f"{key.replace('_', '-')}-{label}", {"chat_template_kwargs": {key: value}}))
    return candidates


def _candidate(name: str, raw: str) -> tuple[str, dict[str, Any]]:
    """One operator candidate. Protected fields are refused; fields another
    setting owns (effort, temperature, cap) are allowed here: trying them is
    the point. Errors name the rule, never the supplied text."""
    if not _CANDIDATE_NAME.fullmatch(name):
        raise ValueError("a candidate name must be 1 to 48 characters of letters, digits, '.', '_' or '-'")
    if name == "baseline":
        # The reference row and the mode's pass/fail: always "no arguments".
        raise ValueError("the candidate name baseline is reserved for the request without arguments")
    return name, parse_body_fields(raw, name="a candidate", allow_owned=True)


def load_candidates(
    settings: Settings, *, builtin: bool, files: list[str], inline: list[str], only: list[str],
) -> list[tuple[str, dict[str, Any]]]:
    """Built-in candidates, then file candidates (one JSON object mapping a
    name to its argument object), then `NAME=JSON` flags; a later definition
    of a name replaces the earlier one in place (`baseline` is reserved).
    `only` keeps the named ones, in the order given."""
    merged: dict[str, dict[str, Any]] = dict(builtin_candidates(settings)) if builtin else {}
    for path in files:
        try:
            # A dedicated message below; never the file's content.
            loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("a candidates file must be readable JSON") from None
        if not isinstance(loaded, dict) or not all(isinstance(body, dict) for body in loaded.values()):
            raise ValueError("a candidates file must be one JSON object mapping names to argument objects")
        for name, body in loaded.items():
            merged.update([_candidate(name, json.dumps(body))])
    for item in inline:
        name, sep, raw = item.partition("=")
        if not sep:
            raise ValueError("--candidate takes NAME=JSON")
        merged.update([_candidate(name, raw)])
    if only:
        missing = [name for name in only if name not in merged]
        if missing:
            raise ValueError("--only names a candidate that is not defined")
        merged = {name: merged[name] for name in only}
    if not merged or len(merged) > ARG_PROBE_MAX_CANDIDATES:
        raise ValueError(f"the argument probe takes 1 to {ARG_PROBE_MAX_CANDIDATES} candidates")
    return list(merged.items())


def _usage_int(usage: Any, *path: str) -> int | None:
    value: Any = usage
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value if type(value) is int and value >= 0 else None


class _ArgObservation:
    """What one candidate's response showed, as counts and timings only.
    Text is measured for length and dropped; nothing here can be printed
    back as prompt, answer or reasoning."""

    def __init__(self) -> None:
        self.finish_reason: str | None = None
        self.usage: Any = None
        self.reasoning_chars = 0
        self.content_chars = 0
        self.ttft_ms: int | None = None
        self.first_content_ms: int | None = None

    def feed(self, part: Any, finish: Any, t0: float) -> bool:
        """One delta or message; False when its shape is unusable."""
        if not isinstance(part, dict):
            return False
        now_ms = int((time.monotonic() - t0) * 1000)
        for key in _REASONING_FIELDS:
            text = part.get(key)
            if isinstance(text, str) and text:
                self.reasoning_chars += len(text)
                self.ttft_ms = now_ms if self.ttft_ms is None else self.ttft_ms
        content = part.get("content")
        if isinstance(content, str) and content:
            self.content_chars += len(content)
            self.ttft_ms = now_ms if self.ttft_ms is None else self.ttft_ms
            self.first_content_ms = now_ms if self.first_content_ms is None else self.first_content_ms
        if isinstance(finish, str) and finish:
            self.finish_reason = finish
        return True

    def feed_frame(self, frame: Any, t0: float, *, part_key: str) -> bool:
        if not isinstance(frame, dict) or "error" in frame:
            return False
        if isinstance(frame.get("usage"), dict):
            self.usage = frame["usage"]
        choices = frame.get("choices")
        if not isinstance(choices, list):
            return False
        return all(
            isinstance(choice, dict) and self.feed(choice.get(part_key) or {}, choice.get("finish_reason"), t0)
            for choice in choices
        )


def probe_candidate(
    settings: Settings, name: str, fields: dict[str, Any], timeout: float, max_tokens: int,
) -> dict[str, Any]:
    """Send the fixed prompt once under one candidate; return its row."""
    body: dict[str, Any] = {
        "model": settings.llm_model_reasoning,
        "messages": [{"role": "user", "content": ARG_PROBE_PROMPT}],
        "temperature": 0,
        "max_tokens": max_tokens,
        **fields,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    seen = _ArgObservation()
    status: int | None = None
    error: str | None = None
    t0 = time.monotonic()
    try:
        with httpx2.stream(
            "POST",
            f"{str(settings.llm_base_url).rstrip('/')}/chat/completions",
            json=body,
            timeout=timeout,
            headers=bearer_auth_headers(settings.llm_api_key),
        ) as resp:
            status = resp.status_code
            if status != 200:
                # The refusal's body is upstream text: never read, never shown.
                error = "http_error"
            elif "application/json" in resp.headers.get("content-type", ""):
                # A gateway that buffers despite the stream ask: no first-token time.
                resp.read()
                if not seen.feed_frame(resp.json(), t0, part_key="message"):
                    error = "malformed_response"
                seen.ttft_ms = seen.first_content_ms = None
            else:
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        if line.startswith("event: error"):
                            error = "stream_error"
                            break
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        frame = json.loads(payload)
                    except ValueError:
                        frame = None
                    if not seen.feed_frame(frame, t0, part_key="delta"):
                        error = "malformed_response"
                        break
    except ValueError:
        error = "malformed_response"
    except (httpx2.HTTPError, OSError) as exc:
        # The exception class is a fixed label; its message may hold upstream text.
        error = f"transport_error:{type(exc).__name__}"
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    if error is None and seen.finish_reason is None:
        error = "no_finish"
    completion = _usage_int(seen.usage, "completion_tokens")
    decode_s = (elapsed_ms - seen.ttft_ms) / 1000 if seen.ttft_ms is not None else 0.0
    return {
        "candidate": name,
        "args": fields,
        "status": status,
        "error": error,
        "finish_reason": seen.finish_reason,
        "prompt_tokens": _usage_int(seen.usage, "prompt_tokens"),
        "completion_tokens": completion,
        "reasoning_tokens": _usage_int(seen.usage, "completion_tokens_details", "reasoning_tokens"),
        "reasoning_field": seen.reasoning_chars > 0,
        "reasoning_chars": seen.reasoning_chars,
        "content_chars": seen.content_chars,
        "ttft_ms": seen.ttft_ms,
        "first_content_ms": seen.first_content_ms,
        # Tokens after the first one over the time after it: the decode rate,
        # reasoning tokens included (they are completion tokens).
        "decode_tps": round((completion - 1) / decode_s, 1) if completion and completion > 1 and decode_s > 0 else None,
        "elapsed_ms": elapsed_ms,
    }


def run_arg_probe(settings: Settings, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """`--probe-args`: one JSON row per candidate, then one summary line."""
    try:
        candidates = load_candidates(
            settings, builtin=not args.no_builtin, files=args.candidates_file,
            inline=args.candidate, only=[n for item in args.only for n in item.split(",") if n],
        )
    except ValueError as exc:
        # Raised here with fixed wording only (no supplied text).
        parser.error(str(exc))
    if not settings.llm_base_url or not settings.llm_model_reasoning:
        print("ARGUMENT PROBE FAILED: LLM_BASE_URL / LLM_MODEL_REASONING not configured")
        return 1
    timeout = args.timeout if args.timeout is not None else ARG_PROBE_TIMEOUT_S
    max_tokens = args.max_tokens if args.max_tokens is not None else ARG_PROBE_MAX_TOKENS
    completed = 0
    baseline_ok: bool | None = None
    for name, fields in candidates:
        row = probe_candidate(settings, name, fields, timeout, max_tokens)
        print(json.dumps(row, sort_keys=True), flush=True)
        ok = row["status"] == 200 and row["error"] is None
        completed += ok
        if name == "baseline":
            baseline_ok = ok
    failed = baseline_ok is False
    print(f"ARGUMENT PROBE {'FAILED' if failed else 'DONE'}: candidates={len(candidates)} completed={completed}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe gateway model legs before cutover.")
    parser.add_argument("--timeout", type=float, default=None,
                        help=f"per-request timeout in seconds (default {PROBE_TIMEOUT_S:g}; "
                             f"{ARG_PROBE_TIMEOUT_S:g} with --probe-args)")
    parser.add_argument(
        "--max-tokens", type=completion_budget,
        default=None,
        help=f"completion budget 1–4096 (readiness: GATEWAY_PROBE_MAX_TOKENS, default {PROBE_MAX_TOKENS}; "
             f"--probe-args: default {ARG_PROBE_MAX_TOKENS}, the readiness variable is not read)",
    )
    parser.add_argument("--probe-args", action="store_true",
                        help="argument probe: one fixed prompt under each candidate argument set, one JSON row each")
    parser.add_argument("--candidates-file", action="append", default=[], metavar="FILE",
                        help="JSON object mapping candidate names to argument objects (repeatable)")
    parser.add_argument("--candidate", action="append", default=[], metavar="NAME=JSON",
                        help="one more candidate (repeatable)")
    parser.add_argument("--only", action="append", default=[], metavar="NAME[,NAME]",
                        help="run only these candidates, in this order")
    parser.add_argument("--no-builtin", action="store_true", help="skip the built-in candidates")
    parser.add_argument("--stream", action="store_true", help="require a successful chat stream ending in [DONE]")
    parser.add_argument("--require-reasoning", action="store_true", help="fail when reasoning is not configured")
    parser.add_argument("--require-tokenizer", action="store_true", help="require chat-template-aware model token counts")
    parser.add_argument("--models", action="store_true", help="list served model ids and exit")
    args = parser.parse_args(argv)
    if args.max_tokens is None and not args.probe_args:
        # The readiness budget's environment default, validated like the flag.
        try:
            args.max_tokens = completion_budget(os.environ.get("GATEWAY_PROBE_MAX_TOKENS") or str(PROBE_MAX_TOKENS))
        except argparse.ArgumentTypeError as exc:
            parser.error(f"argument --max-tokens: {exc}")
    if args.models and (args.stream or args.require_reasoning or args.require_tokenizer):
        parser.error("--models cannot be combined with readiness requirements")
    if args.probe_args and (args.models or args.stream or args.require_reasoning or args.require_tokenizer):
        parser.error("--probe-args cannot be combined with --models or readiness requirements")
    if not args.probe_args and (args.candidates_file or args.candidate or args.only or args.no_builtin):
        parser.error("candidate options require --probe-args")

    settings = load_settings()
    if args.probe_args:
        return run_arg_probe(settings, args, parser)
    if args.timeout is None:
        args.timeout = PROBE_TIMEOUT_S
    failures = 0
    recommendation: str | None = None

    if args.models:
        for label, base, key in (
            ("embed", settings.embed_base_url, settings.embed_api_key),
            ("reasoning", settings.llm_base_url, settings.llm_api_key),
            ("rerank", settings.rerank_base_url or settings.embed_base_url, settings.rerank_api_key),
        ):
            if not base:
                print(f"{label}: not configured")
                continue
            status, detail = check_models(label, base, key, args.timeout)
            print(f"[{status.upper()}] {detail}")
            failures += status == "fail"
        return 1 if failures else 0

    if settings.embed_base_url:
        status, detail = check_models("embed", settings.embed_base_url, settings.embed_api_key, args.timeout)
        print(f"[{status.upper()}] {detail}")
        failures += status == "fail"
    status, detail = check_embeddings(settings, args.timeout)
    print(f"[{status.upper()}] {detail}")
    failures += status == "fail"

    if settings.llm_base_url:
        status, detail = check_models("reasoning", settings.llm_base_url, settings.llm_api_key, args.timeout)
        print(f"[{status.upper()}] {detail}")
        failures += status == "fail"
    status, detail = check_chat(settings, args.timeout, args.stream, args.max_tokens)
    if status == "skip" and (args.require_reasoning or args.stream):
        status, detail = "fail", "reasoning required but LLM_BASE_URL / LLM_MODEL_REASONING not configured"
    print(f"[{status.upper()}] {detail}")
    failures += status == "fail"

    if settings.rerank_enabled and (settings.rerank_base_url or settings.embed_base_url):
        status, detail = check_models(
            "rerank",
            str(settings.rerank_base_url or settings.embed_base_url),
            settings.rerank_api_key,
            args.timeout,
        )
        print(f"[{status.upper()}] {detail}")
        failures += status == "fail"
    status, detail, recommendation = check_rerank(settings, args.timeout)
    print(f"[{status.upper()}] {detail}")
    failures += status == "fail"

    status, detail = check_tokenize(settings, args.timeout, args.require_tokenizer)
    print(f"[{status.upper()}] {detail}")
    failures += status == "fail"

    if recommendation is not None:
        current = settings.rerank_endpoint_order
        print(f"recommendation: RERANK_ENDPOINT_ORDER={recommendation} (current: {current})")

    print("GATEWAY PROBE " + ("FAILED" if failures else "PASSED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
