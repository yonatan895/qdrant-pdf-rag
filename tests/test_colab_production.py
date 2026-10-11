"""Original files and synthetic observations; never a private/model fit proof."""
import hashlib
import json
import os
import shutil
import sys
from copy import deepcopy
from pathlib import Path

import pymupdf
import pytest

from mainframe_rag.config import Settings
from mainframe_rag.ingest.completion import representation_fingerprint
from mainframe_rag.ingest.rules_version import extraction_rules_version
from mainframe_rag.serve import colab_production as production
from mainframe_rag.serve.colab_corpus import production_progress_binding
from mainframe_rag.serve.colab_profiles import build_profile
from tests.test_colab_corpus import synthetic_production_census

REPO = Path(__file__).resolve().parents[1]


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    path.chmod(0o600)


@pytest.fixture
def original_inputs(tmp_path):
    """No fixture artifact pretends to be actual Qwen tokenizer/model evidence."""
    corpus = tmp_path / "original corpus | α"
    books = corpus / "original books"
    books.mkdir(parents=True)
    sources = []
    for name, text in (("a", "Original alpha fixture."), ("b", "Original beta fixture.")):
        path = books / f"manual {name}.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), text)
            document.save(path)
        raw = path.read_bytes()
        sources.append({"relative_path": path.relative_to(corpus).as_posix(),
                        "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    source_inventory = tmp_path / "source-discovery.json"
    _json(source_inventory, {"source_root": str(corpus.resolve()), "source_files": sources,
                             "source_count": 2, "source_unique_hashes": 2,
                             "source_bytes": sum(row["bytes"] for row in sources)})
    golden = tmp_path / "original-golden.jsonl"
    golden.write_text(
        '# Original grounded questions only.\n'
        '{"query":"What is an original fixture?","expected_doc_ids":["original-a"]}\n'
        '{"query":"Compare CSA versus ECSA","expected_doc_ids":["original-b"]}\n'
    )
    profile = build_profile("COLAB_G4_BF16")
    server = profile["servers"][1]
    settings = Settings(_env_file=None, embed_mode="vllm", embed_model=server["model_id"],
                        embed_model_revision=server["revision"], dense_dim=4096,
                        qdrant_collection="original_manuals", dense_query_prefix="Original: ",
                        acronym_expansion_enabled=False, comparative_split_enabled=True,
                        diagnostic_dualpath_enabled=False, contextual_embed_enabled=False,
                        context_llm_model=None, bm25_cache_dir=None, embed_max_input_chars=0,
                        ingest_alias_publish=True, ingest_max_pdf_bytes=0,
                        ingest_max_doc_pages=0, ingest_max_doc_chunks=0, query_max_chars=2000)
    runtime_pins = {"vllm": "0.28.0", "transformers": "5.15.0",
                    "tokenizers": "0.0.0+originalfixture", "torch": "0.0.0+originalfixture"}
    producer = {"development_sha": "6" * 40, "source_sha256": "7" * 64}
    evidence = tmp_path / "original-embedding-evidence"
    evidence.mkdir()
    encoder = {"max_seq_length": 32768, "do_lower_case": False}
    artifacts = {
        "tokenizer.json": {"version": "1.0", "model": {"type": "BPE"}},
        "tokenizer_config.json": {"tokenizer_class": "Qwen2Tokenizer", "model_max_length": 32768},
        "vocab.json": {"original": 0},
        "merges.txt": "# original fixture\n",
        "config.json": {"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"],
                        "max_position_embeddings": 32768},
        "sentence_bert_config.json": encoder,
        "modules.json": [
            {"idx": 0, "name": "0", "path": "", "type": "sentence_transformers.models.Transformer"},
            {"idx": 1, "name": "1", "path": "1_Pooling", "type": "sentence_transformers.models.Pooling"},
            {"idx": 2, "name": "2", "path": "2_Normalize", "type": "sentence_transformers.models.Normalize"},
        ],
        "1_Pooling/config.json": {"word_embedding_dimension": 4096,
                                  "pooling_mode_lasttoken": True,
                                  "pooling_mode_cls_token": False,
                                  "pooling_mode_mean_tokens": False,
                                  "pooling_mode_max_tokens": False},
    }
    files = {}
    for name, value in artifacts.items():
        path = evidence / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        raw = path.read_bytes()
        files[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    manifest = {
        "schema": 1, "model": {"model": server["model_id"], "revision": server["revision"]},
        "tokenizer": {"implementation": "transformers.Qwen2TokenizerFast", "version": "5.15.0"},
        "runtime": runtime_pins, "server_argv": server["argv"], "files": files,
        "observation": {
            "implementation": "vllm.entrypoints.pooling.embed.protocol.EmbeddingCompletionRequest",
            "version": "0.28.0",
            "model_config": {"max_model_len": 4096, "encoder_config": encoder,
                             "pooler_config": {"enable_chunked_processing": False,
                                               "max_embed_len": None,
                                               "seq_pooling_type": "LAST", "use_activation": True}},
            "counting_options": {"add_special_tokens": True, "do_lower_case": False,
                                 "truncation": False},
            "effective_window": 4096,
        },
        "provenance": {"kind": "offline-vllm-pooling-observation", "producer": producer,
                       "resolved_revision": server["revision"], "files_sha256": _digest(files),
                       "runtime_sha256": _digest(runtime_pins),
                       "server_argv_sha256": _digest(server["argv"])},
    }
    _json(evidence / "manifest.json", manifest)
    data_state = tmp_path / "retained-data"
    data_state.mkdir(mode=0o700)
    config = {"corpus": str(corpus), "golden": str(golden),
              "source_inventory": str(source_inventory), "embedding_evidence": str(evidence),
              "inventory": str(tmp_path / "complete-census.json"), "data_state": str(data_state),
              "namespace": "original-kind"}
    arguments = {"settings": settings, "runtime_pins": runtime_pins,
                 "pooling_producer": producer, "recipe_root": REPO,
                 "bounds": production.ScanBounds(max_entries=100, max_bytes=32 * 1024),
                 "deadline": 20.0, "clock": lambda: 10.0}
    return config, profile, arguments, manifest


def _complete(original_inputs):
    config, profile, arguments, _ = original_inputs
    expected = production.build_production_expected(config, profile, **arguments)
    census, _, _ = synthetic_production_census()
    census["sources"] = deepcopy(expected.expected["sources"])
    census["contract"] = deepcopy(expected.expected["contract"])
    for document, source in zip(census["documents"], census["sources"], strict=True):
        document.update(source)
    census["summary"]["source_bytes"] = sum(row["bytes"] for row in census["sources"])
    _json(Path(config["inventory"]), census)
    target = {"namespace": "original-kind", "claim_uid": "original-claim",
              "pv_uid": "original-pv", "storage_path": "/qdrant/storage",
              "logical_collection": "original_manuals"}
    _json(Path(config["data_state"]) / production.LEDGER_FILENAME,
          {"schema": 1, "target": target,
           "progress_binding": production_progress_binding(expected.expected)})
    return expected, census, target


def _prepare(original_inputs, tmp_path, name="run", census_producer=None):
    config, profile, arguments, _ = original_inputs
    state = tmp_path / name
    state.mkdir(mode=0o700)
    approved = census_producer or {"development_sha": "6" * 40, "source_sha256": "7" * 64}
    return production.prepare_production(config, profile, state=state,
                                         census_producer=approved, **arguments)


def test_expected_sources_golden_settings_and_native_query_inputs_are_independent(original_inputs):
    config, profile, arguments, _ = original_inputs
    result = production.build_production_expected(config, profile, **arguments)
    assert result.expected["query_count"] == 2
    assert result.query_input_counts == (1, 2)
    assert result.expected["contract"]["golden_sha256"] == hashlib.sha256(
        Path(config["golden"]).read_bytes()).hexdigest()
    assert result.expected["contract"]["representation_fingerprint"] == representation_fingerprint(
        arguments["settings"], extraction_rules_version())
    assert result.expected["contract"]["source_labels"] == "||"
    assert result.expected["contract"]["effective_window"] == 4096
    assert result.environment["DENSE_QUERY_PREFIX"] == "Original: "
    assert result.environment["COMPARATIVE_SPLIT_ENABLED"] == "true"
    assert result.environment["EMBED_MODEL"] == profile["servers"][1]["model_id"]
    assert "CONTEXT_LLM_MODEL" in result.unset_environment
    assert "CONTEXT_LLM_MODEL" not in result.environment


def test_resolved_settings_are_frozen_even_when_the_ambient_environment_changes(
        original_inputs, monkeypatch):
    config, profile, arguments, _ = original_inputs
    monkeypatch.setenv("DENSE_QUERY_PREFIX", "Unapproved ambient prefix: ")
    monkeypatch.setenv("COMPARATIVE_SPLIT_ENABLED", "false")
    result = production.build_production_expected(config, profile, **arguments)
    assert result.query_input_counts == (1, 2)
    assert result.environment["DENSE_QUERY_PREFIX"] == "Original: "
    assert result.environment["COMPARATIVE_SPLIT_ENABLED"] == "true"


def test_frozen_exports_and_unsets_round_trip_actual_native_settings(original_inputs, monkeypatch):
    config, profile, arguments, _ = original_inputs
    prefix = "Original prefix | α\nQuery:\t"
    arguments["settings"] = arguments["settings"].model_copy(update={"dense_query_prefix": prefix})
    result = production.build_production_expected(config, profile, **arguments)
    for name, value in result.environment.items():
        monkeypatch.setenv(name, value)
    for name in result.unset_environment:
        monkeypatch.delenv(name, raising=False)
    child = Settings(_env_file=None)
    assert child.context_llm_model is None and child.bm25_cache_dir is None
    assert child.dense_query_prefix == prefix
    assert representation_fingerprint(child, extraction_rules_version()) == result.expected["contract"]["representation_fingerprint"]
    assert production.native_query_inputs(child, "Compare CSA versus ECSA") == [
        prefix + "CSA", prefix + "ECSA"]


def test_actual_source_bytes_are_checked_even_when_metadata_and_census_agree(original_inputs):
    config, profile, arguments, _ = original_inputs
    _complete(original_inputs)
    path = next(Path(config["corpus"]).rglob("*.pdf"))
    raw = path.read_bytes()
    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    with pytest.raises(production.ProductionAdmissionError, match="source"):
        production.build_production_expected(config, profile, **arguments)


def test_complete_admission_keeps_full_metadata_private_and_binds_actual_target(
        original_inputs, tmp_path):
    expected, _, target = _complete(original_inputs)
    prepared = _prepare(original_inputs, tmp_path)
    assert prepared.manifest["kind"] == "production-census-admission"
    assert prepared.manifest["acceptance_certified"] is False
    # Two original questions exercise admission, never the live owner's
    # initially 24–30 independently grounded production acceptance cases.
    assert prepared.manifest["summary"]["query_count"] == 2
    assert len(json.dumps(prepared.manifest).encode()) < 64 * 1024
    assert "sources" not in prepared.manifest and "expected" not in prepared.manifest
    assert prepared.sidecar.stat().st_mode & 0o777 == 0o600
    stored = json.loads(prepared.sidecar.read_text())
    assert stored["expected"] == expected.expected
    progress = production.verify_retained_target(prepared, {**target, "pod_uid": "new-pod"})
    assert Path(progress).parent == Path(original_inputs[0]["data_state"])
    assert Path(progress).suffix == ".jsonl"
    assert not Path(progress).exists()


def test_unknown_ledger_does_not_initialize_from_empty_host_directory(original_inputs, tmp_path):
    _complete(original_inputs)
    ledger = Path(original_inputs[0]["data_state"]) / production.LEDGER_FILENAME
    ledger.unlink()
    with pytest.raises(production.ProductionAdmissionError, match="retained"):
        _prepare(original_inputs, tmp_path)
    assert not ledger.exists()


def _initialize(original_inputs, *, collections=(), aliases=(), control=None, recheck=lambda: None):
    config, profile, arguments, _ = original_inputs
    _, _, target = _complete(original_inputs)
    (Path(config["data_state"]) / production.LEDGER_FILENAME).unlink()
    reads = []

    def read(path, body=None):
        reads.append((path, body))
        if path == "/collections":
            return {"status": "ok", "result": {"collections": [{"name": name} for name in collections]}}
        if path == "/aliases":
            return {"status": "ok", "result": {"aliases": [
                {"alias_name": name, "collection_name": physical} for name, physical in aliases]}}
        assert body == {"ids": [production.publication_metadata_point_id("unrelated__completions")],
                        "with_payload": True, "with_vector": False}
        return {"status": "ok", "result": [] if control is None else [{"payload": control}]}

    return production.initialize_fresh_binding(
        config, profile, collection="original_manuals", storage_proof=target,
        census_producer={"development_sha": "6" * 40, "source_sha256": "7" * 64},
        read_qdrant=read, recheck_producer=recheck, **arguments), reads, target


def test_explicit_fresh_binding_allows_next_ordinary_prepare_without_claiming_ingestion(
        original_inputs, tmp_path):
    proof, reads, target = _initialize(original_inputs, collections=("legacy1024",))
    ledger = Path(original_inputs[0]["data_state"]) / production.LEDGER_FILENAME
    stored = json.loads(ledger.read_bytes())
    assert stored["target"] == target
    assert proof["ledger_sha256"] == hashlib.sha256(ledger.read_bytes()).hexdigest()
    assert proof["acceptance_certified"] is False
    assert ledger.stat().st_mode & 0o777 == 0o600
    assert reads == [("/collections", None), ("/aliases", None)]
    prepared = _prepare(original_inputs, tmp_path)
    assert production.verify_retained_target(prepared, target).endswith(".jsonl")
    assert not list(Path(original_inputs[0]["data_state"]).glob("*.jsonl"))


@pytest.mark.parametrize("collections,aliases", [
    (("original_manuals",), ()),
    ((), (("original_manuals", "absent-physical"),)),
    (("original_manuals__completions",), ()),
    (("original_manuals__gen" + "a" * 28 + "_repair",), ()),
    ((), (("original_manuals__build_00000000-0000-0000-0000-000000000001", "absent"),)),
    ((), (("unrelated-alias", "original_manuals__gen" + "a" * 28),)),
])
def test_initialization_refuses_existing_or_dangling_native_target_state(
        original_inputs, collections, aliases):
    with pytest.raises(production.ProductionAdmissionError, match="fresh"):
        _initialize(original_inputs, collections=collections, aliases=aliases)
    assert not (Path(original_inputs[0]["data_state"]) / production.LEDGER_FILENAME).exists()


def test_fresh_names_cannot_hide_target_publication_control(original_inputs):
    control = {"record_type": "publication-metadata", "target_collection": "unrelated__completions",
               "build_schema": 1, "build_id": "00000000-0000-0000-0000-000000000001",
               "logical_alias": "original_manuals", "data_collection": "unrelated",
               "gen_fp": "a" * 16, "corpus_fp": "b" * 12}
    with pytest.raises(production.ProductionAdmissionError, match="fresh"):
        _initialize(original_inputs, collections=("unrelated__completions",), control=control)
    assert not (Path(original_inputs[0]["data_state"]) / production.LEDGER_FILENAME).exists()


def test_producer_replacement_prevents_ledger_commit(original_inputs):
    def replaced():
        raise RuntimeError("original test producer replaced")

    with pytest.raises((production.ProductionAdmissionError, RuntimeError)):
        _initialize(original_inputs, recheck=replaced)
    assert not (Path(original_inputs[0]["data_state"]) / production.LEDGER_FILENAME).exists()


def test_native_publisher_lock_blocks_initialization_without_stealing_ownership(original_inputs):
    config = original_inputs[0]
    progress = Path(config["data_state"]) / "ordinary-progress.jsonl"
    lock = production.acquire_publish_lock(progress, "original_manuals")
    try:
        with pytest.raises(production.ProductionAdmissionError):
            _initialize(original_inputs)
        assert not (progress.parent / production.LEDGER_FILENAME).exists()
        with pytest.raises(RuntimeError):
            production.acquire_publish_lock(progress, "original_manuals")
    finally:
        production.release_run_lock(lock)
    assert _initialize(original_inputs)[0]["kind"] == "fresh-index-initialization"


def test_concurrent_destination_is_never_replaced(original_inputs, monkeypatch):
    ledger = Path(original_inputs[0]["data_state"]) / production.LEDGER_FILENAME
    real_link = os.link

    def competing_install(source, destination, **kwargs):
        assert not ledger.exists()
        # The staged inode is already complete/durable before readers can see it.
        assert json.loads(Path(source).read_bytes())["target"]["logical_collection"] == "original_manuals"
        ledger.write_bytes(b"independent competing destination\n")
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", competing_install)
    with pytest.raises(production.ProductionAdmissionError):
        _initialize(original_inputs)
    assert ledger.read_bytes() == b"independent competing destination\n"
    assert not list(ledger.parent.glob(".retained-index-binding-*.tmp"))


def test_interrupted_staged_write_leaves_no_ledger_then_fresh_retry_works(original_inputs, monkeypatch):
    def interrupted(_descriptor):
        raise OSError("original staged fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", interrupted)
        with pytest.raises(production.ProductionAdmissionError):
            _initialize(original_inputs)
    data = Path(original_inputs[0]["data_state"])
    assert not (data / production.LEDGER_FILENAME).exists()
    assert not list(data.glob(".retained-index-binding-*.tmp"))
    assert _initialize(original_inputs)[0]["kind"] == "fresh-index-initialization"


def test_loader_restores_next_process_identities_without_repreparing(original_inputs, tmp_path):
    _, _, target = _complete(original_inputs)
    prepared = _prepare(original_inputs, tmp_path)
    original = prepared.sidecar.read_bytes()
    loaded = production.load_prepared_production(
        prepared.manifest, state=prepared.sidecar.parent, data_state=prepared.data_state)
    assert loaded == prepared
    assert loaded.unset_environment == prepared.unset_environment
    assert loaded.sidecar.read_bytes() == original
    assert production.verify_retained_target(loaded, target) == production.verify_retained_target(prepared, target)


def test_loader_refuses_tampered_sidecar_without_overwriting_it(original_inputs, tmp_path):
    _complete(original_inputs)
    prepared = _prepare(original_inputs, tmp_path)
    altered = prepared.sidecar.read_bytes() + b" "
    prepared.sidecar.write_bytes(altered)
    with pytest.raises(production.ProductionAdmissionError, match="sidecar changed"):
        production.load_prepared_production(
            prepared.manifest, state=prepared.sidecar.parent, data_state=prepared.data_state)
    assert prepared.sidecar.read_bytes() == altered


@pytest.mark.parametrize("field,value", [
    ("contextual_embed_enabled", True), ("embed_max_input_chars", 100),
    ("ingest_alias_publish", False), ("embed_model_revision", "0" * 40),
])
def test_unsupported_resolved_settings_refuse_without_using_ambient_defaults(
        original_inputs, field, value):
    config, profile, arguments, _ = original_inputs
    arguments["settings"] = arguments["settings"].model_copy(update={field: value})
    with pytest.raises(production.ProductionAdmissionError):
        production.build_production_expected(config, profile, **arguments)


def test_native_query_capture_uses_actual_split_expansion_and_prefix(original_inputs):
    settings = original_inputs[2]["settings"]
    assert production.native_query_inputs(settings, "Compare CSA versus ECSA") == [
        "Original: CSA", "Original: ECSA"]
    expanded = settings.model_copy(update={"acronym_expansion_enabled": True})
    text = production.native_query_inputs(expanded, "How is CSA allocated?")
    assert len(text) == 1 and text[0].startswith("Original: ")
    assert "Common Service Area" in text[0]


@pytest.mark.parametrize("wrapper", [["/bin/true"],
                                     [*production.NATIVE_WORKLOAD, "--summary"]])
def test_arbitrary_production_wrappers_or_native_overrides_refuse(original_inputs, wrapper):
    config, profile, arguments, _ = original_inputs
    config["workload"] = wrapper
    with pytest.raises(production.ProductionAdmissionError, match="workload"):
        production.build_production_expected(config, profile, **arguments)


@pytest.mark.parametrize("name", ["holdout.jsonl", "release_set.jsonl"])
def test_protected_dataset_authorization_stays_with_native_owner(original_inputs, name):
    config, profile, arguments, _ = original_inputs
    original = Path(config["golden"])
    protected = original.with_name(name)
    protected.write_bytes(original.read_bytes())
    config["golden"] = str(protected)
    with pytest.raises(production.ProductionAdmissionError, match="protected"):
        production.build_production_expected(config, profile, **arguments)


def test_actual_golden_bytes_and_native_row_validation_are_required(original_inputs):
    config, profile, arguments, _ = original_inputs
    original = production.build_production_expected(config, profile, **arguments)
    golden = Path(config["golden"])
    golden.write_text('{"query":"Original negative?","expected_behavior":"abstain"}\n')
    changed = production.build_production_expected(config, profile, **arguments)
    assert changed.expected["query_count"] == 1
    assert changed.query_input_counts == (1,)
    assert changed.expected["contract"]["golden_sha256"] != original.expected["contract"]["golden_sha256"]
    golden.write_text('{"query":"private sentinel","expected_doc_ids":[]}\n')
    with pytest.raises(production.ProductionAdmissionError) as raised:
        production.build_production_expected(config, profile, **arguments)
    assert "private sentinel" not in str(raised.value)
    assert str(golden) not in str(raised.value)


def test_self_consistent_census_cannot_hide_wrong_native_effective_query_count(
        original_inputs, tmp_path):
    _, census, _ = _complete(original_inputs)
    census["queries"][1]["embedding_inputs"] = 1
    census["summary"]["query_inputs"] = 2
    _json(Path(original_inputs[0]["inventory"]), census)
    with pytest.raises(production.ProductionAdmissionError, match="native golden"):
        _prepare(original_inputs, tmp_path)


@pytest.mark.parametrize("setting", ["ingest_max_pdf_bytes", "ingest_max_doc_pages", "ingest_max_doc_chunks"])
def test_complete_census_must_fit_frozen_native_document_admission_bounds(
        original_inputs, tmp_path, setting):
    _complete(original_inputs)
    original_inputs[2]["settings"] = original_inputs[2]["settings"].model_copy(update={setting: 1})
    with pytest.raises(production.ProductionAdmissionError, match="native ingest bounds"):
        _prepare(original_inputs, tmp_path)


@pytest.mark.parametrize("change", ["extra", "missing", "symlink", "duplicate"])
def test_actual_source_membership_is_checked_before_admission(original_inputs, change):
    config, profile, arguments, _ = original_inputs
    corpus = Path(config["corpus"])
    path = corpus / "original books/manual a.pdf"
    if change == "extra":
        (corpus / "extra.pdf").write_bytes(path.read_bytes())
    elif change == "missing":
        path.unlink()
    elif change == "symlink":
        (corpus / "visible link").symlink_to(corpus / "original books", target_is_directory=True)
    else:
        inventory = Path(config["source_inventory"])
        rows = json.loads(inventory.read_text())
        other = corpus / "original books/manual b.pdf"
        other.write_bytes(path.read_bytes())
        rows["source_files"][1].update(bytes=path.stat().st_size,
                                       sha256=rows["source_files"][0]["sha256"])
        rows["source_bytes"] = 2 * path.stat().st_size
        rows["source_unique_hashes"] = 1
        _json(inventory, rows)
    with pytest.raises(production.ProductionAdmissionError, match="source"):
        production.build_production_expected(config, profile, **arguments)


@pytest.mark.parametrize("bound", ["entries", "bytes", "deadline"])
def test_source_scan_uses_explicit_entry_stream_and_deadline_bounds(original_inputs, bound):
    config, profile, arguments, _ = original_inputs
    if bound == "entries":
        arguments["bounds"] = production.ScanBounds(max_entries=1, max_bytes=32 * 1024)
    elif bound == "bytes":
        arguments["bounds"] = production.ScanBounds(max_entries=100, max_bytes=1)
    else:
        arguments["deadline"] = 10.0
    with pytest.raises(production.ProductionAdmissionError, match="bound|deadline"):
        production.build_production_expected(config, profile, **arguments)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5])
