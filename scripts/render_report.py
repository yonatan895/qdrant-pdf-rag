#!/usr/bin/env python3
"""CLI report renderer and comparator for retrieval evaluation and benchmarks.

Supports rendering JSON reports into terminal text, markdown, and self-contained HTML.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------- helpers
def _load_json(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Report file not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _get(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _diff_badge(val: float | None, base: float | None, higher_is_better: bool = True) -> str:
    if val is None or base is None:
        return ""
    delta = val - base
    if abs(delta) < 1e-4:
        return "=0.0"
    sign = "+" if delta > 0 else ""
    return f"({sign}{round(delta, 3)})"


# ---------------------------------------------------------------- HTML Styles
BASE_HTML_STYLE = """
<style>
  :root {
    --bg: #0f172a;
    --surface: #1e293b;
    --surface-hover: #334155;
    --border: #334155;
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --accent: #38bdf8;
    --success: #4ade80;
    --warning: #facc15;
    --danger: #f87171;
    --badge-bg: #0f172a;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f8fafc;
      --surface: #ffffff;
      --surface-hover: #f1f5f9;
      --border: #e2e8f0;
      --text: #0f172a;
      --text-muted: #64748b;
      --accent: #0284c7;
      --success: #16a34a;
      --warning: #ca8a04;
      --danger: #dc2626;
      --badge-bg: #e2e8f0;
    }
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background-color: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 2rem;
    line-height: 1.5;
  }
  .container { max-width: 1100px; margin: 0 auto; }
  h1, h2, h3 { color: var(--text); margin-top: 0; }
  .header { margin-bottom: 2rem; border-bottom: 1px solid var(--border); padding-bottom: 1rem; }
  .meta-tag { display: inline-block; background: var(--surface); border: 1px solid var(--border); padding: 0.25rem 0.6rem; border-radius: 6px; font-size: 0.85rem; margin-right: 0.5rem; color: var(--text-muted); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; }
  .card-label { font-size: 0.85rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }
  .card-value { font-size: 1.8rem; font-weight: 700; margin: 0.25rem 0; color: var(--text); }
  .card-sub { font-size: 0.85rem; color: var(--text-muted); }
  table { width: 100%; border-collapse: collapse; margin-top: 1rem; background: var(--surface); border-radius: 8px; overflow: hidden; border: 1px solid var(--border); }
  th, td { padding: 0.75rem 1rem; text-align: left; border-bottom: 1px solid var(--border); }
  th { background-color: var(--surface-hover); font-weight: 600; font-size: 0.85rem; color: var(--text-muted); text-transform: uppercase; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background-color: var(--surface-hover); }
  .badge { display: inline-block; padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }
  .badge-pass { background: rgba(74, 222, 128, 0.15); color: var(--success); }
  .badge-fail { background: rgba(248, 113, 113, 0.15); color: var(--danger); }
  .badge-info { background: rgba(56, 189, 248, 0.15); color: var(--accent); }
  .badge-neutral { background: var(--badge-bg); color: var(--text-muted); }
  .delta-good { color: var(--success); font-weight: 600; font-size: 0.85rem; }
  .delta-bad { color: var(--danger); font-weight: 600; font-size: 0.85rem; }
  .delta-neutral { color: var(--text-muted); font-size: 0.85rem; }
  pre { background: var(--bg); padding: 0.5rem; border-radius: 4px; font-size: 0.85rem; overflow-x: auto; }
