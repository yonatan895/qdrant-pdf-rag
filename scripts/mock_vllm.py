#!/usr/bin/env python3
"""Deterministic OpenAI-compatible model stand-in for CI rehearsal (CI only).

The air-gap deploy path hard-requires a vLLM-shaped embeddings endpoint
(EMBED_BASE_URL), and the airgap scripts refuse EMBED_MODE=hash by design.
The rehearsal therefore runs this mock as the stand-in for the owning team's
in-cluster vLLM: same API surface (/v1/embeddings, /healthz), deterministic
blake2b vectors of DENSE_DIM size — enough to prove URL/dim/env plumbing and
the fail-fast wiring end to end.

It also serves /v1/chat/completions deterministically (the reasoning-model
stand-in for the simulation tier): the answer is derived from the retrieved-
cite blocks in the prompt and echoes a retrieved citation, so it survives
parse_answer's hit-set validation. /tokenize is served at the origin root
(vLLM shape, prompt or messages) for the agent's token accounting. Never a
product path; never in the air gap.

Realism knobs for the load tier (all default-off; zeros = today's instant,
byte-identical server):
    MOCK_TTFT_MS         sleep before the first chat byte (stream and non-stream)
    MOCK_TOKEN_INTERVAL_MS  sleep between streamed chunks (one chunk per piece)
    MOCK_JITTER_MS       uniform(-JITTER, +JITTER) added to each sleep
    MOCK_SEED            seed for jitter + failure draws (same seed + fresh
                         process = byte-identical streams and failure pattern)
    MOCK_ERROR_RATE      per-chat-request failure probability in [0, 1]:
                         stream -> abort after the first chunk (no [DONE]);
                         non-stream -> HTTP 500 fixed-shape error.
Scope: chat only. Embeddings, /tokenize, /healthz and /models stay instant
and infallible so eval/sim determinism never depends on load noise.

    MOCK_DIM=64 PORT=8000 python3 scripts/mock_vllm.py
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = int(os.environ.get("MOCK_DIM", "64"))
PORT = int(os.environ.get("PORT", "8000"))

TTFT_MS = float(os.environ.get("MOCK_TTFT_MS", "0"))
TOKEN_INTERVAL_MS = float(os.environ.get("MOCK_TOKEN_INTERVAL_MS", "0"))
JITTER_MS = float(os.environ.get("MOCK_JITTER_MS", "0"))
SEED = int(os.environ.get("MOCK_SEED", "0"))
ERROR_RATE = float(os.environ.get("MOCK_ERROR_RATE", "0"))

if TTFT_MS < 0 or TOKEN_INTERVAL_MS < 0 or JITTER_MS < 0 or not 0.0 <= ERROR_RATE <= 1.0:
    raise ValueError(
        "mock realism knobs must satisfy TTFT/INTERVAL/JITTER >= 0 and 0 <= ERROR_RATE <= 1"
    )

# Seeded draws (jitter + failure decisions) under one lock: same seed from a
# fresh process replays the same timing noise and failure pattern. Jitter and
# failures never touch response bytes — determinism is structural, not lucky.
_rng = random.Random(SEED)
_rng_lock = threading.Lock()

# Slept-milliseconds observability (load-tier assertions + knob tests):
# X-Mock-Sleep-Ms carries the exact paced total on non-stream chat;
# X-Mock-Ttft-Ms carries the exact first-byte pace on streams (interval
# pacing happens live, so only TTFT is knowable up front). Seeded replay
# reproduces these strings exactly — pacing is pinned without wall-clock
# assertions.

# Fixed-shape injected failure: no internals, no request echo.
_INJECTED_FAILURE = {"error": {"message": "mock injected failure"}}


def _pace(base_ms: float) -> float:
    """Sleep base_ms plus uniform(-JITTER_MS, +JITTER_MS), floored at zero.
    Returns the actual slept milliseconds for the timing headers."""
    delay_ms = base_ms
    if JITTER_MS > 0:
        with _rng_lock:
            delay_ms += _rng.uniform(-JITTER_MS, JITTER_MS)
    slept_ms = max(0.0, delay_ms)
    if slept_ms > 0:
        time.sleep(slept_ms / 1000.0)
    return slept_ms


def _should_fail() -> bool:
    if ERROR_RATE <= 0:
        return False
    with _rng_lock:
        return _rng.random() < ERROR_RATE

# build_messages renders hits as "[n] <cite>\n<text>"; a cite always carries
# the ", p. <label>" suffix, which distinguishes hit markers from other
# numbered lines in prose.
_HIT_HEADER_RE = re.compile(r"^\[\d+\]\s+(?P<cite>.+,\s+p\.\s+.+)$")


def _embed(text: str) -> list[float]:
    """Deterministic bag-of-hashes dense vector, L2 normalized (same spirit
    as the CI HashEmbedder, served over the vLLM API shape)."""
    vec = [0.0] * DIM
    for token in text.split():
        digest = hashlib.blake2b(token.lower().encode(), digest_size=8).digest()
        idx = int.from_bytes(digest, "big") % DIM
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _chat_content(messages: list) -> str:
    """Deterministic reasoning-model output for the simulation tier.

    Finds the retrieved-cite blocks in the last user message, derives the
    answer from hit 1's text, and echoes hit 1's cite — a citation that is
    genuinely in the hit set, so parse_answer validation passes. Same prompt
    in, same body out."""
    user = ""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            user = str(msg.get("content") or "")
            break

    blocks: list[tuple[str, str]] = []  # (cite, first text line)
    cite: str | None = None
    text_lines: list[str] = []
    for line in user.splitlines():
        m = _HIT_HEADER_RE.match(line.strip())
        if m:
            if cite is not None:
                blocks.append((cite, _first_line(text_lines)))
            cite, text_lines = m.group("cite").strip(), []
        elif cite is not None:
            text_lines.append(line)
    if cite is not None:
        blocks.append((cite, _first_line(text_lines)))

    if not blocks:
        return "The retrieved excerpts did not contain a usable citation."
    top_cite, snippet = blocks[0]
    return (
        f"Based on the retrieved excerpts, the manual states: {snippet}\n\n"
        "```jcl\n"
        "// DETERMINISTIC SIMULATION EXAMPLE - NOT PRODUCTION-READY\n"
        f"// derived from: {top_cite}\n"
        "IOSCMDS LIST\n"
        "```\n\n"
        "Citations:\n"
        f"- {top_cite}\n"
    )


def _first_line(lines: list[str], limit: int = 160) -> str:
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) <= limit:
            return stripped
        cut = stripped[:limit].rsplit(" ", 1)[0]
        return cut + " ..."
    return "(no text)"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict | str, headers: dict[str, str] | None = None) -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"error": {"message": "invalid JSON body"}})
            return None
        if not isinstance(req, dict):
            self._send(400, {"error": {"message": "request body must be an object"}})
            return None
        return req

    def do_GET(self) -> None:  # http.server API requires this camelCase name
        if self.path in ("/healthz", "/health"):
            self._send(200, {"status": "ok"})
        elif self.path.endswith("/models"):
            self._send(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "mock-reasoning", "object": "model"},
                        {"id": "mock-embed", "object": "model"},
                    ],
                },
            )
        else:
            self._send(404, {"error": {"message": f"unknown path {self.path}"}})

    def do_POST(self) -> None:  # http.server API requires this camelCase name
        if self.path.endswith("/chat/completions"):
            self._chat_completions()
        elif self.path.endswith("/embeddings"):
            self._embeddings()
        elif self.path == "/tokenize":
            # Exact origin-root route on purpose (vLLM serves /tokenize next
            # to /v1, not under it): a wrong-URL client regression must 404,
            # not silently match a loose suffix.
            self._tokenize()
        else:
            self._send(404, {"error": {"message": f"unknown path {self.path}"}})

    def _chat_completions(self) -> None:
        req = self._read_json()
        if req is None:
            return
        messages = req.get("messages")
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            self._send(400, {"error": {"message": "'messages' must be a list of objects"}})
            return
        content = _chat_content(messages)
        prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in messages)
        completion_tokens = len(content.split())
        finish_reason = req.get("mock_finish_reason", "stop")
        model = req.get("model", "mock-reasoning")
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": 10,
            "total_tokens": prompt_tokens + completion_tokens + 10,
        }
        if req.get("stream"):
            ttft_ms = _pace(TTFT_MS)  # first-byte latency, before any chunk
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Mock-Ttft-Ms", f"{ttft_ms:.1f}")
            self.end_headers()
            abort = _should_fail()
            # One SSE chunk per piece with per-chunk flush: real TTFT plus
            # observable inter-token pacing (the old two-chunk split is
            # gone; reassembly is still byte-exact). Pieces keep their
            # whitespace so concatenation == content.
            pieces = re.findall(r"\S+\s*|\s+", content) or [""]
            for i, piece in enumerate(pieces):
                if i > 0:
                    _pace(TOKEN_INTERVAL_MS)
                delta: dict[str, str] = {"content": piece}
                if i == 0:
                    delta["role"] = "assistant"
                choice: dict = {"index": 0, "delta": delta, "finish_reason": None}
                chunk: dict = {
                    "id": "chatcmpl-mock-deterministic",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [choice],
                }
                if i == len(pieces) - 1 and not abort:
                    choice["finish_reason"] = finish_reason
                    chunk["usage"] = usage
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
                if abort:
                    # Mid-stream failure: close after the first chunk with no
                    # [DONE] and no final — the truncated-SSE shape the
                    # agent's stream recovery path must survive.
                    return
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        slept_ms = _pace(TTFT_MS)
        pace_headers = {"X-Mock-Sleep-Ms": f"{slept_ms:.1f}"}
        if _should_fail():
            self._send(500, _INJECTED_FAILURE, headers=pace_headers)
            return
        self._send(
            200,
            {
                "id": "chatcmpl-mock-deterministic",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish_reason,
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": usage,
            },
            headers=pace_headers,
        )

    def _tokenize(self) -> None:
        req = self._read_json()
        if req is None:
            return
        # vLLM /tokenize accepts either {"prompt": str} or {"messages": [...]}
        # (the message form is chat-template aware — what the agent's
        # verification loop uses).
        msgs = req.get("messages")
        if isinstance(msgs, list):
            text = " ".join(
                str(m.get("content") or "") for m in msgs if isinstance(m, dict)
            )
        else:
            text = str(req.get("prompt") or "")
        # Stable across processes (issue #160): Python's hash() is salted
        # per process, so token ids varied run to run. blake2b matches
        # _embed's spirit; case is preserved (unlike _embed's lowering) so
        # ids lose only the salt, nothing else. Still mock ids, never real
        # vLLM BPE ids — see docs/eval.md §9.
        tokens = [
            int.from_bytes(hashlib.blake2b(w.encode(), digest_size=8).digest(), "big") % 10000 + 1
            for w in text.split()
        ]
        self._send(
            200,
            {
                "count": len(tokens),
                "max_model_len": 4096,
                "tokens": tokens,
                "token_strs": None,
            },
        )

    def _embeddings(self) -> None:
        req = self._read_json()
        if req is None:
            return
        inputs = req.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        if not isinstance(inputs, list) or not all(isinstance(t, str) for t in inputs):
            self._send(400, {"error": {"message": "'input' must be a string or list of strings"}})
            return
        self._send(
            200,
            {
                "object": "list",
                "model": req.get("model", "mock-embed"),
                "data": [
                    {"object": "embedding", "index": i, "embedding": _embed(text)}
                    for i, text in enumerate(inputs)
                ],
                "usage": {"prompt_tokens": sum(len(t.split()) for t in inputs), "total_tokens": 0},
            },
        )

    def log_message(self, fmt: str, *args) -> None:  # keep CI logs one-line JSON-ish
        print(f'{{"logger": "mock-vllm", "msg": "{fmt % args}"}}', flush=True)


if __name__ == "__main__":
    print(json.dumps({"logger": "mock-vllm", "msg": f"listening on :{PORT} dim={DIM}"}), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
