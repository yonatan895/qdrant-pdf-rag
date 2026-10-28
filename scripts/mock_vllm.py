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

Slow-model stand-in for the Open WebUI operator lane (#1118; default off):
    MOCK_PACED_MARKER     a word (8-64 letters/digits); only a chat request whose
                          last user message contains it is paced, so every other
                          request in the same lane stays instant and byte-identical
    MOCK_PACED_TTFT_MS    a streamed reply sends its headers at once (as vLLM
                          does), then is silent this long before the first chunk
                          (a reasoning model thinking); non-stream sleeps it too
    MOCK_PACED_STREAM_MS  the chunks after the first are spread evenly over this
                          long (non-stream adds it to its single sleep)
The reply body is that of an unpaced request.

Rerank stand-in (issue #815, CI gateway lane only): POST .../score (vLLM
`text_1`/`text_2` shape) and POST .../rerank (Cohere/TEI `query`/`documents`
shape). Its one opinion is positional: candidate i of n scores (i+1)/n, so
reranking exactly reverses the order the agent hands over. That proves the
leg engaged and the returned scores reached the right candidates; it says
nothing about relevance or ranking quality. GET /stats counts requests per
route, split into the agent's readiness probe (a single "probe"/"probe"
pair) and search batches. MOCK_RERANK_FAULT: score_unavailable (the score
route answers 404, like a gateway without the vLLM-only route) or
unavailable (both routes answer 503).

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
# Explicit deterministic faults for the gateway contract lane:
# upstream (HTTP 503), malformed (misshapen non-stream payload),
# truncated (SSE abort before [DONE]), error_frame (content, then a 200
# `error` frame, then [DONE]), no_finish (content, then [DONE] without a
# terminal finish_reason). Healthy is byte-identical for model responses;
# health exposes CI control state only.
CHAT_FAULT = os.environ.get("MOCK_CHAT_FAULT", "healthy")
EMBED_FAULT = os.environ.get("MOCK_EMBED_FAULT", "healthy")
if CHAT_FAULT not in ("healthy", "upstream", "malformed", "truncated", "error_frame", "no_finish"):
    raise ValueError("invalid MOCK_CHAT_FAULT")
if EMBED_FAULT not in ("healthy", "upstream", "malformed", "dimension"):
    raise ValueError("invalid MOCK_EMBED_FAULT")
RERANK_FAULT = os.environ.get("MOCK_RERANK_FAULT", "healthy")
if RERANK_FAULT not in ("healthy", "score_unavailable", "unavailable"):
    raise ValueError("invalid MOCK_RERANK_FAULT")

if TTFT_MS < 0 or TOKEN_INTERVAL_MS < 0 or JITTER_MS < 0 or not 0.0 <= ERROR_RATE <= 1.0:
    raise ValueError(
        "mock realism knobs must satisfy TTFT/INTERVAL/JITTER >= 0 and 0 <= ERROR_RATE <= 1"
    )

PACED_MARKER = os.environ.get("MOCK_PACED_MARKER", "")
PACED_TTFT_MS = float(os.environ.get("MOCK_PACED_TTFT_MS", "0"))
PACED_STREAM_MS = float(os.environ.get("MOCK_PACED_STREAM_MS", "0"))
if PACED_MARKER and not re.fullmatch(r"[A-Za-z0-9]{8,64}", PACED_MARKER):
    raise ValueError("MOCK_PACED_MARKER must be 8-64 letters or digits")
if not all(math.isfinite(v) and v >= 0 for v in (PACED_TTFT_MS, PACED_STREAM_MS)):
    raise ValueError("MOCK_PACED_TTFT_MS and MOCK_PACED_STREAM_MS must be finite and >= 0")

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

# Rerank request counters (issue #815): requests received per route, split
# into the agent's /healthz probe and real search batches. Counted before any
# fault is applied, so a faulted attempt still shows as an attempt.
_rerank_stats = {route: {"search": 0, "probe": 0} for route in ("score", "rerank")}
_rerank_stats_lock = threading.Lock()


def _count_rerank(route: str, query: object, documents: object) -> None:
    probe = query == "probe" and documents == ["probe"]
    with _rerank_stats_lock:
        _rerank_stats[route]["probe" if probe else "search"] += 1


def _rerank_scores(count: int) -> list[float]:
    return [round((i + 1) / count, 6) for i in range(count)]


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