</style>
"""


# ---------------------------------------------------------------- Eval Rendering
def render_eval(report: dict[str, Any], baseline: dict[str, Any] | None, fmt: str) -> str:
    n = report.get("n", 0)
    failures = report.get("failures", 0)
    mode = report.get("embed_mode", "unknown")
    coll = report.get("collection", "unknown")
    elapsed = report.get("elapsed_s", 0.0)

    metrics = ["recall@1", "recall@3", "recall@5", "mrr"]

    if fmt == "html":
        cards_html = f"""
        <div class="grid">
          <div class="card">
            <div class="card-label">Recall @ 1</div>
            <div class="card-value">{report.get('recall@1', 0.0)}</div>
            <div class="card-sub">Baseline: {baseline.get('recall@1', 'n/a') if baseline else 'n/a'}</div>
          </div>
          <div class="card">
            <div class="card-label">Recall @ 5</div>
            <div class="card-value">{report.get('recall@5', 0.0)}</div>
            <div class="card-sub">Baseline: {baseline.get('recall@5', 'n/a') if baseline else 'n/a'}</div>
          </div>
          <div class="card">
            <div class="card-label">MRR</div>
            <div class="card-value">{report.get('mrr', 0.0)}</div>
            <div class="card-sub">Baseline: {baseline.get('mrr', 'n/a') if baseline else 'n/a'}</div>
          </div>
          <div class="card">
            <div class="card-label">Queries / Failures</div>
            <div class="card-value">{n} / <span style="color: {'var(--danger)' if failures else 'var(--success)'}">{failures}</span></div>
            <div class="card-sub">Elapsed: {elapsed}s | Mode: {html.escape(str(mode))}</div>
          </div>
        </div>
        """

        summary_rows = ""
        for m in metrics:
            cur = report.get(m)
            base = _get(baseline, m) if baseline else None
            ident = _get(report, f"identifier.{m}")
            nl = _get(report, f"nl.{m}")
            diff_str = ""
            if cur is not None and base is not None:
                d = cur - base
                cls = "delta-good" if d >= 0 else "delta-bad"
                sign = "+" if d > 0 else ""
                diff_str = f'<span class="{cls}">{sign}{d:.3f}</span>'
            summary_rows += f"""
            <tr>
              <td><strong>{m}</strong></td>
              <td>{cur}</td>
              <td>{ident if ident is not None else '-'}</td>
              <td>{nl if nl is not None else '-'}</td>
              <td>{base if base is not None else 'n/a'}</td>
              <td>{diff_str or '-'}</td>
            </tr>
            """

        query_rows = ""
        for row in report.get("rows", []):
            q_text = html.escape(row.get("query", ""))
            kind = row.get("kind", "")
            if "error" in row:
                status = '<span class="badge badge-fail">ERROR</span>'
                hits = f'<span style="color: var(--danger)">{html.escape(row["error"])}</span>'
                r1 = r5 = mrr = "-"
            else:
                status = '<span class="badge badge-pass">OK</span>' if row.get("recall@5", 0) > 0 else '<span class="badge badge-fail">MISS</span>'
                hits = ", ".join(html.escape(h) for h in row.get("hit_doc_ids", []))
                r1 = f"{row.get('recall@1', 0):.0f}"
                r5 = f"{row.get('recall@5', 0):.0f}"
                mrr = f"{row.get('mrr', 0):.2f}"

            query_rows += f"""
            <tr>
              <td>{q_text}</td>
              <td><span class="badge badge-neutral">{kind}</span></td>
              <td>{status}</td>
              <td>{r1}</td>
              <td>{r5}</td>
              <td>{mrr}</td>
              <td><code>{hits}</code></td>
            </tr>
            """

        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Retrieval Evaluation Report</title>
  {BASE_HTML_STYLE}
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Retrieval Accuracy Report</h1>
      <span class="meta-tag">Collection: {html.escape(str(coll))}</span>
      <span class="meta-tag">Embed Mode: {html.escape(str(mode))}</span>
      <span class="meta-tag">Queries: {n}</span>
      <span class="meta-tag">Duration: {elapsed}s</span>
    </div>
    {cards_html}
    <h2>Metric Overview</h2>
    <table>
      <thead>
        <tr><th>Metric</th><th>All</th><th>Identifier</th><th>Natural Language</th><th>Baseline</th><th>Delta</th></tr>
      </thead>
      <tbody>{summary_rows}</tbody>
    </table>
    <h2>Query Details</h2>
    <table>
      <thead>
        <tr><th>Query</th><th>Kind</th><th>Status</th><th>R@1</th><th>R@5</th><th>MRR</th><th>Top Hit Doc IDs</th></tr>
      </thead>
      <tbody>{query_rows}</tbody>
    </table>
  </div>
</body>
</html>
"""

    if fmt == "markdown":
        lines = [
            "## Retrieval Evaluation Report",
            "",
            f"**Collection:** `{coll}` | **Embed Mode:** `{mode}` | **Queries:** `{n}` | **Failures:** `{failures}` | **Elapsed:** `{elapsed}s`",
            "",
            "| Metric | All | Identifier | NL | Baseline | Delta |",
            "|---|---|---|---|---|---|",
        ]
        for m in metrics:
            cur = report.get(m)
            base = _get(baseline, m) if baseline else None
            ident = _get(report, f"identifier.{m}")
            nl = _get(report, f"nl.{m}")
            badge = _diff_badge(cur, base) if base is not None else "-"
            lines.append(f"| {m} | {cur} | {ident if ident is not None else '-'} | {nl if nl is not None else '-'} | {base if base is not None else '-'} | {badge} |")
        lines += [
            "",
            "| Query | Kind | R@1 | R@5 | MRR | Top Hit Doc IDs |",
            "|---|---|---|---|---|---|",
        ]
        for row in report.get("rows", []):
            if "error" in row:
                lines.append(f"| {row['query'][:60]} | error | - | - | - | {row['error'][:40]} |")
            else:
                lines.append(f"| {row['query'][:60]} | {row.get('kind', '')} | {row.get('recall@1', 0):.0f} | {row.get('recall@5', 0):.0f} | {row.get('mrr', 0):.2f} | {', '.join(row.get('hit_doc_ids', []))[:60]} |")
        return "\n".join(lines) + "\n"

    # Default text/terminal
    lines = [
        "============================================================",
        f" RETRIEVAL EVALUATION REPORT ({coll}, mode: {mode})",
        "============================================================",
        f"Queries: {n} | Failures: {failures} | Elapsed: {elapsed}s",
        "------------------------------------------------------------",
        f"{'Metric':<12} {'Current':<10} {'Identifier':<12} {'NL':<10} {'Baseline':<10} {'Delta':<10}",
        "------------------------------------------------------------",
    ]
    for m in metrics:
        cur = report.get(m, 0.0)
        base = _get(baseline, m) if baseline else None
        ident = _get(report, f"identifier.{m}")
        nl = _get(report, f"nl.{m}")
        badge = _diff_badge(cur, base) if base is not None else "-"
        lines.append(f"{m:<12} {cur!s:<10} {ident if ident is not None else '-'!s:<12} {nl if nl is not None else '-'!s:<10} {base if base is not None else '-'!s:<10} {badge:<10}")
    lines.append("============================================================")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- Bench Rendering
