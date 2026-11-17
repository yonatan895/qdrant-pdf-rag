# mypy: disallow_untyped_defs=True, disallow_untyped_calls=True, disallow_any_generics=True, warn_return_any=True, no_implicit_reexport=True, strict_equality=True, warn_unused_ignores=True
"""Synthetic PNG images drawn with an original bitmap font (issue #1287).

Two callers need images with known content and no dependency: the gateway
probe's step-0 image (`scripts/probe_gateway.py --probe-image`, a per-run
digit nonce a model must read back) and the answer eval's synthetic
screenshots (text drawn into a terminal-like panel, so the transcript's
character error rate can be computed against `rendered_text`). Images are
generated at run time and never committed.

Standard library only (`zlib`, `struct`): no Pillow, no font file, no
licence. The 5x7 glyphs below are original to this repository, drawn for it
by hand; they copy no existing font's bitmaps. Coverage: digits, A-Z, space
and `. , : ; - _ / ( ) ' " ? ! = + * # < > [ ] & % $ @`. Lowercase ASCII
draws as uppercase; any other character draws as `?`.

Layout: each character occupies a cell of `CELL_W` x `CELL_H` font pixels
(the glyph in the top-left corner, then one blank column and two blank
rows), every font pixel is `scale` x `scale` image pixels, and `margin`
image pixels of background surround the text. So an image of `n` lines whose
longest line has `c` characters is `2*margin + scale*CELL_W*c` pixels wide
and `2*margin + scale*CELL_H*n` pixels high. Output is deterministic: the same
arguments give the same pixels everywhere and the same bytes with the same
zlib build (compressed bytes may differ between zlib builds, so compare
decoded pixels, not a byte hash, across machines).
"""

from __future__ import annotations

import struct
import zlib

GLYPH_W = 5
GLYPH_H = 7
CELL_W = GLYPH_W + 1
CELL_H = GLYPH_H + 2

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

Color = tuple[int, int, int]