def test_explicit_source_bounds_do_not_coerce_booleans_or_invalid_counts(value):
    with pytest.raises(production.ProductionAdmissionError):
        production.ScanBounds(max_entries=value, max_bytes=1)


def test_artifact_actual_bytes_cannot_be_replaced_by_agreeing_metadata(original_inputs):
    config, profile, arguments, _ = original_inputs
    path = Path(config["embedding_evidence"]) / "tokenizer.json"
    raw = path.read_bytes()
    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    with pytest.raises(production.ProductionAdmissionError, match="artifact"):
        production.build_production_expected(config, profile, **arguments)


def test_optional_tokenizer_artifacts_cannot_escape_the_approved_inventory(original_inputs):
    config, profile, arguments, _ = original_inputs
    path = Path(config["embedding_evidence"]) / "added_tokens.json"
    path.write_text('{"original extra token":1}\n')
    with pytest.raises(production.ProductionAdmissionError, match="artifact coverage"):
        production.build_production_expected(config, profile, **arguments)


@pytest.mark.parametrize("change", ["producer", "runtime", "argv", "special_tokens", "window"])
def test_private_evidence_cannot_approve_its_own_pooling_semantics(original_inputs, change):
    config, profile, arguments, manifest = original_inputs
    if change == "producer":
        manifest["provenance"]["producer"]["source_sha256"] = "8" * 64
        # Keep the approval independent of the mutated evidence object.
        arguments["pooling_producer"] = {"development_sha": "6" * 40, "source_sha256": "7" * 64}
    elif change == "runtime":
        manifest["runtime"]["tokenizers"] = "self-approved-version"
        arguments["runtime_pins"] = {**manifest["runtime"], "tokenizers": "0.0.0+originalfixture"}
        manifest["provenance"]["runtime_sha256"] = _digest(manifest["runtime"])
    elif change == "argv":
        manifest["server_argv"] = [*manifest["server_argv"], "--pooler-config", "{}"]
        manifest["provenance"]["server_argv_sha256"] = _digest(manifest["server_argv"])
    elif change == "special_tokens":
        manifest["observation"]["counting_options"]["add_special_tokens"] = False
    else:
        manifest["observation"]["effective_window"] = 32768
    _json(Path(config["embedding_evidence"]) / "manifest.json", manifest)
    with pytest.raises(production.ProductionAdmissionError):
        production.build_production_expected(config, profile, **arguments)