def render_bench(report: dict[str, Any], baseline: dict[str, Any] | None, fmt: str) -> str:
    env = report.get("env", {})
    ingest = report.get("ingest", {})
    qdrant = report.get("qdrant", {})
    agent = report.get("agent", {})
    search = agent.get("search", {})
    answer = agent.get("answer", {})

    if fmt == "html":
        cards = f"""
        <div class="grid">
          <div class="card">
            <div class="card-label">Search Throughput</div>
            <div class="card-value">{search.get('rps', 0.0):.1f} <span style="font-size:1rem; font-weight:normal;">req/s</span></div>
            <div class="card-sub">p50: {search.get('latency_ms', {}).get('p50', 0)}ms | p95: {search.get('latency_ms', {}).get('p95', 0)}ms</div>
          </div>
          <div class="card">
            <div class="card-label">Answer Throughput</div>
            <div class="card-value">{answer.get('rps', 0.0):.1f} <span style="font-size:1rem; font-weight:normal;">req/s</span></div>
            <div class="card-sub">p50: {answer.get('latency_ms', {}).get('p50', 0)}ms | p95: {answer.get('latency_ms', {}).get('p95', 0)}ms</div>
          </div>
          <div class="card">
            <div class="card-label">Ingest Speed</div>
            <div class="card-value">{ingest.get('docs_per_s', 0.0):.2f} <span style="font-size:1rem; font-weight:normal;">docs/s</span></div>
            <div class="card-sub">{ingest.get('docs', 0)} docs / {ingest.get('chunks', 0)} chunks in {ingest.get('wall_s', 0)}s</div>
          </div>
          <div class="card">
            <div class="card-label">Resources (RAM / Disk)</div>
            <div class="card-value">{qdrant.get('mem_mb', 0)} / {qdrant.get('disk_mb', 0)} <span style="font-size:1rem; font-weight:normal;">MB</span></div>
            <div class="card-sub">Ingest Peak RSS: {ingest.get('peak_rss_mb', 0)} MB</div>
          </div>
        </div>
        """

        table_rows = ""
        bench_metrics = [
            ("ingest.peak_rss_mb", "Ingest Peak RSS (MB)", False),
            ("qdrant.mem_mb", "Qdrant Container Memory (MB)", False),
            ("qdrant.disk_mb", "Qdrant Storage Disk (MB)", False),
            ("agent.search.latency_ms.p95", "Search Latency p95 (ms)", False),
            ("agent.answer.latency_ms.p95", "Answer Latency p95 (ms)", False),
            ("agent.search.rps", "Search Throughput (RPS)", True),
            ("agent.answer.rps", "Answer Throughput (RPS)", True),
        ]
        for dotted, label, higher_better in bench_metrics:
            cur = _get(report, dotted)
            base = _get(baseline, dotted) if baseline else None
            diff_str = "-"
            if cur is not None and base is not None:
                d = cur - base
                good = (d >= 0) if higher_better else (d <= 0)
                cls = "delta-good" if good else "delta-bad"
                sign = "+" if d > 0 else ""
                diff_str = f'<span class="{cls}">{sign}{d:.2f}</span>'
            table_rows += f"""
            <tr>
              <td><strong>{label}</strong></td>
              <td>{cur if cur is not None else '-'}</td>
              <td>{base if base is not None else 'n/a'}</td>
              <td>{diff_str}</td>
            </tr>
            """

        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Benchmark Performance Report</title>
  {BASE_HTML_STYLE}
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>System Performance & Benchmark Report</h1>
      <span class="meta-tag">CPUs: {env.get('cpu_count')}</span>
      <span class="meta-tag">System RAM: {env.get('mem_total_mb')} MB</span>
      <span class="meta-tag">Image: {html.escape(str(env.get('qdrant_image', '')))}</span>
    </div>
    {cards}
    <h2>Key Resource & Latency Metrics</h2>
    <table>
      <thead>
        <tr><th>Metric</th><th>Current</th><th>Baseline</th><th>Delta</th></tr>
      </thead>
      <tbody>{table_rows}</tbody>
    </table>
  </div>
