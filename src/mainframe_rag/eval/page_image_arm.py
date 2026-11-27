"""Page-image A/B of the answer prompt (issue #1318, PR-0; tool only).

Decides, before any store, mount or agent change is built, whether showing
the reasoning model the rendered page of a packed gold-document hit improves
answers on table and syntax rows. Read-only: it changes no agent route,
prompt, default, chart or ingest behaviour.

Per selected golden row the tool

1. runs the shared answer core (`execute_answer_core`) with retrieval
   served by the running agent's `/v1/search` (the text arm: plan, candidate
   selection, packing, prompt, output cap and parse are the library's);
2. reads `source_sha256` and the page span of each packed gold-document hit
   from `/v1/evidence/{reference}` and resolves the file in a local corpus
   directory hashed once (never by doc id; no file or several files for one
   sha256 leaves the row unresolved);
3. runs the core again over the same hits with the same prompt text and the
   rendered page image(s) attached to the final user message as the #1287
   wire shape carries them (`ImageChatMessage`): the image arm;
4. grades both arms with the golden row's `gold_must_contain` and
   `gold_must_not_contain` strings and gives the row one outcome code.

The image arm adds no label line and no protocol wording: both arms send
byte-identical text, so the difference is the image alone. It also reserves
no image tokens (`LLM_IMAGE_TOKEN_RESERVE`); a window overflow is an
`image_error` row, never a broken answer.

Neighbour expansion needs stored-point reads and is not reproduced over HTTP
(the core's own fallback applies); the export says so. Packing runs under
this process's Settings, which must equal the agent's.

Privacy: the private report (`--out`, new, mode 0600, refused inside any
git work tree) holds answers. The export, stdout, stderr and logs carry row
ids, outcome codes, counts, parameters and the verdict only: never a
question, answer, heading, chunk text, doc id, sha256, page number, evidence
reference or image. Exception text is never printed.

Exit codes: 0 run completed (see the verdict); 1 completed with no scored
row; 2 refused (dataset, output path, corpus directory, arguments, model not
configured); 3 the image probe did not pass; 4 the image probe could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import inspect
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mainframe_rag.eval.datasets import DatasetError, read_dev_only_bytes

DEFAULT_CLASSES = ("table", "syntax")
# Design section 3 profile: 150 dpi, 8-bit grey PNG, at most 2 images an answer.
DEFAULT_DPI = 150
DEFAULT_MAX_IMAGES = 2
MAX_PAGES_PER_HIT = 2
MAX_IMAGE_BYTES = 1 << 20

# The closed set of per-row outcome codes (export vocabulary).
SCORED = ("both_pass", "fixed", "broken", "broken_must_not", "both_fail")
NOT_PACKED = ("gold_not_packed_text_pass", "gold_not_packed_text_fail")
UNRESOLVED = ("unresolved_reference", "unresolved_sha_missing", "unresolved_sha_ambiguous",
              "unresolved_page")
EXCLUDED = ("abstain_row", "no_gold_strings")
ERRORS = ("search_failed", "no_hits", "text_error", "image_error", "prompt_mismatch")
OUTCOMES = (*SCORED, *NOT_PACKED, *UNRESOLVED, *EXCLUDED, *ERRORS)

STOP_REASONS = ("few_text_failures", "few_fixed", "breaks", "mostly_not_packed")
MIN_TEXT_FAILURES = 6

REFUSALS = {
    "output": ("refused: --out and --export must be new files in a writable directory "
               "outside any git work tree, not named like the holdout or the release set"),
    "dataset": "refused: the golden file is not a readable dev dataset",
    "rows": "refused: golden rows need unique string ids",
    "classes": "refused: --classes takes a comma-separated list of query classes",
    "corpus": "refused: --corpus-dir is not a readable directory holding PDF files",
    "agent": "refused: --agent-url is required (the running agent's base URL)",
    "model": "refused: the reasoning model is not configured (LLM_BASE_URL, LLM_MODEL_REASONING)",
    "probe_failed": ("refused: the image probe did not pass for the configured reasoning model; "
                     "the image arm was not run"),
    "probe_unavailable": ("refused: the image probe could not be run "
                          "(scripts/probe_gateway.py; see --probe-script)"),
}
EXIT = {"output": 2, "dataset": 2, "rows": 2, "classes": 2, "corpus": 2, "agent": 2, "model": 2,
        "probe_failed": 3, "probe_unavailable": 4}


class Refused(Exception):
    """A fixed-message refusal; `code` keys REFUSALS and EXIT."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PageUnavailable(Exception):
    """The page cannot be rendered from the resolved file."""


