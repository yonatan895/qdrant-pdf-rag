#!/usr/bin/env python3
"""Interactive CLI query tool and demo viewer for Mainframe RAG.

Allows interactive query debugging, filtering, timing breakdown inspection,
and exporting results to terminal tables, JSON, or self-contained HTML.

Usage:
    # Single query
    python scripts/query_demo.py --query "IEA500I operator message" --limit 5

    # Interactive mode (REPL)
    python scripts/query_demo.py

    # Export to HTML
    python scripts/query_demo.py --query "DFSORT tuning" --format html --out query.html
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mainframe_rag.agent.answer import ParsedAnswer
from mainframe_rag.config import Settings, load_settings
from mainframe_rag.ingest.embed import build_embedder
from mainframe_rag.retrieve.query import SearchHit
from mainframe_rag.retrieve.query import search as retrieve_search


def _format_text_hit(rank: int, hit: SearchHit) -> str:
    lines = [
        f"  #{rank} [Score: {hit.score:.4f}] {hit.cite}",
        f"     Doc ID: {hit.doc_id} | Type: {hit.chunk_type} | Page: {hit.page_label}",
    ]
    if hit.message_ids:
        lines.append(f"     Messages: {', '.join(hit.message_ids)}")
    if hit.product or hit.version:
        lines.append(f"     Product: {hit.product or 'unknown'} {hit.version or ''}".rstrip())
    text_preview = "\n".join("     | " + line for line in hit.text.strip().splitlines()[:8])
    if len(hit.text.strip().splitlines()) > 8:
        text_preview += "\n     | ... [truncated]"
    lines.append(text_preview)
    return "\n".join(lines)


def render_query_text(query: str, kind: str, hits: list[SearchHit], timings: dict[str, int]) -> str:
    total_ms = timings.get("embed_ms", 0) + timings.get("qdrant_ms", 0)
    lines = [
        "============================================================",
        f" QUERY: {query}",
        "============================================================",
        f"Classification : [{kind.upper()}]",
        f"Timings        : Embed: {timings.get('embed_ms', 0)}ms | Qdrant: {timings.get('qdrant_ms', 0)}ms | Total: {total_ms}ms",
        f"Hits Found     : {len(hits)}",
        "------------------------------------------------------------",
    ]
    if not hits:
        lines.append("  (No relevant chunks found)")
    else:
        for i, hit in enumerate(hits, 1):
            lines.append(_format_text_hit(i, hit))
            lines.append("------------------------------------------------------------")
    lines.append("============================================================")
    return "\n".join(lines) + "\n"


def render_query_html(query: str, kind: str, hits: list[SearchHit], timings: dict[str, int]) -> str:
    total_ms = timings.get("embed_ms", 0) + timings.get("qdrant_ms", 0)

    cards_html = ""
    for i, h in enumerate(hits, 1):
        msgs = "".join(f'<span class="tag">{html.escape(m)}</span>' for m in h.message_ids)
        cards_html += f"""
        <div class="hit-card">
          <div class="hit-header">
            <span class="hit-rank">#{i}</span>
            <span class="hit-title">{html.escape(h.title or h.doc_id)}</span>
            <span class="score-pill">Score: {h.score:.4f}</span>
          </div>
          <div class="hit-cite"><strong>Citation:</strong> {html.escape(h.cite)}</div>
          <div class="meta-row">
            <span class="meta-item"><strong>Doc ID:</strong> {html.escape(h.doc_id)}</span>
            <span class="meta-item"><strong>Heading:</strong> {html.escape(h.heading)}</span>
            <span class="meta-item"><strong>Page:</strong> {html.escape(h.page_label)}</span>
            <span class="meta-item"><strong>Type:</strong> {html.escape(h.chunk_type)}</span>
            {f'<span class="meta-item"><strong>Messages:</strong> {msgs}</span>' if msgs else ''}
          </div>
          <pre class="hit-text">{html.escape(h.text)}</pre>
        </div>
        """

    if not hits:
        cards_html = '<div class="hit-card"><p><em>No matching chunks found.</em></p></div>'

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Query Inspection: {html.escape(query)}</title>
  <style>
    :root {{
      --bg: #0f172a;
      --surface: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --success: #4ade80;
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        --bg: #f8fafc;
        --surface: #ffffff;
        --border: #e2e8f0;
        --text: #0f172a;
        --text-muted: #64748b;
        --accent: #0284c7;
        --success: #16a34a;
      }}
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg); color: var(--text); margin: 0; padding: 2rem; line-height: 1.5;
    }}
    .container {{ max-width: 1000px; margin: 0 auto; }}
    .header {{ margin-bottom: 2rem; border-bottom: 1px solid var(--border); padding-bottom: 1rem; }}
    .meta-tag {{ display: inline-block; background: var(--surface); border: 1px solid var(--border); padding: 0.3rem 0.7rem; border-radius: 6px; font-size: 0.85rem; margin-right: 0.5rem; color: var(--text-muted); }}
    .hit-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-bottom: 1.5rem; }}
    .hit-header {{ display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.5rem; }}
    .hit-rank {{ font-size: 1.2rem; font-weight: 700; color: var(--accent); }}
    .hit-title {{ font-size: 1.1rem; font-weight: 600; flex-grow: 1; }}
    .score-pill {{ background: rgba(56, 189, 248, 0.15); color: var(--accent); padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.85rem; font-weight: 600; }}
    .hit-cite {{ font-size: 0.95rem; margin-bottom: 0.5rem; color: var(--text); }}
    .meta-row {{ display: flex; flex-wrap: wrap; gap: 0.75rem; font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.75rem; }}
    .tag {{ background: var(--border); color: var(--text); padding: 0.1rem 0.4rem; border-radius: 3px; font-size: 0.8rem; }}
    .hit-text {{ background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 0.75rem; font-family: "SFMono-Regular", Consolas, monospace; font-size: 0.85rem; overflow-x: auto; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Query Inspection Demo</h1>
      <h2>"{html.escape(query)}"</h2>
      <span class="meta-tag">Kind: <strong>{html.escape(kind.upper())}</strong></span>
      <span class="meta-tag">Hits: {len(hits)}</span>
      <span class="meta-tag">Embed: {timings.get('embed_ms', 0)}ms</span>
      <span class="meta-tag">Qdrant: {timings.get('qdrant_ms', 0)}ms</span>
      <span class="meta-tag">Total: {total_ms}ms</span>
    </div>
    {cards_html}
  </div>
</body>
</html>
"""


