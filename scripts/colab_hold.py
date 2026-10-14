#!/usr/bin/env python3
"""Sandbox hold workload for synthetic Colab runs (#742).

Keeps the owned model session up while the operator runs and restarts the
local stack from a second terminal against ``sandbox.env``. Exits 0 when the
private ``sandbox-stop`` file appears or shortly before the controller's work
deadline, so an intended end releases the session as a completed run. The
controller still owns transport/budget checks, teardown and recovery.
"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

DEADLINE_MARGIN_SECONDS = 15.0
POLL_SECONDS = 1.0


def hold(state: Path, work_expiry: float, *, clock=time.time, sleep=time.sleep) -> int:
    stop = state / "sandbox-stop"
    print(f"Sandbox models are up. Source {state / 'sandbox.env'} in another terminal; "
          f"touch {stop} to release the session.", flush=True)
    while not stop.exists() and clock() < work_expiry - DEADLINE_MARGIN_SECONDS:
        sleep(POLL_SECONDS)
    print("Sandbox hold finished; releasing the owned session.", flush=True)
    return 0


def main() -> int:
    try:
        state = Path(os.environ["RAG_COLAB_STATE"])
        work_expiry = float(os.environ["RAG_COLAB_WORK_EXPIRY"])
    except (KeyError, ValueError):
        print("colab_hold: requires the controller's RAG_COLAB_STATE and RAG_COLAB_WORK_EXPIRY", file=sys.stderr)
        return 2
    if not math.isfinite(work_expiry) or not state.is_dir() or not (state / "sandbox.env").is_file():
        print("colab_hold: private run state or sandbox handoff is unavailable", file=sys.stderr)
        return 2
    return hold(state, work_expiry)


if __name__ == "__main__":
    raise SystemExit(main())
