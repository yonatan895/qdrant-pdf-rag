#!/usr/bin/env python3
"""Gateway readiness probe for the LiteLLM cutover.

Reads Settings from the environment (the same keys the agent and ingest
use, including the `*_API_KEY` knobs) and exercises each configured model
leg directly: embed (`/models` + `/embeddings` dimension check), reasoning
(`/models` + `/chat/completions`, streaming optional), rerank (`/v1/score`
vs `/v1/rerank` reachability — prints the recommended
`RERANK_ENDPOINT_ORDER`), tokenizer (`/tokenize` presence; absent behind a
gateway is expected — the agent pins its in-process estimator).

Response shapes mirror the client contracts they diagnose (`embed.py`,
`answer.py`, `rerank.py`, `tokenizer.py` own the parsing; this script only
asks "does the leg answer usably"). 401/403 answers name the missing
virtual-key knob explicitly.

Exit 0 when every required leg answers (embed always; reasoning when
`LLM_MODEL_REASONING` is set; rerank when `RERANK_ENABLED=true`); exit 1
otherwise. Run from inside the cluster, on the same network as the agent:

    kubectl -n mainframe-rag exec deploy/rag-agent -- \
        python3 /app/scripts/probe_gateway.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mainframe_rag.config import Settings, bearer_auth_headers, load_settings

PROBE_TEXT = "gateway readiness probe"
PROBE_TIMEOUT_S = 30.0


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


def check_chat(settings: Settings, timeout: float, stream: bool) -> tuple[str, str]:
    """POST /chat/completions (+ optional SSE [DONE] check). Skipped when no
    reasoning model is configured — answers stay disabled by design then."""
    if not settings.llm_base_url or not settings.llm_model_reasoning:
        return "skip", "reasoning not configured (/v1/answer stays disabled)"
    base_url, model = settings.llm_base_url, settings.llm_model_reasoning
    headers = bearer_auth_headers(settings.llm_api_key)
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "temperature": 0,
        "max_tokens": 16,
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
    if not choices:
        return "fail", "chat /chat/completions returned empty choices"
    detail = f"chat /chat/completions: model={model} finish={choices[0].get('finish_reason')}"
    if not stream:
        return "ok", detail
    try:
        saw_done = False
        with httpx2.stream(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            json={**body, "stream": True},
            timeout=timeout,
            headers=headers,
        ) as stream_resp:
            stream_resp.raise_for_status()
            for line in stream_resp.iter_lines():
                if line.strip() == "data: [DONE]":
                    saw_done = True
                    break
    except (httpx2.HTTPError, OSError) as exc:
        return "ok", detail + f"; streaming FAILED ({exc}) — JSON answers work, SSE TTFT will not"
    if not saw_done:
        return "ok", detail + "; streaming has no [DONE] — JSON answers work, SSE TTFT will not"
    return "ok", detail + "; streaming [DONE] ok"


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


def check_tokenize(settings: Settings, timeout: float) -> tuple[str, str]:
    """POST {origin}/tokenize. Informational only: a gateway without it is
    expected — the agent pins its in-process estimator after one warning."""
    if not settings.llm_base_url or not settings.llm_model_reasoning:
        return "skip", "tokenizer not configured (no reasoning model)"
    origin = settings.llm_base_url.rstrip("/").removesuffix("/v1")
    try:
        resp = httpx2.post(
            f"{origin}/tokenize",
            json={"model": settings.llm_model_reasoning, "prompt": PROBE_TEXT},
            timeout=timeout,
            headers=bearer_auth_headers(settings.llm_api_key),
        )
    except (httpx2.HTTPError, OSError):
        return "ok", "tokenize unreachable — estimator fallback (expected behind LiteLLM)"
    if resp.status_code != 200:
        return "ok", f"tokenize HTTP {resp.status_code} — estimator fallback (expected behind LiteLLM)"
    try:
        data = resp.json()
    except ValueError:
        return "ok", "tokenize non-JSON — estimator fallback (expected behind LiteLLM)"
    if isinstance(data, dict) and ("count" in data or isinstance(data.get("tokens"), list)):
        return "ok", "tokenize served"
    return "ok", "tokenize unusable shape — estimator fallback (expected behind LiteLLM)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe gateway model legs before cutover.")
    parser.add_argument("--timeout", type=float, default=PROBE_TIMEOUT_S)
    parser.add_argument("--stream", action="store_true", help="also verify SSE [DONE] on chat")
    parser.add_argument("--models", action="store_true", help="list served model ids and exit")
    args = parser.parse_args(argv)

    settings = load_settings()
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
    status, detail = check_chat(settings, args.timeout, args.stream)
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

    status, detail = check_tokenize(settings, args.timeout)
    print(f"[{status.upper()}] {detail}")

    if recommendation is not None:
        current = settings.rerank_endpoint_order
        print(f"recommendation: RERANK_ENDPOINT_ORDER={recommendation} (current: {current})")

    print("GATEWAY PROBE " + ("FAILED" if failures else "PASSED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