class _PromptMismatch(Exception):
    """The image arm's prompt text differs from the text arm's."""


# --- the one rendering function (the only PyMuPDF use; docs/licensing.md) -----

def render_page_png(path: Path, page_index: int, dpi: int = DEFAULT_DPI) -> bytes:
    """One PDF page (0-based physical index) as an 8-bit grey PNG at `dpi`.

    The only place this module touches PyMuPDF, imported here so that
    importing the module, selecting rows and scoring never load it."""
    import pymupdf

    try:
        with pymupdf.open(path) as doc:
            if not 0 <= page_index < doc.page_count:
                raise PageUnavailable
            pixmap = doc[page_index].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
            data: bytes = pixmap.tobytes("png")
            return data
    except PageUnavailable:
        raise
    except Exception:  # noqa: BLE001 - library errors carry file text and paths
        raise PageUnavailable from None


# --- corpus index -------------------------------------------------------------

def sha256_file(path: Path) -> str:
    """sha256 of the file bytes, the ingest's `source_sha256` rule."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def index_corpus(root: Path) -> dict[str, list[Path]]:
    """Every PDF under `root` by content sha256, hashed once. A sha256 with
    more than one path stays ambiguous for the caller to refuse."""
    try:
        if not root.is_dir():
            raise Refused("corpus")
        index: dict[str, list[Path]] = {}
        for path in sorted(root.rglob("*")):
            if path.name.startswith(".") or path.suffix.lower() != ".pdf" or not path.is_file():
                continue
            index.setdefault(sha256_file(path), []).append(path)
    except OSError:
        raise Refused("corpus") from None
    if not index:
        raise Refused("corpus")
    return index


# --- the agent over HTTP ------------------------------------------------------

@dataclass
class AgentSearch:
    """The core's `Retriever` served by the running agent's `/v1/search`;
    keeps each returned hit's opaque evidence reference by chunk id."""

    client: Any
    references: dict[str, str] = field(default_factory=dict)

    def _search(self, query: str, product: str | None, version: str | None, limit: int) -> Any:
        from mainframe_rag.agent.core_ports import RetrievalResult
        from mainframe_rag.retrieve.query import SearchHit

        body: dict[str, Any] = {"query": query, "limit": limit}
        if product is not None:
            body["product"] = product
        if version is not None:
            body["version"] = version
        resp = self.client.post("/v1/search", json=body)
        if resp.status_code != 200:
            raise RuntimeError("search failed")
        data = resp.json()
        hits = []
        for raw in data["hits"]:
            hit = SearchHit.model_validate({k: v for k, v in raw.items() if k != "reference"})
            if isinstance(raw.get("reference"), str):
                self.references[hit.chunk_id] = raw["reference"]
            hits.append(hit)
        return RetrievalResult(hits, str(data.get("query_kind") or "unknown"), {})

    async def __call__(self, query: str, *, product: str | None, version: str | None,
                       settings: Any, limit: int | None = None) -> Any:
        return await asyncio.to_thread(
            self._search, query, product, version,
            settings.answer_hit_limit if limit is None else limit)


@dataclass(frozen=True)
class SourceSpan:
    """Where a packed hit lives: file identity and 0-based physical pages."""

    sha256: str
    pages: tuple[int, ...]


def read_source_span(client: Any, reference: str) -> SourceSpan | None:
    """`source_sha256` and the page span of one hit from the evidence route
    (1-based inclusive there, 0-based here). None on any failure; the stored
    text in the response is never kept."""
    try:
        resp = client.get(f"/v1/evidence/{quote(reference, safe='')}")
        if resp.status_code != 200:
            return None
        data = resp.json()
        sha = data["source_sha256"]
        first = data["location"]["physical_page_start"]
        last = data["location"]["physical_page_end"]
        last = first if last is None else last
        if (not isinstance(sha, str) or len(sha) != 64 or type(first) is not int
                or type(last) is not int or first < 1 or last < first):
            return None
        return SourceSpan(sha.lower(), tuple(range(first - 1, last)))
    except Exception:  # noqa: BLE001 - a missing signal, never guessed
        return None


