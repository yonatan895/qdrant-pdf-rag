#!/usr/bin/env python3
"""Offline GitHub issue snapshot for the dev kit (#1118, ADR-0009 §8).

Fetches each issue named in scripts/devkit/snapshot-issues.txt and all of its
comments from the public repository, keeps a fixed set of fields, and writes one
JSON document. The snapshot time is printed on stdout for the kit MANIFEST.

  python3 scripts/devkit/issue_snapshot.py --repo OWNER/REPO --output issues.json

A token in GH_TOKEN or GITHUB_TOKEN is sent only to raise the rate limit; it is
never written. Any text that matches a credential pattern refuses the snapshot,
naming the issue and comment id only: redact it on GitHub, or drop the issue
from the list. Standard library only. Exit 0 written, 2 refused.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ISSUES_FILE = ROOT / "scripts" / "devkit" / "snapshot-issues.txt"
API = "https://api.github.com"
PAGE = 100
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# Shapes of credentials that must never travel in the kit. A match refuses the
# snapshot; the message names where, never what.
CREDENTIAL = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bsha256~[A-Za-z0-9_-]{30,}"
)


class SnapshotError(Exception):
    """A fixed, operator-facing refusal."""


def issue_numbers(path: Path) -> list[int]:
    numbers: list[int] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise SnapshotError(f"{path.name} is missing or unreadable") from None
    for line in lines:
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        if not text.isdigit() or int(text) <= 0 or int(text) in numbers:
            raise SnapshotError(f"{path.name}: each line must hold one new issue number")
        numbers.append(int(text))
    if not numbers:
        raise SnapshotError(f"{path.name} names no issue")
    return numbers


def api_base(value: str) -> str:
    parts = urllib.parse.urlsplit(value)
    loopback = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")
    if not (parts.scheme == "https" or loopback) or parts.query or parts.fragment:
        raise SnapshotError("the API base must be https (loopback http only for tests)")
    return value.rstrip("/")


class Client:
    def __init__(self, base: str, token: str | None) -> None:
        self.base = base
        self.token = token

    def get(self, path: str) -> object:
        request = urllib.request.Request(self.base + path, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mfr-devkit-snapshot",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code < 500 or attempt == 3:
                    raise SnapshotError(f"GitHub API answered HTTP {exc.code}") from None
            except (OSError, ValueError):  # URLError, resets and timeouts are OSErrors
                if attempt == 3:
                    raise SnapshotError("GitHub API is unreachable or answered invalid JSON") from None
            time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    def pages(self, path: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            batch = self.get(f"{path}?per_page={PAGE}&page={page}")
            if not isinstance(batch, list) or not all(isinstance(x, dict) for x in batch):
                raise SnapshotError("GitHub API answered an unexpected comment list")
            items += batch
            if len(batch) < PAGE:
                return items
            page += 1


def _login(user: object) -> str | None:
    return user.get("login") if isinstance(user, dict) else None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def snapshot(client: Client, repo: str, numbers: list[int], now: str) -> dict:
    issues = []
    for number in numbers:
        issue = client.get(f"/repos/{repo}/issues/{number}")
        if not isinstance(issue, dict) or issue.get("number") != number:
            raise SnapshotError(f"GitHub API answered an unexpected record for issue #{number}")
        if CREDENTIAL.search(_text(issue.get("title")) + "\n" + _text(issue.get("body"))):
            raise SnapshotError(
                f"issue #{number} body matches a credential pattern; redact it on GitHub "
                f"or drop the issue from {ISSUES_FILE.name}")
        comments = []
        for comment in client.pages(f"/repos/{repo}/issues/{number}/comments"):
            if CREDENTIAL.search(_text(comment.get("body"))):
                raise SnapshotError(
                    f"issue #{number} comment {comment.get('id')} matches a credential pattern; "
                    f"redact it on GitHub or drop the issue from {ISSUES_FILE.name}")
            comments.append({
                "id": comment.get("id"),
                "author": _login(comment.get("user")),
                "created_at": comment.get("created_at"),
                "updated_at": comment.get("updated_at"),
                "url": comment.get("html_url"),
                "body": _text(comment.get("body")),
            })
        issues.append({
            "number": number,
            "title": _text(issue.get("title")),
            "state": issue.get("state"),
            "labels": sorted(_text(label.get("name")) for label in issue.get("labels") or []
                             if isinstance(label, dict)),
            "author": _login(issue.get("user")),
            "created_at": issue.get("created_at"),
            "updated_at": issue.get("updated_at"),
            "closed_at": issue.get("closed_at"),
            "url": issue.get("html_url"),
            "body": _text(issue.get("body")),
            "comments": comments,
        })
    return {"schema_version": 1, "repository": repo, "snapshot_time": now, "issues": issues}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--issues-file", type=Path, default=ISSUES_FILE)
    args = parser.parse_args(argv)
    try:
        if not REPO.fullmatch(args.repo):
            raise SnapshotError("--repo must be OWNER/REPO")
        numbers = issue_numbers(args.issues_file)
        base = api_base(os.environ.get("DEVKIT_GITHUB_API") or API)
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or None
        now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        document = snapshot(Client(base, token), args.repo, numbers, now)
        args.output.write_text(json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                               encoding="utf-8")
    except SnapshotError as exc:
        print(f"issue-snapshot: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("issue-snapshot: the snapshot could not be written", file=sys.stderr)
        return 2
    print(now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
