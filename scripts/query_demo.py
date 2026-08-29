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
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mainframe_rag.config import load_settings
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
    parsed: dict[str, Any],
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
        parsed.get("answer", "").strip(),
    ]
    if parsed.get("script"):
        lines.extend([
            "",
            "------------------------------------------------------------",
            "EXTRACTED SCRIPT / CODE:",
            "------------------------------------------------------------",
            parsed["script"].strip(),
        ])
    lines.extend([
        "",
        "------------------------------------------------------------",
        f"VALIDATED CITATIONS ({len(parsed.get('citations', []))}):",
        "------------------------------------------------------------",
    ])
    if not parsed.get("citations"):
        lines.append("  (No direct citation lines verified in response)")
    else:
        for c in parsed["citations"]:
            lines.append(f"  * {c}")
    lines.append("============================================================")
    return "\n".join(lines) + "\n"


def render_answer_html(
    query: str,
    kind: str,
    parsed: dict[str, Any],
    hits: list[SearchHit],
    timings: dict[str, int],
) -> str:
    total_ms = timings.get("embed_ms", 0) + timings.get("qdrant_ms", 0)

    citations_html = "".join(f"<li>{html.escape(c)}</li>" for c in parsed.get("citations", []))
    if not citations_html:
        citations_html = "<li><em>No direct citations validated</em></li>"

    script_html = ""
    if parsed.get("script"):
        script_html = f"""
        <div class="hit-card">
          <div class="hit-header">
            <span class="hit-title">Extracted Script / Code</span>
          </div>
          <pre class="hit-text">{html.escape(parsed["script"])}</pre>
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
      <div style="white-space: pre-wrap; line-height: 1.6;">{html.escape(parsed.get("answer", ""))}</div>
      <hr style="border: none; border-top: 1px solid var(--border); margin: 1.5rem 0;" />
      <h3>Validated Citations</h3>
      <ul>{citations_html}</ul>
    </div>
    {script_html}
    <h3>Retrieved Source Excerpts</h3>
    {sources_html}
  </div>
</body>
</html>
"""


def execute_query(
    query: str,
    limit: int = 5,
    product: str | None = None,
    version: str | None = None,
    collection: str | None = None,
) -> tuple[list[SearchHit], str, dict[str, int]]:
    from qdrant_client import QdrantClient

    settings = load_settings()
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
    )
    return hits, kind, timings


def execute_answer(
    query: str,
    limit: int = 3,
    product: str | None = None,
    version: str | None = None,
    collection: str | None = None,
) -> tuple[dict[str, Any], list[SearchHit], str, dict[str, int]]:
    from mainframe_rag.agent.answer import HttpxLLMClient, build_messages, parse_answer

    settings = load_settings()
    hits, kind, timings = execute_query(
        query, limit=limit, product=product, version=version, collection=collection
    )
    if not hits:
        return {
            "answer": "No relevant manual excerpts found in the collection.",
            "citations": [],
            "script": None,
        }, hits, kind, timings

    messages = build_messages(
        query=query,
        hits=hits,
        product=product,
        version=version,
    )
    client = HttpxLLMClient(settings)
    try:
        reply = client.chat(messages)
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
                    raw, limit=limit, product=product, version=version, collection=collection
                )
                print(render_answer_text(raw, kind, parsed, hits, timings))
            else:
                hits, kind, timings = execute_query(
                    raw, limit=limit, product=product, version=version, collection=collection
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
    parser.add_argument("--format", choices=["text", "json", "html"], default="text", help="Output format")
    parser.add_argument("--out", type=Path, default=None, help="Write output to file")

    args = parser.parse_args(argv)

    default_limit = 3 if args.answer else 5
    limit = args.limit or default_limit

    if args.query is None:
        repl_loop(
            limit=limit,
            product=args.product,
            version=args.version,
            collection=args.collection,
            answer_mode=args.answer,
        )
        return 0

    if args.answer:
        parsed, hits, kind, timings = execute_answer(
            args.query,
            limit=limit,
            product=args.product,
            version=args.version,
            collection=args.collection,
        )
        if args.format == "json":
            output = json.dumps({
                "query": args.query,
                "kind": kind,
                "timings": timings,
                "answer": parsed.get("answer"),
                "script": parsed.get("script"),
                "citations": parsed.get("citations"),
                "hits": [asdict(h) for h in hits],
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
        )

        if args.format == "json":
            output = json.dumps({
                "query": args.query,
                "kind": kind,
                "timings": timings,
                "hits": [asdict(h) for h in hits],
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
