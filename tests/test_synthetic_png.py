"""The stdlib PNG renderer behind the image probe and the image eval (#1287)."""

from __future__ import annotations

import struct
import zlib

import pytest

from mainframe_rag import synthetic_png as sp

GLYPH_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ .,:;-_/()'\"?!=+*#<>[]&%$@"


def decode_png(data: bytes) -> tuple[int, int, list[bytes]]:
    """An independent minimal decoder: signature, chunk CRCs, IHDR fields,
    then the filter-0 RGB scanlines."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        assert crc == zlib.crc32(kind + body), kind
        chunks.append((kind, body))
        pos += 12 + length
    assert chunks[0][0] == b"IHDR" and chunks[-1] == (b"IEND", b"")
    width, height, depth, color_type, compression, filt, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (depth, color_type, compression, filt, interlace) == (8, 2, 0, 0, 0)
    raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"))
    stride = 1 + width * 3
    assert len(raw) == stride * height
    rows = [raw[i * stride:(i + 1) * stride] for i in range(height)]
    assert all(row[0] == 0 for row in rows)
    return width, height, [row[1:] for row in rows]


def pixel(rows: list[bytes], x: int, y: int) -> tuple[int, int, int]:
    return rows[y][3 * x], rows[y][3 * x + 1], rows[y][3 * x + 2]


def test_encode_png_round_trips_pixels():
    rows = [bytes([1, 2, 3, 4, 5, 6]), bytes([7, 8, 9, 10, 11, 12])]
    assert decode_png(sp.encode_png(2, 2, rows)) == (2, 2, rows)


@pytest.mark.parametrize("width,height,rows", [
    (0, 1, [b""]), (1, 0, []), (2, 1, [b"\x00" * 3]), (1, 2, [b"\x00" * 3]),
])
def test_encode_png_refuses_inconsistent_rows(width, height, rows):
    with pytest.raises(ValueError):
        sp.encode_png(width, height, rows)


def test_one_glyph_draws_its_bitmap_scaled_in_the_margin():
    # Expected ink for "7", written out here independently of the module.
    seven = ["#####", "....#", "...#.", "..#..", "..#..", ".#...", ".#..."]
    scale, margin = 3, 4
    fg, bg = (10, 200, 30), (250, 240, 230)
    width, height, rows = decode_png(sp.render_text_png(["7"], scale=scale, fg=fg, bg=bg, margin=margin))
    assert (width, height) == (2 * margin + scale * 6, 2 * margin + scale * 9)
    for y in range(height):
        for x in range(width):
            fx, fy = (x - margin) // scale, (y - margin) // scale
            inside = x >= margin and y >= margin and fx < 5 and fy < 7
            ink = inside and seven[fy][fx] == "#"
            assert pixel(rows, x, y) == (fg if ink else bg), (x, y)


@pytest.mark.parametrize("lines,scale,margin", [
    (["AB", "CDEF"], 2, 8), (["X"], 1, 0), (["hello world"], 4, 3), ([""], 2, 5), ([], 3, 2),
])
def test_dimensions_follow_scale_margin_and_text(lines, scale, margin):
    width, height, _ = decode_png(sp.render_text_png(lines, scale=scale, margin=margin))
    cols = max((len(line) for line in lines), default=0)
    assert width == max(1, 2 * margin + scale * 6 * cols)
    assert height == max(1, 2 * margin + scale * 9 * len(lines))


def test_render_text_defaults_are_green_on_black():
    _, _, rows = decode_png(sp.render_text_png(["#"]))
    colors = {pixel(rows, x, y) for y in range(len(rows)) for x in range(len(rows[0]) // 3)}
    assert colors == {(0, 0, 0), (0, 255, 0)}


def test_digits_are_black_on_white_and_about_400_by_100_at_scale_10():
    width, height, rows = decode_png(sp.render_digits_png("123456", scale=10))
    assert (width, height) == (400, 130)
    colors = {pixel(rows, x, y) for y in range(height) for x in range(width)}
    assert colors == {(0, 0, 0), (255, 255, 255)}
    assert decode_png(sp.render_digits_png("123456", scale=20))[:2] == (800, 260)


@pytest.mark.parametrize("digits", ["", "12a4", "１２", "12 3"])
def test_digits_refuse_non_digits(digits):
    with pytest.raises(ValueError):
        sp.render_digits_png(digits, scale=2)


def test_rendered_text_maps_lowercase_and_unsupported_characters():
    assert sp.rendered_text("ieF012 a-b/c") == "IEF012 A-B/C"
    assert sp.rendered_text("tab\there~é{}") == "TAB?HERE????"
    assert sp.rendered_text(GLYPH_CHARS) == GLYPH_CHARS


def test_unsupported_and_lowercase_characters_draw_as_their_mapping():
    assert sp.render_text_png(["abc"]) == sp.render_text_png(["ABC"])
    assert sp.render_text_png(["x~é"]) == sp.render_text_png(["X??"])


def test_font_covers_the_contract_and_every_glyph_is_distinct():
    assert set(sp.GLYPHS) == set(GLYPH_CHARS)
    for char, glyph in sp.GLYPHS.items():
        assert len(glyph) == 7 and all(len(row) == 5 for row in glyph), char
        assert any(any(row) for row in glyph) or char == " ", char
    assert len({glyph for glyph in sp.GLYPHS.values()}) == len(sp.GLYPHS)


def test_output_is_deterministic():
    lines = ["IEF452I JOB01 - JCL ERROR", "what does this mean?"]
    assert sp.render_text_png(lines, scale=2) == sp.render_text_png(list(lines), scale=2)
    assert sp.render_digits_png("907135", scale=10) == sp.render_digits_png("907135", scale=10)


@pytest.mark.parametrize("kwargs", [{"scale": 0}, {"margin": -1}, {"fg": (0, 0, 256)}, {"bg": (-1, 0, 0)}])
def test_render_refuses_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        sp.render_text_png(["A"], **kwargs)