# --- the two arms -------------------------------------------------------------

def _text_of(messages: Sequence[Any]) -> list[tuple[str, str]]:
    return [(m.role, m.content) for m in messages]


class _ArmModel:
    """The core's answer model for one arm. Text arm: forwards every call and
    keeps the last one's messages (the answer call). Image arm: forwards the
    one answer call with `image_urls` on its final user message, and only
    when its text equals the text arm's."""

    def __init__(self, inner: Any, expected: list[tuple[str, str]] | None = None,
                 image_urls: tuple[str, ...] = ()) -> None:
        self._inner = inner
        self._expected = expected
        self._image_urls = image_urls
        self.sent: list[tuple[str, str]] | None = None

    async def chat(self, messages: list[Any], reasoning_effort: str | None = None,
                   temperature: float | None = None, max_tokens: int | None = None) -> Any:
        from mainframe_rag.agent.images import ImageChatMessage

        outgoing = list(messages)
        if self._expected is not None:
            if _text_of(messages) != self._expected or self.sent is not None:
                raise _PromptMismatch
            last = outgoing[-1]
            outgoing[-1] = ImageChatMessage(
                role=last.role, content=last.content, image_urls=self._image_urls)
        self.sent = _text_of(messages)
        result = self._inner.chat(outgoing, reasoning_effort=reasoning_effort,
                                  temperature=temperature, max_tokens=max_tokens)
        if inspect.isawaitable(result):
            result = await result
        return result

    def stream(self, messages: list[Any], reasoning_effort: str, temperature: float,
               max_tokens: int | None = None) -> Any:
        """Never used: both arms run the buffered core."""
        raise NotImplementedError


def arm_failures(entry: dict[str, Any], answer: str, script: str | None) -> tuple[bool, bool]:
    """(gold strings missing, forbidden string present) for one arm's answer,
    by the answer eval's rules: case-folded literals over the answer body
    plus the extracted script."""
    from mainframe_rag.eval.answers import gold_text, missing_gold

    content = gold_text(answer.strip(), script)
    forbidden = any(str(s).lower() in content.lower()
                    for s in entry.get("gold_must_not_contain") or [])
    return bool(missing_gold(entry, content)), forbidden


def scored_outcome(text: tuple[bool, bool], image: tuple[bool, bool]) -> str:
    """The outcome of a row both arms answered. An arm passes when no gold
    string is missing and no forbidden string is present."""
    text_pass, image_pass = not any(text), not any(image)
    if text_pass and image_pass:
        return "both_pass"
    if not text_pass and image_pass:
        return "fixed"
    if not text_pass:
        return "both_fail"
    return "broken_must_not" if image[1] else "broken"