</body>
</html>
"""

    if fmt == "markdown":
        lines = [
            "## Benchmark Results",
            "",
            f"**Environment:** `{env.get('cpu_count')} CPUs` | `{env.get('mem_total_mb')} MB RAM` | `{env.get('qdrant_image')}`",
            "",
            "| Metric | Current | Baseline | Delta |",
            "|---|---|---|---|",
        ]
        bench_metrics = [
            ("ingest.peak_rss_mb", "Ingest Peak RSS (MB)", False),
            ("qdrant.mem_mb", "Qdrant Memory (MB)", False),
            ("qdrant.disk_mb", "Qdrant Disk (MB)", False),
            ("agent.search.latency_ms.p95", "Search Latency p95 (ms)", False),
            ("agent.answer.latency_ms.p95", "Answer Latency p95 (ms)", False),
            ("agent.search.rps", "Search RPS", True),
            ("agent.answer.rps", "Answer RPS", True),
        ]
        for dotted, label, higher_better in bench_metrics:
            cur = _get(report, dotted)
            base = _get(baseline, dotted) if baseline else None
            badge = _diff_badge(cur, base, higher_better) if base is not None else "-"
            lines.append(f"| {label} | {cur} | {base if base is not None else '-'} | {badge} |")
        return "\n".join(lines) + "\n"

    # Default text
    lines = [
        "============================================================",
        " BENCHMARK PERFORMANCE REPORT",
        "============================================================",
        f"Search RPS: {search.get('rps', 0):.1f} | Answer RPS: {answer.get('rps', 0):.1f}",
        f"Search p95: {search.get('latency_ms', {}).get('p95', 0)}ms | Answer p95: {answer.get('latency_ms', {}).get('p95', 0)}ms",
        f"Ingest: {ingest.get('docs_per_s', 0):.2f} docs/s | Peak RSS: {ingest.get('peak_rss_mb', 0)} MB",
        f"Qdrant Mem: {qdrant.get('mem_mb', 0)} MB | Disk: {qdrant.get('disk_mb', 0)} MB",
        "============================================================",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- Compare Evaluations
def compare_eval(base: dict[str, Any], current: dict[str, Any], fmt: str) -> str:
    metrics = ["recall@1", "recall@3", "recall@5", "mrr"]
    base_rows = {r.get("query"): r for r in base.get("rows", []) if "query" in r}
    cur_rows = {r.get("query"): r for r in current.get("rows", []) if "query" in r}

    improved: list[str] = []
    regressed: list[str] = []
    unchanged: list[str] = []

    for q, cur_r in cur_rows.items():
        base_r = base_rows.get(q)
        if not base_r:
            continue
        cur_r5 = cur_r.get("recall@5", 0.0)
        base_r5 = base_r.get("recall@5", 0.0)
        cur_mrr = cur_r.get("mrr", 0.0)
        base_mrr = base_r.get("mrr", 0.0)
        if cur_r5 > base_r5 or cur_mrr > base_mrr:
            improved.append(q)
        elif cur_r5 < base_r5 or cur_mrr < base_mrr:
            regressed.append(q)
        else:
            unchanged.append(q)

    if fmt == "html":
        metric_rows = ""
        for m in metrics:
            b_val = base.get(m, 0.0)
            c_val = current.get(m, 0.0)
            d = c_val - b_val if (c_val is not None and b_val is not None) else 0.0
            cls = "delta-good" if d >= 0 else "delta-bad"
            sign = "+" if d > 0 else ""
            metric_rows += f"""
            <tr>
              <td><strong>{m}</strong></td>
              <td>{b_val}</td>
              <td>{c_val}</td>
              <td><span class="{cls}">{sign}{d:.3f}</span></td>
            </tr>
            """

        def format_q_list(queries: list[str], pill_cls: str, label: str) -> str:
            if not queries:
                return "<p><em>None</em></p>"
            items = "".join(f"<li><code>{html.escape(q)}</code></li>" for q in queries)
            return f'<ul style="margin: 0.5rem 0;">{items}</ul>'

        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Evaluation Comparison</title>
  {BASE_HTML_STYLE}
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Evaluation Comparison</h1>
      <span class="meta-tag">Base queries: {base.get('n')}</span>
      <span class="meta-tag">Current queries: {current.get('n')}</span>
    </div>
    <div class="grid">
      <div class="card">
        <div class="card-label">Improved Queries</div>
        <div class="card-value" style="color: var(--success)">{len(improved)}</div>
      </div>
      <div class="card">
        <div class="card-label">Regressed Queries</div>
        <div class="card-value" style="color: var(--danger)">{len(regressed)}</div>
      </div>
      <div class="card">
        <div class="card-label">Unchanged Queries</div>
        <div class="card-value">{len(unchanged)}</div>
      </div>
    </div>
    <h2>Metric Deltas</h2>
    <table>
      <thead><tr><th>Metric</th><th>Base</th><th>Current</th><th>Delta</th></tr></thead>
      <tbody>{metric_rows}</tbody>
    </table>
    <h2>Query Shift Analysis</h2>
    <div class="grid">
      <div class="card">
        <h3>Improved ({len(improved)})</h3>
        {format_q_list(improved, "badge-pass", "Improved")}
      </div>
      <div class="card">
        <h3>Regressed ({len(regressed)})</h3>
        {format_q_list(regressed, "badge-fail", "Regressed")}
      </div>
    </div>
  </div>
</body>
</html>
"""

    if fmt == "markdown":
        lines = [
            "## Evaluation Comparison",
            "",
            f"**Improved Queries:** `{len(improved)}` | **Regressed Queries:** `{len(regressed)}` | **Unchanged:** `{len(unchanged)}`",
            "",
            "| Metric | Base | Current | Delta |",
            "|---|---|---|---|",
        ]
        for m in metrics:
            b_val = base.get(m)
            c_val = current.get(m)
            badge = _diff_badge(c_val, b_val)
            lines.append(f"| {m} | {b_val} | {c_val} | {badge} |")
        if regressed:
            lines += ["", "### Regressed Queries:"]
            for q in regressed:
                lines.append(f"- `{q}`")
        if improved:
            lines += ["", "### Improved Queries:"]
            for q in improved:
                lines.append(f"- `{q}`")
        return "\n".join(lines) + "\n"

    # Text output
    lines = [
        "============================================================",
        " EVALUATION COMPARISON",
        "============================================================",
        f"Improved: {len(improved)} | Regressed: {len(regressed)} | Unchanged: {len(unchanged)}",
        "------------------------------------------------------------",
        f"{'Metric':<12} {'Base':<10} {'Current':<10} {'Delta':<10}",
        "------------------------------------------------------------",
    ]
    for m in metrics:
        b_val = base.get(m, 0.0)
        c_val = current.get(m, 0.0)
        badge = _diff_badge(c_val, b_val)
        lines.append(f"{m:<12} {b_val!s:<10} {c_val!s:<10} {badge:<10}")
    if regressed:
        lines.append("\nRegressed Queries:")
        for q in regressed:
            lines.append(f"  - {q}")
    if improved:
        lines.append("\nImproved Queries:")
        for q in improved:
            lines.append(f"  + {q}")
    lines.append("============================================================")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- Compare Benchmarks
