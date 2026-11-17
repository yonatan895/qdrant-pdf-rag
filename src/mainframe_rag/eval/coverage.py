"""Corpus coverage check (issue #1191): which wishlist documents does the
deployed corpus serve?

    python -m mainframe_rag.eval.coverage --agent-url URL [--wishlist PATH] [--out FILE]

Reads the served build's catalog through `GET /v1/sources` (the answer
eval's `fetch_coverage`, every page of one build) and matches each wishlist
item (evals/corpus-wishlist.json) against it. Read-only: no search, model or
storage write.

What leaves the process (docs/eval.md, Corpus coverage check): wishlist ids,
titles, categories and priorities, a status per item, the doc ids that
matched a wishlist form number, and title-matched doc ids of a Redbook or
Redpaper number (`SG24-6366-02`, `sg246366`, `REDP-5123`). Any other
title-matched doc id is only counted (`other_matches`): a form-number shape
alone does not prove a public book, since ingest mints that shape from any
site filename or vendor PDF. Catalog titles, unmatched doc ids,
the build id and catalog sizes are never printed or written, so the site's
private documents stay in the gap.

Matching
    - doc_number rule: the doc id, hyphens dropped, is the form number with
      an optional two-digit edition (`SA22-7832-13`, `SA227832`). Any hit is
      `present`.
    - title rule (a `match: title` item, or a numbered item's fallback):
      every keyword of one group occurs in a catalog title as a whole word
      (case-insensitive), optionally only on doc ids that start with one of
      `doc_id_prefixes`. For a `match: title` item, one document family
      (editions of one number count once) is `present`; more than one is
      `ambiguous` unless the item sets `expect_many` (a series). A numbered
      item found only by title is always `ambiguous`: it may be a sibling
      book or a renumbered edition.
    - nothing matched: `missing`.

Exit status: 0 on a completed run whatever the statuses, 1 when the catalog
cannot be read (no partial output), 2 on a usage or wishlist error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx2

from mainframe_rag.eval.answers import _agent_url, _request_timeout, fetch_coverage

SCHEMA = "corpus_coverage/1"
WISHLIST_SCHEMA = "corpus_wishlist/1"
DEFAULT_WISHLIST = Path(__file__).resolve().parents[3] / "evals" / "corpus-wishlist.json"
CATEGORIES = ("hardware_language_reference", "hlasm_library", "language_tool_library",
              "redbook", "practitioner")
STATUSES = ("present", "missing", "ambiguous")
MATCH_KINDS = ("doc_number", "title")

_ITEM_KEYS = frozenset({"id", "category", "title", "priority", "match", "doc_numbers",
                        "title_keywords", "doc_id_prefixes", "expect_many"})
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_NUMBER_RE = re.compile(r"[A-Z]{2,4}\d{2}-\d{4}")  # a form number, no edition suffix
_PREFIX_RE = re.compile(r"[A-Za-z0-9]{1,8}")
# A title-matched doc id is reported by name only as a Redbook or Redpaper
# number. A generic form-number shape is not enough: ingest derives it from
# any site filename (`OPS01-0042.pdf`) or vendor PDF, so such ids stay a
# count. A number hit is the wishlist's own number by construction and is
# always reported.
_PUBLIC_ID_RE = re.compile(r"(?i:SG24|REDP)-?\d{4}(?:-?\d{2})?")
# The family of an id: hyphens dropped, edition removed (REDP first, so its
# edition is not read as part of the number).
_FAMILY_RE = re.compile(r"(REDP\d{4}|[A-Z]{2,4}\d{6})(?:\d{2})?")
_REASON_RE = re.compile(r"[a-z0-9_]{1,32}")


class WishlistError(ValueError):
    """An invalid wishlist; the message is fixed text plus wishlist ids."""


def _normalize(text: str) -> str:
    """Case-folded, typographic apostrophes unified then dropped, whitespace
    collapsed: "User’s Guide" and "users  guide" compare equal."""
    return " ".join(text.replace("’", "'").replace("'", "").casefold().split())


def _keyword_re(keyword: str) -> re.Pattern[str]:
    return re.compile(r"(?<![0-9a-z])" + re.escape(_normalize(keyword)) + r"(?![0-9a-z])")


def _str_list(value: Any, where: str, pattern: re.Pattern[str] | None = None) -> list[str]:
    if (not isinstance(value, list) or not value
            or any(not isinstance(v, str) or not v.strip() for v in value)):
        raise WishlistError(f"{where} must be a non-empty list of strings")
    if pattern is not None and any(not pattern.fullmatch(v) for v in value):
        raise WishlistError(f"{where} has a value of the wrong shape")
    return list(value)


def _item(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WishlistError(f"item {index} must be an object")
    item_id = raw.get("id")
    if not isinstance(item_id, str) or not _ID_RE.fullmatch(item_id):
        raise WishlistError(f"item {index} needs an id of lower-case letters, digits and hyphens")
    where = f"item {item_id}"
    if set(raw) - _ITEM_KEYS:
        raise WishlistError(f"{where} has unknown keys")
    if raw.get("category") not in CATEGORIES:
        raise WishlistError(f"{where} has an unknown category")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise WishlistError(f"{where} needs a title of 1-200 characters")
    priority = raw.get("priority")
    if type(priority) is not int or not 1 <= priority <= 3:
        raise WishlistError(f"{where} needs a priority of 1, 2 or 3")
    match = raw.get("match")
    if match not in MATCH_KINDS:
        raise WishlistError(f"{where} needs match doc_number or title")
    item: dict[str, Any] = {"id": item_id, "category": raw["category"], "title": title,
                            "priority": priority, "match": match,
                            "expect_many": raw.get("expect_many", False)}
    if type(item["expect_many"]) is not bool:
        raise WishlistError(f"{where} expect_many must be true or false")
    if match == "doc_number":
        item["doc_numbers"] = _str_list(raw.get("doc_numbers"), f"{where} doc_numbers", _NUMBER_RE)
    elif "doc_numbers" in raw:
        raise WishlistError(f"{where} is match title but lists doc_numbers")
    groups = raw.get("title_keywords")
    if groups is None and match == "title":
        raise WishlistError(f"{where} is match title but has no title_keywords")
    if groups is not None:
        if not isinstance(groups, list) or not groups:
            raise WishlistError(f"{where} title_keywords must be a non-empty list of lists")
        item["title_keywords"] = [_str_list(g, f"{where} title_keywords") for g in groups]
    if "doc_id_prefixes" in raw:
        item["doc_id_prefixes"] = _str_list(
            raw["doc_id_prefixes"], f"{where} doc_id_prefixes", _PREFIX_RE)
    return item


def load_wishlist(path: Path) -> list[dict[str, Any]]:
    """The validated wishlist items, in file order."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise WishlistError("the wishlist file cannot be read") from None
    except ValueError:
        raise WishlistError("the wishlist file is not JSON") from None
    if not isinstance(data, dict) or data.get("schema") != WISHLIST_SCHEMA:
        raise WishlistError(f"the wishlist schema must be {WISHLIST_SCHEMA}")
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise WishlistError("the wishlist needs a non-empty items list")
    items = [_item(raw, i) for i, raw in enumerate(raw_items)]
    seen: set[str] = set()
    for item in items:
        if item["id"] in seen:
            raise WishlistError(f"item {item['id']} is listed twice")
        seen.add(item["id"])
    return items