def test_incomplete_approved_runtime_pins_refuse_instead_of_guessing(original_inputs):
    config, profile, arguments, _ = original_inputs
    arguments["runtime_pins"] = {key: value for key, value in arguments["runtime_pins"].items()
                                 if key != "tokenizers"}
    with pytest.raises(production.ProductionAdmissionError):
        production.build_production_expected(config, profile, **arguments)


def test_canonical_corpus_root_changes_require_explicit_retained_rebuild(original_inputs, tmp_path):
    first, census, _ = _complete(original_inputs)
    config, profile, arguments, _ = original_inputs
    moved = tmp_path / "different absolute identity"
    shutil.copytree(config["corpus"], moved)
    config["corpus"] = str(moved)
    inventory = Path(config["source_inventory"])
    data = json.loads(inventory.read_text())
    data["source_root"] = str(moved.resolve())
    _json(inventory, data)
    changed = production.build_production_expected(config, profile, **arguments)
    assert first.expected["sources"] == changed.expected["sources"]
    assert production_progress_binding(first.expected) != production_progress_binding(changed.expected)
    census["contract"] = deepcopy(changed.expected["contract"])
    _json(Path(config["inventory"]), census)
    with pytest.raises(production.ProductionAdmissionError, match="retained"):
        _prepare(original_inputs, tmp_path)


