#!/usr/bin/env python3
"""Run the simulation tier as N concurrent pytest processes (CI wall time).

The tier is one pytest selection whose cost is dominated by serial process
spawns (ingest pool, publisher crash workers). Each shard is a separate pytest
process with its OWN disposable Qdrant container (the `qdrant_url` fixture
starts one per process), so shards share no collections, aliases or ports.

Fail-closed, like the single-process run:
  * the node ids are collected once with the exact selection given;
  * every collected id must appear as an executed testcase in the merged
    JUnit (a shard that dies early cannot hide tests);
  * any shard exit code, failure, error or skip fails the run.
With QDRANT_SIM_URL set (one shared server, GitLab service) sharding would
make shards collide on that server, so the selection runs as a single process.

Usage: sim_parallel.py --shards N --junit OUT.xml -- python -m pytest -m integration ...
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collect(command: list[str]) -> list[str]:
    # Fixed verbosity: addopts and -v/-rs would change the one-id-per-line shape.
    base = [c for c in command if c not in ("-v", "-vv", "-q", "-rs")]
    done = subprocess.run([*base, "-o", "addopts=", "--collect-only", "-q", "-p", "no:cacheprovider"],
                          cwd=ROOT, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        sys.stdout.write(done.stdout + done.stderr)
        raise SystemExit("sim collection failed")
    return [line.strip() for line in done.stdout.splitlines() if line.startswith("tests/") and "::" in line]


def partition(ids: list[str], shards: int) -> list[list[str]]:
    """Round-robin keeps adjacent parametrized cases (equal cost) on different shards."""
    shards = max(1, min(shards, len(ids)))
    return [ids[index::shards] for index in range(shards)]


def merge_junit(parts: list[Path], out: Path) -> list[str]:
    root = ET.Element("testsuites")
    names = []
    for part in parts:
        for case in ET.parse(part).getroot().iter("testcase"):
            names.append(f"{case.get('classname', '')}::{case.get('name', '')}")
        for suite in ET.parse(part).getroot().iter("testsuite"):
            root.append(suite)
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    return names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=3)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.junit.exists():
        raise SystemExit("usage: sim_parallel.py --shards N --junit FRESH.xml -- pytest command")
    if os.environ.get("QDRANT_SIM_URL"):
        return subprocess.run([*command, f"--junitxml={args.junit}"], cwd=ROOT, check=False).returncode
    ids = collect(command)
    if not ids:
        raise SystemExit("sim collection selected no tests")
    groups = partition(ids, args.shards)
    print(f"sim_parallel: {len(ids)} tests across {len(groups)} shards "
          f"({', '.join(str(len(g)) for g in groups)})", flush=True)
    with tempfile.TemporaryDirectory(prefix="sim-shards-") as tmp:
        results: dict[int, tuple[int, str, Path]] = {}

        def run(index: int, group: list[str]) -> None:
            xml = Path(tmp) / f"shard-{index}.xml"
            done = subprocess.run([*command, f"--junitxml={xml}", *group], cwd=ROOT,
                                  capture_output=True, text=True, check=False)
            results[index] = (done.returncode, done.stdout + done.stderr, xml)

        threads = [threading.Thread(target=run, args=(i, g)) for i, g in enumerate(groups)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        code = 0
        parts = []
        for index in sorted(results):
            rc, output, xml = results[index]
            print(f"===== sim shard {index + 1}/{len(groups)} exit={rc} =====\n{output}", flush=True)
            code = code or rc
            if xml.exists():
                parts.append(xml)
            else:
                code = code or 1
        executed = merge_junit(parts, args.junit) if parts else []
    if len(executed) != len(ids):
        print(f"sim_parallel: executed {len(executed)} testcases but collected {len(ids)}", file=sys.stderr)
        code = code or 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