def _family(doc_id: str) -> str:
    compact = doc_id.replace("-", "").upper()
    found = _FAMILY_RE.fullmatch(compact)
    return found.group(1) if found else doc_id


def match_item(item: dict[str, Any], catalog: dict[str, list[str]]) -> dict[str, Any]:
    """One wishlist item against the catalog (doc id -> normalized titles).
    Only the wishlist's own fields and public-shaped doc ids are returned."""
    numbers = {n.replace("-", "") for n in item.get("doc_numbers", ())}
    hits = sorted(d for d in catalog if numbers and _family(d) in numbers)
    if hits:
        return _row(item, "present", "doc_number", hits, hits)
    matched_by: str | None = None
    status = "missing"
    if item.get("title_keywords"):
        groups = [[_keyword_re(k) for k in group] for group in item["title_keywords"]]
        prefixes = tuple(p.upper() for p in item.get("doc_id_prefixes", ()))
        hits = sorted(
            d for d, titles in catalog.items()
            if (not prefixes or d.upper().startswith(prefixes))
            and any(all(k.search(t) for k in group) for group in groups for t in titles))
        if hits:
            matched_by = "title"
            families = {_family(d) for d in hits}
            # A numbered item found only by title may be another book (a
            # sibling guide) or a renumbered edition: the maintainer decides.
            single = len(families) == 1 or item["expect_many"]
            status = "present" if single and item["match"] == "title" else "ambiguous"
    return _row(item, status, matched_by, hits, [d for d in hits if _PUBLIC_ID_RE.fullmatch(d)])


