#!/usr/bin/env python3
"""Fetch, verify and stage the dev kit's agent tools (#1172, ADR-0009 §8).

Runs during the connected-side kit build (images/Containerfile.devkit, stage
agent-tools) and nowhere else: the air gap never builds the image. For each pin in
PINS (herdr-core, OpenCode) it

  1. downloads the pin's origin over HTTPS (or, with --archives DIR, reads DIR/<asset>),
  2. refuses the archive unless its sha256 equals the pin's sha256,
  3. extracts only the named regular-file members (binary, licence, notices),
  4. refuses any member whose sha256 differs from its pin, or a committed licence
     text (images/devkit/agents/<tool>-LICENSE) whose sha256 differs,
  5. checks `<binary> --version` names the pinned version,

and only then moves the verified files into --bin-dir and --license-dir. Any refusal
leaves both directories untouched and exits 1 with a fixed message. Nothing is piped
to a shell and nothing runs before its digest is verified. Standard library only.

usage: install_agent_tools.py --pins DIR --bin-dir DIR --license-dir DIR [--archives DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

# (pin file, installed name, extra links to the installed name)
PINS = (("herdr-core-pin.txt", "herdr-core", ("hc",)), ("opencode-pin.txt", "opencode", ()))
HEX64 = re.compile(r"[0-9a-f]{64}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
CHUNK = 1 << 20


class Refused(Exception):
    """A fixed, operator-facing refusal."""


def read_pin(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise Refused(f"{path.name} is missing or unreadable") from None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep or key.strip() in fields:
            raise Refused(f"{path.name} is malformed: one 'field: value' per line, no repeats")
        fields[key.strip()] = value.strip()
    for field in ("version", "asset", "sha256", "binary", "binary-sha256", "origin", "license", "license-sha256"):
        if not fields.get(field):
            raise Refused(f"{path.name} has no {field}")
    for field in ("sha256", "binary-sha256", "license-sha256", "notices-sha256"):
        if field in fields and not HEX64.fullmatch(fields[field]):
            raise Refused(f"{path.name} {field} is not 64 lowercase hex")
    for field in ("asset", "binary", "license-member", "notices-member"):
        if field in fields and not NAME.fullmatch(fields[field]):
            raise Refused(f"{path.name} {field} is not a plain file name")
    if ("notices-member" in fields) != ("notices-sha256" in fields):
        raise Refused(f"{path.name} names notices-member and notices-sha256 together or neither")
    if not fields["origin"].startswith("https://") or not fields["origin"].endswith("/" + fields["asset"]):
        raise Refused(f"{path.name} origin is not an https URL that names its asset")
    return fields


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise Refused("a download redirected away from https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# releases.herdr.dev answers 403 to the default Python-urllib agent (seen 2026-10-31).
USER_AGENT = "mfr-devkit-build/1 (+https://github.com/yonatan895/qdrant-pdf-rag)"


def download(origin: str, target: Path) -> None:
    opener = urllib.request.build_opener(_HttpsOnly)
    request = urllib.request.Request(origin, headers={"User-Agent": USER_AGENT})
    last = "unknown"
    for _ in range(3):
        try:
            with opener.open(request, timeout=60) as response, target.open("wb") as out:
                shutil.copyfileobj(response, out, CHUNK)
            return
        except Refused:
            raise
        except OSError as error:  # URLError, timeouts and resets are OSErrors
            last = f"{type(error).__name__} {getattr(error, 'code', '')}".strip()
    raise Refused(f"the download of {target.name} failed ({last})")


def extract_member(archive: Path, member: str, target: Path) -> None:
    """Extract exactly one regular-file member by name, refusing links and duplicates."""
    found = None
    with tarfile.open(archive, "r:*") as handle:
        for info in handle:
            if info.name.removeprefix("./") == member:
                if found is not None or not info.isreg():
                    raise Refused(f"{archive.name} holds {member} more than once or not as a regular file")
                found = info
                stream = handle.extractfile(info)
                if stream is None:
                    raise Refused(f"{archive.name} member {member} is unreadable")
                with target.open("wb") as out:
                    shutil.copyfileobj(stream, out, CHUNK)
    if found is None:
        raise Refused(f"{archive.name} has no member {member}")


def stage_tool(pins: Path, pin_name: str, installed: str, links: tuple[str, ...], archives: Path | None,
               stage: Path) -> None:
    pin = read_pin(pins / pin_name)
    work = stage / "work" / installed
    work.mkdir(parents=True)
    if archives is None:
        archive = work / pin["asset"]
        download(pin["origin"], archive)
    else:
        archive = archives / pin["asset"]
        if not archive.is_file():
            raise Refused(f"{pin['asset']} is missing from the archive directory")
    if sha256_file(archive) != pin["sha256"]:
        raise Refused(f"{pin['asset']} does not match the sha256 in {pin_name}")
    binary = stage / "bin" / installed
    binary.parent.mkdir(parents=True, exist_ok=True)
    extract_member(archive, pin["binary"], binary)
    if sha256_file(binary) != pin["binary-sha256"]:
        raise Refused(f"{pin['binary']} does not match the binary-sha256 in {pin_name}")
    licences = stage / "licenses" / installed
    licences.mkdir(parents=True)
    licence = licences / "LICENSE"
    if "license-member" in pin:
        extract_member(archive, pin["license-member"], licence)
    else:
        # No licence in the archive: the pinned upstream text committed beside the pin.
        committed = pins / f"{installed}-LICENSE"
        if not committed.is_file() or committed.is_symlink():
            raise Refused(f"{committed.name} is missing")
        shutil.copyfile(committed, licence)
    if sha256_file(licence) != pin["license-sha256"]:
        raise Refused(f"the {installed} licence does not match the license-sha256 in {pin_name}")
    committed = pins / f"{installed}-LICENSE"
    if committed.is_file() and sha256_file(committed) != pin["license-sha256"]:
        raise Refused(f"{committed.name} does not match the license-sha256 in {pin_name}")
    if "notices-member" in pin:
        notices = licences / pin["notices-member"]
        extract_member(archive, pin["notices-member"], notices)
        if sha256_file(notices) != pin["notices-sha256"]:
            raise Refused(f"the {installed} notices do not match the notices-sha256 in {pin_name}")
    binary.chmod(0o755)
    for path in licences.iterdir():
        path.chmod(0o644)
    home = work / "home"
    home.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "OPENCODE_DISABLE_MODELS_FETCH": "1",
           "OPENCODE_DISABLE_AUTOUPDATE": "1"}
    try:
        done = subprocess.run([str(binary), "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=60, env=env, cwd=home, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused(f"{installed} --version did not run") from None
    # "2.2.5", "v2.2.5" or "herdr-core 0.1.15", on stdout or stderr; never "2.2.50".
    named = re.search(rf"(?<![\d.]){re.escape(pin['version'])}(?![\d.])", done.stdout + "\n" + done.stderr)
    if done.returncode != 0 or not named:
        raise Refused(f"{installed} --version does not name the pinned version {pin['version']} "
                      f"(rc {done.returncode})")
    for link in links:
        (stage / "bin" / link).symlink_to(installed)
    shutil.rmtree(work)


def install(pins: Path, bin_dir: Path, license_dir: Path, archives: Path | None) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="agent-tools-") as scratch:
        stage = Path(scratch)
        for pin_name, installed, links in PINS:
            stage_tool(pins, pin_name, installed, links, archives, stage)
        # Everything verified: only now touch the destinations, after checking that
        # nothing there would be replaced.
        binaries = sorted((stage / "bin").iterdir())
        licences = sorted((stage / "licenses").iterdir())
        for path in [*(bin_dir / p.name for p in binaries), *(license_dir / p.name for p in licences)]:
            if path.exists() or path.is_symlink():
                raise Refused(f"{path.name} already exists in the destination")
        bin_dir.mkdir(parents=True, exist_ok=True)
        license_dir.mkdir(parents=True, exist_ok=True)
        for path in binaries:
            if path.is_symlink():
                (bin_dir / path.name).symlink_to(os.readlink(path))
            else:
                shutil.move(path, bin_dir / path.name)
        for path in licences:
            shutil.copytree(path, license_dir / path.name)
        return [p.name for p in binaries]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--license-dir", type=Path, required=True)
    parser.add_argument("--archives", type=Path, help="read <asset> from this directory instead of downloading")
    args = parser.parse_args(argv)
    try:
        names = install(args.pins, args.bin_dir, args.license_dir, args.archives)
    except Refused as error:
        print(f"agent-tools: {error}; nothing was installed", file=sys.stderr)
        return 1
    print("agent-tools: verified and installed " + " ".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
