#!/usr/bin/env python3
"""L1 retrieval regression gate: CPU hash mode against Qdrant simulation.

Orchestrates the entire L1 evaluation pipeline for CI pull requests and local
verification:
  1. Resolves or starts the Qdrant simulator via scripts/qdrant_sim.py
     (docker container from the pinned images.txt, or reuses QDRANT_SIM_URL / QDRANT_URL).
  2. Generates an original synthetic PDF corpus at runtime covering the golden
     dataset expectations (headings, identifiers, terms) without committing binaries.
  3. Ingests the corpus into Qdrant in EMBED_MODE=hash (fast, purely CPU-bound).
  4. Runs evaluate() across all queries and checks for regressions against
     evals/baseline.json.
  5. Renders a markdown delta table via scripts/render_report.py for PR comments / MR notes.
  6. Cleans up collections and stops the simulator container.
  7. Exits nonzero if any regression or query failure occurs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx2
import pymupdf

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from eval_retrieval import (
    check_baseline,
    evaluate,
    load_golden,
    update_baseline,
)
from qdrant_sim import QdrantSimError, start_simulator
from render_report import render_eval

from mainframe_rag.config import load_settings
from mainframe_rag.ingest import run_ingest


def generate_synthetic_golden_corpus(
    entries: list[dict[str, Any]],
    out_dir: Path,
) -> dict[str, Any]:
    """Generate synthetic PDFs at runtime matching the golden entries.

    Builds one PDF per unique expected_doc_id with pages carrying the query
    text, expected headings, identifiers, and required phrases. Also generates
    an unrelated guide to ensure abstain queries have a distractor without false hits.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    docs_map: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        for doc_id in entry.get("expected_doc_ids", []):
            docs_map.setdefault(doc_id, []).append(entry)

    for doc_id, doc_entries in docs_map.items():
        doc = pymupdf.open()
        title = f"Synthetic Reference {doc_id}"
        header = f"{doc_id} {title}"

        # Cover page (page 1)
        p0 = doc.new_page()
        p0.insert_textbox(
            pymupdf.Rect(72, 72, p0.rect.width - 72, p0.rect.height - 90),
            f"{title}\nz/OS V2R5\n{doc_id}\n\nSynthetic documentation fixture for {doc_id}.",
            fontsize=11,
        )
        p0.insert_textbox(pymupdf.Rect(72, 30, p0.rect.width - 72, 55), header, fontsize=8)

        toc: list[list[Any]] = [[1, "Contents", 1]]
        for idx, entry in enumerate(doc_entries, start=2):
            p = doc.new_page()
            eid = entry.get("id", f"entry_{idx}")
            heading = entry.get("expected_heading") or f"Chapter {idx}. {eid}"
            leaf = heading.split(">")[-1].strip()

            text_parts = [
                leaf,
                "",
                entry.get("query", ""),
                "",
            ]
            ident = entry.get("must_cite_identifier")
            if ident:
                text_parts.append(f"Identifier: {ident}")
            for term in entry.get("gold_must_contain") or []:
                text_parts.append(f"Term: {term}")
            syntax = entry.get("syntax_pattern")
            if syntax:
                text_parts.append(f"Syntax construct: {syntax}")

            body_text = "\n".join(text_parts)
            p.insert_textbox(
                pymupdf.Rect(72, 72, p.rect.width - 72, p.rect.height - 90),
                body_text,
                fontsize=11,
            )
            p.insert_textbox(pymupdf.Rect(72, 30, p.rect.width - 72, 55), header, fontsize=8)
            toc.append([1, leaf, idx])

        doc.set_toc(toc)
        doc.save(out_dir / f"{doc_id}.pdf")
        doc.close()

    # Distractor document for negative/abstain queries
    distractor = pymupdf.open()
    d0 = distractor.new_page()
    d0.insert_textbox(
        pymupdf.Rect(72, 72, d0.rect.width - 72, d0.rect.height - 90),
        "Generic Distractor Guide\nHardware specifications and cabling diagrams.",
        fontsize=11,
    )
    distractor.save(out_dir / "generic-distractor.pdf")
    distractor.close()

    return {"docs_generated": len(docs_map) + 1, "out_dir": out_dir}