def _row(item: dict[str, Any], status: str, matched_by: str | None, hits: list[str],
         public: list[str]) -> dict[str, Any]:
    return {"id": item["id"], "title": item["title"], "category": item["category"],
            "priority": item["priority"], "status": status, "matched_by": matched_by,
            "doc_ids": public, "other_matches": len(hits) - len(public)}


def build_catalog(doc_ids: Iterable[str], titles: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    catalog: dict[str, list[str]] = {d: [] for d in doc_ids}
    for doc_id, title in titles:
        catalog.setdefault(doc_id, []).append(_normalize(title))
    return catalog


def coverage_report(items: list[dict[str, Any]], catalog: dict[str, list[str]],
                    wishlist_sha256: str) -> dict[str, Any]:
    rows = [match_item(item, catalog) for item in items]
    counts = {s: sum(r["status"] == s for r in rows) for s in STATUSES}
    return {"schema": SCHEMA, "wishlist_sha256": wishlist_sha256, "counts": counts, "items": rows}


def format_table(report: dict[str, Any]) -> str:
    rows = report["items"]
    width = max([len("ID")] + [len(r["id"]) for r in rows])
    lines = [f"{'ID':<{width}}  PRI  {'STATUS':<9}  {'MATCHED BY':<10}  DOC IDS / TITLE"]
    for r in rows:
        ids = ", ".join(r["doc_ids"])
        if r["other_matches"]:
            ids = (ids + ", " if ids else "") + f"+{r['other_matches']} unnumbered"
        lines.append(f"{r['id']:<{width}}  {r['priority']:>3}  {r['status']:<9}  "
                     f"{r['matched_by'] or '-':<10}  {ids or '-'}  [{r['title']}]")
    counts = report["counts"]
    lines.append(", ".join(f"{counts[s]} {s}" for s in STATUSES))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mainframe_rag.eval.coverage",
        description="Compare the deployed corpus catalog with the wishlist (read-only).")
    parser.add_argument("--agent-url", type=_agent_url, required=True,
                        help="the agent's base URL, e.g. a port-forward http://127.0.0.1:8087")
    parser.add_argument("--wishlist", type=Path, default=DEFAULT_WISHLIST,
                        help="wishlist JSON (default: evals/corpus-wishlist.json of this checkout)")
    parser.add_argument("--out", type=Path, default=None,
                        help="also write the JSON export to this file")
    parser.add_argument("--request-timeout", type=_request_timeout, default=60.0,
                        help="per-request timeout in seconds (default 60)")
    args = parser.parse_args(argv)

    try:
        items = load_wishlist(args.wishlist)
        wishlist_sha256 = hashlib.sha256(args.wishlist.read_bytes()).hexdigest()
    except WishlistError as exc:
        print(f"error: invalid wishlist: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("error: invalid wishlist: the wishlist file cannot be read", file=sys.stderr)
        return 2

    with httpx2.Client(base_url=args.agent_url, timeout=args.request_timeout,
                       follow_redirects=False, trust_env=False) as client:
        served = fetch_coverage(client)
    if served["status"] != "known":
        reason = served["reason"] if _REASON_RE.fullmatch(str(served["reason"])) else "unknown"
        print(f"error: the agent's source catalog could not be read ({reason})", file=sys.stderr)
        return 1

    report = coverage_report(items, build_catalog(served["doc_ids"], served["titles"]),
                             wishlist_sha256)
    sys.stdout.write(format_table(report))
    if args.out is not None:
        try:
            args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except OSError:
            print("error: the --out file cannot be written", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