def test_query_and_golden_changes_reuse_compatible_document_progress(original_inputs, tmp_path):
    first, census, target = _complete(original_inputs)
    prepared = _prepare(original_inputs, tmp_path, "before")
    config, profile, arguments, _ = original_inputs
    ledger = Path(config["data_state"]) / production.LEDGER_FILENAME
    original_ledger = ledger.read_bytes()
    golden = Path(config["golden"])
    golden.write_text(golden.read_text().replace("What is an original fixture?", "Describe an original fixture?"))
    arguments["settings"] = arguments["settings"].model_copy(update={"dense_query_prefix": "Changed prefix: "})
    changed = production.build_production_expected(config, profile, **arguments)
    assert first.expected["contract"]["golden_sha256"] != changed.expected["contract"]["golden_sha256"]
    assert first.expected["contract"]["query_recipe_sha256"] != changed.expected["contract"]["query_recipe_sha256"]
    census["contract"] = deepcopy(changed.expected["contract"])
    census["completed_at"] = 2.0
    census["producer"]["development_sha"] = "8" * 40
    _json(Path(config["inventory"]), census)
    renewed = _prepare(original_inputs, tmp_path, "after", census_producer=census["producer"])
    assert ledger.read_bytes() == original_ledger
    assert production.verify_retained_target(prepared, target) == production.verify_retained_target(renewed, target)


