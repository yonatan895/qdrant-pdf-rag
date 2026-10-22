"""Sphinx configuration for the mainframe_rag API reference (issue #889).

Docs-only: Sphinx is installed from site-src/reference/requirements.docs.lock.txt
and never from a product lock. The package is imported from the checked-out
source tree; third-party runtime dependencies are mocked so the build needs
nothing but Sphinx and cannot reach Qdrant, models or secrets.
"""

import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "src"))

project = "mainframe_rag API reference"
author = "mainframe-rag contributors"
copyright = "Apache-2.0"
language = "en"

extensions = [
    "sphinx.ext.apidoc",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

# Module stubs are generated at build time from src/, so they cannot drift.
apidoc_modules = [
    {
        "path": str(REPO / "src" / "mainframe_rag"),
        "destination": "api",
        "exclude_patterns": ["**/__main__.py"],
        "max_depth": 2,
        "separate_modules": True,
        "module_first": True,
    },
]

autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
}
autodoc_member_order = "bysource"

# Third-party runtime dependencies: mocked, never imported.
autodoc_mock_imports = [
    "anyio",
    "fastapi",
    "fastembed",
    "httpx2",
    "opentelemetry",
    "prometheus_client",
    "pydantic",
    "pydantic_settings",
    "pymupdf",
    "qdrant_client",
    "starlette",
    "uvicorn",
    "yaml",
]

exclude_patterns = ["_build"]
html_theme = "furo"
html_title = "mainframe_rag API reference"
html_show_sourcelink = False

# Docstrings in this package are plain prose, not reStructuredText, so docutils
# reports indentation/emphasis findings on text that is correct as written.
# Source files are protected (some modules' bytes are the ingest rules
# version), so these are filtered here rather than edited. Real failures stay
# fatal under -W: import errors (autodoc.import_object), unresolved toctree
# entries and broken references to documented objects.
#   docutils - prose-vs-RST markup findings (23 of 25 warnings at adoption)
# The ambiguous ``type`` cross-reference between the TypedDicts CoreToken.type
# and CoreFinal.type is dropped by one exact-message filter below, so the
# ``ref.python`` category stays fatal for every other broken reference.
suppress_warnings = ["docutils"]


# Missing Python cross-references only warn when nitpicky. Turn that on so a
# broken reference to this package fails -W, and ignore targets that cannot be
# resolved offline: everything outside mainframe_rag (no intersphinx, so the
# stdlib and mocked third-party types have no inventory) and private
# (underscore) names that are intentionally not documented.
nitpicky = True
nitpick_ignore_regex = [
    (r"py:.*", r"(?!mainframe_rag\.).*"),
    (r"py:.*", r".*\._.*"),
]


def _skip_mock_artifacts(app, what, name, obj, skip, options):
    """Skip raw classmethod objects.

    With pydantic mocked, ``@field_validator`` (stacked over ``@classmethod``)
    leaves an uncallable classmethod object on the class, for which autodoc
    cannot build a signature. Validators are implementation detail.
    """
    if isinstance(obj, classmethod):
        return True
    return skip


_KNOWN_AMBIGUOUS = (
    "more than one target found for cross-reference 'type': "
    "mainframe_rag.agent.answer_core.CoreToken.type, "
    "mainframe_rag.agent.answer_core.CoreFinal.type"
)


class _DropKnownAmbiguity(logging.Filter):
    def filter(self, record):
        return _KNOWN_AMBIGUOUS not in record.getMessage()


def _install_filter(app):
    # Handlers (not the logger) carry Sphinx's -W counting; put ours first.
    for handler in logging.getLogger("sphinx").handlers:
        handler.addFilter(_DropKnownAmbiguity())
        handler.filters.insert(0, handler.filters.pop())


def setup(app):
    app.connect("autodoc-skip-member", _skip_mock_artifacts)
    app.connect("builder-inited", _install_filter)
