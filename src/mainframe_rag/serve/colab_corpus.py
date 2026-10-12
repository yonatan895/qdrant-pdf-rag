"""Pure admission of trusted, privately produced full-corpus census evidence.

The caller independently supplies current sources and the resolved embedding,
recipe, tokenizer, golden and effective pooling-window contract. This module
reads no files and performs no discovery, parsing, tokenization or allocation.
It reconciles evidence; it cannot establish that a producer actually measured
the declared inputs or that retained Qdrant vectors satisfy their completions.

A retained binding is controller bookkeeping, not a database representation
schema. Unknown or changed bindings refuse admission. Only an explicit fresh
index/rebuild decision may initialize a ledger with production_progress_binding;
constructing that value itself grants no admission or migration permission.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, get_args

from mainframe_rag.ingest.classify import ChunkType

# The controller already bounds its inventory prerequisite at 16 MiB.
MAX_CENSUS_BYTES = 16 * 1024 * 1024
CHUNK_TYPES = get_args(ChunkType)
Object = dict[str, Any]


class CensusAdmissionError(ValueError):
    """Fixed diagnostics; never interpolate private paths, questions or text."""


@dataclass(frozen=True)
class Admission:
    progress_binding: Object
    progress_key: str
    census_sha256: str
    summary: Object
    producer: Object


def _object(value: Any, keys: set[str]) -> Object:
    if type(value) is not dict or set(value) != keys:
        raise CensusAdmissionError("census schema is incomplete or unsupported")
    return value


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CensusAdmissionError("census counts must be bounded nonnegative integers")
    return value


def _text(value: Any) -> str:
    if type(value) is not str or not value or "\0" in value:
        raise CensusAdmissionError("census identity is missing or invalid")
    return value


def _sha(value: Any, width: int = 64) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{" + str(width) + r"}", value) is None:
        raise CensusAdmissionError("census immutable identity is invalid")
    return value


def _path(value: Any) -> str:
    text = _text(value)
    path = PurePosixPath(text)
    if not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != text:
        raise CensusAdmissionError("census source path must be canonical and relative")
    return text


def _digest(value: Any) -> str:
    """Canonical JSON identity with the existing inventory byte ceiling."""
    digest = hashlib.sha256()
    size = 0
    try:
        chunks = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False).iterencode(value)
        for chunk in chunks:
            raw = chunk.encode("utf-8")
            size += len(raw)
            if size > MAX_CENSUS_BYTES:
                raise CensusAdmissionError("census metadata exceeds its bounded byte inventory")
            digest.update(raw)
    except CensusAdmissionError:
        raise
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise CensusAdmissionError("census metadata must be bounded finite JSON") from None
    return digest.hexdigest()


def _sources(value: Any) -> list[Object]:
    if type(value) is not list or not value:
        raise CensusAdmissionError("census requires the complete nonempty source set")
    result = []
    seen = set()
    for source in value:
        row = _object(source, {"relative_path", "bytes", "sha256"})
        path = _path(row["relative_path"])
        if path in seen:
            raise CensusAdmissionError("census repeats a source path")
        seen.add(path)
        result.append({"relative_path": path, "bytes": _integer(row["bytes"], 1),
                       "sha256": _sha(row["sha256"])})
    return sorted(result, key=lambda row: row["relative_path"])


def _contract(value: Any) -> Object:
    contract = _object(value, {
        "representation_fingerprint", "document_recipe_sha256", "source_labels",
        "embedding", "tokenizer", "counting_options", "golden_sha256",
        "query_recipe_sha256", "effective_window",
    })
    # The independent caller obtains this opaque value from the existing
    # completion.representation_fingerprint owner, not a second field projection.
    _text(contract["representation_fingerprint"])
    _sha(contract["document_recipe_sha256"])
    _text(contract["source_labels"])
    _sha(contract["golden_sha256"])
    _sha(contract["query_recipe_sha256"])
    _integer(contract["effective_window"], 1)
    embedding = _object(contract["embedding"], {"model", "revision", "dense_dim"})
    _text(embedding["model"])
    _sha(embedding["revision"], 40)
    _integer(embedding["dense_dim"], 1)
    tokenizer = _object(contract["tokenizer"], {"model", "revision", "implementation", "version", "files"})
    for name in ("model", "implementation", "version"):
        _text(tokenizer[name])
    _sha(tokenizer["revision"], 40)
    if (tokenizer["model"], tokenizer["revision"]) != (embedding["model"], embedding["revision"]):
        raise CensusAdmissionError("census tokenizer does not identify the embedding model")
    files = tokenizer["files"]
    if type(files) is not dict or not files:
        raise CensusAdmissionError("census tokenizer artifact inventory is missing")
    for name, artifact in files.items():
        _path(name)
        row = _object(artifact, {"bytes", "sha256"})
        _integer(row["bytes"], 1)
        _sha(row["sha256"])
    options = _object(contract["counting_options"], {"add_special_tokens", "do_lower_case", "truncation"})
    if any(type(value) is not bool for value in options.values()) or options["truncation"]:
        raise CensusAdmissionError("census counting options must prove nontruncating tokenization")
    return contract


def _expected(value: Any) -> tuple[list[Object], Object, int]:
    _digest(value)
    expected = _object(value, {"sources", "contract", "query_count"})
    return _sources(expected["sources"]), _contract(expected["contract"]), _integer(expected["query_count"], 1)


def _binding(sources: list[Object], contract: Object) -> Object:
    return {
        "schema": 1,
        "corpus_sha256": _digest(sources),
        "representation_fingerprint": contract["representation_fingerprint"],
        "document_recipe_sha256": contract["document_recipe_sha256"],
        "document_tokenization_sha256": _digest({
            "embedding": contract["embedding"], "tokenizer": contract["tokenizer"],
            "counting_options": contract["counting_options"],
        }),
        "source_labels": contract["source_labels"],
    }


def production_progress_binding(expected: Any) -> Object:
    """Construct bookkeeping for an independently authorized fresh/rebuilt index.

    This is neither evidence of a fresh index nor permission to initialize an
    unknown retained ledger. Golden/query changes and larger nontruncating
    windows require renewed admission, but do not change document checkpoints.
    """
    sources, contract, _ = _expected(expected)
    return _binding(sources, contract)


def _types(value: Any) -> dict[str, int]:
    row = _object(value, set(CHUNK_TYPES))
    return {name: _integer(row[name]) for name in CHUNK_TYPES}


def admit_production_census(census: Any, expected: Any, retained_binding: Any) -> Admission:
    """Reconcile complete evidence against independent current expectations.

    Query rows describe every effective expanded/split/prefixed embedding input
    of the declared query recipe. The trusted producer must measure the actual
    pooling options/window, which need not equal the server's max_model_len.
    """
    census_digest = _digest(census)
    sources, contract, query_count = _expected(expected)
    binding = _binding(sources, contract)
    if retained_binding is None:
        raise CensusAdmissionError("retained census binding is unknown; explicit fresh index or rebuild decision required")
    _digest(retained_binding)
    retained = _object(retained_binding, set(binding))
    if type(retained["schema"]) is not int or retained["schema"] != 1 or retained != binding:
        raise CensusAdmissionError("retained corpus or representation recipe changed; explicit fresh index or rebuild decision required")
    data = _object(census, {
        "schema", "status", "completed_at", "producer", "sources", "contract",
        "documents", "queries", "summary",
    })
    if type(data["schema"]) is not int or data["schema"] != 1 or data["status"] != "complete":
        raise CensusAdmissionError("census is incomplete or unsupported")
    completed = data["completed_at"]
    if (type(completed) not in (int, float) or completed <= 0
            or type(completed) is float and not math.isfinite(completed)):
        raise CensusAdmissionError("census completion attribution is invalid")
    producer = _object(data["producer"], {"development_sha", "source_sha256"})
    _sha(producer["development_sha"], 40)
    _sha(producer["source_sha256"])
    if _sources(data["sources"]) != sources or _contract(data["contract"]) != contract:
        raise CensusAdmissionError("census sources or embedding contract do not match current expectations")
    documents, queries = data["documents"], data["queries"]
    if type(documents) is not list or len(documents) != len(sources):
        raise CensusAdmissionError("census does not account for every source document")
    if type(queries) is not list or len(queries) != query_count:
        raise CensusAdmissionError("census does not account for every golden query")
    source_by_path = {source["relative_path"]: source for source in sources}
    seen_paths: set[str] = set()
    totals: Object = {
        "source_count": len(sources), "source_bytes": sum(row["bytes"] for row in sources),
        "document_count": len(documents), "pages": 0, "chunks": 0,
        "chunk_types": {name: 0 for name in CHUNK_TYPES}, "document_inputs": 0,
        "query_count": query_count, "query_inputs": 0,
        "max_document_tokens": 0, "max_query_tokens": 0,
        "failures": 0, "exclusions": 0, "truncated_inputs": 0,
    }
    for document in documents:
        row = _object(document, {
            "relative_path", "bytes", "sha256", "status", "pages", "chunks",
            "chunk_types", "embedding_inputs", "max_document_tokens",
        })
        path = _path(row["relative_path"])
        source = {"relative_path": path, "bytes": _integer(row["bytes"], 1), "sha256": _sha(row["sha256"])}
        if path in seen_paths or source_by_path.get(path) != source or row["status"] != "ok":
            raise CensusAdmissionError("census document source is missing, repeated or failed")
        seen_paths.add(path)
        pages, chunks = _integer(row["pages"], 1), _integer(row["chunks"], 1)
        inputs, maximum = _integer(row["embedding_inputs"], 1), _integer(row["max_document_tokens"], 1)
        types = _types(row["chunk_types"])
        if sum(types.values()) != chunks or inputs != chunks:
            raise CensusAdmissionError("census document chunk and embedding input counts disagree")
        totals["pages"] += pages
        totals["chunks"] += chunks
        totals["document_inputs"] += inputs
        totals["max_document_tokens"] = max(totals["max_document_tokens"], maximum)
        for name, count in types.items():
            totals["chunk_types"][name] += count
    seen_queries = set()
    for query in queries:
        row = _object(query, {"index", "embedding_inputs", "max_query_tokens"})
        index = _integer(row["index"])
        if index >= query_count or index in seen_queries:
            raise CensusAdmissionError("census golden query is missing or repeated")
        seen_queries.add(index)
        totals["query_inputs"] += _integer(row["embedding_inputs"], 1)
        totals["max_query_tokens"] = max(totals["max_query_tokens"], _integer(row["max_query_tokens"], 1))
    summary = _object(data["summary"], set(totals))
    for name in totals:
        if name == "chunk_types":
            _types(summary[name])
        else:
            _integer(summary[name])
    if summary != totals:
        raise CensusAdmissionError("census aggregate counts, failures or maxima do not reconcile")
    if max(totals["max_document_tokens"], totals["max_query_tokens"]) > contract["effective_window"]:
        raise CensusAdmissionError("measured embedding inputs exceed the effective pooling window")
    return Admission(binding, _digest(binding), census_digest, totals, dict(producer))