def render_answer_text(
    query: str,
    kind: str,
    parsed: ParsedAnswer,
    hits: list[SearchHit],
    timings: dict[str, int],
) -> str:
    total_ms = timings.get("embed_ms", 0) + timings.get("qdrant_ms", 0)
    lines = [
        "============================================================",
        f" QUESTION: {query}",
        "============================================================",
        f"Classification : [{kind.upper()}]",
        f"Timings        : Embed: {timings.get('embed_ms', 0)}ms | Qdrant: {timings.get('qdrant_ms', 0)}ms | Total: {total_ms}ms",
        f"Excerpts Used  : {len(hits)}",
        "------------------------------------------------------------",
        "MODEL REASONING ANSWER:",
        "------------------------------------------------------------",
        parsed.answer.strip(),
    ]
    if parsed.script:
        lines.extend([
            "",
            "------------------------------------------------------------",
            "EXTRACTED SCRIPT / CODE:",
            "------------------------------------------------------------",
            parsed.script.strip(),
        ])
    cites = parsed.citations
    inferred = parsed.citations_inferred
    inferred_indices = parsed.inferred_indices
    if inferred:
        cite_status = f" [inferred from excerpt {'[' + ', '.join(map(str, inferred_indices)) + ']'}]"
    elif cites:
        cite_status = " [explicit Citations: section]"
    else:
        cite_status = ""

    lines.extend([
        "",
        "------------------------------------------------------------",
        f"VALIDATED CITATIONS ({len(cites)}){cite_status}:",
        "------------------------------------------------------------",
    ])
    if not cites:
        lines.append("  (No direct citation lines verified in response)")
    else:
        for c in cites:
            lines.append(f"  * {c}")
    lines.append("============================================================")
    return "\n".join(lines) + "\n"


