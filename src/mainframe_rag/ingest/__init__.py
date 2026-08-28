"""Ingest subpackage: parse, chunk, classify, embed, upsert IBM-style manuals."""

from mainframe_rag.ingest.chunk import Chunk, make_chunks, outline_sections
from mainframe_rag.ingest.classify import classify
from mainframe_rag.ingest.ibm_pdf import ParsedDoc, parse_pdf
from mainframe_rag.ingest.walk import walk_pdfs

__all__ = [
    "Chunk",
    "ParsedDoc",
    "classify",
    "make_chunks",
    "outline_sections",
    "parse_pdf",
    "walk_pdfs",
]
