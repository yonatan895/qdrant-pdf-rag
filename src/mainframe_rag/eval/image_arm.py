"""Pasted-image arms of the chat eval (issue #1287, design §6).

`--endpoint chat --image-arm {text,image,image+ask}` asks the same golden rows
three ways: the question typed (today's body), an image-only user turn, or
the image plus a short fixed typed question. The image is drawn at run time
from the row's question as a synthetic 80-column, green-on-black terminal
panel by the stdlib renderer (`mainframe_rag.synthetic_png`); nothing is
committed and no font or image library is involved.

Each image row records the image size and count, how many `image_reads` the
agent's finish chunk returned, its `image_read_failed` flag, and the
transcript character error rate (CER) against the text that was actually
drawn. The transcript itself never reaches the row: it is read off the
recorded frames, reduced to one number here, and dropped. The export carries
the CER only as a fixed bucket label.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import textwrap
from dataclasses import dataclass
from typing import Any

IMAGE_ARMS = ("text", "image", "image+ask")
PANEL_COLUMNS = 80
# Draft wording (maintainer reviews): the typed question of the image+ask arm.
IMAGE_ASK = "What does this mean and what do I do?"
# The CER export labels, in order; `none` is "no transcript to score".
CER_BUCKETS = ("0", "<0.05", "<0.2", "<0.5", ">=0.5", "none")
# The agent's `ImageRead.shown`: SCREEN TEXT, then this line when the model
# described the image (agent/images.py). Only the screen text is scored.
_DESCRIPTION = "Description: "
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ImageTurn:
    """One row's request under an arm. `body` is None for the text arm (the
    run keeps today's single-turn body); `reference` is the drawn text."""

    arm: str
    body: dict[str, Any] | None
    image_n: int
    image_bytes: int
    reference: str | None


def image_ask_sha256() -> str:
    """Identity of the image+ask wording for `params` (the wording itself
    stays out of the export)."""
    return hashlib.sha256(IMAGE_ASK.encode("utf-8")).hexdigest()


def panel_lines(question: str) -> list[str]:
    """The question wrapped to the panel width, each line padded to it so
    every panel is 80 columns wide."""
    lines = textwrap.wrap(question, width=PANEL_COLUMNS, break_long_words=True,
                          break_on_hyphens=False) or [""]
    return [line.ljust(PANEL_COLUMNS) for line in lines]


def normalize(text: str) -> str:
    """Case and layout out of the comparison: the font has no lowercase, and
    wrapping is the panel's, not the reader's."""
    return _SPACE_RE.sub(" ", text).strip().upper()


def build_turn(arm: str, question: str) -> ImageTurn:
    """The row's request under `arm`. Raises ValueError for an unknown arm."""
    if arm not in IMAGE_ARMS:
        raise ValueError("unknown image arm")
    if arm == "text":
        return ImageTurn(arm=arm, body=None, image_n=0, image_bytes=0, reference=None)
    from mainframe_rag.synthetic_png import render_text_png, rendered_text

    lines = panel_lines(question)
    png = render_text_png(lines)
    image = {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}}
    content: list[dict[str, Any]] = [image]
    if arm == "image+ask":
        content = [{"type": "text", "text": IMAGE_ASK}, image]
    return ImageTurn(
        arm=arm,
        body={"messages": [{"role": "user", "content": content}], "stream": True},
        image_n=1,
        image_bytes=len(png),
        reference=normalize(" ".join(rendered_text(line) for line in lines)),
    )


def levenshtein(a: str, b: str) -> int:
    """Edit distance (insert, delete, substitute; each costs 1)."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def screen_text(shown: str) -> str:
    """The SCREEN TEXT part of one `image_reads[].text` (the agent appends
    the description as a last paragraph)."""
    if shown.startswith(_DESCRIPTION):
        return ""
    head, sep, _ = shown.rpartition("\n\n" + _DESCRIPTION)
    return head if sep else shown


def character_error_rate(reference: str, transcript: str) -> float | None:
    """Edits to turn the read screen text into the drawn text, per drawn
    character; both normalized. None without a reference."""
    if not reference:
        return None
    return levenshtein(reference, normalize(screen_text(transcript))) / len(reference)


def cer_bucket(cer: Any) -> str:
    """A CER as a fixed export label."""
    if type(cer) not in (int, float) or math.isnan(cer) or cer < 0:
        return "none"
    if cer == 0:
        return "0"
    for bound, label in ((0.05, "<0.05"), (0.2, "<0.2"), (0.5, "<0.5")):
        if cer < bound:
            return label
    return ">=0.5"


def _finish_choice(frames: list[str]) -> dict[str, Any] | None:
    for line in reversed(frames):
        raw = line[5:].strip()
        if raw == "[DONE]":
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        choices = payload.get("choices") if isinstance(payload, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else None
        if isinstance(choice, dict) and choice.get("finish_reason") is not None:
            return choice
        return None
    return None


def unsent_fields(arm: str) -> dict[str, Any]:
    """Row fields when no request was sent (health failure)."""
    return {"image_arm": arm, "image_n": None, "image_bytes": None, "image_reads_n": None,
            "image_read_failed": None, "image_cer": None}


def apply_image_reads(row: dict[str, Any], turn: ImageTurn, frames: list[str] | None) -> None:
    """Add the row's image fields from the recorded stream frames. Unknown
    stays None: a missing or malformed `image_reads` is not a zero."""
    row.update(image_arm=turn.arm, image_n=turn.image_n, image_bytes=turn.image_bytes,
               image_reads_n=None, image_read_failed=None, image_cer=None)
    if turn.reference is None:
        return
    choice = _finish_choice(frames or [])
    if choice is None:
        return
    row["image_read_failed"] = choice.get("image_read_failed") is True
    reads = choice.get("image_reads")
    if not isinstance(reads, list) or any(
        not isinstance(r, dict) or not isinstance(r.get("text"), str) for r in reads
    ):
        return
    row["image_reads_n"] = len(reads)
    if reads:
        # One image per row: the first read is its read.
        row["image_cer"] = character_error_rate(turn.reference, reads[0]["text"])


def image_metrics(rows: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    """Counts only: rows per CER bucket, rows without reads, failed reads."""
    image_rows = [r for r in rows if "image_arm" in r]
    buckets = dict.fromkeys(CER_BUCKETS, 0)
    for row in image_rows:
        buckets[cer_bucket(row.get("image_cer"))] += 1
    return {
        "arm": arm,
        "n": len(image_rows),
        "cer": buckets if arm != "text" else None,
        "reads_missing": (sum(1 for r in image_rows if r.get("image_reads_n") in (None, 0))
                          if arm != "text" else None),
        "read_failed": (sum(1 for r in image_rows if r.get("image_read_failed") is True)
                        if arm != "text" else None),
    }
