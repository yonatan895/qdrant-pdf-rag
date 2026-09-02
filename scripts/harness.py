#!/usr/bin/env python3
"""Layered harness orchestrator: snapshot pin -> L1 -> promotion gate.

The promotion gate (pure logic in gate_verdict, hermetically tested):
  1. Zero P0 trap failures — every must_not violation is absolute; traps
     are never averaged into a rate that could pass.
  2. No per-class regression — any class whose recall@5 or MRR drops more
     than the baseline's class_regression_floor (absolute, default 0.05)
     fails, so an aggregate gain cannot hide a class collapse.
  3. Improvement beats CI overlap — at least one primary metric
     (recall@5, MRR) must improve with a 95% paired-bootstrap CI whose
     interval excludes zero on the improvement side. A CI straddling zero
     is noise and does not merge.

Baselines are mode-keyed (hash vs vllm) like the eval baselines and store
PER-QUERY metric values: retrieval is deterministic against the pinned
snapshot, so the gate joins candidate and baseline rows by entry id and
bootstraps paired deltas — measuring the change, not sampling noise.

Snapshot pinning guarantees the index is identical across runs: the
harness keeps one named snapshot per collection, restores it before a run
(drift-only by default: the restore is skipped when the live fingerprint
matches the pin, since L1 never mutates the index; --restore always
forces it), and refuses to run when the fingerprint cannot be verified —
a silently drifted index would make every CI lie.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

from bootstrap_ci import ci95_paired, ci_excludes_zero
from eval_retrieval import load_golden

PRIMARY_METRICS = ("recall@5", "mrr")
DEFAULT_CLASS_FLOOR = 0.05


# --------------------------------------------------------------- gate verdict
def gate_verdict(
    candidate: dict[str, Any],
    baseline: dict[str, Any] | None,
    *,
    resamples: int = 2000,
    seed: int = 0,
) -> tuple[str, list[str]]:
    """Verdict for the promotion gate: "baseline" (nothing to compare),
    "merge", or "hold" (with reasons). Pure function.

    Pairing joins per-query values by entry id; entries present in only one
    side are skipped (they still fail the per-class/P0 checks through the
    aggregates)."""
    if baseline is None:
        return "baseline", ["no baseline recorded; candidate stored as the first baseline"]

    reasons: list[str] = []

    cand_traps = candidate.get("traps", {})
    failed = cand_traps.get("failed") or []
    if failed:
        reasons.append(f"P0 trap failures: {failed}")

    floor = float(baseline.get("_meta", {}).get("class_regression_floor", DEFAULT_CLASS_FLOOR))
    base_classes = baseline.get("classes", {})
    cand_classes = candidate.get("classes", {})
    for cls in sorted(set(base_classes) & set(cand_classes)):
        for metric in ("recall@5", "mrr"):
            b = base_classes[cls].get(metric)
            c = cand_classes[cls].get(metric)
            if b is None or c is None:
                continue
            if b - c > floor:
                reasons.append(
                    f"class regression: {cls} {metric} {b} -> {c} (floor {floor})"
                )

    base_pq = baseline.get("per_query", {})
    cand_pq = candidate.get("per_query", {})
    improvements: list[str] = []
    for metric in PRIMARY_METRICS:
        pairs = [
            (cand_pq[eid][metric], base_pq[eid][metric])
            for eid in sorted(set(cand_pq) & set(base_pq))
            if metric in cand_pq.get(eid, {}) and metric in base_pq.get(eid, {})
        ]
        if not pairs:
            reasons.append(f"no paired values for {metric}; CI overlap cannot be evaluated")
            continue
        ci = ci95_paired(pairs, resamples=resamples, seed=seed)
        if ci is None:  # pragma: no cover — pairs is non-empty here
            continue
        if ci_excludes_zero(ci, improvement=True):
            improvements.append(f"{metric} {tuple(round(x, 4) for x in ci)}")
    if not improvements:
        reasons.append(
            "no primary metric improved beyond CI overlap ("
            + ", ".join(PRIMARY_METRICS)
            + ")"
        )

    if reasons:
        return "hold", reasons
    return "merge", improvements


# ------------------------------------------------------------------ baseline
def baseline_path_for(repo: Path, embed_mode: str) -> Path:
    return repo / "benchmarks" / ("harness.json" if embed_mode != "vllm" else "harness-vllm.json")


def load_baseline(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_baseline(path: Path, summary: dict[str, Any], *, embed_mode: str, snapshot: dict[str, Any]) -> None:
    doc = {
        "_meta": {
            "note": "Harness promotion baseline; re-record via `make harness-baseline` (dedicated PR, AGENTS.md).",
            "embed_mode": embed_mode,
            "snapshot": snapshot,
            "class_regression_floor": DEFAULT_CLASS_FLOOR,
            "updated": time.strftime("%Y-%m-%d"),
        },
        **summary,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


# ------------------------------------------------------------- snapshot pin
# Qdrant auto-names snapshots ({collection}-{checksum}-{ts}.snapshot) and
# ignores client-proposed names; each snapshot is large (the 840k-point
# collection pins at ~7.8GB), so the pin policy is: keep exactly ONE
# snapshot per collection, record its server-assigned name + the collection
# points count in the baseline, and restore from that recorded name.
# Measured on the 840k-point collection: recover takes ~30s.

def snapshot_fingerprint(client: Any, collection: str) -> dict[str, Any] | None:
    """Points count + the current pin snapshot name, or None when the
    collection is unreachable (fail closed upstream)."""
    try:
        points = client.get_collection(collection).points_count
        snaps = sorted(s.name for s in client.list_snapshots(collection))
    except Exception:  # noqa: BLE001 — unreachable Qdrant fails closed upstream
        return None
    return {"points_count": points, "snapshot_name": snaps[0] if snaps else None}


def pin_snapshot(client: Any, collection: str) -> dict[str, Any]:
    """Ensure exactly one snapshot exists; create it when none does. Returns
    the fingerprint (name + points count) to record in the baseline — its
    points_count is the drift reference for later runs."""
    fp = snapshot_fingerprint(client, collection)
    if fp is None:
        raise RuntimeError(f"collection {collection!r} unreachable; refusing to pin a snapshot blind")
    if fp["snapshot_name"] is None:
        created = client.create_snapshot(collection, wait=True)
        fp = {"points_count": fp["points_count"], "snapshot_name": created.name}
    # Delete strays beyond the oldest so repeated baselines cannot fill the
    # disk (names sort by timestamp).
    snaps = sorted(s.name for s in client.list_snapshots(collection))
    for stray in snaps[1:]:
        client.delete_snapshot(collection, stray)
    return fp


def restore_snapshot(client: Any, collection: str, snapshot_name: str) -> dict[str, Any]:
    """Restore the pin snapshot (server-local file URL) and verify the
    post-restore fingerprint. Raises (fail closed) when the pin is missing
    or unverifiable — a silently drifted index would make every CI and gate
    verdict lie."""
    fp = snapshot_fingerprint(client, collection)
    if fp is None or fp["snapshot_name"] is None:
        raise RuntimeError("pin snapshot missing; run `make harness-baseline` to re-pin")
    client.recover_snapshot(
        collection, location=f"file:///qdrant/snapshots/{collection}/{snapshot_name}", wait=True
    )
    post = snapshot_fingerprint(client, collection)
    if post is None:
        raise RuntimeError("post-restore fingerprint unverifiable; refusing to evaluate")
    return post


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Layered harness: L1 retrieval + promotion gate")
    parser.add_argument("--golden", type=Path, action="append", default=None,
                        help="golden JSONL (repeatable; default dev+holdout)")
    parser.add_argument("--collection", default=None, help="Qdrant collection (default: settings)")
    parser.add_argument("--restore", choices=("drift", "always", "never"), default="drift",
                        help="snapshot restore policy (default drift: only when the fingerprint differs)")
    parser.add_argument("--gate", action="store_true", help="gate against the stored baseline (exit 1 on hold)")
    parser.add_argument("--update-baseline", action="store_true", help="record the run as the mode's baseline")
    parser.add_argument("--baseline", type=Path, default=None, help="override the mode-keyed baseline path")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--resamples", type=int, default=2000)
    args = parser.parse_args(argv)

    from mainframe_rag.config import load_settings
    from mainframe_rag.manifest import write_run_manifest

    settings = load_settings()
    collection = args.collection or settings.qdrant_collection
    baseline_path = args.baseline or baseline_path_for(REPO, settings.embed_mode)
    golden_paths = args.golden or [REPO / "evals" / "golden.jsonl", REPO / "evals" / "holdout.jsonl"]
    entries: list = []
    for p in golden_paths:
        entries.extend(load_golden(p))
    baseline = load_baseline(baseline_path)
    print(f"[*] harness L1: {len(entries)} entries, collection {collection!r}, "
          f"embed_mode={settings.embed_mode}, baseline={baseline_path.name}", file=sys.stderr)

    from qdrant_client import QdrantClient

    from mainframe_rag.ingest.embed import build_embedder

    qdrant = QdrantClient(url=settings.qdrant_url, timeout=60)
    embedder = build_embedder(settings, None)

    fp = snapshot_fingerprint(qdrant, collection)
    if fp is None:
        print(f"FAIL: collection {collection!r} unreachable; refusing to evaluate", file=sys.stderr)
        return 1
    t0 = time.monotonic()
    if args.restore == "always":
        if not fp["snapshot_name"]:
            fp = pin_snapshot(qdrant, collection)
        fp = restore_snapshot(qdrant, collection, fp["snapshot_name"])
        policy_note = "restored (always)"
    elif args.restore == "drift":
        # Drift = the live points count or pin name differs from the
        # fingerprint recorded with the baseline. L1 never mutates the
        # index, so a matching fingerprint skips the restore; a mismatched
        # one means another actor mutated the collection and the pin must
        # be re-applied before any CI is honest.
        recorded = (baseline or {}).get("_meta", {}).get("snapshot", {})
        expected_points = recorded.get("points_count")
        expected_name = recorded.get("snapshot_name")
        if (
            fp["snapshot_name"]
            and expected_points is not None
            and fp["points_count"] == expected_points
            and (expected_name is None or fp["snapshot_name"] == expected_name)
        ):
            policy_note = f"skipped (fingerprint matches pin: {expected_points} points)"
        elif fp["snapshot_name"]:
            fp = restore_snapshot(qdrant, collection, fp["snapshot_name"])
            policy_note = f"restored (drift: live {fp['points_count']} vs pin {expected_points})"
        else:
            fp = pin_snapshot(qdrant, collection)
            policy_note = "pin created (no prior snapshot)"
    else:
        policy_note = "skipped (--restore never)"
    restore_s = round(time.monotonic() - t0, 1)
    print(f"[*] snapshot: {policy_note} in {restore_s}s ({fp['points_count']} points)", file=sys.stderr)

    from harness_l1 import aggregate, collect_rows

    rows = collect_rows(entries, qdrant, embedder, collection, settings)
    summary = aggregate(rows)

    print(
        f"[*] L1 overall: {json.dumps(summary['overall'])} "
        f"traps: failed={len(summary['traps']['failed'])}",
        file=sys.stderr,
    )
    for cls, block in summary["classes"].items():
        print(f"    {cls:12s} {json.dumps(block)}", file=sys.stderr)

    if args.update_baseline or baseline is None:
        save_baseline(baseline_path, summary, embed_mode=settings.embed_mode, snapshot=fp)
        print(f"[*] baseline recorded: {baseline_path}", file=sys.stderr)
        verdict, reasons = "baseline", ["first baseline recorded"]
    else:
        verdict, reasons = gate_verdict(summary, baseline, resamples=args.resamples)
    if args.gate or baseline is not None:
        print(f"[*] GATE VERDICT: {verdict}", file=sys.stderr)
        for r in reasons:
            print(f"    - {r}", file=sys.stderr)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps({"verdict": verdict, "reasons": reasons, "summary": summary,
                        "snapshot": fp, "restore_s": restore_s}, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    try:
        write_run_manifest("harness", settings, {"verdict": verdict, **summary["overall"],
                                                 "traps": summary["traps"], "restore_s": restore_s})
    except Exception as exc:  # noqa: BLE001 — observability never gates
        print(f"warn: manifest append failed: {exc}", file=sys.stderr)

    return 0 if verdict in ("merge", "baseline") else 1


if __name__ == "__main__":
    sys.exit(main())