@dataclass
class Tool:
    """One run's ports and parameters."""

    settings: Any
    agent: Any
    model: Any
    tokenizer: Any
    corpus: dict[str, list[Path]]
    dpi: int = DEFAULT_DPI
    max_images: int = DEFAULT_MAX_IMAGES
    render: Callable[[Path, int, int], bytes] = render_page_png

    async def _core(self, query: str, model: _ArmModel, search: AgentSearch,
                    hits: list[Any] | None = None, kind: str | None = None) -> Any:
        from mainframe_rag.agent.answer_core import (
            AnswerCoreDeps,
            AnswerCoreInput,
            execute_answer_core,
        )

        deps = AnswerCoreDeps(settings=self.settings, llm=model, retrieve=search,
                              tokenizer=self.tokenizer)
        return await execute_answer_core(
            AnswerCoreInput(query=query, hits=hits, query_kind=kind), deps)

    def _images(self, gold: list[Any], search: AgentSearch) -> tuple[str, list[dict[str, Any]]]:
        """(outcome code or "", images) for the packed gold hits, in packed
        order: at most MAX_PAGES_PER_HIT pages a hit, `max_images` in all,
        each page once."""
        images: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for entry in gold:
            if len(images) >= self.max_images:
                break
            reference = search.references.get(entry.chunk_id)
            span = read_source_span(self.agent, reference) if reference else None
            if span is None:
                return "unresolved_reference", []
            paths = self.corpus.get(span.sha256, [])
            if len(paths) != 1:
                return ("unresolved_sha_missing" if not paths else "unresolved_sha_ambiguous"), []
            for page in span.pages[:MAX_PAGES_PER_HIT]:
                if len(images) >= self.max_images or (span.sha256, page) in seen:
                    continue
                seen.add((span.sha256, page))
                try:
                    png = self.render(paths[0], page, self.dpi)
                except (PageUnavailable, OSError):
                    return "unresolved_page", []
                if len(png) > MAX_IMAGE_BYTES:
                    return "unresolved_page", []
                images.append({"sha256": span.sha256, "page": page, "png": png,
                               "prompt_index": entry.prompt_index})
        return ("" if images else "unresolved_page"), images

    async def run_row(self, entry: dict[str, Any]) -> dict[str, Any]:
        """One golden row: its outcome code plus the private detail."""
        from mainframe_rag.agent.answer import PromptBudgetExceeded
        from mainframe_rag.agent.answer_core import LLMChatError, RetrievalError

        row: dict[str, Any] = {"id": entry["id"], "query_class": entry.get("query_class"),
                               "outcome": None, "image_n": 0, "image_bytes": 0}
        expected = set(entry.get("expected_doc_ids") or [])
        if entry.get("expected_behavior", "answer") != "answer" or not expected:
            return {**row, "outcome": "abstain_row"}
        if not entry.get("gold_must_contain"):
            return {**row, "outcome": "no_gold_strings"}
        search = AgentSearch(self.agent)
        text_model = _ArmModel(self.model)
        try:
            text = await self._core(entry["query"], text_model, search)
        except RetrievalError:
            return {**row, "outcome": "search_failed"}
        except (LLMChatError, PromptBudgetExceeded):
            return {**row, "outcome": "text_error"}
        if not text.hits or text_model.sent is None:
            return {**row, "outcome": "no_hits"}
        text_fail = arm_failures(entry, text.answer, text.script)
        row.update(text_answer=text.answer, text_missing_gold=text_fail[0],
                   text_forbidden=text_fail[1], text_prompt_tokens=text.usage.prompt_tokens,
                   packed_doc_ids=[e.doc_id for e in text.evidence.entries])
        gold = [e for e in text.evidence.entries if e.doc_id in expected and e.included_chars > 0]
        if not gold:
            return {**row, "outcome": NOT_PACKED[1] if any(text_fail) else NOT_PACKED[0]}
        code, images = self._images(gold, search)
        if code:
            return {**row, "outcome": code}
        row.update(image_n=len(images), image_bytes=sum(len(i["png"]) for i in images),
                   image_pages=[{"sha256": i["sha256"], "page": i["page"],
                                 "prompt_index": i["prompt_index"]} for i in images])
        urls = tuple("data:image/png;base64," + base64.b64encode(i["png"]).decode("ascii")
                     for i in images)
        image_model = _ArmModel(self.model, expected=text_model.sent, image_urls=urls)
        try:
            image = await self._core(entry["query"], image_model, search,
                                     hits=list(text.hits), kind=text.query_kind)
        except LLMChatError as exc:
            mismatch = isinstance(exc.__cause__, _PromptMismatch)
            return {**row, "outcome": "prompt_mismatch" if mismatch else "image_error"}
        except (_PromptMismatch, PromptBudgetExceeded):
            return {**row, "outcome": "prompt_mismatch"}
        if image_model.sent is None:
            return {**row, "outcome": "prompt_mismatch"}
        image_fail = arm_failures(entry, image.answer, image.script)
        row.update(image_answer=image.answer, image_missing_gold=image_fail[0],
                   image_forbidden=image_fail[1], image_prompt_tokens=image.usage.prompt_tokens)
        return {**row, "outcome": scored_outcome(text_fail, image_fail)}


# --- the stop rule ------------------------------------------------------------

