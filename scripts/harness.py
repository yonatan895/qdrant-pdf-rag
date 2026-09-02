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
# points count in the baseline, and restore from that RECORDED name.
# Ordering is by the server's creation_time — snapshot names sort by their
# checksum segment, never by age. Measured on the 840k-point collection:
# recover takes ~30s. The fingerprint (points count + pin name) is a cheap
# drift guard, not a content pin: equal count + equal name is treated as
# undrifted, not as proof of identical vectors.

def snapshot_fingerprint(client: Any, collection: str, prefer_name: str | None = None) -> dict[str, Any] | None:
    """Points count + snapshot names ordered oldest-first by the server's
    creation_time (never lexicographic — the name leads with a checksum).
    `prefer_name` moves the recorded pin to the front when it still exists.
    None when the collection is unreachable (fail closed upstream)."""
    try:
        points = client.get_collection(collection).points_count
        snaps = sorted(client.list_snapshots(collection), key=lambda s: (str(s.creation_time or ""), s.name))
        names = [s.name for s in snaps]
    except Exception:  # noqa: BLE001 — unreachable Qdrant fails closed upstream
        return None
    if prefer_name and prefer_name in names:
        names.remove(prefer_name)
        names.insert(0, prefer_name)
    return {"points_count": points, "snapshot_names": names}


def resolve_snapshot_action(
    fp: dict[str, Any],
    recorded: dict[str, Any] | None,
    *,
    restore: str,
    record_mode: bool,
) -> tuple[str, Any]:
    """Pure snapshot policy: (action, payload) where action is one of
    "skip" | "restore" | "pin" | "fail" and the payload is the snapshot
    name to restore or the fail reason.

    Fail-closed rules (the review blockers): a gate run NEVER pins live
    state — a deleted pin plus a mutated collection would otherwise be
    promoted as the pin; restore always targets the RECORDED pin name from
    the baseline, never whichever snapshot happens to be listed first."""
    recorded_name = (recorded or {}).get("snapshot_name")
    recorded_points = (recorded or {}).get("points_count")
    live_names = fp["snapshot_names"]
    if restore == "never":
        return "skip", None
    if restore == "always":
        if record_mode:
            return ("restore", recorded_name) if recorded_name and recorded_name in live_names else ("pin", None)
        if not recorded_name:
            return "fail", "no recorded pin in the baseline; --restore always cannot verify what to restore"
        if recorded_name not in live_names:
            return "fail", f"recorded pin {recorded_name!r} no longer exists on the server"
        return "restore", recorded_name
    # drift policy (default)
    if (
        recorded_name
        and live_names
        and live_names[0] == recorded_name
        and recorded_points is not None
        and fp["points_count"] == recorded_points
    ):
        return "skip", recorded_name
    if record_mode:
        # A baseline run owns pinning: create or re-adopt, then prune strays.
        return "pin", None
    if recorded_name:
        if recorded_name in live_names:
            # Fingerprint drifted (points count, or a stray snapshot sorts
            # first) — restore the recorded pin regardless of list order.
            return "restore", recorded_name
        return (
            "fail",
            (
                f"recorded pin {recorded_name!r} no longer exists on the server; "
                "a gate run never pins live state — re-record with make harness-baseline"
            ),
        )
    return (
        "fail",
        (
            "no recorded pin in the baseline; a gate run never pins live state — "
            "record one with make harness-baseline"
        ),
    )


def pin_snapshot(
    client: Any,
    collection: str,
    keep: str | None = None,
    expected_points: int | None = None,
) -> dict[str, Any]:
    """Pin the LIVE collection state for a baseline run.

    The pin is re-adopted ONLY when the fingerprint matches the recorded
    one (`keep` listed first by prefer_name AND points count equal) — the
    skip path's exact condition. Anything else (no snapshots, a stray, or
    a drifted points count from a new ingest) creates a NEW snapshot of the
    current state and prunes everything else INCLUDING the previous pin:
    an old pin snapshots a different index and can never reproduce the
    metrics a drifted record run just measured, so recording
    {points_count: new, snapshot_name: old} would be a baseline that no
    restore can reproduce. Never called on a gate run."""
    fp = snapshot_fingerprint(client, collection, prefer_name=keep)
    if fp is None:
        raise RuntimeError(f"collection {collection!r} unreachable; refusing to pin a snapshot blind")
    names = fp["snapshot_names"]
    matched = (
        bool(names)
        and keep is not None
        and names[0] == keep
        and expected_points is not None
        and fp["points_count"] == expected_points
    )
    if not matched:
        created = client.create_snapshot(collection, wait=True)
        for stray in (n for n in names if n != created.name):
            client.delete_snapshot(collection, stray)
        return {"points_count": fp["points_count"], "snapshot_name": created.name}
    for stray in names[1:]:
        client.delete_snapshot(collection, stray)
    return {"points_count": fp["points_count"], "snapshot_name": names[0]}


