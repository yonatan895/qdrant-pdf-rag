"""Test-only publisher process: terminate after an acknowledged durable boundary."""
from __future__ import annotations

import argparse
import os
import signal
import threading
from pathlib import Path


def main():
    from mainframe_rag.ingest import inventory, qdrant_io, representation, run_ingest

    parser = argparse.ArgumentParser()
    parser.add_argument("--boundary", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--hold", action="store_true")
    options, ingest_args = parser.parse_known_args()
    if ingest_args[:1] == ["--"]:
        ingest_args = ingest_args[1:]

    stop_lock = threading.Lock()

    def stop():
        # Document writes use several threads even with one parse worker.
        # Exactly one callback may write the receipt before process-wide exit;
        # another must not truncate it between flush and os._exit.
        with stop_lock:
            with options.receipt.open("w") as receipt:
                receipt.write(options.boundary)
                receipt.flush()
                os.fsync(receipt.fileno())
            if options.hold:
                os.kill(os.getpid(), signal.SIGSTOP)
            os._exit(86)  # Deliberately bypass finally, atexit and Python lock cleanup.

    boundary = options.boundary
    if boundary in {"corpus-clone", "control-clone"}:
        owner, name = qdrant_io, "clone_collection"

        def matches(args, kwargs):
            source = args[2]
            return source.endswith("__completions") == (boundary == "control-clone")
    elif boundary == "pending":
        owner, name = representation, "write_manifest"

        def matches(args, kwargs):
            return kwargs.get("state") == "pending"
    elif boundary == "retired-inventory":
        owner, name = inventory, "append_record"

        def matches(args, kwargs):
            return args[1].status == "retired"
    else:
        owner, name = {
            "sidecar": (run_ingest, "write_publish_state"),
            "rekey": (representation, "rekey_manifest"),
            "invalidated": (run_ingest, "delete_completion"),
            "deleted": (run_ingest, "delete_by_revision"),
            "points": (run_ingest, "upsert_chunks"),
            "completion": (run_ingest, "write_completion"),
            "inventory": (run_ingest, "append_record"),
            "committed": (run_ingest, "commit_manifest"),
            "removals": (run_ingest, "apply_approved_removals"),
            "receipt": (run_ingest, "write_publication_metadata"),
            "cutover": (run_ingest, "swap_alias_to"),
            "cleanup": (run_ingest, "clear_publish_state"),
        }[boundary]

        def matches(args, kwargs):
            return True

    original = getattr(owner, name)

    def interrupted(*args, **kwargs):
        result = original(*args, **kwargs)
        if matches(args, kwargs):
            stop()
        return result

    setattr(owner, name, interrupted)
    return run_ingest.main(ingest_args)


if __name__ == "__main__":
    raise SystemExit(main())
