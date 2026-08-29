"""Unit tests for scripts/query_demo.py (pure functions, no network/docker)."""

from pathlib import Path
from unittest.mock import patch

from scripts.query_demo import (
    _format_text_hit,
    main,
    render_query_html,
    render_query_text,
)

from mainframe_rag.retrieve.query import SearchHit


def _sample_hit() -> SearchHit:
    return SearchHit(
        chunk_id="abc",
        score=0.0333,
        cite="SA22-0000-00 z/OS Messages, Chapter 1 > IEA Messages, p. 1-5",
        heading="Chapter 1 > IEA Messages",
        text="IEA500I IOSCMDS COMMAND REJECTED",
        doc_id="SA22-0000-00",
        title="z/OS Messages",
        page_label="1-5",
        chunk_type="message",
        product="z/OS",
        version="3.2",
        message_ids=("IEA500I",),
    )


def test_format_text_hit():
    hit = _sample_hit()
    formatted = _format_text_hit(1, hit)
    assert "#1 [Score: 0.0333]" in formatted
    assert "SA22-0000-00" in formatted
    assert "IEA500I" in formatted


def test_render_query_text():
    hits = [_sample_hit()]
    rendered = render_query_text("IEA500I", "identifier", hits, {"embed_ms": 5, "qdrant_ms": 10})
    assert "QUERY: IEA500I" in rendered
    assert "[IDENTIFIER]" in rendered
    assert "Embed: 5ms | Qdrant: 10ms" in rendered
    assert "Hits Found     : 1" in rendered


def test_render_query_html():
    hits = [_sample_hit()]
    html_out = render_query_html("IEA500I", "identifier", hits, {"embed_ms": 5, "qdrant_ms": 10})
    assert "<!DOCTYPE html>" in html_out
    assert "Query Inspection Demo" in html_out
    assert "IEA500I" in html_out
    assert "Score: 0.0333" in html_out


@patch("scripts.query_demo.retrieve_search")
@patch("scripts.query_demo.build_embedder")
@patch("qdrant_client.QdrantClient")
def test_main_cli_single_query(mock_qdrant, mock_embed, mock_search, tmp_path: Path):
    mock_search.return_value = ([_sample_hit()], "identifier", {"embed_ms": 2, "qdrant_ms": 8})
    out_file = tmp_path / "query.json"

    rc = main(["--query", "IEA500I", "--format", "json", "--out", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    assert '"query": "IEA500I"' in out_file.read_text(encoding="utf-8")