def test_unrelated_controller_edits_do_not_invalidate_document_progress(original_inputs, tmp_path):
    config, profile, arguments, _ = original_inputs
    initial = production.build_production_expected(config, profile, **arguments)
    candidate = tmp_path / "prepared candidate"
    for record in initial.prerequisites["recipes"].values():
        source = Path(record["path"])
        destination = candidate / source.relative_to(REPO)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    controller = candidate / "scripts/colab.py"
    controller.parent.mkdir(parents=True)
    controller.write_text("# Original unrelated controller edit.\n")
    arguments["recipe_root"] = candidate
    first = production.build_production_expected(config, profile, **arguments)
    controller.write_text("# Another original unrelated controller edit.\n")
    second = production.build_production_expected(config, profile, **arguments)
    assert production_progress_binding(initial.expected) == production_progress_binding(first.expected)
    assert production_progress_binding(first.expected) == production_progress_binding(second.expected)
    query = candidate / "src/mainframe_rag/retrieve/query.py"
    query.write_text(query.read_text() + "\n# Candidate differs from the actual native producer.\n")
    with pytest.raises(production.ProductionAdmissionError, match="candidate"):
        production.build_production_expected(config, profile, **arguments)


@pytest.mark.parametrize("field", ["namespace", "claim_uid", "pv_uid", "storage_path"])
def test_actual_retained_target_drift_refuses(original_inputs, tmp_path, field):
    _, _, target = _complete(original_inputs)
    prepared = _prepare(original_inputs, tmp_path)
    with pytest.raises(production.ProductionAdmissionError, match="storage target"):
        production.verify_retained_target(prepared, {**target, field: "different-target"})


