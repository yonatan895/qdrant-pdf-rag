"""Shared fixtures: generate the synthetic PDF if missing; env for settings."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "synthetic"
SYNTHETIC_PDF = FIXTURE_DIR / "SA22-0000-00_outline.pdf"


@pytest.fixture(scope="session")
def synthetic_pdf() -> Path:
    if not SYNTHETIC_PDF.exists():
        from scripts.make_synthetic_pdf import build

        build(SYNTHETIC_PDF)
    return SYNTHETIC_PDF
