#!/usr/bin/env python3
"""Deterministic OpenAI-compatible embeddings stand-in for CI rehearsal (CI only).

The air-gap deploy path hard-requires a vLLM-shaped embeddings endpoint
(EMBED_BASE_URL), and the airgap scripts refuse EMBED_MODE=hash by design.
The rehearsal therefore runs this mock as the stand-in for the owning team's
in-cluster vLLM: same API surface (/v1/embeddings, /healthz), deterministic
blake2b vectors of DENSE_DIM size — enough to prove URL/dim/env plumbing and
the fail-fast wiring end to end. Never a product path; never in the air gap.

    MOCK_DIM=64 PORT=8000 python3 scripts/mock_vllm.py
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = int(os.environ.get("MOCK_DIM", "64"))
PORT = int(os.environ.get("PORT", "8000"))


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


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict | str) -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        if self.path in ("/healthz", "/health"):
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": {"message": f"unknown path {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        if not self.path.endswith("/embeddings"):
            self._send(404, {"error": {"message": f"unknown path {self.path}"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"error": {"message": "invalid JSON body"}})
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