def render_answer_html(
    query: str,
    kind: str,
    parsed: ParsedAnswer,
    hits: list[SearchHit],
    timings: dict[str, int],
) -> str:
    total_ms = timings.get("embed_ms", 0) + timings.get("qdrant_ms", 0)

    citations_html = "".join(f"<li>{html.escape(c)}</li>" for c in parsed.citations)
    if not citations_html:
        citations_html = "<li><em>No direct citations validated</em></li>"

    script_html = ""
    if parsed.script:
        script_html = f"""
        <div class="hit-card">
          <div class="hit-header">
            <span class="hit-title">Extracted Script / Code</span>
          </div>
          <pre class="hit-text">{html.escape(parsed.script)}</pre>
        </div>
        """

    sources_html = ""
    for i, h in enumerate(hits, 1):
        sources_html += f"""
        <div class="hit-card">
          <div class="hit-header">
            <span class="hit-rank">#{i}</span>
            <span class="hit-title">{html.escape(h.title or h.doc_id)}</span>
            <span class="score-pill">Score: {h.score:.4f}</span>
          </div>
          <div class="hit-cite"><strong>Citation:</strong> {html.escape(h.cite)}</div>
          <pre class="hit-text">{html.escape(h.text)}</pre>
        </div>
        """

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>RAG Answer: {html.escape(query)}</title>
  <style>
    :root {{
      --bg: #0f172a;
      --surface: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --success: #4ade80;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg); color: var(--text); margin: 0; padding: 2rem; line-height: 1.5;
    }}
    .container {{ max-width: 1000px; margin: 0 auto; }}
    .header {{ margin-bottom: 2rem; border-bottom: 1px solid var(--border); padding-bottom: 1rem; }}
    .meta-tag {{ display: inline-block; background: var(--surface); border: 1px solid var(--border); padding: 0.3rem 0.7rem; border-radius: 6px; font-size: 0.85rem; margin-right: 0.5rem; color: var(--text-muted); }}
    .answer-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }}
    .hit-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-bottom: 1.5rem; }}
    .hit-header {{ display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.5rem; }}
    .hit-rank {{ font-size: 1.2rem; font-weight: 700; color: var(--accent); }}
    .hit-title {{ font-size: 1.1rem; font-weight: 600; flex-grow: 1; }}
    .score-pill {{ background: rgba(56, 189, 248, 0.15); color: var(--accent); padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.85rem; font-weight: 600; }}
    .hit-cite {{ font-size: 0.95rem; margin-bottom: 0.5rem; color: var(--text); }}
    .hit-text {{ background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 0.75rem; font-family: "SFMono-Regular", Consolas, monospace; font-size: 0.85rem; overflow-x: auto; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Mainframe RAG Answer</h1>
      <h2>"{html.escape(query)}"</h2>
      <span class="meta-tag">Kind: <strong>{html.escape(kind.upper())}</strong></span>
      <span class="meta-tag">Excerpts: {len(hits)}</span>
      <span class="meta-tag">Embed: {timings.get('embed_ms', 0)}ms</span>
      <span class="meta-tag">Qdrant: {timings.get('qdrant_ms', 0)}ms</span>
      <span class="meta-tag">Total: {total_ms}ms</span>
    </div>
    <div class="answer-card">
      <h3>Reasoning Answer</h3>
      <div style="white-space: pre-wrap; line-height: 1.6;">{html.escape(parsed.answer)}</div>
      <hr style="border: none; border-top: 1px solid var(--border); margin: 1.5rem 0;" />
      <h3>Validated Citations <span style="font-size: 0.85rem; font-weight: normal; color: var(--text-muted);">{"(Inferred from bracketed indices)" if parsed.citations_inferred else "(Explicit Citations: section)"}</span></h3>
      <ul>{citations_html}</ul>
    </div>
    {script_html}
    <h3>Retrieved Source Excerpts</h3>
    {sources_html}
  </div>
</body>
</html>
"""


def resolve_runtime_settings(
    collection: str | None = None,
    embed_url: str | None = None,
    embed_model: str | None = None,
    embed_mode: str | None = None,
    dense_dim: int | None = None,
    vllm_url: str | None = None,
    model: str | None = None,
) -> Settings:
    settings = load_settings()
    updates: dict[str, Any] = {}

    if collection:
        updates["qdrant_collection"] = collection

    # 1. Resolve embedding server, model & dimension
    target_embed_mode = embed_mode or (
        os.environ.get("EMBED_MODE") if "EMBED_MODE" in os.environ else None
    )
    target_embed_url = (
        embed_url
        or os.environ.get("EMBED_BASE_URL")
        or settings.embed_base_url
        or "http://localhost:8001/v1"
    )

    if embed_model:
        # Caller-supplied explicit embedding model is authoritative
        updates["embed_model"] = embed_model

    if dense_dim:
        updates["dense_dim"] = dense_dim

    if target_embed_mode == "vllm" or (
        target_embed_mode is None
        and "EMBED_MODE" not in os.environ
        and "EMBED_BASE_URL" not in os.environ
    ):
        try:
            m_resp = httpx2.get(f"{target_embed_url.rstrip('/')}/models", timeout=1.5)
            if m_resp.status_code == 200:
                raw_json = m_resp.json()
                avail = [
                    m.get("id")
                    for m in raw_json.get("data", [])
                    if isinstance(m, dict) and m.get("id")
                ]
                cur = embed_model or settings.embed_model
                chosen_embed_model = None
                if cur and cur in avail:
                    chosen_embed_model = cur
                elif cur:
                    for m in avail:
                        if m.endswith(cur) or cur.endswith(m) or m.split("/")[-1] == cur.split("/")[-1]:
                            chosen_embed_model = m
                            break
                    else:
                        if embed_model:
                            chosen_embed_model = embed_model
                        elif len(avail) == 1:
                            chosen_embed_model = avail[0]
                elif len(avail) == 1:
                    chosen_embed_model = avail[0]

                resolved_dim = dense_dim or settings.dense_dim
                if resolved_dim is None and chosen_embed_model:
                    try:
                        p_resp = httpx2.post(
                            f"{target_embed_url.rstrip('/')}/embeddings",
                            json={"model": chosen_embed_model, "input": "probe"},
                            timeout=3.0,
                        )
                        if p_resp.status_code == 200:
                            p_json = p_resp.json()
                            data = p_json.get("data", [])
                            if data and isinstance(data, list) and isinstance(data[0], dict) and "embedding" in data[0]:
                                resolved_dim = len(data[0]["embedding"])
                    except (httpx2.HTTPError, OSError, ValueError, KeyError, TypeError):
                        pass

                updates["embed_mode"] = "vllm"
                updates["embed_base_url"] = target_embed_url
                if chosen_embed_model:
                    updates["embed_model"] = chosen_embed_model
                if resolved_dim:
                    updates["dense_dim"] = resolved_dim
            elif target_embed_mode == "vllm":
                updates["embed_mode"] = "vllm"
                updates["embed_base_url"] = target_embed_url
            else:
                updates["embed_mode"] = "hash"
                updates["allow_hash_mode"] = True
        except (httpx2.HTTPError, OSError, ValueError, KeyError, TypeError):
            if target_embed_mode == "vllm":
                updates["embed_mode"] = "vllm"
                updates["embed_base_url"] = target_embed_url
            else:
                updates["embed_mode"] = "hash"
                updates["allow_hash_mode"] = True
    elif target_embed_mode == "hash":
        updates["embed_mode"] = "hash"
        updates["allow_hash_mode"] = True
    elif target_embed_mode:
        updates["embed_mode"] = target_embed_mode
        if embed_url:
            updates["embed_base_url"] = embed_url

    # 2. Resolve LLM reasoning server & model
    target_llm_url = (
        vllm_url
        or os.environ.get("LLM_BASE_URL")
        or settings.llm_base_url
        or "http://localhost:8000/v1"
    )
    if model:
        # Caller-supplied explicit reasoning model is authoritative
        updates["llm_model_reasoning"] = model

    if vllm_url:
        updates["llm_base_url"] = vllm_url

    try:
        m_resp = httpx2.get(f"{target_llm_url.rstrip('/')}/models", timeout=1.5)
        if m_resp.status_code == 200:
            raw_json = m_resp.json()
            avail = [
                m.get("id")
                for m in raw_json.get("data", [])
                if isinstance(m, dict) and m.get("id")
            ]
            cur = model or settings.llm_model_reasoning
            chosen_llm_model = None
            if cur and cur in avail:
                chosen_llm_model = cur
            elif cur:
                for m in avail:
                    if m.endswith(cur) or cur.endswith(m) or m.split("/")[-1] == cur.split("/")[-1]:
                        chosen_llm_model = m
                        break
                else:
                    if model:
                        chosen_llm_model = model
                    elif len(avail) == 1:
                        chosen_llm_model = avail[0]
            elif len(avail) == 1:
                chosen_llm_model = avail[0]

            updates["llm_base_url"] = target_llm_url
            if chosen_llm_model:
                updates["llm_model_reasoning"] = chosen_llm_model
    except (httpx2.HTTPError, OSError, ValueError, KeyError, TypeError):
        pass

    if updates:
        settings = settings.model_copy(update=updates)
    return settings


def execute_query(
    query: str,
    limit: int = 5,
    product: str | None = None,
    version: str | None = None,
    collection: str | None = None,
    settings: Settings | None = None,
) -> tuple[list[SearchHit], str, dict[str, int]]:
    from qdrant_client import QdrantClient

    if settings is None:
        settings = resolve_runtime_settings(collection=collection)
    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_s,
    )
    embedder = build_embedder(settings)
    target_coll = collection or settings.qdrant_collection
    hits, kind, timings = retrieve_search(
        client, embedder, target_coll, query,
        product=product, version=version, limit=limit,
        settings=settings,
    )
    return hits, kind, timings


def execute_answer(
    query: str,
    limit: int = 3,
    product: str | None = None,
    version: str | None = None,
    collection: str | None = None,
    settings: Settings | None = None,
) -> tuple[dict[str, Any], list[SearchHit], str, dict[str, int]]:
    from mainframe_rag.agent.answer import (
        HttpxLLMClient,
        build_messages,
        classify_query_complexity,
        parse_answer,
    )

    if settings is None:
        settings = resolve_runtime_settings(collection=collection)

    hits, kind, timings = execute_query(
        query, limit=limit, product=product, version=version, collection=collection, settings=settings
    )
    if not hits:
        return {
            "answer": "No relevant manual excerpts found in the collection.",
            "citations": [],
            "script": None,
        }, hits, kind, timings

    complexity = classify_query_complexity(query)
    max_context = (
        settings.prompt_max_context_chars_complex
        if complexity == "complex"
        else settings.prompt_max_context_chars
    )
    max_chunk = (
        settings.prompt_max_chunk_chars_complex
        if complexity == "complex"
        else settings.prompt_max_chunk_chars
    )
    effort = (
        settings.llm_reasoning_effort_complex
        if complexity == "complex"
        else settings.llm_reasoning_effort_simple
    )

    messages = build_messages(
        query=query,
        hits=hits,
        product=product,
        version=version,
        max_context_chars=max_context,
        max_chunk_chars=max_chunk,
        complexity=complexity,
    )
    client = HttpxLLMClient(settings)
    try:
        reply = client.chat(
            messages,
            reasoning_effort=effort,
            temperature=settings.llm_temperature,
        )
    finally:
        client.close()

    allowed_citations = {h.cite for h in hits}
    parsed = parse_answer(reply, allowed_citations, ordered_cites=[h.cite for h in hits])
    return parsed, hits, kind, timings


def repl_loop(
    limit: int = 5,
    product: str | None = None,
    version: str | None = None,
    collection: str | None = None,
    answer_mode: bool = False,
    settings: Settings | None = None,
) -> None:
    print("============================================================")
    print(" MAINFRAME RAG: INTERACTIVE REPL")
    print(f" Mode: {'[ANSWER - Reasoning LLM + Citations]' if answer_mode else '[SEARCH - Retrieval Chunks & Scores]'}")
    print(" Commands:")
    print("   :mode       Toggle between SEARCH and ANSWER modes")
    print("   :limit <N>  Set chunk limit")
    print("   exit / quit Exit the REPL")
    print("============================================================\n")
    while True:
        try:
            prompt_str = "rag-answer> " if answer_mode else "rag-search> "
            raw = input(prompt_str).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break
        if not raw:
            continue
        if raw.lower() in ("exit", "quit", "q"):
            print("Exiting.")
            break
        if raw.startswith(":mode"):
            answer_mode = not answer_mode
            print(f"Switched mode to {'[ANSWER]' if answer_mode else '[SEARCH]'}\n")
            continue
        if raw.startswith(":limit "):
            try:
                new_limit = int(raw.split()[1])
                if new_limit < 1:
                    print("Error: limit must be a positive integer (>= 1)")
                else:
                    limit = new_limit
                    print(f"Set limit to {limit}\n")
            except (IndexError, ValueError):
                print("Usage: :limit <positive-int>")
            continue

        try:
            if answer_mode:
                print("[*] Retrieving from Qdrant and calling reasoning model...")
                parsed, hits, kind, timings = execute_answer(
                    raw, limit=limit, product=product, version=version, collection=collection, settings=settings
                )
                print(render_answer_text(raw, kind, parsed, hits, timings))
            else:
                hits, kind, timings = execute_query(
                    raw, limit=limit, product=product, version=version, collection=collection, settings=settings
                )
                print(render_query_text(raw, kind, hits, timings))
        except Exception as exc:  # noqa: BLE001
            print(f"Error executing query: {exc}", file=sys.stderr)


def _positive_int(val: str) -> int:
    try:
        ival = int(val)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid integer value: {val!r}") from exc
    if ival < 1:
        raise argparse.ArgumentTypeError(f"Limit must be a positive integer (>= 1), got {ival}")
    return ival


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", "-q", default=None, help="Query string to search (default: interactive REPL)")
    parser.add_argument("--answer", "-a", action="store_true", help="Generate reasoning answer with citations using LLM")
    parser.add_argument("--limit", "-k", type=_positive_int, default=None, help="Number of results to retrieve (default: 5 for search, 3 for answer)")
    parser.add_argument("--product", default=None, help="Optional product filter (e.g. 'z/OS')")
    parser.add_argument("--version", default=None, help="Optional version filter (e.g. '3.2')")
    parser.add_argument("--collection", default=None, help="Qdrant collection override")
    parser.add_argument("--embed-url", default=None, help="Embedding server URL (e.g. http://localhost:8001/v1)")
    parser.add_argument("--embed-model", default=None, help="Embedding model name (e.g. Qwen3-Embedding-0.6B)")
    parser.add_argument("--embed-mode", choices=["vllm", "hash"], default=None, help="Embedding mode ('vllm' or 'hash')")
    parser.add_argument("--dense-dim", type=int, default=None, help="Dense vector dimension override")
    parser.add_argument("--vllm-url", default=None, help="LLM reasoning server URL (e.g. http://localhost:8000/v1)")
    parser.add_argument("--model", default=None, help="LLM reasoning model name override")
    parser.add_argument("--format", choices=["text", "json", "html"], default="text", help="Output format")
    parser.add_argument("--out", type=Path, default=None, help="Write output to file")

    args = parser.parse_args(argv)

    settings = resolve_runtime_settings(
        collection=args.collection,
        embed_url=args.embed_url,
        embed_model=args.embed_model,
        embed_mode=args.embed_mode,
        dense_dim=args.dense_dim,
        vllm_url=args.vllm_url,
        model=args.model,
    )

    default_limit = 3 if args.answer else 5
    limit = args.limit or default_limit

    if args.query is None:
        repl_loop(
            limit=limit,
            product=args.product,
            version=args.version,
            collection=args.collection,
            answer_mode=args.answer,
            settings=settings,
        )
        return 0

    if args.answer:
        parsed, hits, kind, timings = execute_answer(
            args.query,
            limit=limit,
            product=args.product,
            version=args.version,
            collection=args.collection,
            settings=settings,
        )
        if args.format == "json":
            output = json.dumps({
                "query": args.query,
                "kind": kind,
                "timings": timings,
                "answer": parsed.answer,
                "script": parsed.script,
                "citations": parsed.citations,
                "citations_inferred": parsed.citations_inferred,
                "inferred_indices": parsed.inferred_indices,
                "hits": [h.model_dump() for h in hits],
            }, indent=2)
        elif args.format == "html":
            output = render_answer_html(args.query, kind, parsed, hits, timings)
        else:
            output = render_answer_text(args.query, kind, parsed, hits, timings)
    else:
        hits, kind, timings = execute_query(
            args.query,
            limit=limit,
            product=args.product,
            version=args.version,
            collection=args.collection,
            settings=settings,
        )

        if args.format == "json":
            output = json.dumps({
                "query": args.query,
                "kind": kind,
                "timings": timings,
                "hits": [h.model_dump() for h in hits],
            }, indent=2)
        elif args.format == "html":
            output = render_query_html(args.query, kind, hits, timings)
        else:
            output = render_query_text(args.query, kind, hits, timings)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
        print(f"Output written to {args.out}")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
