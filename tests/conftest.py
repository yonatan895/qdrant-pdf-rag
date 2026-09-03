"""Shared fixtures: generate original test PDFs at runtime. Never commit PDFs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session")
def synthetic_pdf(tmp_path_factory) -> Path:
    from scripts.make_synthetic_pdf import build

    out = tmp_path_factory.mktemp("ibm_shape") / "SA22-0000-00_outline.pdf"
    build(out)
    return out


@pytest.fixture(scope="session")
def plain_pdf(tmp_path_factory) -> Path:
    from scripts.make_synthetic_pdf import build_plain

    out = tmp_path_factory.mktemp("plain") / "widget-guide.pdf"
    build_plain(out)
    return out


@pytest.fixture(scope="session")
def jcl_pdf(tmp_path_factory) -> Path:
    from scripts.make_synthetic_pdf import build_jcl

    out = tmp_path_factory.mktemp("jcl") / "SA22-8004-00_smpjcl.pdf"
    build_jcl(out)
    return out


@pytest.fixture(scope="session")
def rexx_pdf(tmp_path_factory) -> Path:
    from scripts.make_synthetic_pdf import build_rexx

    out = tmp_path_factory.mktemp("rexx") / "SA22-8005-00_smprexx.pdf"
    build_rexx(out)
    return out
