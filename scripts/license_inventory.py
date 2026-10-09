#!/usr/bin/env python3
"""Offline third-party license inventory check for release transfer (issue #376).

``licenses/inventory.json`` records the license basis of everything this
repository ships: every locked runtime wheel, the pinned images, vendored chart,
JS and Task tool, BM25 weights and Qdrant skills. The check keeps that record
reconciled with the real pins (``locks/``, ``images.txt``, vendored bytes) so a
new or changed dependency, image digest or notice file cannot reach a bundle
unrecorded, and so unresolved owner decisions stay visible.

This is evidence plumbing, not a legal verdict. A recorded license class is the
declared metadata; it never marks a component approved. Approval is an owner
record (``status: approved`` plus a protected-record reference). No network
access, no installs, no execution of artifacts.

  check      reconcile the inventory with the pins; --release also requires
             every owner decision to be recorded and current
  generate   re-derive artifact-observed fields from wheels / installed
             dist-info and print the merged inventory (human fields kept)
  notices    render THIRD-PARTY-NOTICES.txt for the offline bundle
"""
from __future__ import annotations

import argparse
import email.parser
import hashlib
import io
import json
import re
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    from scripts import dependency_lock as locks
except ModuleNotFoundError:
    import dependency_lock as _local_locks

    locks = _local_locks

INVENTORY = "licenses/inventory.json"
SCHEMA_VERSION = 1

# Ordered least to most restrictive. "election-required" is a dual license whose
# alternatives differ in class and no election is recorded.
ORDER = ("permissive", "weak-copyleft", "strong-copyleft", "network-copyleft",
         "election-required", "proprietary", "unknown")
# Classes that cannot rest on declared metadata alone: they need an owner
# decision (status pending-owner-decision, excluded or approved with a reference).
RESTRICTED = frozenset(ORDER[2:])
STATUSES = ("declared", "pending-owner-decision", "approved", "excluded")
SPDX_BASES = ("declared-expression", "normalized-license-field", "normalized-classifier",
              "license-file-text", "upstream-project-license", "not-verified")

PERMISSIVE = {"MIT", "MIT-CMU", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "Apache-2.0", "ISC",
              "PSF-2.0", "Zlib", "CC0-1.0", "Unlicense", "HPND", "BSL-1.0"}
WEAK = {"MPL-2.0", "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later"}
STRONG = {"GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later"}
NETWORK = {"AGPL-3.0-only", "AGPL-3.0-or-later"}

# Non-SPDX strings seen in artifact metadata -> SPDX, used only by ``generate``
# to seed a new entry. The result is a recorded, reviewable value, never trusted
# silently: ``spdx_basis`` says how it was obtained.
DECLARED_NORMALIZATION = {
    "MIT License": "MIT", "MIT": "MIT", "The MIT License (MIT)": "MIT",
    "Apache 2.0": "Apache-2.0", "Apache License": "Apache-2.0", "Apache-2.0": "Apache-2.0",
    "3-Clause BSD License": "BSD-3-Clause", "BSD-3-Clause": "BSD-3-Clause",
    "MPL-2.0": "MPL-2.0", "MPL-2.0 AND MIT": "MPL-2.0 AND MIT",
}
CLASSIFIER_NORMALIZATION = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
}
LICENSE_FILE = re.compile(r"(?i)^(licen[sc]e|copying|notice)")
TOKEN = re.compile(r"\(|\)|[A-Za-z0-9.+:-]+")
PROBLEM_LIMIT = 40


