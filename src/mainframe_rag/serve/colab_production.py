"""Checked host expectations for the conservative native Colab workload.

No PDF parsing, tokenizer/model loading, network, allocation or database writes.
The caller supplies approved inference pins and a separately approved private
pooling producer identity; an evidence file cannot approve its own provenance.
That producer must actually observe the named runtime against these artifacts.
Hashes establish identity, not truthful measurement or stored-vector validity.

This admits census/budget evidence, never the live owner's production acceptance
case count or quality result. Native publication/completion gates remain required.
Corpus bytes must remain unchanged after verification. Only the native unmodified
local:stack invocation is supported; arbitrary wrappers cannot be certified.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import httpx2

from mainframe_rag.config import Settings
from mainframe_rag.eval.datasets import (
    HOLDOUT_FILENAME,
    RELEASE_SET_FILENAME,
    parse_golden_text,
)
from mainframe_rag.ingest.completion import representation_fingerprint, source_labels
from mainframe_rag.ingest.embed import VllmEmbedder
from mainframe_rag.ingest.rules_version import hash_rule_files, rule_module_paths
from mainframe_rag.ingest.walk import _IGNORED_DIRS
from mainframe_rag.serve.colab_corpus import (
    MAX_CENSUS_BYTES,
    CensusAdmissionError,
    admit_production_census,
)
from mainframe_rag.serve.colab_profiles import build_profile

LEDGER_FILENAME = "retained-index-binding.json"
SIDECAR_FILENAME = "production-expected.json"
MANIFEST_BYTES = 64 * 1024
# Match the existing remote snapshot metadata envelope, never tensor weights.
ARTIFACT_FILE_BYTES = 32 * 1024 * 1024
ARTIFACT_TOTAL_BYTES = 64 * 1024 * 1024
ARTIFACT_FILES = 64
ARTIFACT_ENTRIES = 256
ARTIFACT_SUFFIXES = {".json", ".txt", ".model", ".tiktoken", ".jinja"}
RECIPE_FILE_BYTES = 4 * 1024 * 1024
NATIVE_WORKLOAD = ("sh", "scripts/tools/run-task.sh", "local:stack")
Object = dict[str, Any]
Clock = Callable[[], float]
QueryInputs = Callable[[Settings, str], Sequence[str]]

# Explicitly exported values override ambient environment/.env in consumers.
# Secrets, routing URLs and unrelated answer/ranking settings are not persisted.
_SETTINGS = (
    "embed_mode", "embed_model", "embed_model_revision", "dense_dim", "qdrant_collection",
    "bm25_model", "bm25_weights_revision", "bm25_cache_dir", "dense_query_prefix", "acronym_expansion_enabled",
    "comparative_split_enabled", "diagnostic_dualpath_enabled", "contextual_embed_enabled",
    "context_llm_model", "context_max_chars", "embed_max_input_chars", "ingest_max_pdf_bytes",
    "ingest_max_doc_pages", "ingest_max_doc_chunks", "ingest_alias_publish", "query_max_chars",
)


class ProductionAdmissionError(ValueError):
    """Fixed diagnostics, never private paths, questions or upstream messages."""


@dataclass(frozen=True)
class ScanBounds:
    """Caller-selected hard ceilings; no automatic corpus-dependent defaults."""

    max_entries: int
    max_bytes: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in
               (self.max_entries, self.max_bytes)):
            raise ProductionAdmissionError("source scan requires explicit positive entry and byte bounds")


@dataclass(frozen=True)
class ProductionExpectation:
    expected: Object
    environment: Mapping[str, str]
    query_input_counts: tuple[int, ...]
    prerequisites: Object
    runtime: Object
    corpus_root: str
    bounds: ScanBounds
    deadline_seconds: int
    recipe_root: str

    @property
    def unset_environment(self) -> tuple[str, ...]:
        return tuple(name.upper() for name in _SETTINGS if name.upper() not in self.environment)


@dataclass(frozen=True)
class PreparedProduction:
    manifest: Object
    environment: Mapping[str, str]
    sidecar: Path
    sidecar_sha256: str
    ledger: Path
    ledger_sha256: str
    data_state: Path

    @property
    def unset_environment(self) -> tuple[str, ...]:
        return tuple(name.upper() for name in _SETTINGS if name.upper() not in self.environment)


@dataclass
class _Scan:
    bounds: ScanBounds
    deadline: float
    clock: Clock
    entries: int = 0
    consumed: int = 0

    def check(self) -> None:
        now = self.clock()
        if (type(self.deadline) not in (int, float) or not math.isfinite(self.deadline)
                or type(now) not in (int, float) or not math.isfinite(now)
                or now >= self.deadline):
            raise ProductionAdmissionError("production input verification deadline expired or invalid")

    def entry(self) -> None:
        self.check()
        self.entries += 1
        if self.entries > self.bounds.max_entries:
            raise ProductionAdmissionError("source discovery exceeded its explicit entry bound")

    def consume(self, size: int) -> None:
        self.check()
        self.consumed += size
        if self.consumed > self.bounds.max_bytes:
            raise ProductionAdmissionError("source stream exceeded its explicit byte bound")


def _object(value: Any, keys: set[str]) -> Object:
    if type(value) is not dict or set(value) != keys:
        raise ProductionAdmissionError("production evidence schema is incomplete or unsupported")
    return value


def _integer(value: Any, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ProductionAdmissionError("production evidence counts are invalid")
    return value


def _sha(value: Any, width: int = 64) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{" + str(width) + "}", value) is None:
        raise ProductionAdmissionError("production evidence immutable identity is invalid")
    return value


def _text(value: Any) -> str:
    if type(value) is not str or not value or "\0" in value:
        raise ProductionAdmissionError("production evidence identity is missing or invalid")
    return value


def _relative(value: Any) -> str:
    text = _text(value)
    path = PurePosixPath(text)
    if not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != text:
        raise ProductionAdmissionError("production artifact/source path is not canonical and relative")
    return text


def _bytes(value: Any, limit: int = MAX_CENSUS_BYTES) -> bytes:
    pieces = []
    size = 0
    for part in json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False).iterencode(value):
        raw = part.encode("utf-8")
        size += len(raw)
        if size > limit:
            raise ProductionAdmissionError("production metadata exceeded its bounded envelope")
        pieces.append(raw)
    return b"".join(pieces)


def _digest(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> Object:
    result: Object = {}
    for key, value in pairs:
        if key in result:
            raise ProductionAdmissionError("production JSON repeats a field")
        result[key] = value
    return result


def _read(path: Path, limit: int, scan: _Scan | None = None) -> bytes:
    if scan:
        scan.check()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ProductionAdmissionError("production input must be a bounded regular file")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > limit or _stamp(before) != _stamp(after):
        raise ProductionAdmissionError("production input grew or changed during verification")
    if scan:
        scan.check()
    return raw


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _json(raw: bytes) -> Any:
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(
                          ProductionAdmissionError("production JSON must contain finite values")))


def _record(path: Path, raw: bytes) -> Object:
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _hash_file(path: Path, size: int, scan: _Scan, *, source: bool) -> str:
    scan.check()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise ProductionAdmissionError("source or artifact size/type differs from independent inventory")
        digest = hashlib.sha256()
        consumed = 0
        while raw := stream.read(min(65536, size - consumed + 1)):
            scan.check()
            consumed += len(raw)
            if consumed > size:
                raise ProductionAdmissionError("source or artifact grew during bounded verification")
            if source:
                scan.consume(len(raw))
            digest.update(raw)
        after = os.fstat(stream.fileno())
    if consumed != size or _stamp(before) != _stamp(after):
        raise ProductionAdmissionError("source or artifact changed during verification")
    return digest.hexdigest()


def _sources(path: Path, root: Path, scan: _Scan) -> tuple[list[Object], Object]:
    raw = _read(path, MAX_CENSUS_BYTES, scan)
    data = _json(raw)
    if type(data) is not dict or data.get("source_root") != str(root):
        raise ProductionAdmissionError("independent source inventory does not bind this canonical corpus root")
    rows = data.get("source_files")
    if type(rows) is not list or not rows:
        raise ProductionAdmissionError("independent source inventory is missing")
    if len(rows) > scan.bounds.max_entries:
        raise ProductionAdmissionError("source inventory exceeds the explicit entry bound")
    result: list[Object] = []
    paths: set[str] = set()
    hashes: set[str] = set()
    for item in rows:
        scan.check()
        row = _object(item, {"relative_path", "bytes", "sha256"})
        name, size, digest = _relative(row["relative_path"]), _integer(row["bytes"]), _sha(row["sha256"])
        if name in paths or digest in hashes:
            raise ProductionAdmissionError("source inventory repeats a path or unsupported duplicate-byte source")
        paths.add(name)
        hashes.add(digest)
        result.append({"relative_path": name, "bytes": size, "sha256": digest})
    if (_integer(data.get("source_count")) != len(result)
            or _integer(data.get("source_unique_hashes")) != len(hashes)
            or _integer(data.get("source_bytes")) != sum(row["bytes"] for row in result)):
        raise ProductionAdmissionError("independent source inventory counts disagree")
    return sorted(result, key=lambda row: row["relative_path"]), _record(path, raw)


def _verify_sources(root: Path, sources: list[Object], scan: _Scan) -> None:
    if sum(row["bytes"] for row in sources) > scan.bounds.max_bytes:
        raise ProductionAdmissionError("source inventory exceeds the explicit stream byte bound")
    expected = {row["relative_path"]: row for row in sources}
    observed: set[str] = set()
    pending = [root]
    while pending:
        scan.check()
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                scan.entry()
                # Native walk owner ignores these names below the corpus root.
                if entry.name.startswith(".") or entry.name in _IGNORED_DIRS:
                    continue
                if entry.is_symlink():
                    raise ProductionAdmissionError("source discovery does not support visible symlinks")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() == ".pdf":
                    path = Path(entry.path)
                    name = path.relative_to(root).as_posix()
                    row = expected.get(name)
                    if row is None or name in observed:
                        raise ProductionAdmissionError("actual source set differs from independent inventory")
                    if _hash_file(path, row["bytes"], scan, source=True) != row["sha256"]:
                        raise ProductionAdmissionError("actual source hash differs from independent inventory")
                    observed.add(name)
    if observed != set(expected):
        raise ProductionAdmissionError("actual source set is missing an independently inventoried document")


def _capture_inputs(settings: Settings, texts: list[str], *, query: bool) -> list[str]:
    captured: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if set(body) != {"model", "input"} or body["model"] != settings.embed_model:
            raise ProductionAdmissionError("native embedding request options are unsupported")
        captured.extend(body["input"])
        return httpx2.Response(200, json={"data": [
            {"index": index, "embedding": []} for index in range(len(body["input"]))]})

    # Real client/request producer, in-memory transport only; never expose keys.
    routing = settings.model_copy(update={"embed_base_url": "http://capture.invalid/v1",
                                          "embed_api_key": None})
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        embedder = VllmEmbedder(routing, client=client)
        if query:
            embedder.dense_query(texts)
        else:
            embedder.dense(texts)
    return captured


def native_query_inputs(settings: Settings, query: str) -> Sequence[str]:
    """Capture native split/expanded/prefixed strings without inference or tokens."""
    from mainframe_rag.retrieve.query import _effective_query, _split_paths_for

    legs, _ = _split_paths_for(settings, query)
    return _capture_inputs(settings, [_effective_query(settings, leg) for leg in legs], query=True)


def _profile(profile: Object) -> tuple[Object, int]:
    servers = profile["servers"]
    if type(servers) is not list or len(servers) != 2 or servers[1].get("role") != "embed":
        raise ProductionAdmissionError("production requires the supported native two-model profile")
    server = servers[1]
    argv = server["argv"]
    if type(argv) is not list or argv.count("--max-model-len") != 1:
        raise ProductionAdmissionError("embedding server window flags are unsupported")
    window = int(argv[argv.index("--max-model-len") + 1])
    wanted = build_profile(profile["name"], embed_max_len=window,
                           deadline_seconds=profile["deadline_seconds"])
    current = {key: value for key, value in profile.items()
               if key not in ("inference_lock_sha256", "expires_at")}
    if not wanted["accelerator"] or _digest(current) != _digest(wanted):
        raise ProductionAdmissionError("embedding profile/argv differs from its native owner")
    return server, window


def _settings(settings: Settings, server: Object, profile: Object) -> Object:
    if not isinstance(settings, Settings):
        raise ProductionAdmissionError("production requires resolved native Settings")
    if (settings.embed_mode != "vllm" or settings.embed_model != server["model_id"]
            or settings.embed_model_revision != server["revision"]
            or settings.require_dense_dim() != profile["dense_dim"]):
        raise ProductionAdmissionError("resolved embedding Settings do not match the selected profile")
    if settings.contextual_embed_enabled or settings.embed_max_input_chars:
        raise ProductionAdmissionError("contextual or character-bounded document recipes need unsupported census evidence")
    if not settings.ingest_alias_publish:
        raise ProductionAdmissionError("production requires native publication gates")
    return {name: getattr(settings, name) for name in _SETTINGS}


def _environment(values: Object) -> Mapping[str, str]:
    # Optional None must stay absent. Exporting "" changes native Settings'
    # optional-string values and can change rp2 even with contextual mode off.
    return MappingProxyType({name.upper(): (str(value).lower() if type(value) is bool else str(value))
                             for name, value in values.items() if value is not None})


def _artifact_set(directory: Path, files: Object, scan: _Scan) -> None:
    # Optional tokenizer/config files also affect loading. A subset inventory
    # cannot establish the named producer's inputs. This metadata-only bundle
    # has no tensors or caches, and later matches the remote metadata set exactly.
    pending, observed, entry_count = [directory], set(), 0
    while pending:
        scan.check()
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                scan.check()
                entry_count += 1
                if entry_count > ARTIFACT_ENTRIES or entry.is_symlink():
                    raise ProductionAdmissionError("embedding artifact discovery exceeded its bounded supported envelope")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    name = Path(entry.path).relative_to(directory).as_posix()
                    if name != "manifest.json":
                        observed.add(name)
                else:
                    raise ProductionAdmissionError("embedding artifact bundle contains an unsupported entry")
    if observed != set(files):
        raise ProductionAdmissionError("actual embedding artifact coverage differs from its complete inventory")


def _embedding_evidence(directory: Path, server: Object, window: int, profile: Object,
                        pins: Mapping[str, str], producer: Mapping[str, str], scan: _Scan
                        ) -> tuple[Object, Object, Object]:
    raw = _read(directory / "manifest.json", MANIFEST_BYTES, scan)
    data = _object(_json(raw), {"schema", "model", "tokenizer", "runtime", "server_argv",
                               "files", "observation", "provenance"})
    if type(data["schema"]) is not int or data["schema"] != 1:
        raise ProductionAdmissionError("embedding evidence schema is unsupported")
    approved = _object(dict(producer), {"development_sha", "source_sha256"})
    _sha(approved["development_sha"], 40)
    _sha(approved["source_sha256"])
    runtime = {name: _text(pins[name]) for name in ("vllm", "transformers", "tokenizers", "torch")}
    if (runtime["vllm"] != profile["vllm_version"]
            or runtime["transformers"] != profile["transformers_version"]
            or runtime["vllm"] != "0.28.0" or runtime["transformers"] != "5.15.0"
            or _digest(data["runtime"]) != _digest(runtime)
            or _digest(data["server_argv"]) != _digest(server["argv"])
            or data["model"] != {"model": server["model_id"], "revision": server["revision"]}
            or data["tokenizer"] != {"implementation": "transformers.Qwen2TokenizerFast",
                                      "version": runtime["transformers"]}):
        raise ProductionAdmissionError("embedding runtime/model/tokenizer/flags evidence is unsupported or mismatched")
    files = data["files"]
    if type(files) is not dict or not 1 <= len(files) <= ARTIFACT_FILES:
        raise ProductionAdmissionError("embedding artifact inventory is incomplete or oversized")
    _artifact_set(directory, files, scan)
    total = 0
    records = {"manifest": _record(directory / "manifest.json", raw)}
    for name, item in files.items():
        name = _relative(name)
        row = _object(item, {"bytes", "sha256"})
        size, digest = _integer(row["bytes"]), _sha(row["sha256"])
        total += size
        if (size > ARTIFACT_FILE_BYTES or total > ARTIFACT_TOTAL_BYTES
                or Path(name).suffix not in ARTIFACT_SUFFIXES):
            raise ProductionAdmissionError("embedding artifacts exceed the supported metadata envelope")
        path = directory / name
        if path.resolve(strict=True) != path or _hash_file(path, size, scan, source=False) != digest:
            raise ProductionAdmissionError("embedding artifact bytes or paths differ from independent evidence")
        records[name] = {"path": str(path), "bytes": size, "sha256": digest}
    required = {"tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt",
                "config.json", "sentence_bert_config.json", "modules.json"}
    if not required <= set(files):
        raise ProductionAdmissionError("embedding tokenizer/model/encoder configuration coverage is incomplete")
    model = _json(_read(directory / "config.json", MANIFEST_BYTES, scan))
    token = _json(_read(directory / "tokenizer_config.json", MANIFEST_BYTES, scan))
    encoder = _json(_read(directory / "sentence_bert_config.json", MANIFEST_BYTES, scan))
    modules = _json(_read(directory / "modules.json", MANIFEST_BYTES, scan))
    if (type(model) is not dict or type(token) is not dict or type(encoder) is not dict
            or model.get("model_type") != "qwen3"
            or model.get("architectures") != ["Qwen3ForCausalLM"]
            or model.get("auto_map") or token.get("auto_map")
            or model.get("rope_scaling") or model.get("rope_parameters")
            or model.get("use_sliding_window")
            or token.get("tokenizer_class") not in {"Qwen2Tokenizer", "Qwen2TokenizerFast"}
            or type(encoder.get("do_lower_case")) is not bool
            or window > min(_integer(model.get("max_position_embeddings")),
                            _integer(encoder.get("max_seq_length")))):
        raise ProductionAdmissionError("model/encoder/tokenizer configuration has unsupported runtime semantics")
    if (type(modules) is not list or len(modules) != 3
            or [module.get("type") for module in modules] != [
                "sentence_transformers.models.Transformer", "sentence_transformers.models.Pooling",
                "sentence_transformers.models.Normalize"]
            or modules[0].get("path") != ""):
        raise ProductionAdmissionError("embedding module/configuration recipe is unsupported")
    pooling_name = _relative(modules[1]["path"]) + "/config.json"
    if pooling_name not in files:
        raise ProductionAdmissionError("embedding pooling configuration coverage is incomplete")
    pooling = _json(_read(directory / pooling_name, MANIFEST_BYTES, scan))
    if (type(pooling) is not dict or pooling.get("word_embedding_dimension") != profile["dense_dim"]
            or pooling.get("pooling_mode_lasttoken") is not True
            or any(value is not False for key, value in pooling.items()
                   if key.startswith("pooling_mode_") and key != "pooling_mode_lasttoken")):
        raise ProductionAdmissionError("embedding pooling configuration is unsupported")
    # Supported v0.28 plain-string completion request: special tokens are on;
    # lowercasing comes from checked encoder config. No chunked/max_embed_len
    # overrides exist in the exact native argv. This is not a general vLLM parser.
    options = {"add_special_tokens": True, "do_lower_case": encoder["do_lower_case"], "truncation": False}
    observation = {
        "implementation": "vllm.entrypoints.pooling.embed.protocol.EmbeddingCompletionRequest",
        "version": runtime["vllm"],
        "model_config": {"max_model_len": window, "encoder_config": encoder,
                         "pooler_config": {"enable_chunked_processing": False, "max_embed_len": None,
                                           "seq_pooling_type": "LAST", "use_activation": True}},
        "counting_options": options, "effective_window": window,
    }
    provenance = {"kind": "offline-vllm-pooling-observation", "producer": approved,
                  "resolved_revision": server["revision"], "files_sha256": _digest(files),
                  "runtime_sha256": _digest(runtime), "server_argv_sha256": _digest(server["argv"])}
    if (_digest(data["observation"]) != _digest(observation)
            or _digest(data["provenance"]) != _digest(provenance)):
        raise ProductionAdmissionError("approved pooling observation/provenance does not match checked runtime semantics")
    contract = {"tokenizer": {"model": server["model_id"], "revision": server["revision"],
                              **data["tokenizer"], "files": files},
                "counting_options": options, "effective_window": window}
    return contract, records, {"pins": runtime, "server_argv": server["argv"],
                               "metadata_sha256": {name: row["sha256"] for name, row in files.items()}}


def _recipes(root: Path, corpus: Path, values: Object, runtime: Object, scan: _Scan
             ) -> tuple[str, str, str, Object]:
    package = root / "src/mainframe_rag"
    loaded_package = Path(__file__).resolve().parents[1]
    rules = tuple(package / path.relative_to(loaded_package) for path in rule_module_paths())
    records: Object = {}

    def read(path: Path, *, native: bool = False) -> bytes:
        raw = _read(path, RECIPE_FILE_BYTES, scan)
        if native:
            loaded = loaded_package / path.relative_to(package)
            if loaded != path and raw != _read(loaded, RECIPE_FILE_BYTES, scan):
                raise ProductionAdmissionError("prepared candidate differs from the native expectation producers")
        records[str(path)] = _record(path, raw)
        return raw

    for path in rules:
        read(path, native=True)
    rules_v = hash_rule_files(rules)  # Native identity owner, no second hash scheme.
    scan.check()
    # These owners are exercised in this process. They are per-run prerequisites,
    # not blanket source-SHA document compatibility keys.
    for name in ("config.py", "ingest/completion.py", "ingest/representation.py",
                 "ingest/context.py", "ingest/rules_version.py", "eval/datasets.py"):
        read(package / name, native=True)
    embed = ast.parse(read(package / "ingest/embed.py", native=True))
    selected = []
    query_method = None
    functions = {"build_embed_text", "chunk_embed_text", "embed_batch", "build_embedder", "_bm25_model"}
    methods = {"__init__", "_resolve", "_http", "dense", "sparse"}
    for node in embed.body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            selected.append(ast.dump(node, include_attributes=False))
            functions.remove(node.name)
        if isinstance(node, ast.ClassDef) and node.name == "VllmEmbedder":
            for method in node.body:
                if isinstance(method, ast.FunctionDef) and method.name in methods:
                    selected.append(ast.dump(method, include_attributes=False))
                    methods.remove(method.name)
                elif isinstance(method, ast.FunctionDef) and method.name == "dense_query":
                    query_method = ast.dump(method, include_attributes=False)
    if functions or methods or query_method is None:
        raise ProductionAdmissionError("native embedding recipe source boundary changed; explicit review required")
    # Conservative closure: whole native ingest owner and application lock can
    # reject control-only changes there. Controller/query source is excluded.
    document = {
        "schema": 1, "corpus_root": str(corpus), "embedding_functions": selected,
        "ingest_owner_sha256": hashlib.sha256(read(package / "ingest/run_ingest.py")).hexdigest(),
        "bounds_owner_sha256": hashlib.sha256(read(package / "ingest/bounds.py", native=True)).hexdigest(),
        "application_lock_sha256": hashlib.sha256(read(root / "requirements.lock.txt")).hexdigest(),
        "runtime": runtime["pins"],
    }
    from mainframe_rag.retrieve.rewrite import ACRONYM_GLOSSARY_VERSION

    query_sources = {}
    for name in ("retrieve/query.py", "retrieve/rewrite.py", "retrieve/split.py",
                 "retrieve/filters.py", "retrieve/screen.py", "regexes.py",
                 f"retrieve/acronyms_{ACRONYM_GLOSSARY_VERSION}.json"):
        query_sources[name] = hashlib.sha256(read(package / name, native=True)).hexdigest()
    query = {"schema": 1, "sources": query_sources, "dense_query": query_method,
             "settings": {name: values[name] for name in (
                 "dense_query_prefix", "acronym_expansion_enabled", "comparative_split_enabled",
                 "diagnostic_dualpath_enabled", "query_max_chars")}}
    return rules_v, _digest(document), _digest(query), records


def _workload(workload: Any) -> None:
    if workload is None or workload == [] or workload == ():
        return
    if type(workload) not in (list, tuple) or tuple(workload) != NATIVE_WORKLOAD:
        raise ProductionAdmissionError("production admission supports only the native workload without wrappers or overrides")


def build_production_expected(
    config: Mapping[str, Any], profile: Object, *, settings: Settings,
    runtime_pins: Mapping[str, str], pooling_producer: Mapping[str, str], recipe_root: Path,
    bounds: ScanBounds, deadline: float, clock: Clock = time.monotonic,
    query_inputs: QueryInputs = native_query_inputs,
) -> ProductionExpectation:
    """Verify actual files and current native contracts; never read census claims.

    ``runtime_pins`` must come from the validated approved inference lock, and
    ``pooling_producer`` from a separate review/approval. ``recipe_root`` must be
    the host's exact prepared candidate. The clock and input-capture callable are
    injectable adapters; ordinary host callers retain native_query_inputs.
    Entry/aggregate-source-byte ceilings and an absolute monotonic deadline are
    mandatory. The deadline cannot exceed the selected profile's finite envelope.
    """
    try:
        _workload(config.get("workload"))
        scan = _Scan(bounds, deadline, clock)
        scan.check()
        server, window = _profile(profile)
        if deadline - clock() > profile["deadline_seconds"]:
            raise ProductionAdmissionError("source verification deadline exceeds the selected finite profile")
        values = _settings(settings, server, profile)
        _capture_inputs(settings, ["Original contract input."], query=False)
        corpus = Path(config["corpus"]).resolve(strict=True)
        if not corpus.is_dir():
            raise ProductionAdmissionError("production source root is unavailable")
        sources, source_record = _sources(Path(config["source_inventory"]).resolve(strict=True), corpus, scan)
        _verify_sources(corpus, sources, scan)
        golden = Path(config["golden"])
        if golden.name in {HOLDOUT_FILENAME, RELEASE_SET_FILENAME} or golden.resolve().name in {
                HOLDOUT_FILENAME, RELEASE_SET_FILENAME}:
            raise ProductionAdmissionError("protected datasets require their native authorization/pin owner; unsupported here")
        golden = golden.resolve(strict=True)
        evidence, artifacts, runtime = _embedding_evidence(
            Path(config["embedding_evidence"]).resolve(strict=True), server, window, profile,
            runtime_pins, pooling_producer, scan)
        recipe_root = recipe_root.resolve(strict=True)
        rules_v, document_recipe, query_recipe, recipes = _recipes(
            recipe_root, corpus, values, runtime, scan)
        golden_raw = _read(golden, MAX_CENSUS_BYTES, scan)
        try:
            entries = parse_golden_text(golden_raw.decode("utf-8"))
        except (SystemExit, ValueError, UnicodeError):
            raise ProductionAdmissionError("production golden entries are invalid") from None
        if not entries:
            raise ProductionAdmissionError("production golden entries are empty")
        counts = []
        for entry in entries:
            scan.check()
            if len(entry.query) > settings.query_max_chars:
                raise ProductionAdmissionError("production golden query exceeds the native request bound")
            texts = query_inputs(settings, entry.query)
            if (type(texts) not in (list, tuple) or not texts
                    or any(type(text) is not str or not text for text in texts)):
                raise ProductionAdmissionError("native effective query inputs are invalid or unsupported")
            _bytes(texts)
            counts.append(len(texts))
        contract = {
            "representation_fingerprint": representation_fingerprint(settings, rules_v),
            "document_recipe_sha256": document_recipe, "source_labels": source_labels(None, None, None),
            "embedding": {"model": server["model_id"], "revision": server["revision"],
                          "dense_dim": profile["dense_dim"]},
            **evidence, "golden_sha256": hashlib.sha256(golden_raw).hexdigest(),
            "query_recipe_sha256": query_recipe,
        }
        expected = {"sources": sources, "contract": contract, "query_count": len(entries)}
        _bytes(expected)
        return ProductionExpectation(
            expected, _environment(values), tuple(counts),
            {"source_inventory": source_record, "golden": _record(golden, golden_raw),
             "artifacts": artifacts, "recipes": recipes}, runtime, str(corpus), bounds,
            profile["deadline_seconds"], str(recipe_root))
    except ProductionAdmissionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError,
            RuntimeError, RecursionError):
        raise ProductionAdmissionError("production expectation inputs are unavailable or invalid") from None


def _private_directory(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
        raise ProductionAdmissionError("production retained/run state must be an existing private directory")
    return path.resolve(strict=True)


def _ledger(path: Path, namespace: str, collection: str) -> tuple[Object, bytes]:
    if not path.exists():
        raise ProductionAdmissionError("retained binding is unknown; explicit fresh index or rebuild decision required")
    if path.stat().st_mode & 0o077:
        raise ProductionAdmissionError("retained binding must remain private")
    raw = _read(path, MANIFEST_BYTES)
    data = _object(_json(raw), {"schema", "target", "progress_binding"})
    target = _object(data["target"], {"namespace", "claim_uid", "pv_uid", "storage_path", "logical_collection"})
    if (type(data["schema"]) is not int or data["schema"] != 1
            or any(type(value) is not str or not value or "\0" in value for value in target.values())
            or target["namespace"] != namespace or target["logical_collection"] != collection
            or target["storage_path"] != "/qdrant/storage"):
        raise ProductionAdmissionError("retained target binding is unsupported or mismatched")
    return data, raw


def _write_sidecar(path: Path, value: Object) -> bytes:
    raw = _bytes(value)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return raw


def prepare_production(
    config: Mapping[str, Any], profile: Object, *, state: Path, settings: Settings,
    runtime_pins: Mapping[str, str], pooling_producer: Mapping[str, str],
    census_producer: Mapping[str, str], recipe_root: Path,
    bounds: ScanBounds, deadline: float, clock: Clock = time.monotonic,
    query_inputs: QueryInputs = native_query_inputs,
) -> PreparedProduction:
    """Reconcile complete census and existing ledger, then write one private sidecar.

    Never initializes/changes a ledger or progress inventory. A caller must later
    reverify inputs and bind the actual storage proof before allocation, and match
    the observed runtime report before starting the admitted workload.
    """
    try:
        data_state = _private_directory(Path(config["data_state"]))
        state = _private_directory(state)
        ledger_path = data_state / LEDGER_FILENAME
        ledger, ledger_raw = _ledger(ledger_path, _text(config["namespace"]), settings.qdrant_collection)
        expected = build_production_expected(
            config, profile, settings=settings, runtime_pins=runtime_pins,
            pooling_producer=pooling_producer, recipe_root=recipe_root, bounds=bounds,
            deadline=deadline, clock=clock, query_inputs=query_inputs)
        census_path = Path(config["inventory"]).resolve(strict=True)
        census_raw = _read(census_path, MAX_CENSUS_BYTES, _Scan(bounds, deadline, clock))
        census = _json(census_raw)
        approved_census = _object(dict(census_producer), {"development_sha", "source_sha256"})
        _sha(approved_census["development_sha"], 40)
        _sha(approved_census["source_sha256"])
        if type(census) is not dict or census.get("producer") != approved_census:
            raise ProductionAdmissionError("complete census producer lacks the independent approved identity")
        admission = admit_production_census(census, expected.expected, ledger["progress_binding"])
        query_counts = {row["index"]: row["embedding_inputs"] for row in census["queries"]}
        if tuple(query_counts[index] for index in range(len(expected.query_input_counts))) != expected.query_input_counts:
            raise ProductionAdmissionError("census query input counts differ from native golden query producers")
        for document in census["documents"]:
            for setting, key in (("ingest_max_pdf_bytes", "bytes"), ("ingest_max_doc_pages", "pages"),
                                 ("ingest_max_doc_chunks", "chunks")):
                limit = getattr(settings, setting)
                if limit and document[key] > limit:
                    raise ProductionAdmissionError("census document exceeds the frozen native ingest bounds")
        environment = dict(expected.environment)
        unset_environment = list(expected.unset_environment)
        prerequisite = {**expected.prerequisites, "census": _record(census_path, census_raw),
                        "ledger": _record(ledger_path, ledger_raw)}
        payload = {
            "schema": 1, "expected": expected.expected, "environment": environment,
            "unset_environment": unset_environment,
            "query_input_counts": list(expected.query_input_counts), "prerequisites": prerequisite,
            "runtime": expected.runtime, "corpus_root": expected.corpus_root,
            "bounds": {"max_entries": bounds.max_entries, "max_bytes": bounds.max_bytes},
            "deadline_seconds": expected.deadline_seconds,
            "recipe_root": expected.recipe_root,
            "target": ledger["target"], "progress_binding": admission.progress_binding,
            "admission": {"summary": admission.summary, "producer": admission.producer,
                          "census_sha256": admission.census_sha256},
        }
        manifest = {
            "schema": 1, "kind": "production-census-admission", "acceptance_certified": False,
            "sources_verified": True, "summary": admission.summary, "producer": admission.producer,
            "census_sha256": admission.census_sha256, "progress_binding": admission.progress_binding,
            "progress_key": admission.progress_key,
            "environment_sha256": _digest({"set": environment, "unset": unset_environment}),
            "prerequisites_sha256": _digest(prerequisite), "target_sha256": _digest(ledger["target"]),
        }
        # Include the fixed-width sidecar field in the public envelope check
        # before creating any file.
        _bytes({**manifest, "sidecar_sha256": "0" * 64}, MANIFEST_BYTES)
        sidecar = state / SIDECAR_FILENAME
        raw = _write_sidecar(sidecar, payload)
        digest = hashlib.sha256(raw).hexdigest()
        manifest["sidecar_sha256"] = digest
        return PreparedProduction(manifest, MappingProxyType(environment), sidecar, digest,
                                  ledger_path, hashlib.sha256(ledger_raw).hexdigest(), data_state)
    except ProductionAdmissionError:
        raise
    except CensusAdmissionError as exc:
        raise ProductionAdmissionError(str(exc)) from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError,
            RuntimeError, RecursionError):
        raise ProductionAdmissionError("production admission inputs are unavailable or invalid") from None


def _prepared(prepared: PreparedProduction) -> Object:
    _private_directory(prepared.sidecar.parent)
    _private_directory(prepared.data_state)
    if prepared.sidecar.stat().st_mode & 0o077 or prepared.ledger.stat().st_mode & 0o077:
        raise ProductionAdmissionError("prepared production bookkeeping must remain private")
    raw = _read(prepared.sidecar, MAX_CENSUS_BYTES)
    if hashlib.sha256(raw).hexdigest() != prepared.sidecar_sha256:
        raise ProductionAdmissionError("prepared production sidecar changed")
    data = _object(_json(raw), {"schema", "expected", "environment", "unset_environment", "query_input_counts",
                               "prerequisites", "runtime", "corpus_root", "bounds", "target",
                               "progress_binding", "deadline_seconds", "recipe_root", "admission"})
    if (type(data["schema"]) is not int or data["schema"] != 1
            or type(prepared.manifest.get("schema")) is not int or prepared.manifest["schema"] != 1
            or prepared.manifest.get("kind") != "production-census-admission"
            or prepared.manifest.get("acceptance_certified") is not False
            or prepared.manifest.get("sources_verified") is not True
            or prepared.manifest["sidecar_sha256"] != prepared.sidecar_sha256
            or _digest({"set": data["environment"], "unset": data["unset_environment"]})
                != prepared.manifest["environment_sha256"]
            or dict(prepared.environment) != data["environment"]
            or list(prepared.unset_environment) != data["unset_environment"]
            or _digest(data["target"]) != prepared.manifest["target_sha256"]
            or data["progress_binding"] != prepared.manifest["progress_binding"]
            or _digest(data["progress_binding"]) != prepared.manifest["progress_key"]
            or data["prerequisites"]["ledger"]["path"] != str(prepared.ledger)
            or data["prerequisites"]["ledger"]["sha256"] != prepared.ledger_sha256
            or prepared.ledger.parent != prepared.data_state
            or any(prepared.manifest[name] != data["admission"][name] for name in
                   ("summary", "producer", "census_sha256"))
            or _digest(data["prerequisites"]) != prepared.manifest["prerequisites_sha256"]):
        raise ProductionAdmissionError("prepared production identities changed")
    return data


def load_prepared_production(manifest: Mapping[str, Any], *, state: Path,
                             data_state: Path) -> PreparedProduction:
    """Restore an existing prepared sidecar across CLI processes without writes.

    Validates prepared identities only. Actual input freshness, target proof and
    runtime observations still require the corresponding checks before use.
    """
    try:
        public = _object(dict(manifest), {
            "schema", "kind", "acceptance_certified", "sources_verified", "summary", "producer",
            "census_sha256", "progress_binding", "progress_key", "environment_sha256",
            "prerequisites_sha256", "target_sha256", "sidecar_sha256",
        })
        _bytes(public, MANIFEST_BYTES)
        state, data_state = _private_directory(state), _private_directory(data_state)
        sidecar, ledger = state / SIDECAR_FILENAME, data_state / LEDGER_FILENAME
        raw = _read(sidecar, MAX_CENSUS_BYTES)
        digest = _sha(public["sidecar_sha256"])
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ProductionAdmissionError("prepared production sidecar changed")
        data = _json(raw)
        environment = data["environment"]
        if type(environment) is not dict or any(type(name) is not str or type(value) is not str
                                               for name, value in environment.items()):
            raise ProductionAdmissionError("prepared production exports are unsupported")
        prepared = PreparedProduction(public, MappingProxyType(dict(environment)), sidecar, digest,
                                      ledger, _sha(data["prerequisites"]["ledger"]["sha256"]), data_state)
        _prepared(prepared)
        return prepared
    except ProductionAdmissionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError,
            RuntimeError, RecursionError):
        raise ProductionAdmissionError("prepared production state is unavailable or invalid") from None


def verify_production_sources(prepared: PreparedProduction, *, deadline: float,
                              clock: Clock = time.monotonic) -> None:
    """Recheck complete source bytes and prerequisite files before allocation.

    Host candidate/tool/transport validation remains independently mandatory.
    This pass cannot prevent later administrator/corpus mutation.
    """
    try:
        data = _prepared(prepared)
        bounds = ScanBounds(**data["bounds"])
        scan = _Scan(bounds, deadline, clock)
        scan.check()
        if deadline - clock() > data["deadline_seconds"]:
            raise ProductionAdmissionError("production verification exceeds the selected finite profile")
        records = data["prerequisites"]
        flattened = [records[name] for name in ("source_inventory", "golden", "census", "ledger")]
        flattened += list(records["artifacts"].values()) + list(records["recipes"].values())
        for record in flattened:
            size = _integer(record["bytes"])
            if _hash_file(Path(record["path"]), size, scan, source=False) != record["sha256"]:
                raise ProductionAdmissionError("prepared production prerequisite bytes changed")
        artifact_directory = Path(records["artifacts"]["manifest"]["path"]).parent
        _artifact_set(artifact_directory, data["expected"]["contract"]["tokenizer"]["files"], scan)
        _verify_sources(Path(data["corpus_root"]), data["expected"]["sources"], scan)
    except ProductionAdmissionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, RuntimeError):
        raise ProductionAdmissionError("prepared production inputs are unavailable or invalid") from None


def verify_retained_target(prepared: PreparedProduction, storage_proof: Mapping[str, Any], *,
                           workload: Sequence[str] = ()) -> str:
    """Compare existing storage-owner proof before allocation; return bound path.

    The caller must pass prove_qdrant_storage's result, not a claimed service or
    directory name. Per-run pod/container liveness remains that owner's job.
    """
    try:
        _workload(workload)
        data = _prepared(prepared)
        raw = _read(prepared.ledger, MANIFEST_BYTES)
        if hashlib.sha256(raw).hexdigest() != prepared.ledger_sha256:
            raise ProductionAdmissionError("retained ledger changed after preparation")
        target = data["target"]
        if any(storage_proof.get(key) != target[key] for key in
               ("namespace", "claim_uid", "pv_uid", "storage_path")):
            raise ProductionAdmissionError("actual retained storage target differs from its approved ledger")
        if data["environment"]["QDRANT_COLLECTION"] != target["logical_collection"]:
            raise ProductionAdmissionError("actual retained collection differs from its approved ledger")
        return _progress_path(prepared, data)
    except ProductionAdmissionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, RuntimeError):
        raise ProductionAdmissionError("retained target proof is unavailable or invalid") from None


def _progress_path(prepared: PreparedProduction, data: Object) -> str:
    key = _digest({"target": data["target"], "progress_binding": data["progress_binding"]})
    return str(prepared.data_state / f"ingest-{key}.jsonl")


def verify_workload_environment(
    prepared: PreparedProduction, environment: Mapping[str, str], *, execution_root: Path,
    expected_interpreter: str, cwd: Path, workload: Sequence[str] = (),
) -> None:
    """Refuse semantic overrides in the final native child environment.

    Interpreter spelling and execution root come from the host's independently
    checked manifest; never resolve a venv interpreter symlink. The host still
    owns executable/source identity, routing credentials and native storage proof.
    Apply these frozen values after gateway environment construction.
    """
    try:
        _workload(workload)
        data = _prepared(prepared)
        root = execution_root.resolve(strict=True)
        if str(root) != data["recipe_root"] or cwd.resolve(strict=True) != root:
            raise ProductionAdmissionError("production workload cwd/source root differs from the prepared candidate")
        interpreter = _text(expected_interpreter)
        if not Path(interpreter).is_absolute():
            raise ProductionAdmissionError("production interpreter must have its independently prepared absolute spelling")
        embedding = data["expected"]["contract"]["embedding"]
        expected = {**data["environment"], "PY": interpreter, "PYTHONPATH": str(root / "src"),
                    "LOCAL_STACK_DRYRUN": "0", "CORPUS_DIR": data["corpus_root"],
                    "PRODUCTION_CORPUS_DIR": data["corpus_root"],
                    "PRODUCTION_GOLDEN_PATH": data["prerequisites"]["golden"]["path"],
                    "LOCAL_INGEST_PROGRESS": _progress_path(prepared, data),
                    "GATEWAY_EMBED_MODEL": embedding["model"],
                    "GATEWAY_EMBED_MODEL_REVISION": embedding["revision"]}
        # Native Settings folds names case-insensitively. A later lowercase
        # variant can override a correct uppercase export or restore an unset
        # optional value; require one canonical spelling for protected names.
        canonical = {name.casefold(): name for name in
                     (*expected, *data["unset_environment"], "PYTHONHOME")}
        if (any(name != canonical.get(name.casefold(), name) for name in environment)
                or any(environment.get(name) != value for name, value in expected.items())
                or any(name in environment for name in data["unset_environment"])
                or environment.get("PYTHONHOME")):
            raise ProductionAdmissionError("production workload environment overrides the frozen native contract")
    except ProductionAdmissionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, RuntimeError):
        raise ProductionAdmissionError("production workload environment is unavailable or invalid") from None


def verify_runtime_evidence(prepared: PreparedProduction, observed: Mapping[str, Any]) -> None:
    """Match actual remote software/embedding metadata before workload startup.

    Missing observations refuse. The remote owner must observe tokenizers and
    the embedding child's launched argv digest to satisfy this stronger contract.
    """
    try:
        data = _prepared(prepared)
        runtime = data["runtime"]
        software = observed["software"]
        if any(software.get(name) != version for name, version in runtime["pins"].items()):
            raise ProductionAdmissionError("observed inference runtime differs from the approved pooling evidence")
        embedding = data["expected"]["contract"]["embedding"]
        models = [model for model in observed["models"] if model.get("model_id") == embedding["model"]]
        if (len(models) != 1 or models[0].get("resolved_revision") != embedding["revision"]
                or models[0].get("server_argv_sha256") != _digest(runtime["server_argv"])
                or models[0].get("metadata_sha256") != runtime["metadata_sha256"]):
            raise ProductionAdmissionError("observed embedding artifacts differ from the approved pooling evidence")
    except ProductionAdmissionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, RuntimeError):
        raise ProductionAdmissionError("observed production runtime evidence is incomplete or unsupported") from None