def test_source_and_ledger_are_rechecked_before_allocation(original_inputs, tmp_path):
    _complete(original_inputs)
    prepared = _prepare(original_inputs, tmp_path)
    production.verify_production_sources(prepared, deadline=20.0, clock=lambda: 10.0)
    source = Path(original_inputs[0]["corpus"]) / "original books/manual a.pdf"
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(production.ProductionAdmissionError):
        production.verify_production_sources(prepared, deadline=20.0, clock=lambda: 10.0)


def test_changed_retained_ledger_or_sidecar_never_authorizes_start(original_inputs, tmp_path):
    _, _, target = _complete(original_inputs)
    prepared = _prepare(original_inputs, tmp_path)
    original = prepared.ledger.read_bytes()
    prepared.ledger.write_bytes(original + b" ")
    with pytest.raises(production.ProductionAdmissionError, match="ledger changed"):
        production.verify_retained_target(prepared, target)
    prepared.ledger.write_bytes(original)
    prepared.sidecar.write_bytes(prepared.sidecar.read_bytes() + b" ")
    with pytest.raises(production.ProductionAdmissionError, match="sidecar changed"):
        production.verify_retained_target(prepared, target)


def test_full_prepared_metadata_must_remain_private(original_inputs, tmp_path):
    _, _, target = _complete(original_inputs)
    prepared = _prepare(original_inputs, tmp_path)
    prepared.sidecar.chmod(0o644)
    with pytest.raises(production.ProductionAdmissionError, match="private"):
        production.verify_retained_target(prepared, target)