class InventoryError(ValueError):
    """The inventory or its inputs are unusable (fixed message, no contents)."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


# --- license expression classification -------------------------------------


def id_class(identifier: str) -> str:
    if identifier in PERMISSIVE:
        return "permissive"
    if identifier in WEAK:
        return "weak-copyleft"
    if identifier in STRONG:
        return "strong-copyleft"
    if identifier in NETWORK:
        return "network-copyleft"
    if identifier.startswith("LicenseRef-"):
        return "proprietary"
    return "unknown"


def expression_class(expression: str, elected: str | None = None) -> str:
    """Conservative class of an SPDX-style expression.

    AND takes the most restrictive operand. OR takes the shared class when all
    alternatives agree, else ``election-required`` unless an election is recorded.
    ``X WITH exception`` is classified as X.
    """
    if elected:
        return expression_class(elected)
    tokens = TOKEN.findall(expression)
    if not tokens or "".join(tokens) != re.sub(r"\s+", "", expression):
        return "unknown"
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def advance() -> str:
        nonlocal position
        position += 1
        return tokens[position - 1]

    def operand() -> str:
        token = advance()
        if token == "(":
            value = alternatives()
            if peek() != ")":
                raise InventoryError("unbalanced license expression")
            advance()
            return value
        if token in {")", "AND", "OR", "WITH"}:
            raise InventoryError("malformed license expression")
        if peek() == "WITH":
            advance()
            advance()
        return id_class(token.removesuffix("+"))

    def conjunction() -> str:
        value = operand()
        while peek() == "AND":
            advance()
            value = max(value, operand(), key=ORDER.index)
        return value

    def alternatives() -> str:
        values = [conjunction()]
        while peek() == "OR":
            advance()
            values.append(conjunction())
        return values[0] if len(set(values)) == 1 else "election-required"

    try:
        result = alternatives()
    except (InventoryError, IndexError):
        return "unknown"
    return result if position == len(tokens) else "unknown"


# --- artifact metadata ------------------------------------------------------


@dataclass
class Dist:
    name: str
    version: str
    kind: str  # "wheel" | "installed"
    label: str
    wheel_sha256: str | None
    metadata: bytes
    license_files: dict[str, bytes] = field(default_factory=dict)


def parse_metadata(data: bytes):
    return email.parser.BytesParser().parsebytes(data, headersonly=True)


def declared_license(message) -> tuple[str, str]:
    """(declared string, source) chosen deterministically from METADATA."""
    expression = (message.get("License-Expression") or "").strip()
    if expression:
        return expression, "License-Expression"
    field_value = (message.get("License") or "").strip()
    if field_value and field_value != "UNKNOWN":
        first = field_value.splitlines()[0].strip()
        return first[:80], "License"
    classifiers = sorted(c.split("::", 1)[0].strip() + " :: " + c.split("::", 1)[1].strip()
                         for c in message.get_all("Classifier", []) if c.startswith("License ::"))
    if classifiers:
        return "; ".join(classifiers), "Classifier"
    return "", "none"


def _is_license_member(relative: str) -> bool:
    parts = relative.split("/")
    if parts[0] == "licenses":
        return len(parts) > 1
    return len(parts) == 1 and bool(LICENSE_FILE.match(parts[0]))


def wheel_dist(path: Path) -> Dist:
    with zipfile.ZipFile(path) as archive:
        infos = {i.filename: i for i in archive.infolist()}
        metadata_names = [n for n in infos if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise InventoryError("wheel must contain exactly one METADATA")
        root = metadata_names[0].rsplit("/", 1)[0] + "/"
        data = archive.read(metadata_names[0])
        message = parse_metadata(data)
        files = {n[len(root):]: archive.read(n) for n, i in infos.items()
                 if n.startswith(root) and not i.is_dir() and _is_license_member(n[len(root):])
                 and i.file_size <= 4_000_000}
    return Dist(locks.canonical(message["Name"]), message["Version"], "wheel", path.name,
                sha256_file(path), data, files)


def installed_dist(directory: Path) -> Dist:
    data = (directory / "METADATA").read_bytes()
    message = parse_metadata(data)
    files = {}
    for item in sorted(directory.rglob("*")):
        relative = item.relative_to(directory).as_posix()
        if item.is_file() and not item.is_symlink() and _is_license_member(relative) and item.stat().st_size <= 4_000_000:
            files[relative] = item.read_bytes()
    return Dist(locks.canonical(message["Name"]), message["Version"], "installed", directory.name,
                None, data, files)


def collect_dists(wheelhouses: list[Path], site_packages: list[Path]) -> dict[tuple[str, str], list[Dist]]:
    found: dict[tuple[str, str], list[Dist]] = {}
    for directory in wheelhouses:
        for wheel in sorted(directory.glob("*.whl")):
            dist = wheel_dist(wheel)
            found.setdefault((dist.name, dist.version), []).append(dist)
    for directory in site_packages:
        for info in sorted(directory.glob("*.dist-info")):
            if (info / "METADATA").is_file():
                dist = installed_dist(info)
                found.setdefault((dist.name, dist.version), []).append(dist)
    return found


def select_dist(name: str, locked: dict, found: dict[tuple[str, str], list[Dist]]) -> Dist | None:
    """Prefer the exact locked wheel bytes, then a version-matching installed dist."""
    candidates = found.get((name, locked["version"]), [])
    for dist in candidates:
        if dist.kind == "wheel" and dist.wheel_sha256 == locked["sha256"] and dist.label == locked["wheel"]:
            return dist
    return next((d for d in candidates if d.kind == "installed"), None)


def derived_fields(dist: Dist) -> dict:
    message = parse_metadata(dist.metadata)
    declared, source = declared_license(message)
    return {
        "license_declared": declared,
        "declared_source": source,
        "license_files": [{"name": n, "sha256": sha256_bytes(b)} for n, b in sorted(dist.license_files.items())],
    }


def fingerprint(text: str) -> str | None:
    head = text[:3000]
    if "Apache License" in head and "Version 2.0" in head:
        return "Apache-2.0"
    if "Mozilla Public License" in head and "2.0" in head:
        return "MPL-2.0"
    if re.search(r"Permission is hereby granted, free of charge", head) and "MIT" in head[:300] or head.lstrip().startswith("MIT License"):
        return "MIT"
    if "Redistribution and use in source and binary forms" in head:
        return "BSD-3-Clause" if "Neither the name" in head or "endorse or promote" in head else "BSD-2-Clause"
    return None


def seed_spdx(declared: str, source: str) -> tuple[str, str]:
    if source == "License-Expression":
        return declared, "declared-expression"
    if source == "License" and declared in DECLARED_NORMALIZATION:
        return DECLARED_NORMALIZATION[declared], "normalized-license-field"
    if source == "Classifier":
        values = {CLASSIFIER_NORMALIZATION.get(part.strip(), "") for part in declared.split(";")}
        if len(values) == 1 and "" not in values:
            return values.pop(), "normalized-classifier"
    return "UNRECORDED", "not-verified"


# --- inventory checks -------------------------------------------------------

VOLATILE = frozenset({"status", "approval_ref", "note"})


def load_inventory(root: Path, path: Path | None = None) -> dict:
    target = path or root / INVENTORY
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InventoryError("license inventory is missing or not valid JSON") from exc
    if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION
            or not isinstance(data.get("python"), list) or not isinstance(data.get("components"), list)):
        raise InventoryError("unsupported license inventory schema")
    return data


def scope_digest(inventory: dict) -> str:
    """Digest of everything an owner approval covers (decision fields excluded)."""
    def strip(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if k not in VOLATILE}
    scoped = {"python": [strip(e) for e in inventory["python"]],
              "components": [strip(e) for e in inventory["components"]]}
    blob = json.dumps(scoped, sort_keys=True, separators=(",", ":")).encode()
    return sha256_bytes(blob)


def check_policy(entry: dict, label: str, problems: list[str], unresolved: list[str]) -> None:
    spdx = entry.get("license_spdx")
    if not isinstance(spdx, str) or not spdx or spdx == "UNRECORDED":
        problems.append(f"{label}: license is not recorded")
        return
    if entry.get("spdx_basis") not in SPDX_BASES:
        problems.append(f"{label}: spdx_basis is not one of the recognized bases")
    expected = expression_class(spdx, entry.get("elected"))
    if entry.get("class") != expected:
        problems.append(f"{label}: recorded class {entry.get('class')!r} differs from {expected!r} derived from {spdx!r}")
        return
    status = entry.get("status")
    if status not in STATUSES:
        problems.append(f"{label}: status must be one of {', '.join(STATUSES)}")
        return
    if status == "approved" and not entry.get("approval_ref"):
        problems.append(f"{label}: approved status requires a protected approval_ref")
    if expected in RESTRICTED and status == "declared":
        problems.append(f"{label}: class {expected} cannot rest on declared metadata; record an owner decision state")
    if expected == "weak-copyleft" and not entry.get("obligation"):
        problems.append(f"{label}: weak-copyleft license requires a recorded obligation")
    if expected in RESTRICTED and not entry.get("obligation") and status != "excluded":
        problems.append(f"{label}: restricted class requires a recorded obligation")
    if status == "pending-owner-decision":
        unresolved.append(f"{label}: {spdx} ({expected}) awaits owner decision")


def check_python(root: Path, inventory: dict, problems: list[str], unresolved: list[str],
                 found: dict[tuple[str, str], list[Dist]] | None) -> None:
    try:
        _, locked = locks.load(root, "runtime")
    except (locks.LockError, OSError, KeyError, ValueError):
        problems.append("runtime lock profile is not valid; inventory cannot be reconciled")
        return
    entries: dict[str, dict] = {}
    for entry in inventory["python"]:
        name = entry.get("name")
        if not isinstance(name, str) or name in entries:
            problems.append("python inventory has a missing or duplicate package name")
            continue
        entries[name] = entry
    for name in sorted(set(locked) - set(entries)):
        problems.append(f"python:{name}: locked runtime package has no license record")
    for name in sorted(set(entries) - set(locked)):
        problems.append(f"python:{name}: recorded package is not in the locked runtime profile")
    for name in sorted(set(locked) & set(entries)):
        entry, lock = entries[name], locked[name]
        label = f"python:{name}=={lock['version']}"
        for key in ("version", "wheel", "sha256"):
            if entry.get(key) != lock[key]:
                problems.append(f"{label}: recorded {key} differs from the lock; re-review licenses")
        check_policy(entry, label, problems, unresolved)
        if not entry.get("license_files") and entry.get("status") == "declared":
            problems.append(f"{label}: wheel carries no license text; record an owner decision state for the missing notice")
        if found is not None:
            dist = select_dist(name, lock, found)
            if dist is None:
                problems.append(f"{label}: no matching wheel or installed distribution to verify license metadata")
                continue
            derived = derived_fields(dist)
            for key in ("license_declared", "declared_source", "license_files"):
                if entry.get(key) != derived[key]:
                    problems.append(f"{label}: {key} differs from artifact metadata; license change needs review")


def check_components(root: Path, inventory: dict, problems: list[str], unresolved: list[str]) -> None:
    seen_ids: set[str] = set()
    bound_files: set[str] = set()
    bound_images: set[str] = set()
    pins = images_txt(root, problems)
    for entry in inventory["components"]:
        cid = entry.get("id")
        if not isinstance(cid, str) or cid in seen_ids:
            problems.append("component inventory has a missing or duplicate id")
            continue
        seen_ids.add(cid)
        label = f"component:{cid}"
        if not isinstance(entry.get("distributed_by_repo"), bool):
            problems.append(f"{label}: distributed_by_repo must be true or false")
        check_policy(entry, label, problems, unresolved)
        bind = entry.get("bind") or {}
        ref = bind.get("images_txt")
        if ref is not None:
            bound_images.add(ref)
            if pins.get(ref) != bind.get("digest"):
                problems.append(f"{label}: images.txt pin or digest differs from the record; re-review the image")
        for item in bind.get("files", []):
            bound_files.add(item["path"])
            if not file_matches(root, item["path"], item["sha256"]):
                problems.append(f"{label}: {item['path']} is missing or differs from the recorded hash")
        for item in bind.get("tar_members", []):
            bound_files.add(item["archive"])
            if tar_member_hash(root / item["archive"], item["member"]) != item["sha256"]:
                problems.append(f"{label}: {item['archive']}:{item['member']} is missing or differs from the recorded hash")
        if "project_license_text" in bind:
            try:
                declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["license"]["text"]
            except (OSError, KeyError, ValueError):
                declared = None
            if declared != bind["project_license_text"]:
                problems.append(f"{label}: pyproject.toml license differs from the record")
        if "pin_license_sha256" in bind:
            pin = root / "scripts/tools/task-pin.txt"
            text = pin.read_text(encoding="utf-8") if pin.is_file() else ""
            match = re.search(r"(?m)^license-sha256:\s*([0-9a-f]{64})\s*$", text)
            if not match or match[1] != bind["pin_license_sha256"]:
                problems.append(f"{label}: pinned license digest differs from the record")
    for ref in sorted(set(pins) - bound_images):
        problems.append(f"image {ref.split('/')[-1]}: images.txt entry has no license record")
    for chart in sorted(root.glob("charts/*.tgz")):
        if chart.relative_to(root).as_posix() not in bound_files:
            problems.append(f"chart {chart.name}: vendored chart archive has no license record")
    for vendor in sorted(root.glob("src/**/static/vendor")):
        for item in sorted(p for p in vendor.iterdir() if p.is_file()):
            if item.relative_to(root).as_posix() not in bound_files:
                problems.append(f"vendored asset {item.name}: no license record")


def images_txt(root: Path, problems: list[str]) -> dict[str, str]:
    path = root / "images.txt"
    if not path.is_file():
        problems.append("images.txt is missing")
        return {}
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2:
            pins[parts[0]] = parts[1]
    return pins


def file_matches(root: Path, relative: str, expected: str) -> bool:
    path = root / relative
    return path.is_file() and not path.is_symlink() and sha256_file(path) == expected


def tar_member_hash(archive: Path, member: str) -> str | None:
    try:
        with tarfile.open(archive, "r:*") as handle:
            info = handle.getmember(member)
            stream = handle.extractfile(info) if info.isfile() and info.size <= 4_000_000 else None
            return sha256_bytes(stream.read()) if stream else None
    except (OSError, KeyError, tarfile.TarError):
        return None


def release_findings(inventory: dict, unresolved: list[str]) -> tuple[str, list[str]]:
    """(state, release blockers) for the recorded owner approval of this inventory."""
    approval = inventory.get("release_approval") or {}
    blockers = list(unresolved)
    if not approval.get("approval_ref"):
        state = "absent"
        blockers.append("release approval: no owner approval is recorded for this inventory")
    elif approval.get("approved_digest") != scope_digest(inventory):
        state = "stale"
        blockers.append("release approval: dependency or license records changed since the approved digest")
    else:
        state = "current"
    return state, blockers


def run_check(root: Path, inventory: dict, found, release: bool) -> tuple[list[str], list[str], str]:
    problems: list[str] = []
    unresolved: list[str] = []
    check_python(root, inventory, problems, unresolved, found)
    check_components(root, inventory, problems, unresolved)
    state, blockers = release_findings(inventory, unresolved)
    if release:
        problems.extend(blockers)
    return problems, unresolved, state


# --- notices ---------------------------------------------------------------


def render_notices(root: Path, inventory: dict, bundle_dir: Path | None) -> str:
    problems: list[str] = []
    check_components(root, inventory, problems, [])
    if problems:
        raise InventoryError("notice materials are missing or differ from the license record")
    state, _ = release_findings(inventory, [])
    pending = unresolved_of(inventory)
    lines = [
        "THIRD-PARTY NOTICES AND LICENSE REVIEW STATUS",
        "=" * 78,
        "",
        "Generated from licenses/inventory.json at the packed commit. This file lists the",
        "declared licenses of what the bundle ships and carries the notice texts the",
        "repository holds. It is not a legal approval: an entry is approved only when an",
        "owner decision is recorded in protected release records (see docs/licensing.md).",
        "",
        f"Owner approval of this inventory: {state}",
        f"Inventory scope digest: {scope_digest(inventory)}",
        f"Components awaiting owner decision: {len(pending)}",
    ]
    for item in pending:
        lines.append(f"  - {item}")
    lines += ["", "COMPONENTS", "-" * 78]
    for entry in inventory["components"]:
        lines.append(f"{entry['id']}  [{entry['status']}]")
        lines.append(f"    license: {entry['license_spdx']}  class: {entry['class']}  basis: {entry['spdx_basis']}")
        if entry.get("obligation"):
            lines.append(f"    obligation: {entry['obligation']}")
    lines += ["", "PYTHON DISTRIBUTIONS (locked runtime wheelhouse, installed in both application images)", "-" * 78,
              "License texts for these ship inside each image under <distribution>.dist-info.", ""]
    for entry in inventory["python"]:
        files = ", ".join(f["name"] for f in entry["license_files"]) or "(no license file in wheel)"
        lines.append(f"{entry['name']}=={entry['version']}  {entry['license_spdx']}  [{entry['class']}; {entry['status']}]")
        lines.append(f"    license files: {files}")
        if entry.get("obligation"):
            lines.append(f"    obligation: {entry['obligation']}")
    lines += ["", "NOTICE TEXTS", "-" * 78]
    for entry in inventory["components"]:
        bind = entry.get("bind") or {}
        for item in bind.get("files", []):
            if item.get("notice"):
                lines += ["", f"### {entry['id']}: {item['path']}", (root / item["path"]).read_text(encoding="utf-8").rstrip()]
        for item in bind.get("tar_members", []):
            data = tar_member_text(root / item["archive"], item["member"])
            lines += ["", f"### {entry['id']}: {item['archive']}:{item['member']}", data.rstrip()]
        member = bind.get("bundle_member")
        if member:
            if bundle_dir is None or not (bundle_dir / member["name"]).is_file() or sha256_file(bundle_dir / member["name"]) != member["sha256"]:
                raise InventoryError("bundled notice member is missing or differs from the license record")
            lines += ["", f"### {entry['id']}: {member['name']}", (bundle_dir / member["name"]).read_text(encoding="utf-8").rstrip()]
    return "\n".join(lines) + "\n"


def unresolved_of(inventory: dict) -> list[str]:
    out: list[str] = []
    for entry in inventory["components"]:
        if entry.get("status") == "pending-owner-decision":
            out.append(f"component:{entry['id']}: {entry['license_spdx']} ({entry['class']})")
    for entry in inventory["python"]:
        if entry.get("status") == "pending-owner-decision":
            out.append(f"python:{entry['name']}=={entry['version']}: {entry['license_spdx']} ({entry['class']})")
    return out


def tar_member_text(archive: Path, member: str) -> str:
    with tarfile.open(archive, "r:*") as handle:
        stream = handle.extractfile(member)
        if stream is None:
            raise InventoryError("notice member is missing")
        return io.TextIOWrapper(io.BytesIO(stream.read()), encoding="utf-8").read()


# --- generate ---------------------------------------------------------------


def generate(root: Path, inventory: dict | None, found) -> tuple[dict, list[str]]:
    _, locked = locks.load(root, "runtime")
    previous = {e["name"]: e for e in (inventory or {}).get("python", [])}
    warnings: list[str] = []
    entries = []
    for name, lock in sorted(locked.items()):
        old = previous.get(name, {})
        dist = select_dist(name, lock, found)
        if dist is None:
            raise InventoryError(f"no wheel or installed distribution matches {name}=={lock['version']}")
        derived = derived_fields(dist)
        spdx, basis = old.get("license_spdx"), old.get("spdx_basis")
        if (derived["license_declared"], derived["declared_source"]) != (old.get("license_declared"), old.get("declared_source")) or not spdx:
            spdx, basis = seed_spdx(derived["license_declared"], derived["declared_source"])
            if old.get("license_spdx") and old["license_spdx"] != spdx:
                warnings.append(f"{name}: declared license changed; spdx re-seeded, owner re-review needed")
        for fname, content in dist.license_files.items():
            guess = fingerprint(content.decode("utf-8", "replace"))
            top_level = fname.removeprefix("licenses/").count("/") == 0
            if guess and spdx and guess not in spdx and top_level and fname.lower().removeprefix("licenses/").startswith("licen"):
                warnings.append(f"{name}: {fname} looks like {guess} but the record says {spdx}")
        entry = {
            "name": name, "version": lock["version"], "wheel": lock["wheel"], "sha256": lock["sha256"],
            "binary_wheel": not lock["wheel"].endswith("-none-any.whl"),
            "evidence": "wheel-bytes" if dist.kind == "wheel" else "installed-dist-info",
            **derived,
            "license_spdx": spdx, "spdx_basis": basis,
        }
        entry["class"] = expression_class(spdx, old.get("elected")) if spdx != "UNRECORDED" else "unknown"
        for key in ("elected", "obligation", "note"):
            if old.get(key):
                entry[key] = old[key]
        entry["status"] = old.get("status", "declared")
        entry["approval_ref"] = old.get("approval_ref")
        entries.append(entry)
    merged = dict(inventory or {"schema_version": SCHEMA_VERSION, "components": [],
                                "release_approval": {"approval_ref": None, "approved_digest": None}})
    merged["python"] = entries
    return merged, warnings


# --- CLI --------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=locks.ROOT)
    parser.add_argument("--inventory", type=Path, help=f"default: <root>/{INVENTORY}")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "generate"):
        child = sub.add_parser(name)
        child.add_argument("--wheelhouse", type=Path, action="append", default=[],
                           help="directory of wheels; metadata is re-derived and compared")
        child.add_argument("--site-packages", type=Path, action="append", default=[],
                           help="installed site-packages; used when no matching locked wheel is present")
        if name == "check":
            child.add_argument("--release", action="store_true",
                               help="also fail on any unresolved owner decision or stale/absent approval")
    notices = sub.add_parser("notices")
    notices.add_argument("--bundle-dir", type=Path, help="directory holding bundled notice members (task-LICENSE)")
    notices.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    try:
        location = args.inventory or root / INVENTORY
        existing = load_inventory(root, args.inventory) if args.command != "generate" or location.is_file() else None
        if args.command == "generate":
            if not (args.wheelhouse or args.site_packages):
                raise InventoryError("generate needs --wheelhouse and/or --site-packages")
            merged, warnings = generate(root, existing, collect_dists(args.wheelhouse, args.site_packages))
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
            json.dump(merged, sys.stdout, indent=2, sort_keys=True)
            print()
            return 0
        if existing is None:
            raise InventoryError("license inventory is missing")
        inventory = existing
        if args.command == "notices":
            args.output.write_text(render_notices(root, inventory, args.bundle_dir), encoding="utf-8")
            print(f"license-inventory: wrote {args.output.name}")
            return 0
        found = collect_dists(args.wheelhouse, args.site_packages) if (args.wheelhouse or args.site_packages) else None
        problems, unresolved, state = run_check(root, inventory, found, args.release)
    except (InventoryError, locks.LockError, OSError, KeyError, TypeError, zipfile.BadZipFile, tarfile.TarError):
        print("license-inventory: FAIL: license inventory or its inputs are invalid")
        return 2
    for problem in problems[:PROBLEM_LIMIT]:
        print(f"license-inventory: FAIL: {problem}")
    if len(problems) > PROBLEM_LIMIT:
        print(f"license-inventory: FAIL: ... and {len(problems) - PROBLEM_LIMIT} more")
    if not args.release:
        for item in unresolved:
            print(f"license-inventory: UNRESOLVED: {item}")
    summary = (f"python={len(inventory['python'])} components={len(inventory['components'])} "
               f"unresolved={len(unresolved)} release_approval={state}")
    if problems:
        print(f"license-inventory: FAIL ({summary})")
        return 1
    print(f"license-inventory: OK ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
