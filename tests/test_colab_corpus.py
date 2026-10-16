"""Original metadata fixtures for pure corpus admission; no manual/model reads."""
from copy import deepcopy

import pytest

from mainframe_rag.serve import colab_corpus
from mainframe_rag.serve.colab_corpus import (
    CensusAdmissionError,
    admit_production_census,
    production_progress_binding,
)


def synthetic_production_census():
    """Reusable (census, independent expectations, retained ledger) test inputs.

    Hashes and lengths below identify synthetic metadata only. They are not
    measured PDFs/tokenizer artifacts and must never authorize a private run.
    Totals and maxima are written independently of the admission implementation.
    """
    sources = [
        {"relative_path": "original books/manual a.pdf", "bytes": 91, "sha256": "1" * 64},
        {"relative_path": "original books/manual b.pdf", "bytes": 103, "sha256": "2" * 64},
    ]
    contract = {
        "representation_fingerprint": "rp2:" + "a" * 16,
        "document_recipe_sha256": "b" * 64,
        "source_labels": "unknown|original|v1",
        "embedding": {"model": "synthetic/embedding", "revision": "3" * 40, "dense_dim": 7},
        "tokenizer": {
            "model": "synthetic/embedding", "revision": "3" * 40,
            "implementation": "original-test-tokenizer", "version": "synthetic-version",
            "files": {
                "tokenizer.json": {"bytes": 71, "sha256": "4" * 64},
                "tokenizer_config.json": {"bytes": 43, "sha256": "5" * 64},
            },
        },
        "counting_options": {"add_special_tokens": True, "do_lower_case": False, "truncation": False},
        "golden_sha256": "c" * 64,
        "query_recipe_sha256": "d" * 64,
        "effective_window": 16,
    }
    census = {
        "schema": 1, "status": "complete", "completed_at": 1.0,
        "producer": {"development_sha": "6" * 40, "source_sha256": "7" * 64},
        "sources": deepcopy(sources), "contract": deepcopy(contract),
        "documents": [
            {**sources[0], "status": "ok", "pages": 2, "chunks": 3,
             "chunk_types": {"message": 1, "syntax": 0, "table": 0, "narrative": 2},
             "embedding_inputs": 3, "max_document_tokens": 8},
            {**sources[1], "status": "ok", "pages": 4, "chunks": 2,
             "chunk_types": {"message": 0, "syntax": 1, "table": 1, "narrative": 0},
             "embedding_inputs": 2, "max_document_tokens": 12},
        ],
        "queries": [
            {"index": 0, "embedding_inputs": 1, "max_query_tokens": 3},
            {"index": 1, "embedding_inputs": 2, "max_query_tokens": 7},
        ],
        "summary": {
            "source_count": 2, "source_bytes": 194, "document_count": 2,
            "pages": 6, "chunks": 5,
            "chunk_types": {"message": 1, "syntax": 1, "table": 1, "narrative": 2},
            "document_inputs": 5, "query_count": 2, "query_inputs": 3,
            "max_document_tokens": 12, "max_query_tokens": 7,
            "failures": 0, "exclusions": 0, "truncated_inputs": 0,
        },
    }
    expected = {"sources": deepcopy(sources), "contract": deepcopy(contract), "query_count": 2}
    return census, expected, production_progress_binding(expected)