def stop_rule(counts: dict[str, int]) -> dict[str, Any]:
    """The design's stop rule over outcome counts (section 6), as a decision
    aid. Stop when any holds: fewer than 6 text-arm failures among the scored
    rows; the image arm fixes fewer than one third of them; it breaks more
    than one text-arm pass, or breaks any by a forbidden (must-not) string;
    or more text-arm failures are "gold not packed" than scored."""
    n = {code: int(counts.get(code, 0)) for code in OUTCOMES}
    failures = n["fixed"] + n["both_fail"]
    breaks = n["broken"] + n["broken_must_not"]
    reasons = []
    if failures < MIN_TEXT_FAILURES:
        reasons.append("few_text_failures")
    if 3 * n["fixed"] < failures:
        reasons.append("few_fixed")
    if breaks > 1 or n["broken_must_not"] > 0:
        reasons.append("breaks")
    if n["gold_not_packed_text_fail"] > failures:
        reasons.append("mostly_not_packed")
    return {
        "verdict": "stop" if reasons else "continue",
        "reasons": reasons,
        "scored": sum(n[c] for c in SCORED),
        "text_failures": failures,
        "fixed": n["fixed"],
        "breaks": breaks,
        "breaks_must_not": n["broken_must_not"],
        "not_packed": sum(n[c] for c in NOT_PACKED),
        "not_packed_text_failures": n["gold_not_packed_text_fail"],
        "unresolved": sum(n[c] for c in UNRESOLVED),
        "errors": sum(n[c] for c in ERRORS),
        "excluded": sum(n[c] for c in EXCLUDED),
    }