def test_runtime_observations_match_exact_artifacts_pins_and_launched_flags(original_inputs, tmp_path):
    _complete(original_inputs)
    prepared = _prepare(original_inputs, tmp_path)
    stored = json.loads(prepared.sidecar.read_text())
    embedding = stored["expected"]["contract"]["embedding"]
    model = {"model_id": embedding["model"], "resolved_revision": embedding["revision"],
             "metadata_sha256": stored["runtime"]["metadata_sha256"],
             "server_argv_sha256": _digest(stored["runtime"]["server_argv"])}
    observed = {"software": stored["runtime"]["pins"], "models": [model]}
    production.verify_runtime_evidence(prepared, observed)
    for changed in ("missing_tokenizers", "changed_artifact", "extra_artifact", "changed_argv"):
        altered = deepcopy(observed)
        if changed == "missing_tokenizers":
            altered["software"].pop("tokenizers")
        elif changed == "changed_artifact":
            altered["models"][0]["metadata_sha256"]["config.json"] = "0" * 64
        elif changed == "extra_artifact":
            altered["models"][0]["metadata_sha256"]["added_tokens.json"] = "0" * 64
        else:
            altered["models"][0]["server_argv_sha256"] = "0" * 64
        with pytest.raises(production.ProductionAdmissionError):
            production.verify_runtime_evidence(prepared, altered)


def test_complete_census_requires_independently_approved_producer(original_inputs, tmp_path):
    _, census, _ = _complete(original_inputs)
    census["producer"]["source_sha256"] = "8" * 64
    _json(Path(original_inputs[0]["inventory"]), census)
    with pytest.raises(production.ProductionAdmissionError, match="producer"):
        _prepare(original_inputs, tmp_path)


def test_final_workload_environment_refuses_ambient_semantic_overrides(original_inputs, tmp_path):
    _, _, target = _complete(original_inputs)
    prepared = _prepare(original_inputs, tmp_path)
    stored = json.loads(prepared.sidecar.read_text())
    embedding = stored["expected"]["contract"]["embedding"]
    environment = {**prepared.environment, "PY": sys.executable,
                   "PYTHONPATH": str(REPO / "src"), "LOCAL_STACK_DRYRUN": "0",
                   "CORPUS_DIR": stored["corpus_root"],
                   "PRODUCTION_CORPUS_DIR": stored["corpus_root"],
                   "PRODUCTION_GOLDEN_PATH": stored["prerequisites"]["golden"]["path"],
                   "LOCAL_INGEST_PROGRESS": production.verify_retained_target(prepared, target),
                   "GATEWAY_EMBED_MODEL": embedding["model"],
                   "GATEWAY_EMBED_MODEL_REVISION": embedding["revision"],
                   "UNRELATED_ORIGINAL_SETTING": "preserved"}
    arguments = {"execution_root": REPO, "expected_interpreter": sys.executable, "cwd": REPO}
    production.verify_workload_environment(prepared, environment, **arguments)
    for name in ("PY", "PYTHONPATH", "LOCAL_STACK_DRYRUN", "CORPUS_DIR", "PRODUCTION_GOLDEN_PATH",
                 "LOCAL_INGEST_PROGRESS", "DENSE_QUERY_PREFIX", "EMBED_MODEL_REVISION",
                 "GATEWAY_EMBED_MODEL_REVISION", "CONTEXT_LLM_MODEL"):
        with pytest.raises(production.ProductionAdmissionError, match="environment"):
            production.verify_workload_environment(prepared, {**environment, name: "override"}, **arguments)
    with pytest.raises(production.ProductionAdmissionError, match="cwd"):
        production.verify_workload_environment(prepared, environment,
                                               **{**arguments, "cwd": tmp_path})