def run_gate(
    *,
    golden_path: Path = REPO_ROOT / "evals" / "golden.jsonl",
    baseline_path: Path = REPO_ROOT / "evals" / "baseline.json",
    collection: str = "local-corpus",
    qdrant_url: str | None = None,
    out_path: Path | None = None,
    summary_path: Path | None = None,
    delta_path: Path | None = None,
    update_baseline_flag: bool = False,
    keep_sim: bool = False,
) -> tuple[int, str]:
    """Execute the L1 retrieval evaluation gate end-to-end.

    Returns (exit_code, delta_markdown).
    """
    raw_entries: list[dict[str, Any]] = [
        json.loads(line)
        for line in golden_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    entries = load_golden(golden_path)
    sim_url = qdrant_url or os.environ.get("QDRANT_SIM_URL") or os.environ.get("QDRANT_URL")

    try:
        sim = start_simulator(REPO_ROOT, sim_url)
    except QdrantSimError as exc:
        msg = f"FAIL: unable to start or connect to Qdrant simulator: {exc}"
        print(msg, file=sys.stderr)
        return 1, f"## Retrieval Evaluation Gate (L1)\n\n**ERROR:** {msg}\n"

    temp_dir = tempfile.mkdtemp(prefix="gate_l1_")
    temp_path = Path(temp_dir)
    corpus_dir = temp_path / "corpus"
    progress_file = temp_path / "inventory.jsonl"

    env_backup = dict(os.environ)
    try:
        t0 = time.monotonic()
        generate_synthetic_golden_corpus(raw_entries, corpus_dir)
        gen_time = round(time.monotonic() - t0, 2)
        print(f"[*] generated synthetic golden corpus in {gen_time}s", file=sys.stderr)

        # Ingest in hash mode
        os.environ["QDRANT_URL"] = sim.url
        os.environ["QDRANT_COLLECTION"] = collection
        os.environ["EMBED_MODE"] = "hash"
        os.environ["ALLOW_HASH_MODE"] = "true"

        t_ing0 = time.monotonic()
        rc = run_ingest.main(["--src", str(corpus_dir), "--progress", str(progress_file), "--workers", "1"])
        ing_time = round(time.monotonic() - t_ing0, 2)
        if rc != 0:
            msg = f"FAIL: synthetic corpus ingest failed with return code {rc}"
            print(msg, file=sys.stderr)
            return 1, f"## Retrieval Evaluation Gate (L1)\n\n**ERROR:** {msg}\n"
        print(f"[*] ingested synthetic corpus in {ing_time}s", file=sys.stderr)

        # Evaluate
        settings = load_settings()
        t_ev0 = time.monotonic()
        report = evaluate(entries, settings)
        ev_time = round(time.monotonic() - t_ev0, 2)
        print(
            f"[*] evaluation completed in {ev_time}s (n={report['n']}, failures={report['failures']})",
            file=sys.stderr,
        )

        baseline: dict[str, Any] | None = None
        regressions: list[str] = []
        if baseline_path.exists():
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

        if update_baseline_flag:
            update_baseline(report, baseline_path)
            print(f"[*] baseline updated at {baseline_path}", file=sys.stderr)
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        elif baseline is not None:
            regressions = check_baseline(report, baseline)

        # Render reports
        delta_md = render_eval(report, baseline, fmt="markdown")

        if out_path:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if summary_path:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(delta_md, encoding="utf-8")
        if delta_path:
            delta_path.parent.mkdir(parents=True, exist_ok=True)
            delta_path.write_text(delta_md, encoding="utf-8")

        if regressions:
            print("[!] REGRESSIONS vs baseline detected:", file=sys.stderr)
            for reg in regressions:
                print(f"    - {reg}", file=sys.stderr)
            return 1, delta_md

        if report.get("failures", 0) > 0:
            print(f"[!] {report['failures']} query failures occurred", file=sys.stderr)
            return 1, delta_md

        print("[*] L1 retrieval gate PASSED: 0 regressions, 0 failures", file=sys.stderr)
        return 0, delta_md

    finally:
        # Cleanup collection from Qdrant
        try:
            httpx2.delete(f"{sim.url.rstrip('/')}/collections/{collection}", timeout=5.0)
        except (httpx2.HTTPError, OSError):
            pass
        # Cleanup temp files
        shutil.rmtree(temp_dir, ignore_errors=True)
        # Restore env
        for k in ("QDRANT_URL", "QDRANT_COLLECTION", "EMBED_MODE", "ALLOW_HASH_MODE"):
            if k in env_backup:
                os.environ[k] = env_backup[k]
            else:
                os.environ.pop(k, None)
        # Stop simulator if we created it
        if not keep_sim:
            sim.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=REPO_ROOT / "evals" / "golden.jsonl", help="Golden JSONL path")
    parser.add_argument("--baseline", type=Path, default=REPO_ROOT / "evals" / "baseline.json", help="Baseline JSON path")
    parser.add_argument("--collection", default="local-corpus", help="Qdrant collection name")
    parser.add_argument("--qdrant-url", default=None, help="Optional external Qdrant URL (defaults to simulator)")
    parser.add_argument("--out", type=Path, default=None, help="Write full JSON report to this path")
    parser.add_argument("--summary", type=Path, default=None, help="Write markdown summary to this path")
    parser.add_argument("--delta", type=Path, default=None, help="Write markdown delta table to this path")
    parser.add_argument("--update-baseline", action="store_true", help="Record candidate report as baseline")
    parser.add_argument("--keep-sim", action="store_true", help="Do not stop simulator on exit")

    args = parser.parse_args(argv)
    code, md = run_gate(
        golden_path=args.golden,
        baseline_path=args.baseline,
        collection=args.collection,
        qdrant_url=args.qdrant_url,
        out_path=args.out,
        summary_path=args.summary,
        delta_path=args.delta,
        update_baseline_flag=args.update_baseline,
        keep_sim=args.keep_sim,
    )
    # Output delta markdown to stdout for easy pipe/inspection
    print(md)
    return code


if __name__ == "__main__":
    sys.exit(main())
