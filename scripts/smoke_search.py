#!/usr/bin/env python3
"""In-cluster smoke test for /v1/search (issue #8). Runs inside the agent image.

Exits 1 if the search returns fewer than --min-hits hits or none of the hits
(cite / heading / text) contain the expected substring. Prints hits for the CI log.
"""

from __future__ import annotations

import argparse
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://rag-agent:8080")
    parser.add_argument("--query", required=True)
    parser.add_argument("--expect", required=True, help="substring that must appear in a hit")
    parser.add_argument("--min-hits", type=int, default=1)
    args = parser.parse_args()

    resp = httpx.post(f"{args.url.rstrip('/')}/v1/search",
                      json={"query": args.query, "limit": 8}, timeout=30)
    resp.raise_for_status()
    hits = resp.json()["hits"]
    blob = "\n".join(
        f"{h.get('cite', '')}\n{h.get('heading', '')}\n{h.get('text', '')}" for h in hits
    ).lower()

    ok = len(hits) >= args.min_hits and args.expect.lower() in blob
    print(f"smoke: hits={len(hits)} expect={args.expect!r} found={ok}")
    if not ok:
        print(blob[:2000])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