def compare_bench(base: dict[str, Any], current: dict[str, Any], fmt: str) -> str:
    bench_metrics = [
        ("ingest.peak_rss_mb", "Ingest Peak RSS (MB)", False),
        ("ingest.docs_per_s", "Ingest Speed (docs/s)", True),
        ("qdrant.mem_mb", "Qdrant Memory (MB)", False),
        ("qdrant.disk_mb", "Qdrant Disk (MB)", False),
        ("agent.search.rps", "Search RPS", True),
        ("agent.search.latency_ms.p50", "Search p50 (ms)", False),
        ("agent.search.latency_ms.p95", "Search p95 (ms)", False),
        ("agent.answer.rps", "Answer RPS", True),
        ("agent.answer.latency_ms.p50", "Answer p50 (ms)", False),
        ("agent.answer.latency_ms.p95", "Answer p95 (ms)", False),
    ]

    if fmt == "html":
        rows = ""
        for dotted, label, higher_better in bench_metrics:
            b_val = _get(base, dotted)
            c_val = _get(current, dotted)
            diff_str = "-"
            if b_val is not None and c_val is not None:
                d = c_val - b_val
                good = (d >= 0) if higher_better else (d <= 0)
                cls = "delta-good" if good else "delta-bad"
                sign = "+" if d > 0 else ""
                diff_str = f'<span class="{cls}">{sign}{d:.2f}</span>'
            rows += f"""
            <tr>
              <td><strong>{label}</strong></td>
              <td>{b_val if b_val is not None else '-'}</td>
              <td>{c_val if c_val is not None else '-'}</td>
              <td>{diff_str}</td>
            </tr>
            """

        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Benchmark Comparison</title>
  {BASE_HTML_STYLE}
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Benchmark Performance Comparison</h1>
    </div>
    <table>
      <thead><tr><th>Metric</th><th>Base</th><th>Current</th><th>Delta</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</body>