def build_export(rows: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    """The text-free export: parameters, counts, row id plus outcome code,
    and the verdict. Anything else in a row stays in the private report."""
    counts = Counter(row["outcome"] for row in rows)
    token_deltas = [row["image_prompt_tokens"] - row["text_prompt_tokens"] for row in rows
                    if row["outcome"] in SCORED and row.get("image_prompt_tokens")
                    and row.get("text_prompt_tokens")]
    return {
        "schema": "page-image-ab/1",
        "params": params,
        "n": len(rows),
        "counts": {code: counts.get(code, 0) for code in OUTCOMES},
        "images": {"rows": sum(1 for r in rows if r["image_n"]),
                   "n": sum(r["image_n"] for r in rows),
                   "bytes": sum(r["image_bytes"] for r in rows),
                   "prompt_token_delta_mean": (round(sum(token_deltas) / len(token_deltas))
                                               if token_deltas else None)},
        "rows": [{"id": row["id"], "outcome": row["outcome"]} for row in rows],
        "stop_rule": stop_rule(counts),
    }


# --- entry point --------------------------------------------------------------

def default_probe_script() -> Path:
    """`scripts/probe_gateway.py` beside `src/` (checkout and images alike)."""
    return Path(__file__).resolve().parents[3] / "scripts" / "probe_gateway.py"


def run_image_probe(script: Path) -> bool:
    """`probe_gateway.py --probe-image` for the configured model: True on its
    PASS (exit 0). Its output is one row of numbers and a fixed label."""
    if not script.is_file():
        raise Refused("probe_unavailable")
    try:
        done = subprocess.run(
            [sys.executable, str(script), "--probe-image"], stdout=sys.stderr, check=False)
    except OSError:
        raise Refused("probe_unavailable") from None
    return done.returncode == 0


def _classes(raw: str) -> list[str]:
    classes = [c.strip() for c in raw.split(",") if c.strip()]
    if not classes or len(set(classes)) != len(classes):
        raise Refused("classes")
    return classes


def _bounded(low: int, high: int) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        value = int(raw)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value
    return parse


def select_rows(raw: bytes, classes: Sequence[str]) -> list[dict[str, Any]]:
    """Rows of the requested query classes, in file order."""
    try:
        records = [json.loads(line) for line in map(str.strip, raw.decode("utf-8").splitlines())
                   if line and not line.startswith("#")]
    except ValueError:
        raise Refused("dataset") from None
    if any(not isinstance(r, dict) or not isinstance(r.get("query"), str) for r in records):
        raise Refused("dataset")
    rows = [r for r in records if r.get("query_class") in classes]
    ids = [r.get("id") for r in rows]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise Refused("rows")
    return rows


def _write(path: Path, data: bytes) -> None:
    from mainframe_rag.eval import gold_candidates

    try:
        gold_candidates.write_private(path, data)
    except gold_candidates.Refused:
        raise Refused("output") from None


def main(
    argv: list[str] | None = None, *,
    settings: Any = None, agent: Any = None, model: Any = None, tokenizer: Any = None,
    probe: Callable[[], bool] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Page-image A/B of the answer prompt on table/syntax rows (#1318).")
    parser.add_argument("--golden", type=Path, required=True, help="dev golden JSONL path")
    parser.add_argument("--corpus-dir", type=Path, required=True,
                        help="local copy of the served corpus (PDF files), hashed once")
    parser.add_argument("--agent-url", default=None, help="running agent base URL")
    parser.add_argument("--out", type=Path, required=True,
                        help="new private JSONL report (answers), outside any git work tree")
    parser.add_argument("--export", type=Path, required=True,
                        help="new text-free JSON export (ids, codes, counts, verdict)")
    parser.add_argument("--classes", default=",".join(DEFAULT_CLASSES),
                        help="comma-separated query classes (default: table,syntax)")
    parser.add_argument("--dpi", type=_bounded(50, 300), default=DEFAULT_DPI)
    parser.add_argument("--max-images", type=_bounded(1, 4), default=DEFAULT_MAX_IMAGES)
    parser.add_argument("--request-timeout", type=float, default=120.0)
    parser.add_argument("--probe-script", type=Path, default=None,
                        help="path of scripts/probe_gateway.py (default: beside src/)")
    args = parser.parse_args(argv)
    from mainframe_rag.eval import gold_candidates

    try:
        # Every refusal that needs no service comes before the first call.
        classes = _classes(args.classes)
        for path in (args.out, args.export):
            try:
                gold_candidates.refuse_output(path)
            except gold_candidates.Refused:
                raise Refused("output") from None
        if args.out.absolute() == args.export.absolute():
            raise Refused("output")
        if agent is None and not args.agent_url:
            raise Refused("agent")
        try:
            raw = read_dev_only_bytes(args.golden)
        except DatasetError:
            raise Refused("dataset") from None
        rows = select_rows(raw, classes)
        if settings is None:
            from mainframe_rag.config import load_settings

            settings = load_settings()
        if not settings.llm_base_url or not settings.llm_model_reasoning:
            raise Refused("model")
        script = args.probe_script or default_probe_script()
        if not (probe() if probe is not None else run_image_probe(script)):
            raise Refused("probe_failed")
        corpus = index_corpus(args.corpus_dir)
    except Refused as exc:
        print(REFUSALS[exc.code], file=sys.stderr)
        return EXIT[exc.code]

    close: list[Any] = []
    try:
        if agent is None:
            import httpx2

            agent = httpx2.Client(base_url=args.agent_url.rstrip("/"), follow_redirects=False,
                                  timeout=args.request_timeout, trust_env=False)
            close.append(agent)
        if model is None:
            from mainframe_rag.agent.answer import HttpxLLMClient
            from mainframe_rag.agent.tokenizer import build_tokenizer

            model = HttpxLLMClient(settings)
            close.append(model)
            tokenizer = build_tokenizer(settings)
        tool = Tool(settings=settings, agent=agent, model=model, tokenizer=tokenizer,
                    corpus=corpus, dpi=args.dpi, max_images=args.max_images)

        async def run_all() -> list[dict[str, Any]]:
            done = []
            for number, entry in enumerate(rows, 1):
                result = await tool.run_row(entry)
                print(f"[{number}/{len(rows)}] {result['id']}: {result['outcome']}",
                      file=sys.stderr)
                done.append(result)
            return done

        results = asyncio.run(run_all())
    finally:
        for owner in close:
            owner.close()

    params = {
        "golden": {"name": args.golden.name, "sha256": hashlib.sha256(raw).hexdigest()},
        "classes": classes,
        "dpi": args.dpi,
        "max_images": args.max_images,
        "colorspace": "gray",
        "format": "png",
        "corpus_files": sum(len(paths) for paths in corpus.values()),
        "model": settings.llm_model_reasoning,
        "answer_hit_limit": settings.answer_hit_limit,
        "answer_select_enabled": bool(settings.answer_select_enabled),
        "answer_expand_enabled": bool(settings.answer_expand_enabled),
        "expansion": "not_reproduced",
        "image_text": "identical",
    }
    export = build_export(results, params)
    try:
        _write(args.out, "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in results).encode("utf-8"))
        _write(args.export, (json.dumps(export, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    except Refused as exc:
        print(REFUSALS[exc.code], file=sys.stderr)
        return EXIT[exc.code]
    rule = export["stop_rule"]
    print(json.dumps({"counts": {k: v for k, v in export["counts"].items() if v},
                      "stop_rule": rule}, sort_keys=True))
    print(f"VERDICT {rule['verdict']}"
          + (f" ({', '.join(rule['reasons'])})" if rule["reasons"] else "")
          + ": a decision aid over a small sample, not a statistic", file=sys.stderr)
    return 0 if rule["scored"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
