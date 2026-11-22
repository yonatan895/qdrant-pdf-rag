"""Shared property tests: no secret survives a truncation cut (#1347).

The oracle is whole-text screening of the untruncated input. A canary that
screening the whole text redacts must not appear in a truncated or windowed
result, whole or as any `FRAGMENT`-character slice. A canary the whole-text
screen leaves visible is a pattern limit, out of scope here, and is not checked.

The strategies draw small structural parameters (secret family, line end,
record width, multibyte edges, and the cut offset inside or around the
secret), and the suites build the bytes from them so the cut lands on a drawn
position near the key, separator and value. A uniformly random cut would
almost never land there. The suites drive the real tool paths through their
existing fakes (FakeFTP for the z/OS bridge, FakeGitLab for the GitLab gateway).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from hypothesis import settings
from hypothesis import strategies as st

# Canary letters that no key word, marker, frame line or filler uses, so a
# 6-character run of them in a result can only come from a planted secret.
CANARY_ALPHABET = "QXJZqxjz"
FRAGMENT = 6
E_ACUTE = b"\xc3\xa9"  # U+00E9, two UTF-8 bytes
EURO = b"\xe2\x82\xac"  # U+20AC, three UTF-8 bytes


def property_settings(max_examples: int, deadline_ms: int = 5000) -> settings:
    """Deterministic and bounded: the same examples on every run (no example
    database), a per-example time budget, no health check suppressed."""
    return settings(
        max_examples=max_examples, derandomize=True, database=None, deadline=deadline_ms
    )


@dataclass(frozen=True)
class Family:
    """A secret shape; `{v}` is the canary and a newline a line end."""

    template: bytes
    spans_lines: bool  # the key and the value sit on different lines


# Key chains, z/OS operand and logon forms, space-separated values, auth
# headers and URL credentials.
FAMILIES: dict[str, Family] = {
    "kv_same_line": Family(b"//S1 EXEC PGM=X,PASSWORD={v}", False),
    "kv_spaced": Family(b"db_password = {v}", False),
    "kv_colon": Family(b"API_KEY: {v}", False),
    "kv_export": Family(b"export AUTH_TOKEN={v}", False),
    "kv_next_line": Family(b"PASSWORD=\n{v}", True),
    "kv_chain": Family(b"PASSWORD=\nTOKEN=\nPW=\n{v}", True),
    "kv_chain_spaced": Family(b"PASSWORD =  \n  TOKEN :\n\tPW=   \n{v}", True),
    "zos_operand": Family(b"ALTUSER BOB PASSWORD({v}) NOOIDCARD", False),
    "zos_operand_next_line": Family(b"ALTUSER BOB PASSWORD(\n  {v})", True),
    "zos_phrase": Family(b"ALTUSER BOB PHRASE('{v} IS LONG') X", False),
    "zos_phrase_next_line": Family(b"ALTUSER BOB PHRASE(\n'{v} IS LONG')", True),
    "tso_logon": Family(b"LOGON BOB/{v}/NEWPW", False),
    "tso_logon_next_line": Family(b"LOGON\n  BOB/{v}", True),
    "space_form_next_line": Family(b"PASSPHRASE\n\n  {v}", True),
    "bearer": Family(b"Authorization: Bearer {v}", False),
    "bearer_next_line": Family(b"HDR Bearer\n  {v}", True),
    "url_credentials": Family(b"git clone https://deploy:{v}@gitlab.example.test/x.git", False),
}
SAME_LINE = tuple(name for name, family in FAMILIES.items() if not family.spans_lines)
PEM_LABELS = (b"", b"RSA ", b"EC ", b"OPENSSH ", b"ENCRYPTED ")
# What precedes the secret on its first line: nothing, multibyte characters,
# a short record, or a record longer than the bridge's tail lookbehind.
LEADS = (b"", E_ACUTE + EURO + b" ", b"X" * 120 + b" ", b"X" * 5000 + b" ")
SHORT_LEADS = LEADS[:3]
LINE_ENDS = (b"\n", b"\r\n", b"\r")


@dataclass(frozen=True)
class Secret:
    family: str
    raw: bytes  # as planted: line ends applied, no trailing line end
    canaries: tuple[str, ...]


def canaries(min_size: int = 12, max_size: int = 24) -> st.SearchStrategy[str]:
    return st.text(CANARY_ALPHABET, min_size=min_size, max_size=max_size)


@st.composite
def kv_secrets(
    draw: st.DrawFn, families: Sequence[str], eol: bytes, leads: Sequence[bytes] = LEADS
) -> Secret:
    """One key/value-shaped secret from `families` with line end `eol`."""
    name = draw(st.sampled_from(sorted(families)))
    value = draw(canaries())
    raw = draw(st.sampled_from(leads)) + FAMILIES[name].template.replace(b"{v}", value.encode())
    return Secret(name, raw.replace(b"\n", eol), (value,))


@st.composite
def pem_secrets(
    draw: st.DrawFn, eol: bytes, *, ansi: bool = False, max_lines: int = 20, open_end: bool = True
) -> Secret:
    """A PEM private key: any label, sometimes (`open_end`) no END line, and
    (`ansi`) colour escapes around the markers as a CI log writes them."""
    label = draw(st.sampled_from(PEM_LABELS))
    lines = draw(st.lists(canaries(16, 64), min_size=1, max_size=max_lines))
    begin = b"-----BEGIN " + label + b"PRIVATE KEY-----"
    end = b"-----END " + label + b"PRIVATE KEY-----"
    if ansi and draw(st.booleans()):
        begin = b"\x1b[0;32m" + begin + b"\x1b[0m"
        end = b"\x1b[31;1m" + end + b"\x1b[0K"
    parts = [begin, *(line.encode() for line in lines)]
    has_end = not open_end or draw(st.integers(0, 4)) > 0  # one key in five has no END line
    if has_end:
        parts.append(end)
    return Secret("pem" if has_end else "pem_no_end", eol.join(parts), tuple(lines))


@st.composite
def cut_offsets(draw: st.DrawFn, secret: Secret) -> int:
    """An offset into `secret.raw` (or just around it) where a cut lands:
    near its start or end, after a line end, or at a canary's edge."""
    raw = secret.raw
    points = {0, len(raw)}
    points.update(i + 1 for i, byte in enumerate(raw) if byte in b"\r\n")
    for canary in secret.canaries:
        at = raw.find(canary.encode())
        points.update((at, at + len(canary)))
    point = draw(st.sampled_from(sorted(points)))
    return point + draw(st.integers(-3, 3))


