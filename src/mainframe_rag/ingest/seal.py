"""Stored-content certificate for immutable builds; never an authorization grant.

The caller holds the target writer lock and proves intended membership before
capture. Read-back includes all payloads/vectors, not just completion metadata.
Only the exact publication receipt ID is excluded from controls (self-reference).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from qdrant_client import models

if TYPE_CHECKING:
    from mainframe_rag.ports import QdrantPoints

SEAL_SCHEMA = 1


def decode_content_seal(value: object) -> dict:
    """An absent field is handled by the caller; a present invalid field refuses."""
    if not isinstance(value, dict) or set(value) != {"schema", "data", "control"}:
        raise ValueError("invalid content seal")
    if type(value["schema"]) is not int or value["schema"] != SEAL_SCHEMA:
        raise ValueError("unsupported content seal")
    for role in ("data", "control"):
        part = value[role]
        if not isinstance(part, dict) or set(part) != {"count", "sha256"}:
            raise ValueError("invalid content seal member")
        if type(part["count"]) is not int or part["count"] < 0:
            raise ValueError("invalid content seal count")
        if not isinstance(part["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", part["sha256"]):
            raise ValueError("invalid content seal digest")
    return value


def _json(value: Any) -> bytes:
    # No Unicode normalization, float rounding, list sorting or field omission.
    # Qdrant returns the stored numeric representation; NaN/Infinity are invalid.
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def _point_digest(point: Any) -> bytes:
    vectors = point.vector
    if not isinstance(point.payload, dict) or not isinstance(vectors, dict) or not vectors:
        raise ValueError("content seal requires full payload and named vectors")
    projected = {
        name: vector.model_dump(mode="json") if isinstance(vector, models.SparseVector) else vector
        for name, vector in vectors.items()
    }
    return hashlib.sha256(_json([point.id, point.payload, projected])).digest()


def _collection_digest(
    client: QdrantPoints, collection: str, binding: list[str], role: str, excluded_id: str | None,
) -> dict:
    # Keep only IDs and fixed-size hashes across pages; never retain all corpus
    # text/vectors. Sorted IDs make the certificate independent of page order.
    leaves: dict[str, bytes] = {}
    offsets: set[str] = set()
    offset = None
    while True:
        records, next_offset = client.scroll(
            collection, limit=256, offset=offset, with_payload=False,
        )
        # Use the existing points port's full retrieve projection, as completion
        # verification does. Never depend on scroll's default vector projection.
        ids: list[str] = []
        for record in records:
            if not isinstance(record.id, str):
                raise TypeError("content seal requires application string point IDs")
            ids.append(record.id)
        points = client.retrieve(collection, ids, with_payload=True, with_vectors=True) if ids else []
        if len(set(ids)) != len(ids) or sorted(str(p.id) for p in points) != sorted(ids):
            raise ValueError("incomplete content seal projection")
        for point in points:
            # Application data/control identities are strings, never coerced.
            if not isinstance(point.id, str):
                raise TypeError("invalid content seal point identity")
            identity = _json(point.id).decode("utf-8")
            if point.id == excluded_id:
                continue
            if identity in leaves:
                raise ValueError("duplicate content seal point")
            leaves[identity] = _point_digest(point)
        if next_offset is None:
            break
        token = _json(next_offset).decode("utf-8")
        if not records or token in offsets:
            raise ValueError("stalled content seal scan")
        offsets.add(token)
        offset = next_offset
    root = hashlib.sha256(_json(["build-content-seal", SEAL_SCHEMA, binding, role, len(leaves)]))
    for identity in sorted(leaves):
        # Fixed-width leaf hashes already commit to the exact typed ID.
        root.update(leaves[identity])
    return {"count": len(leaves), "sha256": root.hexdigest()}


def capture_content_seal(
    client: QdrantPoints, *, build_id: str, alias: str, physical: str,
    gen_fp: str, corpus_fp: str, receipt_id: str,
) -> dict:
    """Capture verified immutable bytes, bound to the complete build identity."""
    binding = [build_id, alias, physical, gen_fp, corpus_fp]
    return {
        "schema": SEAL_SCHEMA,
        "data": _collection_digest(client, physical, binding, "data", None),
        "control": _collection_digest(client, physical + "__completions", binding, "control", receipt_id),
    }
