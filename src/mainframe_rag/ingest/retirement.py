"""Retirement tombstone: the persistent record data disposal writes into a
build's control collection before anything of that build is deleted (issue
#1373; owner contract docs/ingest.md, Rollback / disposal).

One point per control collection, at a deterministic id, beside the
publication receipt. `retiring` is written first and read back; `retired`
replaces it only after the data collection is confirmed absent. Nothing else
rewrites or removes a tombstone: retired stays retired. The record carries
ids, names, counts, sizes and times only, never manual text, keys or peer
credentials. It has a `record_type` and no `doc_id`, so no completion-marker,
catalog or receipt scan reads it as one of theirs. It is inside the build's
control seal on purpose: a tombstoned build never passes a seal check again.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from mainframe_rag.ingest.build import BuildBinding, canonical_build_id
from mainframe_rag.ports import QdrantReader

TOMBSTONE_RECORD_TYPE = "retirement-tombstone"
TOMBSTONE_SCHEMA = 1
STATE_RETIRING = "retiring"
STATE_RETIRED = "retired"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_FIELDS = frozenset({
    "record_type", "tombstone_schema", "target_collection", "build_id", "logical_alias",
    "data_collection", "gen_fp", "corpus_fp", "state", "content_seal", "source_catalog",
    "data_points", "snapshots", "keep", "readers_stopped_at", "recovery_ref", "operator",
    "plan_digest", "tool_release", "created_at", "retired_at", "completion",
})
_COMPLETION_FIELDS = frozenset({"resumed", "readers_stopped_at", "operator", "recovery_ref", "kept_unverified"})
_SNAPSHOT_FIELDS = frozenset({"peer", "name", "size", "checksum", "creation_time"})


class InvalidTombstone(RuntimeError):
    """A point sits at the tombstone id but is not a valid tombstone of this
    control collection. Fail closed: never treated as absent."""


def tombstone_point_id(control: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{TOMBSTONE_RECORD_TYPE}|{control}"))


def decode_tombstone(payload: object, control: str) -> dict[str, Any]:
    """Validate a stored tombstone of `control`; raises InvalidTombstone."""
    try:
        if not isinstance(payload, dict) or set(payload) != _FIELDS:
            raise ValueError("fields")
        if payload["record_type"] != TOMBSTONE_RECORD_TYPE or payload["tombstone_schema"] != TOMBSTONE_SCHEMA:
            raise ValueError("type")
        if type(payload["tombstone_schema"]) is not int:
            raise ValueError("schema")
        if payload["target_collection"] != control or payload["data_collection"] + "__completions" != control:
            raise ValueError("pair")
        canonical_build_id(payload["build_id"])
        for key in ("logical_alias", "gen_fp", "corpus_fp", "created_at", "readers_stopped_at", "operator"):
            if not isinstance(payload[key], str) or not payload[key]:
                raise ValueError(key)
        if payload["state"] not in (STATE_RETIRING, STATE_RETIRED):
            raise ValueError("state")
        if (payload["state"] == STATE_RETIRED) != isinstance(payload["retired_at"], str):
            raise ValueError("retired_at")
        if payload["state"] == STATE_RETIRING and payload["retired_at"] is not None:
            raise ValueError("retired_at")
        if not isinstance(payload["plan_digest"], str) or not _DIGEST.fullmatch(payload["plan_digest"]):
            raise ValueError("plan_digest")
        if payload["data_points"] is not None and type(payload["data_points"]) is not int:
            raise ValueError("data_points")
        for key in ("recovery_ref", "tool_release"):
            if payload[key] is not None and not isinstance(payload[key], str):
                raise ValueError(key)
        snapshots = payload["snapshots"]
        if not isinstance(snapshots, list) or any(
            not isinstance(s, dict) or set(s) != _SNAPSHOT_FIELDS or not isinstance(s["name"], str) or not s["name"]
            for s in snapshots
        ):
            raise ValueError("snapshots")
        keep = payload["keep"]
        if (not isinstance(keep, dict) or set(keep) != {"n", "kept_build_ids", "below_keep_override"}
                or type(keep["n"]) is not int or keep["n"] < 1 or type(keep["below_keep_override"]) is not bool
                or not isinstance(keep["kept_build_ids"], list)):
            raise ValueError("keep")
        for kept in keep["kept_build_ids"]:
            canonical_build_id(kept)
        # Written with the `retired` update only: the run that finished, its
        # own reader-stop attestation, and any kept build that no longer
        # verified when a resumed run finished.
        completion = payload["completion"]
        if (payload["state"] == STATE_RETIRED) != isinstance(completion, dict):
            raise ValueError("completion")
        if isinstance(completion, dict):
            if (set(completion) != _COMPLETION_FIELDS or type(completion["resumed"]) is not bool
                    or not isinstance(completion["readers_stopped_at"], str)
                    or not isinstance(completion["operator"], str)
                    or not isinstance(completion["kept_unverified"], list)
                    or (completion["recovery_ref"] is not None
                        and not isinstance(completion["recovery_ref"], str))):
                raise ValueError("completion")
            for kept in completion["kept_unverified"]:
                canonical_build_id(kept)
    except (ValueError, TypeError, KeyError):
        raise InvalidTombstone("invalid retirement tombstone") from None
    return payload


def read_tombstone(client: QdrantReader, control: str) -> dict[str, Any] | None:
    """The tombstone stored in `control`, None when there is none (or no such
    collection). A point at the tombstone id that does not decode raises."""
    if not client.collection_exists(control):
        return None
    points = client.retrieve(control, ids=[tombstone_point_id(control)], with_payload=True)
    if not points:
        return None
    return decode_tombstone(points[0].payload or {}, control)


def holds_tombstone(client: QdrantReader, control: str) -> bool:
    """Whether any point sits at the tombstone id of `control`, valid or not.
    The publication guards use this: an undecodable tombstone still marks a
    control collection that must never be removed or reused."""
    if not client.collection_exists(control):
        return False
    return bool(client.retrieve(control, ids=[tombstone_point_id(control)], with_payload=False))


def tombstone_matches(tombstone: dict[str, Any], binding: BuildBinding) -> bool:
    """A tombstone vouches only for the exact build its control binds."""
    return (
        tombstone["build_id"] == binding.build_id
        and tombstone["logical_alias"] == binding.alias
        and tombstone["data_collection"] == binding.physical
        and tombstone["gen_fp"] == binding.gen_fp
        and tombstone["corpus_fp"] == binding.corpus_fp
    )
