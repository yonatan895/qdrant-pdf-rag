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
parse_answer's hit-set validation. Never a product path; never in the air gap.

    MOCK_DIM=64 PORT=8000 python3 scripts/mock_vllm.py
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = int(os.environ.get("MOCK_DIM", "64"))
PORT = int(os.environ.get("PORT", "8000"))

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
    def _send(self, code: int, payload: dict | str) -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
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
        self._send(
            200,
            {
                "id": "chatcmpl-mock-deterministic",
                "object": "chat.completion",
                "model": req.get("model", "mock-reasoning"),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": _chat_content(messages)},
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
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