def restore_snapshot(
    client: Any,
    collection: str,
    snapshot_name: str,
    snapshots_dir: str = "/qdrant/snapshots",
    expected_points: int | None = None,
) -> dict[str, Any]:
    """Restore the RECORDED pin snapshot and verify the post-restore
    fingerprint. The server-local file URL assumes the container's snapshot
    path (docker compose layout) — override via Settings.qdrant_snapshots_dir
    for other deployments. When `expected_points` is provided (the recorded
    pin's count) the post-restore collection must match it — recover can
    report success without actually rolling the collection back, and a gate
    that then evaluates a mutated index is worse than no gate. Raises (fail
    closed) on any mismatch."""
    fp = snapshot_fingerprint(client, collection, prefer_name=snapshot_name)
    if fp is None or snapshot_name not in fp["snapshot_names"]:
        raise RuntimeError(f"pin snapshot {snapshot_name!r} missing; run `make harness-baseline` to re-pin")
    client.recover_snapshot(
        collection,
        location=f"file://{snapshots_dir.rstrip('/')}/{collection}/{snapshot_name}",
        wait=True,
    )
    post = snapshot_fingerprint(client, collection, prefer_name=snapshot_name)
    if post is None:
        raise RuntimeError("post-restore fingerprint unverifiable; refusing to evaluate")
    if expected_points is not None and post["points_count"] != expected_points:
        raise RuntimeError(
            f"post-restore points count {post['points_count']} != recorded pin "
            f"{expected_points}; the index did not roll back — refusing to evaluate"
        )
    return {"points_count": post["points_count"], "snapshot_name": snapshot_name}


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
    # Fail closed BEFORE anything runs: a gate (or any non-recording run)
    # without a baseline would otherwise be invited to create one from
    # whatever is live — recording happens only via --update-baseline.
    if baseline is None and not args.update_baseline:
        print(f"FAIL: no baseline at {baseline_path}; a gate run never creates one. "
              "Record it first: make harness-baseline", file=sys.stderr)
        return 1
    print(f"[*] harness L1: {len(entries)} entries, collection {collection!r}, "
          f"embed_mode={settings.embed_mode}, baseline={baseline_path.name}", file=sys.stderr)

    from qdrant_client import QdrantClient

    from mainframe_rag.ingest.embed import build_embedder

    qdrant = QdrantClient(url=settings.qdrant_url, timeout=60)
    embedder = build_embedder(settings, None)

    recorded = (baseline or {}).get("_meta", {}).get("snapshot", {})
    fp = snapshot_fingerprint(qdrant, collection, prefer_name=recorded.get("snapshot_name"))
    if fp is None:
        print(f"FAIL: collection {collection!r} unreachable; refusing to evaluate", file=sys.stderr)
        return 1
    t0 = time.monotonic()
    action, payload = resolve_snapshot_action(
        fp, recorded, restore=args.restore, record_mode=args.update_baseline
    )
    if action == "fail":
        print(f"FAIL: {payload}", file=sys.stderr)
        return 1
    if action == "skip":
        policy_note = f"skipped (fingerprint matches pin: {fp['points_count']} points)"
    elif action == "restore":
        fp = restore_snapshot(
            qdrant,
            collection,
            payload,
            settings.qdrant_snapshots_dir,
            expected_points=recorded.get("points_count"),
        )
        policy_note = f"restored recorded pin {payload!r}"
    else:  # pin — only reachable in record mode
        fp = pin_snapshot(
            qdrant,
            collection,
            keep=recorded.get("snapshot_name"),
            expected_points=recorded.get("points_count"),
        )
        policy_note = f"pinned {fp['snapshot_name']!r} ({fp['points_count']} points)"
    restore_s = round(time.monotonic() - t0, 1)
    print(f"[*] snapshot: {policy_note} in {restore_s}s ({fp['points_count']} points)", file=sys.stderr)
    # The baseline records the PIN (singular snapshot_name), not the live
    # fingerprint (an ordered list) — the drift policy matches on this shape.
    pin = {
        "points_count": fp["points_count"],
        "snapshot_name": payload if action == "skip" else fp.get("snapshot_name"),
    }

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

    if args.update_baseline:
        save_baseline(baseline_path, summary, embed_mode=settings.embed_mode, snapshot=pin)
        print(f"[*] baseline recorded: {baseline_path}", file=sys.stderr)
        verdict, reasons = "baseline", ["baseline recorded"]
    else:
        verdict, reasons = gate_verdict(summary, baseline, resamples=args.resamples)
    print(f"[*] GATE VERDICT: {verdict}", file=sys.stderr)
    for r in reasons:
        print(f"    - {r}", file=sys.stderr)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps({"verdict": verdict, "reasons": reasons, "summary": summary,
                        "snapshot": pin, "restore_s": restore_s}, indent=1, ensure_ascii=False) + "\n",
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