def filler(
    size: int, *, width: int = 40, eol: bytes = b"\n", multibyte: bool = False, tag: str = "log"
) -> bytes:
    """Exactly `size` bytes of numbered `width`-byte lines (the last one may
    be cut short), optionally with multibyte characters at each line's edges.
    No canary letter appears in it."""
    first, last = (E_ACUTE, EURO) if multibyte else (b"", b"")
    out = bytearray()
    number = 0
    while len(out) < size:
        stem = first + f"{tag} {number:06d} ".encode()
        out += stem.ljust(max(len(stem), width - len(eol) - len(last)), b".") + last + eol
        number += 1
    return bytes(out[:size])


def assert_no_secret_survives(result: str, whole_screened: str, secret: Secret) -> int:
    """The oracle: every canary the whole-text screen redacts is absent from
    `result`, whole and as every FRAGMENT-character slice. Returns how many
    canaries were in scope (redacted by the whole-text screen)."""
    checked = 0
    for canary in secret.canaries:
        pieces = {canary[i : i + FRAGMENT] for i in range(len(canary) - FRAGMENT + 1)}
        if any(piece in whole_screened for piece in pieces):
            continue  # a pattern limit of the screen itself, not a cut
        checked += 1
        leaked = sorted(piece for piece in pieces if piece in result)
        if leaked:
            at = result.find(leaked[0])
            raise AssertionError(
                f"{secret.family}: a fragment of a secret that whole-text screening redacts survives "
                f"the cut: ...{result[max(0, at - 80) : at + 30]!r}..."
            )
    return checked