def _paced(messages: list) -> bool:
    """The slow-model stand-in applies: a marker is configured and the last user message carries it."""
    if not PACED_MARKER:
        return False
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return PACED_MARKER in str(msg.get("content") or "")
    return False


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
            self._send(200, {"status": "ok", "chat_fault": CHAT_FAULT,
                             "embed_fault": EMBED_FAULT, "ttft_ms": TTFT_MS,
                             "rerank_fault": RERANK_FAULT, "paced_marker": PACED_MARKER,
                             "paced_ttft_ms": PACED_TTFT_MS, "paced_stream_ms": PACED_STREAM_MS})
        elif self.path == "/stats":
            with _rerank_stats_lock:
                snapshot = {route: dict(counts) for route, counts in _rerank_stats.items()}
            self._send(200, {**snapshot, "rerank_fault": RERANK_FAULT})
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
        elif self.path.endswith("/score"):
            self._score()
        elif self.path.endswith("/rerank"):
            self._rerank()
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
        if CHAT_FAULT == "upstream":
            self._send(503, _INJECTED_FAILURE)
            return
        if CHAT_FAULT == "malformed":
            self._send(200, {"choices": [{"message": None}]})
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
        paced = _paced(messages)
        if req.get("stream"):
            ttft_ms = 0.0 if paced else _pace(TTFT_MS)  # first-byte latency, before any chunk
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Mock-Ttft-Ms", f"{ttft_ms:.1f}")
            self.end_headers()
            if paced:
                # Headers out, then silence: what every hop downstream sees while a
                # reasoning model thinks before its first token.
                self.wfile.flush()
                time.sleep(PACED_TTFT_MS / 1000.0)
            abort = CHAT_FAULT == "truncated" or _should_fail()
            # Stream-only completion-shape faults (issue #365): the upstream
            # attempt fails but still terminates with [DONE], the exact
            # counterexample the application parser must reject.
            error_frame = CHAT_FAULT == "error_frame"
            no_finish = CHAT_FAULT == "no_finish"
            # One SSE chunk per piece with per-chunk flush: real TTFT plus
            # observable inter-token pacing (the old two-chunk split is
            # gone; reassembly is still byte-exact). Pieces keep their
            # whitespace so concatenation == content.
            pieces = re.findall(r"\S+\s*|\s+", content) or [""]
            paced_gap_s = PACED_STREAM_MS / 1000.0 / max(len(pieces) - 1, 1)
            for i, piece in enumerate(pieces):
                if i > 0:
                    if paced:
                        time.sleep(paced_gap_s)
                    else:
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
                if (
                    i == len(pieces) - 1
                    and not abort
                    and not error_frame
                    and not no_finish
                ):
                    choice["finish_reason"] = finish_reason
                    chunk["usage"] = usage
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
                if abort:
                    # Mid-stream failure: close after the first chunk with no
                    # [DONE] and no final — the truncated-SSE shape the
                    # agent's stream recovery path must survive.
                    return
            if error_frame:
                # Error frame then [DONE]: a failed generation that still
                # looks terminated; never a successful completion.
                self.wfile.write(f"data: {json.dumps(_INJECTED_FAILURE)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        if paced:
            slept_ms = PACED_TTFT_MS + PACED_STREAM_MS
            time.sleep(slept_ms / 1000.0)
        else:
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

    def _score(self) -> None:
        req = self._read_json()
        if req is None:
            return
        documents = req.get("text_2")
        _count_rerank("score", req.get("text_1"), documents)
        if RERANK_FAULT == "score_unavailable":
            self._send(404, _INJECTED_FAILURE)
            return
        if RERANK_FAULT == "unavailable":
            self._send(503, _INJECTED_FAILURE)
            return
        if not isinstance(req.get("text_1"), str) or not isinstance(documents, list):
            self._send(400, {"error": {"message": "'text_1' and 'text_2' are required"}})
            return
        self._send(
            200,
            {
                "id": "score-mock",
                "object": "list",
                "model": req.get("model", "mock-rerank"),
                "data": [
                    {"index": i, "object": "score", "score": score}
                    for i, score in enumerate(_rerank_scores(len(documents)))
                ],
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
        )

    def _rerank(self) -> None:
        req = self._read_json()
        if req is None:
            return
        documents = req.get("documents")
        _count_rerank("rerank", req.get("query"), documents)
        if RERANK_FAULT == "unavailable":
            self._send(503, _INJECTED_FAILURE)
            return
        if not isinstance(req.get("query"), str) or not isinstance(documents, list):
            self._send(400, {"error": {"message": "'query' and 'documents' are required"}})
            return
        self._send(
            200,
            {
                "id": "rerank-mock",
                "model": req.get("model", "mock-rerank"),
                "results": [
                    {"index": i, "relevance_score": score}
                    for i, score in enumerate(_rerank_scores(len(documents)))
                ],
                "meta": {"billed_units": {"search_units": 1}},
            },
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
        if EMBED_FAULT == "upstream":
            self._send(503, _INJECTED_FAILURE)
            return
        if EMBED_FAULT == "malformed":
            self._send(200, {"data": []})
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
                    {"object": "embedding", "index": i,
                     "embedding": _embed(text)[:-1] if EMBED_FAULT == "dimension" else _embed(text)}
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