@pytest.mark.parametrize("variant,value", [
    ("dense_query_prefix", "Original conflicting case variant: "),
    ("context_llm_model", "original-conflicting-case-model"),
    # Even a currently equal duplicate is ambiguous; only canonical names
    # satisfy the final child-environment contract.
    ("dense_query_prefix", "Original: "),
])
def test_case_insensitive_settings_cannot_override_frozen_exports_or_unsets(
        original_inputs, tmp_path, monkeypatch, variant, value):
    _, _, target = _complete(original_inputs)
    prepared = _prepare(original_inputs, tmp_path)
    stored = json.loads(prepared.sidecar.read_text())
    embedding = stored["expected"]["contract"]["embedding"]
    environment = {**prepared.environment, "PY": sys.executable,
                   "PYTHONPATH": str(REPO / "src"), "LOCAL_STACK_DRYRUN": "0",
                   "CORPUS_DIR": stored["corpus_root"],
                   "PRODUCTION_CORPUS_DIR": stored["corpus_root"],
                   "PRODUCTION_GOLDEN_PATH": stored["prerequisites"]["golden"]["path"],
                   "LOCAL_INGEST_PROGRESS": production.verify_retained_target(prepared, target),
                   "GATEWAY_EMBED_MODEL": embedding["model"],
                   "GATEWAY_EMBED_MODEL_REVISION": embedding["revision"]}
    # Native Settings folds environment names case-insensitively. Establish the
    # actual child insertion order, including a later conflicting lowercase key.
    controlled = {name.casefold() for name in environment} | {
        name.casefold() for name in prepared.unset_environment}
    for name in tuple(os.environ):
        if name.casefold() in controlled:
            monkeypatch.delenv(name)
    environment[variant] = value
    for name, item in environment.items():
        monkeypatch.setenv(name, item)
    child = Settings(_env_file=None)
    assert getattr(child, variant) == value
    if variant == "dense_query_prefix":
        assert environment["DENSE_QUERY_PREFIX"] == prepared.environment["DENSE_QUERY_PREFIX"]
        assert production.native_query_inputs(child, "Compare CSA versus ECSA") == [
            value + "CSA", value + "ECSA"]
    else:
        assert "CONTEXT_LLM_MODEL" in prepared.unset_environment
        assert "CONTEXT_LLM_MODEL" not in environment
        assert representation_fingerprint(child, extraction_rules_version()) != stored["expected"]["contract"]["representation_fingerprint"]
    with pytest.raises(production.ProductionAdmissionError, match="environment"):
        production.verify_workload_environment(
            prepared, environment, execution_root=REPO,
            expected_interpreter=sys.executable, cwd=REPO)


def test_interpreter_spelling_is_preserved_even_for_a_venv_symlink(original_inputs, tmp_path):
    _, _, target = _complete(original_inputs)
    prepared = _prepare(original_inputs, tmp_path)
    interpreter = tmp_path / "original prepared interpreter"
    interpreter.symlink_to(sys.executable)
    stored = json.loads(prepared.sidecar.read_text())
    embedding = stored["expected"]["contract"]["embedding"]
    environment = {**prepared.environment, "PY": str(interpreter), "PYTHONPATH": str(REPO / "src"),
                   "LOCAL_STACK_DRYRUN": "0", "CORPUS_DIR": stored["corpus_root"],
                   "PRODUCTION_CORPUS_DIR": stored["corpus_root"],
                   "PRODUCTION_GOLDEN_PATH": stored["prerequisites"]["golden"]["path"],
                   "LOCAL_INGEST_PROGRESS": production.verify_retained_target(prepared, target),
                   "GATEWAY_EMBED_MODEL": embedding["model"],
                   "GATEWAY_EMBED_MODEL_REVISION": embedding["revision"]}
    production.verify_workload_environment(prepared, environment, execution_root=REPO,
                                           expected_interpreter=str(interpreter), cwd=REPO)
    with pytest.raises(production.ProductionAdmissionError, match="environment"):
        production.verify_workload_environment(prepared, {**environment, "PY": str(interpreter.resolve())},
                                               execution_root=REPO, expected_interpreter=str(interpreter), cwd=REPO)
