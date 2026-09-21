"""Explicit, backed-up removal of duplicate legacy points from an unfinished build.

This deliberately handles only sourceless points whose exact payload/vectors
still exist in the retained live collection and whose replacement is complete.
It never retires sources, edits controls, or publishes. Resume normal ingest next.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models

from mainframe_rag.config import Settings, load_settings
from mainframe_rag.ingest.completion import (
    acquire_publish_lock,
    acquire_run_lock,
    completion_collection_for,
    is_doc_complete,
    release_run_lock,
)
from mainframe_rag.ingest.inventory import load_inventory
from mainframe_rag.ingest.publish import publish_state_path, read_publish_state
from mainframe_rag.ingest.representation import (
    digest_of,
    manifest_digest,
    read_manifest_record,
)
from mainframe_rag.ingest.rules_version import extraction_rules_version


def _bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _record(point: models.Record) -> dict[str, Any]:
    data = point.model_dump(mode="json")
    return {"id": str(point.id), "payload": data["payload"], "vector": data["vector"]}


def _write(path: Path, content: bytes) -> None:
    # Exclusive files keep a retry from overwriting its only recovery evidence.
    with path.open("xb") as file:
        os.chmod(path, 0o600)
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


@contextmanager
def _locked(progress: Path, alias: str) -> Iterator[None]:
    with ExitStack() as stack:
        for acquire in (lambda: acquire_publish_lock(progress, alias),
                        lambda: acquire_run_lock(progress)):
            stack.callback(release_run_lock, acquire())
        yield


def _binding(client: QdrantClient, settings: Settings, progress: Path) -> dict[str, Any]:
    state = read_publish_state(progress, settings.qdrant_collection)
    if state is None:
        raise ValueError("repair requires an unfinished publication build")
    staging = state["staging"]
    aliases = {a.alias_name: a.collection_name for a in client.get_aliases().aliases}
    live = aliases.get(settings.qdrant_collection)
    if not live or staging in aliases.values():
        raise ValueError("repair requires a distinct, unserved staging collection")
    record = read_manifest_record(client, completion_collection_for(staging))
    wanted = manifest_digest(settings, extraction_rules_version())
    if record is None or record.state != "pending" or digest_of(record.manifest) != wanted:
        raise ValueError("repair requires the pending representation for these exact inputs")
    return {"alias": settings.qdrant_collection, "staging": staging, "live": live,
            "manifest_digest": wanted, "state_sha256": _digest(publish_state_path(progress, settings.qdrant_collection).read_bytes()),
            "inventory_sha256": _digest(progress.read_bytes())}


def _residue(client: QdrantClient, staging: str, limit: int) -> list[dict[str, Any]]:
    selector = models.Filter(must=[models.IsEmptyCondition(
        is_empty=models.PayloadField(key="source_rev"))])
    result: list[dict[str, Any]] = []
    offset = None
    while True:
        page, offset = client.scroll(staging, scroll_filter=selector, offset=offset,
                                    limit=64, with_payload=True, with_vectors=True)
        result.extend(_record(p) for p in page)
        if len(result) > limit:
            raise ValueError("residue exceeds the explicitly selected repair bound")
        if offset is None:
            return sorted(result, key=lambda p: p["id"])


def _verify_replacements(client: QdrantClient, settings: Settings, progress: Path,
                         binding: dict[str, Any], points: list[dict[str, Any]],
                         labels: str) -> None:
    inventory = load_inventory(progress)
    selected = settings.model_copy(update={"qdrant_collection": binding["staging"]})
    families = {(p["payload"].get("doc_id"), p["payload"].get("sha256")) for p in points}
    for doc_id, sha in families:
        records = [r for r in inventory.values() if r.doc_id == doc_id and r.sha256 == sha]
        if not isinstance(doc_id, str) or not doc_id or not isinstance(sha, str) or not sha or len(records) != 1:
            raise ValueError("residue has no unique source-matched replacement")
        rec = records[0]
        if (rec.status not in ("upserted", "skipped") or not rec.source_rev
                or rec.rules_version != extraction_rules_version()
                or not is_doc_complete(client, selected, doc_id, sha256=sha,
                    rules_v=rec.rules_version, source_labels=labels, source_rev=rec.source_rev,
                    required_manifest_digest=binding["manifest_digest"])):
            raise ValueError("replacement content is not complete under the wanted contract")
    # Every removed point has both a private export and an independent retained
    # copy. Counts or matching source hashes alone are not a rollback proof.
    old: list[dict[str, Any]] = []
    for start in range(0, len(points), 64):
        ids = [p["id"] for p in points[start:start + 64]]
        old.extend(_record(p) for p in client.retrieve(binding["live"], ids,
                                                      with_payload=True, with_vectors=True))
    if _bytes(sorted(old, key=lambda p: p["id"])) != _bytes(points):
        raise ValueError("retained rollback points differ from the proposed removal")


def plan_repair(client: QdrantClient, settings: Settings, progress: Path,
                directory: Path, *, max_points: int, source_labels: str = "||") -> str:
    if max_points < 1:
        raise ValueError("max_points must be positive")
    with _locked(progress, settings.qdrant_collection):
        binding = _binding(client, settings, progress)
        points = _residue(client, binding["staging"], max_points)
        if not points:
            raise ValueError("no duplicate legacy residue to plan")
        _verify_replacements(client, settings, progress, binding, points, source_labels)
        backup = _bytes(points)
        plan = {"version": 1, **binding, "source_labels": source_labels,
                "points_sha256": _digest(backup), "count": len(points),
                "before_count": client.count(binding["staging"], exact=True).count}
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        _write(directory / "points.json", backup)
        _write(directory / "inventory.jsonl", progress.read_bytes())
        _write(directory / "publish-state.json",
               publish_state_path(progress, settings.qdrant_collection).read_bytes())
        _write(directory / "plan.json", _bytes(plan))
        return _digest(_bytes(plan))


def apply_repair(client: QdrantClient, settings: Settings, progress: Path,
                 directory: Path, approved_sha256: str) -> int:
    raw = (directory / "plan.json").read_bytes()
    if _digest(raw) != approved_sha256:
        raise ValueError("approved plan checksum differs")
    plan = json.loads(raw)
    backup = (directory / "points.json").read_bytes()
    if plan.get("version") != 1 or _digest(backup) != plan["points_sha256"]:
        raise ValueError("unsupported plan or corrupt backup")
    points = json.loads(backup)
    if len(points) != plan["count"] or not points:
        raise ValueError("invalid backup membership")
    for point in points:
        models.PointStruct.model_validate(point)
    with _locked(progress, settings.qdrant_collection):
        binding = _binding(client, settings, progress)
        if any(plan.get(k) != v for k, v in binding.items()):
            raise ValueError("publication inputs changed after approval")
        _verify_replacements(client, settings, progress, binding, points, plan["source_labels"])
        remaining = _residue(client, binding["staging"], plan["count"])
        approved = {p["id"]: p for p in points}
        if any(p != approved.get(p["id"]) for p in remaining):
            raise ValueError("staging residue changed after approval")
        expected = plan["before_count"] - plan["count"]
        if client.count(binding["staging"], exact=True).count != expected + len(remaining):
            raise ValueError("staging membership changed after approval")
        # Retrying a partial/acknowledgement-lost deletion can only remove the
        # still-identical remaining subset of the originally approved IDs.
        if remaining:
            result = client.delete(binding["staging"],
                points_selector=models.PointIdsList(points=[p["id"] for p in remaining]), wait=True)
            if result.status != models.UpdateStatus.COMPLETED:
                raise RuntimeError("repair deletion was not acknowledged")
        if (_residue(client, binding["staging"], plan["count"])
                or client.count(binding["staging"], exact=True).count != expected):
            raise RuntimeError("repair postcondition failed")
        _verify_replacements(client, settings, progress, binding, points, plan["source_labels"])
        if _binding(client, settings, progress) != binding:
            raise RuntimeError("publication binding changed during repair")
        receipt = directory / "applied.json"
        if not receipt.exists():
            _write(receipt, _bytes({"plan_sha256": approved_sha256, "remaining_count": expected}))
        return len(remaining)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "apply"))
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--max-points", type=int)
    parser.add_argument("--source-labels", default="||")
    parser.add_argument("--approve-plan")
    args = parser.parse_args(argv)
    if args.operation == "plan" and (args.max_points is None or args.approve_plan):
        parser.error("plan requires --max-points and does not accept --approve-plan")
    if args.operation == "apply" and (not args.approve_plan or args.max_points is not None):
        parser.error("apply requires --approve-plan and does not accept --max-points")
    try:
        settings = load_settings()
        with closing(QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key,
                                  timeout=settings.qdrant_ingest_timeout_s)) as client:
            if args.operation == "plan":
                checksum = plan_repair(client, settings, args.progress, args.directory,
                                       max_points=args.max_points, source_labels=args.source_labels)
                print(json.dumps({"status": "planned", "plan_sha256": checksum}))
            else:
                count = apply_repair(client, settings, args.progress, args.directory, args.approve_plan)
                print(json.dumps({"status": "repaired", "deleted": count,
                                  "next": "resume the recorded build through normal ingest"}))
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI boundary redacts upstream/private data
        # Never dump source names, payloads, credentials or upstream text.
        print(json.dumps({"status": "refused", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