def _replace(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


def _refuses(census, expected, retained, match=None):
    with pytest.raises(CensusAdmissionError, match=match):
        admit_production_census(census, expected, retained)


def test_complete_census_reconciles_independent_totals_without_mutating_inputs():
    census, expected, retained = synthetic_production_census()
    before = deepcopy((census, expected, retained))

    result = admit_production_census(census, expected, retained)

    assert result.summary == {
        "source_count": 2, "source_bytes": 194, "document_count": 2,
        "pages": 6, "chunks": 5,
        "chunk_types": {"message": 1, "syntax": 1, "table": 1, "narrative": 2},
        "document_inputs": 5, "query_count": 2, "query_inputs": 3,
        "max_document_tokens": 12, "max_query_tokens": 7,
        "failures": 0, "exclusions": 0, "truncated_inputs": 0,
    }
    assert result.progress_binding == retained
    assert len(result.progress_key) == len(result.census_sha256) == 64
    result.summary["chunk_types"]["table"] = 99
    result.producer["development_sha"] = "f" * 40
    assert (census, expected, retained) == before


@pytest.mark.parametrize("replacement", [
    None,  # no census at all
    {"max_document_tokens": 12, "max_query_tokens": 7},  # two maxima are not a census
])
def test_two_maxima_or_missing_census_cannot_authorize_production(replacement):
    _, expected, retained = synthetic_production_census()
    _refuses(replacement, expected, retained)


# One case per distinct refusal branch of admit_production_census.
@pytest.mark.parametrize("path,value", [
    pytest.param(("schema",), 2, id="schema-version"),
    pytest.param(("status",), "partial", id="status-not-complete"),
    pytest.param(("completed_at",), True, id="completion-bool"),
    pytest.param(("completed_at",), float("nan"), id="completion-nonfinite"),
    pytest.param(("completed_at",), 0, id="completion-nonpositive"),
    pytest.param(("documents",), [], id="document-count"),
    pytest.param(("queries",), [], id="query-count"),
    pytest.param(("documents", 0, "status"), "error", id="document-failed"),
    pytest.param(("documents", 0, "sha256"), "e" * 64, id="document-substituted"),
    pytest.param(("documents", 0, "pages"), 0, id="document-zero-pages"),
    pytest.param(("documents", 0, "chunks"), 4, id="chunks-vs-chunk-types"),
    pytest.param(("documents", 0, "embedding_inputs"), 2, id="inputs-vs-chunks"),
    pytest.param(("queries", 0, "embedding_inputs"), 0, id="query-zero-inputs"),
    pytest.param(("queries", 0, "index"), 2, id="query-index-out-of-range"),
    pytest.param(("sources", 0, "sha256"), "e" * 64, id="source-substituted"),
])
def test_partial_failed_or_substituted_evidence_refuses(path, value):
    census, expected, retained = synthetic_production_census()
    _replace(census, path, value)
    _refuses(census, expected, retained)


@pytest.mark.parametrize("group", ["sources", "documents", "queries"])
def test_repeated_rows_cannot_mask_an_unaccounted_input(group):
    census, expected, retained = synthetic_production_census()
    census[group][1] = deepcopy(census[group][0])
    _refuses(census, expected, retained)


# Three recomputation paths: a sum, a maximum, and a constant-zero failure ledger.
@pytest.mark.parametrize("field", ["pages", "max_query_tokens", "failures"])
def test_aggregate_counts_and_maxima_are_recomputed(field):
    census, expected, retained = synthetic_production_census()
    census["summary"][field] += 1
    _refuses(census, expected, retained, "do not reconcile")


# bool (would pass a numeric test), a negative, and a non-int type, at each distinct
# validator call site: min-1 row counts, min-0 chunk/type counts, query index, summary.
@pytest.mark.parametrize("path,value", [
    pytest.param(("documents", 0, "pages"), True, id="min1-bool"),
    pytest.param(("documents", 0, "pages"), 1.5, id="min1-float"),
    pytest.param(("documents", 0, "chunk_types", "message"), True, id="min0-bool"),
    pytest.param(("documents", 0, "chunk_types", "message"), -1, id="min0-negative"),
    pytest.param(("queries", 0, "index"), True, id="query-index-bool"),
    pytest.param(("summary", "failures"), True, id="summary-bool"),
    pytest.param(("summary", "chunk_types", "table"), "1", id="summary-type-str"),
])
def test_counts_never_coerce_booleans_or_nonintegers(path, value):
    census, expected, retained = synthetic_production_census()
    _replace(census, path, value)
    _refuses(census, expected, retained)


# Each case reaches a different contract check: generic equality, tokenizer/embedding
# identity, integer validation, counting-option type, nontruncation, artifact inventory.
@pytest.mark.parametrize("path,value", [
    pytest.param(("golden_sha256",), "e" * 64, id="equality"),
    pytest.param(("effective_window",), 32, id="window-equality"),
    pytest.param(("embedding", "model"), "synthetic/other", id="tokenizer-identity"),
    pytest.param(("embedding", "dense_dim"), True, id="dense-dim-bool"),
    pytest.param(("counting_options", "add_special_tokens"), 1, id="option-not-bool"),
    pytest.param(("counting_options", "truncation"), True, id="truncating"),
    pytest.param(("tokenizer", "files"), {}, id="no-artifact-inventory"),
])
def test_census_contract_must_match_independent_current_expectations(path, value):
    census, expected, retained = synthetic_production_census()
    _replace(census["contract"], path, value)
    _refuses(census, expected, retained)


@pytest.mark.parametrize("target", ["document", "query"])
def test_actual_pooling_window_accepts_boundary_and_refuses_one_more_token(target):
    census, expected, retained = synthetic_production_census()
    field = "max_document_tokens" if target == "document" else "max_query_tokens"
    row = census["documents"][1] if target == "document" else census["queries"][1]
    row[field] = census["summary"][field] = 16
    assert admit_production_census(census, expected, retained).summary[field] == 16
    row[field] = census["summary"][field] = 17
    _refuses(census, expected, retained, "effective pooling window")


def test_source_paths_round_trip_whitespace_unicode_and_delimiters_exactly():
    census, expected, _ = synthetic_production_census()
    path = "original group/α ; | $ book.pdf"
    for rows in (census["sources"], expected["sources"], census["documents"]):
        rows[0]["relative_path"] = path
    retained = production_progress_binding(expected)
    assert admit_production_census(census, expected, retained).progress_binding == retained
    census["documents"][0]["relative_path"] = path.replace(" ; | ", " ")
    _refuses(census, expected, retained)


# absolute, parent traversal, non-canonical spelling, and empty (".") paths.
@pytest.mark.parametrize("path", ["/private/manual.pdf", "a/../manual.pdf", "a//manual.pdf", "."])
def test_noncanonical_or_escaping_source_paths_refuse(path):
    census, expected, retained = synthetic_production_census()
    census["sources"][0]["relative_path"] = path
    _refuses(census, expected, retained)


def test_progress_survives_controller_attribution_order_and_timestamp_changes():
    census, expected, retained = synthetic_production_census()
    first = admit_production_census(census, expected, retained)
    census["producer"] = {"development_sha": "e" * 40, "source_sha256": "f" * 64}
    census["completed_at"] = 2.0
    for key in ("sources", "documents", "queries"):
        census[key].reverse()
    expected["sources"].reverse()
    second = admit_production_census(census, expected, retained)
    assert second.progress_key == first.progress_key
    assert second.census_sha256 != first.census_sha256


def test_query_only_and_nontruncating_window_changes_keep_document_checkpoints():
    census, expected, retained = synthetic_production_census()
    first = admit_production_census(census, expected, retained)
    for contract in (census["contract"], expected["contract"]):
        contract.update(golden_sha256="e" * 64, query_recipe_sha256="f" * 64, effective_window=32)
    second = admit_production_census(census, expected, retained)
    assert second.progress_binding == first.progress_binding
    assert second.progress_key == first.progress_key


# One case per field of the retained binding: fingerprint, recipe, labels, tokenization.
@pytest.mark.parametrize("path,value", [
    (("representation_fingerprint",), "rp2:" + "e" * 16),
    (("document_recipe_sha256",), "e" * 64),
    (("source_labels",), "unknown|different|v1"),
    (("tokenizer", "files", "tokenizer.json", "sha256"), "e" * 64),
])
def test_compatible_new_evidence_cannot_reuse_an_incompatible_retained_ledger(path, value):
    census, expected, retained = synthetic_production_census()
    _replace(census["contract"], path, value)
    _replace(expected["contract"], path, value)
    _refuses(census, expected, retained, "explicit fresh index or rebuild")


def test_changed_source_set_cannot_reuse_retained_progress():
    census, expected, retained = synthetic_production_census()
    for rows in (census["sources"], expected["sources"], census["documents"]):
        rows[0]["sha256"] = "e" * 64
    _refuses(census, expected, retained, "explicit fresh index or rebuild")


@pytest.mark.parametrize("binding", [None, {}])
def test_unknown_or_corrupt_ledger_never_initializes_itself(binding):
    census, expected, _ = synthetic_production_census()
    _refuses(census, expected, binding)


@pytest.mark.parametrize("change", ["unexpected-field", "missing-chunk-type"])
def test_schema_unknown_fields_and_missing_chunk_type_refuse(change):
    census, expected, retained = synthetic_production_census()
    if change == "unexpected-field":
        census["unexpected"] = "private details"
    else:
        del census["documents"][0]["chunk_types"]["table"]
    _refuses(census, expected, retained)


def test_metadata_byte_ceiling_and_fixed_diagnostics(monkeypatch):
    census, expected, retained = synthetic_production_census()
    census["private-text"] = "never expose original private metadata"
    monkeypatch.setattr(colab_corpus, "MAX_CENSUS_BYTES", 64)
    with pytest.raises(CensusAdmissionError) as error:
        admit_production_census(census, expected, retained)
    assert "bounded" in str(error.value)
    assert "private" not in str(error.value)
