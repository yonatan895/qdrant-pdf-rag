"""Reusable evaluation measurement core (issue #508, C2).

Dependency direction: Task / supported script entry points / CI composition
call cohesive evaluation operations here; this package calls explicit
datasets, scorers and reports; product APIs are touched only when executing
a live run (lazy imports inside those functions).

Production ``agent`` / ``retrieve`` / ``ingest`` modules must not import
this package. This package must not import ``scripts.*``, initialize the
FastAPI app, contact endpoints, inspect Docker, load private data, or mutate
global environment at import time. No plugin registry, workflow engine,
``utils.py`` catch-all, or evaluator base-class hierarchy.
"""

from __future__ import annotations
