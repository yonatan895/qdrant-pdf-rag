"""The refusal and read-failure types of the retained-generation commands and
their exit codes (issue #1373).

They live here, in a module that is never run as a script, because
`python -m mainframe_rag.ingest.generations` loads that file as `__main__`:
anything it defined itself would be a different class from the one
`mainframe_rag.ingest.disposal` raises after importing the module by name,
and a refusal would then miss its handler (review B1 of #1393).
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNREADABLE = 3
EXIT_REFUSED = 4


class Refused(Exception):
    """A fixed, operator-facing refusal (exit 4). Nothing further changes."""


class Unreadable(Exception):
    """Qdrant or a peer could not be read (exit 3). Fixed text only."""