# '#' is ink, '.' is background; seven rows of five columns per glyph.
_GLYPH_ROWS: dict[str, tuple[str, ...]] = {
    "0": (".###.", "#...#", "#...#", "#.#.#", "#...#", "#...#", ".###."),
    "1": ("..#..", ".##..", "#.#..", "..#..", "..#..", "..#..", "#####"),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("####.", "....#", "....#", ".###.", "....#", "....#", "####."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", "..#..", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    "A": ("..#..", ".#.#.", "#...#", "#...#", "#####", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("###..", "#..#.", "#...#", "#...#", "#...#", "#..#.", "###.."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": (".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", ".#.#."),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    " ": (".....", ".....", ".....", ".....", ".....", ".....", "....."),
    ".": (".....", ".....", ".....", ".....", ".....", ".##..", ".##.."),
    ",": (".....", ".....", ".....", ".....", ".##..", "..#..", ".#..."),
    ":": (".....", ".##..", ".##..", ".....", ".##..", ".##..", "....."),
    ";": (".....", ".##..", ".##..", ".....", ".##..", "..#..", ".#..."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    "_": (".....", ".....", ".....", ".....", ".....", ".....", "#####"),
    "/": ("....#", "....#", "...#.", "..#..", ".#...", "#....", "#...."),
    "(": ("...#.", "..#..", ".#...", ".#...", ".#...", "..#..", "...#."),
    ")": (".#...", "..#..", "...#.", "...#.", "...#.", "..#..", ".#..."),
    "'": ("..#..", "..#..", ".#...", ".....", ".....", ".....", "....."),
    '"': (".#.#.", ".#.#.", ".#.#.", ".....", ".....", ".....", "....."),
    "?": (".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#.."),
    "!": ("..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."),
    "=": (".....", ".....", "#####", ".....", "#####", ".....", "....."),
    "+": (".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."),
    "*": (".....", "#.#.#", ".###.", "..#..", ".###.", "#.#.#", "....."),
    "#": (".#.#.", ".#.#.", "#####", ".#.#.", "#####", ".#.#.", ".#.#."),
    "<": ("...#.", "..#..", ".#...", "#....", ".#...", "..#..", "...#."),
    ">": (".#...", "..#..", "...#.", "....#", "...#.", "..#..", ".#..."),
    "[": (".###.", ".#...", ".#...", ".#...", ".#...", ".#...", ".###."),
    "]": (".###.", "...#.", "...#.", "...#.", "...#.", "...#.", ".###."),
    "&": (".##..", "#..#.", "#.#..", ".#...", "#.#.#", "#..#.", ".##.#"),
    "%": ("##..#", "##..#", "...#.", "..#..", ".#...", "#..##", "#..##"),
    "$": ("..#..", ".####", "#.#..", ".###.", "..#.#", "####.", "..#.."),
    "@": (".###.", "#...#", "#.###", "#.#.#", "#.###", "#....", ".###."),
}

# Per glyph, per row: which of the five columns carry ink.
GLYPHS: dict[str, tuple[tuple[bool, ...], ...]] = {
    char: tuple(tuple(cell == "#" for cell in row) for row in rows) for char, rows in _GLYPH_ROWS.items()
}


def rendered_text(line: str) -> str:
    """What `render_text_png` draws for `line`: lowercase ASCII as uppercase,
    any character without a glyph as `?`. One output character per input
    character, so a transcript can be scored against it."""
    out = []
    for char in line:
        upper = char.upper() if "a" <= char <= "z" else char
        out.append(upper if upper in GLYPHS else "?")
    return "".join(out)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def encode_png(width: int, height: int, rgb_rows: list[bytes]) -> bytes:
    """An 8-bit RGB, non-interlaced PNG of `height` rows of `width*3` bytes."""
    if width < 1 or height < 1:
        raise ValueError("a PNG needs a width and a height of at least 1")
    if len(rgb_rows) != height or any(len(row) != width * 3 for row in rgb_rows):
        raise ValueError("rgb_rows must hold height rows of width*3 bytes")
    # Filter type 0 (none) before each scanline.
    raw = b"".join(b"\x00" + row for row in rgb_rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def _color(value: Color) -> bytes:
    if len(value) != 3 or any(not 0 <= part <= 255 for part in value):
        raise ValueError("a color is three integers from 0 to 255")
    return bytes(value)


def render_text_png(
    lines: list[str],
    *,
    scale: int = 2,
    fg: Color = (0, 255, 0),
    bg: Color = (0, 0, 0),
    margin: int = 8,
) -> bytes:
    """Draw `lines` (one text row each) as a PNG; see the module docstring
    for the layout and the size formula. An empty list or empty lines still
    give an image of at least one pixel."""
    if scale < 1:
        raise ValueError("scale must be at least 1")
    if margin < 0:
        raise ValueError("margin must not be negative")
    ink, paper = _color(fg), _color(bg)
    shown = [rendered_text(line) for line in lines]
    cols = max((len(line) for line in shown), default=0)
    width = max(1, 2 * margin + scale * CELL_W * cols)
    height = max(1, 2 * margin + scale * CELL_H * len(shown))
    blank = paper * width
    if cols == 0:
        # Nothing to draw: background only.
        return encode_png(width, height, [blank] * height)
    side = paper * margin
    rows: list[bytes] = [blank] * margin
    for line in shown:
        padded = line.ljust(cols)
        for y in range(CELL_H):
            pixels = bytearray(side)
            for char in padded:
                glyph_row = GLYPHS[char][y] if y < GLYPH_H else (False,) * GLYPH_W
                for on in (*glyph_row, False):
                    pixels += (ink if on else paper) * scale
            pixels += side
            row = bytes(pixels)
            rows.extend([row] * scale)
    rows.extend([blank] * margin)
    return encode_png(width, height, rows)


def render_digits_png(digits: str, *, scale: int) -> bytes:
    """Black digits on white, one line, with a margin of two font pixels."""
    if not digits or not digits.isascii() or not digits.isdecimal():
        raise ValueError("digits must be a non-empty string of ASCII digits")
    return render_text_png([digits], scale=scale, fg=(0, 0, 0), bg=(255, 255, 255), margin=2 * scale)
