#!/usr/bin/env python3
"""Compatibility entry for the canonical answer measurement owner.

Retire only after callers migrate, the successor is qualified/documented,
and the maintainer approves removing the supported script command.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Preserve the documented uninstalled-checkout CLI, as the retrieval delegate does.
_SOURCE = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from mainframe_rag.eval.answers import (
    VERIFICATION_STATES,
    ZERO_HITS_ANSWER,
    AnswerCapture,
    answer_completeness,
    failure_bucket,
    inferred_index_off_gold,
    is_abstention,
    is_refusal,
    is_zero_hits_answer,
    judge,
    main,
    run_query,
    select_sample,
    summarize,
    why_mode,
    write_summary,
)

# Historical imported name; L2 now uses the intentional public capture API.
_AnswerCapture = AnswerCapture

__all__ = ['VERIFICATION_STATES', 'ZERO_HITS_ANSWER', 'AnswerCapture', '_AnswerCapture', 'answer_completeness', 'failure_bucket', 'inferred_index_off_gold', 'is_abstention', 'is_refusal', 'is_zero_hits_answer', 'judge', 'main', 'run_query', 'select_sample', 'summarize', 'why_mode', 'write_summary']

if __name__ == "__main__":
    sys.exit(main())