</html>
"""

    if fmt == "markdown":
        lines = [
            "## Benchmark Comparison",
            "",
            "| Metric | Base | Current | Delta |",
            "|---|---|---|---|",
        ]
        for dotted, label, higher_better in bench_metrics:
            b_val = _get(base, dotted)
            c_val = _get(current, dotted)
            badge = _diff_badge(c_val, b_val, higher_better) if (b_val is not None and c_val is not None) else "-"
            lines.append(f"| {label} | {b_val if b_val is not None else '-'} | {c_val if c_val is not None else '-'} | {badge} |")
        return "\n".join(lines) + "\n"

    # Default text
    lines = [
        "============================================================",
        " BENCHMARK PERFORMANCE COMPARISON",
        "============================================================",
        f"{'Metric':<30} {'Base':<12} {'Current':<12} {'Delta':<12}",
        "------------------------------------------------------------",
    ]
    for dotted, label, higher_better in bench_metrics:
        b_val = _get(base, dotted)
        c_val = _get(current, dotted)
        badge = _diff_badge(c_val, b_val, higher_better) if (b_val is not None and c_val is not None) else "-"
        lines.append(f"{label:<30} {b_val if b_val is not None else '-'!s:<12} {c_val if c_val is not None else '-'!s:<12} {badge:<12}")
    lines.append("============================================================")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI Main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    # eval
    p_eval = subparsers.add_parser("eval", help="Render retrieval evaluation report")
    p_eval.add_argument("--report", type=Path, required=True, help="Path to eval JSON report")
    p_eval.add_argument("--baseline", type=Path, default=None, help="Optional baseline JSON path")
    p_eval.add_argument("--format", choices=["text", "markdown", "html"], default="text")
    p_eval.add_argument("--out", type=Path, default=None, help="Write output to file")

    # bench
    p_bench = subparsers.add_parser("bench", help="Render benchmark report")
    p_bench.add_argument("--report", type=Path, required=True, help="Path to bench JSON report")
    p_bench.add_argument("--baseline", type=Path, default=None, help="Optional baseline JSON path")
    p_bench.add_argument("--format", choices=["text", "markdown", "html"], default="text")
    p_bench.add_argument("--out", type=Path, default=None, help="Write output to file")

    # compare-eval
    p_ceval = subparsers.add_parser("compare-eval", help="Compare two eval JSON reports")
    p_ceval.add_argument("--base", type=Path, required=True, help="Base eval JSON path")
    p_ceval.add_argument("--current", type=Path, required=True, help="Current eval JSON path")
    p_ceval.add_argument("--format", choices=["text", "markdown", "html"], default="text")
    p_ceval.add_argument("--out", type=Path, default=None, help="Write output to file")

    # compare-bench
    p_cbench = subparsers.add_parser("compare-bench", help="Compare two bench JSON reports")
    p_cbench.add_argument("--base", type=Path, required=True, help="Base bench JSON path")
    p_cbench.add_argument("--current", type=Path, required=True, help="Current bench JSON path")
    p_cbench.add_argument("--format", choices=["text", "markdown", "html"], default="text")
    p_cbench.add_argument("--out", type=Path, default=None, help="Write output to file")

    args = parser.parse_args(argv)

    if args.command == "eval":
        report_data = _load_json(args.report)
        baseline_data = _load_json(args.baseline) if args.baseline else None
        output = render_eval(report_data, baseline_data, args.format)
    elif args.command == "bench":
        report_data = _load_json(args.report)
        baseline_data = _load_json(args.baseline) if args.baseline else None
        output = render_bench(report_data, baseline_data, args.format)
    elif args.command == "compare-eval":
        base_data = _load_json(args.base)
        cur_data = _load_json(args.current)
        output = compare_eval(base_data, cur_data, args.format)
    elif args.command == "compare-bench":
        base_data = _load_json(args.base)
        cur_data = _load_json(args.current)
        output = compare_bench(base_data, cur_data, args.format)
    else:
        parser.error(f"Unknown command {args.command}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
        print(f"Report written to {args.out}")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
