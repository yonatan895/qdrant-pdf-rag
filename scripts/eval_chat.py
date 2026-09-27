#!/usr/bin/env python3
"""Compatibility entry for the canonical multi-turn A/B evaluator."""
from __future__ import annotations

import sys
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from mainframe_rag.eval.chat import (  # noqa: F401 — same-object compatibility exports
    ARM_CONDENSED,
    ARM_LITERAL,
    ASSISTANT_PLACEHOLDER,
    DEFAULT_FOLLOW_UP,
    FOLLOW_UPS,
    SEARCH_LIMIT,
    ChatMessage,
    GoldenEntry,
    HttpxLLMClient,
    VenueError,
    _retrieve_arm,
    arm_entry,
    condense_query,
    evaluate_sessions,
    follow_up_query,
    load_golden,
    load_settings,
    main,
    require_rc_for_collection,
    require_rc_for_golden,
    retrieve_search,
    score_entry,
    select_entries,
    session_messages,
    summarize_arms,
    summary_markdown,
    write_run_manifest,
)

if __name__ == "__main__":
    raise SystemExit(main())
