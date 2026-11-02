#!/usr/bin/env python3
"""Offline third-party license inventory check for release transfer (issue #376).

``licenses/inventory.json`` records the license basis of everything this
repository ships: every locked runtime wheel, the pinned images, vendored chart,
JS and Task tool, BM25 weights, Qdrant skills and every offline reference doc
set (GH#1174). The check keeps that record reconciled with the real pins
(``locks/``, ``images.txt``, vendored bytes, ``scripts/airgap/reference-docs.json``) so a
new or changed dependency, image digest or notice file cannot reach a bundle
unrecorded, and so unresolved owner decisions stay visible.

This is evidence plumbing, not a legal verdict. A recorded license class is the
declared metadata; it never marks a component approved. Approval is an owner
record (``status: approved`` plus a protected-record reference). No network
access, no installs, no execution of artifacts.

  check      reconcile the inventory with the pins; --release also requires
             every owner decision to be recorded and current; --kit adds the
             signed dev kit's scope (``devkit``: dev wheels, RPMs, tool pins,
             base image) and requires its decisions and kit approval current
  generate   re-derive artifact-observed fields from wheels / installed
             dist-info and print the merged inventory (human fields kept);
             --kit re-derives the ``devkit`` dev wheels and, with
             --rpm-licenses, the RPM records from real RPM header tags
  notices    render THIRD-PARTY-NOTICES.txt for the offline bundle, or with
             --kit for the dev kit directory
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
# GH#1174: the pinned doc sets of the bundle's reference-docs member. Every set
# needs exactly one component record bound to the same archive and licence.
REFERENCE_DOCS = "scripts/airgap/reference-docs.json"
REFERENCE_DOCS_BIND = ("url", "sha256", "license_files")
SCHEMA_VERSION = 1

# Ordered least to most restrictive. "election-required" is a dual license whose
# alternatives differ in class and no election is recorded.
ORDER = ("permissive", "weak-copyleft", "strong-copyleft", "network-copyleft",
         "election-required", "proprietary", "unknown")
# Classes that cannot rest on declared metadata alone: they need an owner
# decision (status pending-owner-decision, excluded or approved with a reference).
RESTRICTED = frozenset(ORDER[2:])
STATUSES = ("declared", "pending-owner-decision", "approved", "excluded")
# decision.legal_confirmation: only "pending" blocks a release; any other value is rejected.
LEGAL_CONFIRMATIONS = ("pending", "confirmed", "waived-by-owner-decision")
SPDX_BASES = ("declared-expression", "normalized-license-field", "normalized-classifier",
              "license-file-text", "upstream-project-license", "not-verified",
              "rpm-license-tag", "normalized-rpm-license-tag")

PERMISSIVE = {"MIT", "MIT-0", "MIT-CMU", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "Apache-2.0", "ISC",
              "PSF-2.0", "Zlib", "CC0-1.0", "Unlicense", "HPND", "BSL-1.0", "OFL-1.1"}
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
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
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

VOLATILE = frozenset({"status", "approval_ref", "note", "decision"})


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
    decision = entry.get("decision")
    if decision is not None and not (isinstance(decision, dict) and decision.get("owner") and decision.get("date")
                                     and decision.get("disposition")):
        problems.append(f"{label}: decision record needs owner, date and disposition")
    legal = decision.get("legal_confirmation", "") if isinstance(decision, dict) else ""
    if (isinstance(decision, dict) and "legal_confirmation" in decision and legal not in LEGAL_CONFIRMATIONS) or (
            entry.get("elected") and not (isinstance(decision, dict) and "legal_confirmation" in decision)):
        problems.append(f"{label}: decision.legal_confirmation must be one of {', '.join(LEGAL_CONFIRMATIONS)}")
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
        check_wheel_entry(root, entries[name], locked[name], problems, unresolved, found)


def check_wheel_entry(root: Path, entry: dict, lock: dict, problems: list[str], unresolved: list[str],
                      found: dict[tuple[str, str], list[Dist]] | None) -> None:
    """One wheel record against its lock line (and, with ``found``, its artifact metadata)."""
    name = entry["name"]
    label = f"python:{name}=={lock['version']}"
    for key in ("version", "wheel", "sha256"):
        if entry.get(key) != lock[key]:
            problems.append(f"{label}: recorded {key} differs from the lock; re-review licenses")
    check_policy(entry, label, problems, unresolved)
    for item in (entry.get("bind") or {}).get("files", []):
        if not file_matches(root, item["path"], item["sha256"]):
            problems.append(f"{label}: {item['path']} is missing or differs from the recorded hash")
    if not entry.get("license_files") and entry.get("status") == "declared":
        problems.append(f"{label}: wheel carries no license text; record an owner decision state for the missing notice")
    if found is not None:
        dist = select_dist(name, lock, found)
        if dist is None:
            problems.append(f"{label}: no matching wheel or installed distribution to verify license metadata")
            return
        derived = derived_fields(dist)
        for key in ("license_declared", "declared_source", "license_files"):
            if entry.get(key) != derived[key]:
                problems.append(f"{label}: {key} differs from artifact metadata; license change needs review")


def reference_doc_sets(root: Path, problems: list[str]) -> dict[str, dict]:
    try:
        data = json.loads((root / REFERENCE_DOCS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        problems.append("reference docs manifest is missing or not valid JSON")
        return {}
    rows = data.get("sets") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        problems.append("reference docs manifest has no sets list")
        return {}
    sets: dict[str, dict] = {}
    for row in rows:
        ident = row.get("id") if isinstance(row, dict) else None
        if not isinstance(ident, str) or ident in sets:
            problems.append("reference docs manifest has a missing or duplicate set id")
            continue
        sets[ident] = row
    return sets


def check_reference_docs(entry: dict, label: str, sets: dict[str, dict], bound: set[str], problems: list[str]) -> None:
    pin = (entry.get("bind") or {}).get("reference_docs")
    if not isinstance(pin, dict):
        problems.append(f"{label}: reference-docs record has no bind.reference_docs")
        return
    row = sets.get(str(pin.get("set")))
    if row is None:
        problems.append(f"{label}: bound reference docs set is not in {REFERENCE_DOCS}")
        return
    bound.add(row["id"])
    if any(pin.get(key) != row.get(key) for key in REFERENCE_DOCS_BIND) or entry.get("license_spdx") != row.get("license"):
        problems.append(f"{label}: reference docs archive, licence files or licence differ from the record; re-review the doc set")


def check_components(root: Path, inventory: dict, problems: list[str], unresolved: list[str]) -> None:
    seen_ids: set[str] = set()
    bound_files: set[str] = set()
    bound_images: set[str] = set()
    bound_docs: set[str] = set()
    pins = images_txt(root, problems)
    doc_sets = reference_doc_sets(root, problems)
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
        if entry.get("kind") == "reference-docs" or "reference_docs" in bind:
            check_reference_docs(entry, label, doc_sets, bound_docs, problems)
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
    for ident in sorted(set(doc_sets) - bound_docs):
        problems.append(f"reference docs set {ident}: no license record")
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
    # A recorded election can await legal confirmation after its status flips
    # (#376 pymupdf); the status alone must not release it.
    for group in ("python", "components"):
        for entry in inventory.get(group) or []:
            if (entry.get("decision") or {}).get("legal_confirmation") == "pending":
                blockers.append(f"{entry.get('name', '?')}: legal confirmation of the recorded decision is pending")
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


# --- dev kit scope (#1118, ADR-0009 §8) -----------------------------------
#
# The signed dev kit is a separate artifact with its own packing cadence (it ships
# only when its key changes), so its records live in a separate ``devkit`` section
# with its own approval. The release scope digest above never covers it, and the
# kit digest covers every record the kit ships: all locked wheels (the dev profile
# includes the runtime one), the shared release components it names, its RPMs
# and its tools.

KIT_RPMS = "images/devkit-packages.txt"
KIT_CONTAINERFILE = "images/Containerfile.devkit"
# Every tool the kit ships, by pin file: Task, Helm and oc inside the image,
# oras.exe and the static Linux oras beside it. pack.sh's sbom.json reads this list.
KIT_TOOL_PINS = (("go-task/task", "task-pin.txt"), ("helm", "helm-pin.txt"),
                 ("openshift-client", "oc-pin.txt"), ("oras (oras.exe)", "oras-windows-pin.txt"),
                 ("oras (Linux, fallback push)", "oras-pin.txt"))
# Pin fields a kit tool record binds: any change needs a license re-review.
KIT_TOOL_FIELDS = ("version", "sha256", "license", "license-sha256")
KIT_FIX = "record it in licenses/inventory.json devkit (docs/licensing.md#dev-kit)"

# Fedora legacy RPM license abbreviations that fedora-license-data maps to exactly
# one SPDX expression (forge.fedoraproject.org/legal/fedora-license-data, commit
# cbd8b74c, 2026-08-27). An abbreviation it maps to several identifiers ("BSD",
# "MIT", "LGPLv2", "LGPLv2+", "GFDL", "BSD with advertising") is ambiguous and
# becomes NOASSERTION, which classes as unknown: an owner decision, never a guess.
RPM_LEGACY = {"ASL 2.0": "Apache-2.0", "GPLv2": "GPL-2.0-only", "GPLv2+": "GPL-2.0-or-later",
              "GPLv3+": "GPL-3.0-or-later", "ISC": "ISC", "Sleepycat": "Sleepycat",
              "Public Domain": "LicenseRef-Fedora-Public-Domain"}
RPM_LEGACY_AMBIGUOUS = frozenset({"BSD", "MIT", "LGPLv2", "LGPLv2+", "GFDL", "BSD with advertising"})
KNOWN_SPDX = PERMISSIVE | WEAK | STRONG | NETWORK


def normalize_rpm_tag(tag: str) -> tuple[str, str, list[str]]:
    """(SPDX expression, spdx_basis, ambiguous terms) for an RPM License header tag."""
    if "(" in tag or ")" in tag:
        # Splitting would drop the grouping and change the meaning: keep it whole.
        return "NOASSERTION", "normalized-rpm-license-tag", [tag.strip()]
    spdx_form = re.search(r"\s(AND|OR)\s", tag) is not None
    parts = re.split(r"\s+(and|or|AND|OR)\s+", tag.strip())
    terms, operators = parts[0::2], [p.upper() for p in parts[1::2]]
    if not spdx_form and len(terms) == 1 and terms[0] in KNOWN_SPDX and terms[0] not in RPM_LEGACY_AMBIGUOUS:
        spdx_form = True
    out, ambiguous, legacy = [], [], False
    for term in terms:
        if spdx_form and (term in KNOWN_SPDX or term.startswith("LicenseRef-")):
            out.append(term)
        elif not spdx_form and term in RPM_LEGACY:
            out.append(RPM_LEGACY[term])
            legacy = True
        else:
            out.append("NOASSERTION")
            ambiguous.append(term)
    expression = out[0] + "".join(f" {op} {term}" for op, term in zip(operators, out[1:], strict=True))
    return expression, "normalized-rpm-license-tag" if legacy or ambiguous else "rpm-license-tag", ambiguous


def rpm_name(nevra: str) -> tuple[str, str]:
    name, epoch_version, release_arch = nevra.rsplit("-", 2)
    return name, f"{epoch_version}-{release_arch}"


def read_pin(root: Path, name: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    path = root / "scripts/tools" / name
    for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
        key, sep, value = line.partition(":")
        if sep and not line.startswith("#"):
            fields.setdefault(key.strip(), value.strip())
    return fields


def kit_rpm_lines(root: Path) -> list[str] | None:
    path = root / KIT_RPMS
    if not path.is_file():
        return None
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def kit_base_digest(root: Path) -> str | None:
    path = root / KIT_CONTAINERFILE
    match = re.search(r"(?m)^FROM\s+\S+@(sha256:[0-9a-f]{64})", path.read_text(encoding="utf-8")) if path.is_file() else None
    return match[1] if match else None


def kit_section(inventory: dict) -> dict | None:
    kit = inventory.get("devkit")
    if (not isinstance(kit, dict) or not isinstance(kit.get("python"), list)
            or not isinstance(kit.get("components"), list) or not isinstance(kit.get("shared"), list)):
        return None
    return kit


def kit_records(inventory: dict) -> tuple[list[dict], list[dict]]:
    """(wheel records, component records) of everything the kit ships."""
    kit = kit_section(inventory) or {"python": [], "components": [], "shared": []}
    by_id = {c.get("id"): c for c in inventory["components"]}
    shared = [by_id[cid] for cid in kit["shared"] if cid in by_id]
    return [*inventory["python"], *kit["python"]], [*shared, *kit["components"]]


def kit_scope_digest(inventory: dict) -> str:
    """Digest of everything a kit approval covers (decision fields excluded)."""
    wheels, components = kit_records(inventory)

    def strip(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if k not in VOLATILE}
    scoped = {"python": sorted((strip(e) for e in wheels), key=lambda e: str(e.get("name"))),
              "components": [strip(e) for e in components],
              "shared": (kit_section(inventory) or {}).get("shared", [])}
    return sha256_bytes(json.dumps(scoped, sort_keys=True, separators=(",", ":")).encode())


def kit_unresolved(inventory: dict) -> list[str]:
    wheels, components = kit_records(inventory)
    out = [f"python:{e.get('name')}=={e.get('version')}: {e.get('license_spdx')} ({e.get('class')})"
           for e in wheels if e.get("status") == "pending-owner-decision"]
    out += [f"{e.get('id')}: {e.get('license_spdx')} ({e.get('class')})"
            for e in components if e.get("status") == "pending-owner-decision"]
    return out


def kit_findings(inventory: dict) -> tuple[str, list[str]]:
    """(kit approval state, pack blockers): the kit's equivalent of release_findings."""
    blockers = [f"devkit: {item} awaits owner decision" for item in kit_unresolved(inventory)]
    wheels, components = kit_records(inventory)
    for entry in [*wheels, *components]:
        label = entry.get("id") or f"python:{entry.get('name')}"
        if (entry.get("decision") or {}).get("legal_confirmation") == "pending":
            blockers.append(f"devkit: {label}: legal confirmation of the recorded decision is pending")
        # Every kit record binds something the kit ships, so exclusion is never a
        # disposition here: drop the component from its pin, or record an approval.
        if entry.get("status") == "excluded":
            blockers.append(f"devkit: {label}: excluded, but the kit still ships it; remove it from its pin "
                            "or record an owner approval")
    approval = (kit_section(inventory) or {}).get("approval") or {}
    if not approval.get("approval_ref"):
        state = "absent"
        blockers.append("devkit approval: no owner approval is recorded for the kit inventory; "
                        "record devkit.approval with the kit scope digest (docs/licensing.md#dev-kit)")
    elif approval.get("approved_digest") != kit_scope_digest(inventory):
        state = "stale"
        blockers.append("devkit approval: kit dependency or license records changed since the approved digest; "
                        "re-review and record the new kit scope digest (docs/licensing.md#dev-kit)")
    else:
        state = "current"
    return state, blockers


def check_kit(root: Path, inventory: dict, problems: list[str],
              found: dict[tuple[str, str], list[Dist]] | None) -> None:
    """Reconcile the devkit section with the kit's real pins."""
    kit = kit_section(inventory)
    if kit is None:
        problems.append(f"devkit: the inventory has no devkit section; {KIT_FIX}")
        return
    scratch: list[str] = []
    try:
        _, dev = locks.load(root, "dev")
        _, runtime = locks.load(root, "runtime")
    except (locks.LockError, OSError, KeyError, ValueError):
        problems.append("devkit: dev or runtime lock profile is not valid; kit inventory cannot be reconciled")
        return
    entries: dict[str, dict] = {}
    for entry in kit["python"]:
        name = entry.get("name")
        if not isinstance(name, str) or name in entries or name in runtime:
            problems.append("devkit: python records have a missing, duplicate or runtime-recorded package name")
            continue
        entries[name] = entry
    dev_only = set(dev) - set(runtime)
    for name in sorted(dev_only - set(entries)):
        problems.append(f"devkit: python:{name}: locked dev package has no kit license record; {KIT_FIX}")
    for name in sorted(set(entries) - dev_only):
        problems.append(f"devkit: python:{name}: kit record is not a dev-only package of the dev lock")
    for name in sorted(set(entries) & dev_only):
        check_wheel_entry(root, entries[name], dev[name], problems, scratch, found)

    by_id = {c.get("id"): c for c in inventory["components"]}
    for cid in kit["shared"]:
        if cid not in by_id:
            problems.append(f"devkit: shared component {cid} is not in the inventory")
    base = kit_base_digest(root)
    if base is None or not any((by_id.get(cid) or {}).get("bind", {}).get("digest") == base for cid in kit["shared"]):
        problems.append(f"devkit: the base image of {KIT_CONTAINERFILE} has no shared license record with its digest; {KIT_FIX}")

    rpms = kit_rpm_lines(root)
    if rpms is None:
        problems.append(f"devkit: {KIT_RPMS} is missing")
        rpms = []
    pins = {pin for _, pin in KIT_TOOL_PINS}
    seen_ids: set[str] = set()
    recorded_rpms: set[str | None] = set()
    recorded_pins: set[str] = set()
    for entry in kit["components"]:
        cid = entry.get("id")
        if not isinstance(cid, str) or cid in seen_ids or cid in by_id:
            problems.append("devkit: component records have a missing or duplicate id, or reuse a release component id")
            continue
        seen_ids.add(cid)
        label = f"devkit:{cid}"
        if not isinstance(entry.get("distributed_by_repo"), bool):
            problems.append(f"{label}: distributed_by_repo must be true or false")
        check_policy(entry, label, problems, scratch)
        bind = entry.get("bind") or {}
        if entry.get("kind") == "rpm":
            nevra = bind.get("nevra")
            recorded_rpms.add(nevra)
            if nevra not in rpms:
                problems.append(f"{label}: recorded RPM {nevra} is not in {KIT_RPMS}; re-review its license")
        elif entry.get("kind") == "tool":
            pin_name = bind.get("pin", "").removeprefix("scripts/tools/")
            recorded_pins.add(pin_name)
            if pin_name not in pins:
                problems.append(f"{label}: {pin_name} is not a kit tool pin")
                continue
            pin = read_pin(root, pin_name)
            if {k: pin.get(k) for k in KIT_TOOL_FIELDS} != {k: bind.get(k) for k in KIT_TOOL_FIELDS}:
                problems.append(f"{label}: scripts/tools/{pin_name} differs from the record; re-review the tool license")
            if bind.get("notice_member") and not pin.get("license-sha256"):
                problems.append(f"{label}: a notice member needs a license-sha256 in its pin")
        else:
            problems.append(f"{label}: kind must be rpm or tool")
    for nevra in sorted(set(rpms) - recorded_rpms):
        problems.append(f"devkit: rpm {nevra}: kit package has no license record; {KIT_FIX}")
    for _, pin_name in KIT_TOOL_PINS:
        if pin_name not in recorded_pins:
            problems.append(f"devkit: tool scripts/tools/{pin_name}: kit tool has no license record; {KIT_FIX}")


def run_kit_check(root: Path, inventory: dict, found) -> tuple[list[str], list[str], str]:
    """(record problems, owner-decision blockers, kit approval state).

    Record problems (a kit component without a record, a record off its pin) fail
    every kit check; owner-decision blockers fail all but a rehearsal (--rehearsal)."""
    problems: list[str] = []
    check_kit(root, inventory, problems, found)
    state, blockers = kit_findings(inventory)
    return problems, blockers, state


# --- notices ---------------------------------------------------------------


def render_notices(root: Path, inventory: dict, bundle_dir: Path | None) -> str:
    problems: list[str] = []
    check_components(root, inventory, problems, [])
    for entry in inventory["python"]:
        for item in (entry.get("bind") or {}).get("files", []):
            if not file_matches(root, item["path"], item["sha256"]):
                problems.append("bound python notice file differs")
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
        docs = (entry.get("bind") or {}).get("reference_docs")
        if docs:
            texts = ", ".join(f"reference-docs/sets/{docs['set']}/{name}" for name in docs["license_files"])
            lines.append(f"    source: {docs['url']} (sha256 {docs['sha256']})")
            lines.append(f"    licence texts: {texts} in reference-docs.tar.gz")
    lines += ["", "PYTHON DISTRIBUTIONS (locked runtime wheelhouse, installed in both application images)", "-" * 78,
              "License texts for these ship inside each image under <distribution>.dist-info.", ""]
    for entry in inventory["python"]:
        files = ", ".join(f["name"] for f in entry["license_files"]) or "(no license file in wheel)"
        lines.append(f"{entry['name']}=={entry['version']}  {entry['license_spdx']}  [{entry['class']}; {entry['status']}]")
        lines.append(f"    license files: {files}")
        if entry.get("obligation"):
            lines.append(f"    obligation: {entry['obligation']}")
    lines += ["", "NOTICE TEXTS", "-" * 78]
    for entry in inventory["python"]:
        for item in (entry.get("bind") or {}).get("files", []):
            if item.get("notice"):
                lines += ["", f"### python:{entry['name']}: {item['path']}", (root / item["path"]).read_text(encoding="utf-8").rstrip()]
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


def render_kit_notices(root: Path, inventory: dict, kit_dir: Path) -> str:
    """THIRD-PARTY-NOTICES.txt for the dev kit: every kit component, its decision
    state and the notice texts the repository and the kit hold."""
    kit = kit_section(inventory)
    if kit is None:
        raise InventoryError("the inventory has no devkit section")
    wheels, components = kit_records(inventory)
    texts: list[str] = []
    for entry in [*wheels, *components]:
        bind = entry.get("bind") or {}
        label = f"python:{entry['name']}" if "id" not in entry else entry["id"]
        for item in bind.get("files", []):
            if not file_matches(root, item["path"], item["sha256"]):
                raise InventoryError("notice materials are missing or differ from the license record")
            if item.get("notice"):
                texts += ["", f"### {label}: {item['path']}", (root / item["path"]).read_text(encoding="utf-8").rstrip()]
        member = bind.get("notice_member")
        if member:
            pin = read_pin(root, bind.get("pin", "").removeprefix("scripts/tools/"))
            path = kit_dir / member
            if not path.is_file() or path.is_symlink() or sha256_file(path) != pin.get("license-sha256"):
                raise InventoryError("kit notice member is missing or differs from its pinned license digest")
            texts += ["", f"### {label}: {member} (kit member)", path.read_text(encoding="utf-8").rstrip()]
    state, _ = kit_findings(inventory)
    pending = kit_unresolved(inventory)
    lines = [
        "DEV KIT THIRD-PARTY NOTICES AND LICENSE REVIEW STATUS",
        "=" * 78,
        "",
        "Generated from the devkit section of licenses/inventory.json at the packed commit.",
        "It lists every third-party component the kit ships, its declared license and the",
        "owner decision state, and carries the notice texts the repository holds. It is not",
        "a legal approval (docs/licensing.md#dev-kit).",
        "",
        f"Owner approval of the kit inventory: {state}",
        f"Kit scope digest: {kit_scope_digest(inventory)}",
        f"Components awaiting owner decision: {len(pending)}",
        *[f"  - {item}" for item in pending],
        "", "SHARED WITH THE RELEASE (base image and project)", "-" * 78,
    ]

    def component_lines(entry: dict) -> list[str]:
        out = [f"{entry['id']}  {entry.get('name', '')}  [{entry['status']}]",
               f"    license: {entry['license_spdx']}  class: {entry['class']}  basis: {entry['spdx_basis']}"]
        if entry.get("kind") == "rpm":
            out.append(f"    nevra: {entry['bind']['nevra']}  RPM License tag: {entry['license_declared']}")
        if entry.get("kind") == "tool":
            out.append(f"    pin: {entry['bind']['pin']} {entry['bind'].get('version', '')}")
        if entry.get("approval_ref"):
            out.append(f"    approval: {entry['approval_ref']}")
        if entry.get("obligation"):
            out.append(f"    obligation: {entry['obligation']}")
        return out

    shared = set(kit["shared"])
    for entry in components:
        if entry["id"] in shared:
            lines += component_lines(entry)
    lines += ["", "TOOLS (task, helm and oc in the image under /usr/local/bin; oras.exe and oras beside it)", "-" * 78]
    for entry in components:
        if entry.get("kind") == "tool" and entry["id"] not in shared:
            lines += component_lines(entry)
    lines += ["", "RPMS (images/devkit-packages.txt; license texts in the image under /usr/share/licenses)", "-" * 78]
    for entry in components:
        if entry.get("kind") == "rpm" and entry["id"] not in shared:
            lines += component_lines(entry)
    lines += ["", "PYTHON DISTRIBUTIONS (dev lock; license texts in the image under <distribution>.dist-info)", "-" * 78]
    for entry in sorted(wheels, key=lambda e: e["name"]):
        files = ", ".join(f["name"] for f in entry["license_files"]) or "(no license file in wheel)"
        lines.append(f"{entry['name']}=={entry['version']}  {entry['license_spdx']}  [{entry['class']}; {entry['status']}]")
        lines.append(f"    license files: {files}")
        if entry.get("obligation"):
            lines.append(f"    obligation: {entry['obligation']}")
    lines += ["", "NOTICE TEXTS", "-" * 78, *texts]
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
    merged = dict(inventory or {"schema_version": SCHEMA_VERSION, "components": [],
                                "release_approval": {"approval_ref": None, "approved_digest": None}})
    merged["python"] = wheel_entries(locked, previous, found, warnings)
    return merged, warnings


def read_rpm_licenses(path: Path) -> dict[str, str]:
    """``NEVRA<TAB>License tag`` lines, as written by
    rpm -qa --qf '%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\\t%{LICENSE}\\n'."""
    tags: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        nevra, sep, tag = line.partition("\t")
        if sep and nevra.strip() and tag.strip():
            tags[nevra.strip()] = tag.strip()
    return tags


def generate_kit(root: Path, inventory: dict, found, rpm_tags: dict[str, str] | None) -> tuple[dict, list[str]]:
    """Re-derive the devkit dev-only wheel records and, with ``rpm_tags``, the RPM
    records. Human fields are kept while the declared license is unchanged; a new
    restricted-class record is seeded pending-owner-decision, never approved."""
    _, dev = locks.load(root, "dev")
    _, runtime = locks.load(root, "runtime")
    kit = dict(inventory.get("devkit") or {"shared": [], "python": [], "components": [],
                                           "approval": {"approval_ref": None, "approved_digest": None}})
    warnings: list[str] = []
    previous = {e["name"]: e for e in kit.get("python", [])}
    wheels = wheel_entries({n: v for n, v in dev.items() if n not in runtime}, previous, found, warnings)
    for entry in wheels:
        if entry["name"] not in previous and entry["class"] in RESTRICTED:
            entry["status"] = "pending-owner-decision"
    kit["python"] = wheels
    if rpm_tags is not None:
        rpms = kit_rpm_lines(root) or []
        old = {c["bind"]["nevra"]: c for c in kit.get("components", []) if c.get("kind") == "rpm"}
        old_by_name = {c["name"]: c for c in old.values()}
        records = []
        for nevra in rpms:
            if nevra not in rpm_tags:
                raise InventoryError(f"no RPM License tag for {nevra}")
            name, version = rpm_name(nevra)
            tag = rpm_tags[nevra]
            spdx, basis, ambiguous = normalize_rpm_tag(tag)
            entry = {"id": f"rpm:{name}", "kind": "rpm", "name": name, "version": version,
                     "evidence": "rpm-header", "license_declared": tag, "license_spdx": spdx,
                     "spdx_basis": basis, "class": expression_class(spdx), "distributed_by_repo": True,
                     "ships_in": ["dev workspace image"], "bind": {"nevra": nevra}}
            if ambiguous:
                entry["ambiguous_terms"] = ambiguous
            prior = old.get(nevra) or old_by_name.get(name) or {}
            if prior.get("license_declared") == tag:
                for key in ("status", "approval_ref", "decision", "obligation", "note"):
                    if prior.get(key) is not None:
                        entry[key] = prior[key]
            elif prior:
                warnings.append(f"rpm {name}: License tag changed; decision fields dropped, owner re-review needed")
            entry.setdefault("status", "pending-owner-decision" if entry["class"] in RESTRICTED else "declared")
            entry.setdefault("approval_ref", None)
            records.append(entry)
        others = [c for c in kit.get("components", []) if c.get("kind") != "rpm"]
        kit["components"] = others + records
    merged = dict(inventory)
    merged["devkit"] = kit
    return merged, warnings


def wheel_entries(locked: dict, previous: dict[str, dict], found, warnings: list[str]) -> list[dict]:
    """Records for the locked wheels: artifact-derived fields fresh, human fields kept."""
    entries: list[dict] = []
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
        for key in ("elected", "obligation", "note", "decision", "bind"):
            if old.get(key):
                entry[key] = old[key]
        entry["status"] = old.get("status", "declared")
        entry["approval_ref"] = old.get("approval_ref")
        entries.append(entry)
    return entries


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
            child.add_argument("--kit", action="store_true",
                               help="also reconcile the devkit section with the kit pins and fail on any "
                                    "unresolved kit decision or stale/absent kit approval (dev kit pack gate)")
            child.add_argument("--rehearsal", action="store_true",
                               help="with --kit: report open kit decisions and the kit approval as UNRESOLVED "
                                    "instead of failing (an ephemeral-key, non-promotable kit)")
        else:
            child.add_argument("--kit", action="store_true",
                               help="re-derive the devkit dev-only wheel records instead of the runtime ones")
            child.add_argument("--rpm-licenses", type=Path,
                               help="with --kit: NEVRA<TAB>License lines read from the built image's rpmdb")
    notices = sub.add_parser("notices")
    notices.add_argument("--bundle-dir", type=Path, help="directory holding bundled notice members (task-LICENSE; "
                                                         "with --kit, the kit directory holding oras-LICENSE)")
    notices.add_argument("--kit", action="store_true", help="render the dev kit notices")
    notices.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    kit_summary = ""
    open_kit: list[str] = []
    try:
        location = args.inventory or root / INVENTORY
        existing = load_inventory(root, args.inventory) if args.command != "generate" or location.is_file() else None
        if args.command == "generate":
            if not (args.wheelhouse or args.site_packages):
                raise InventoryError("generate needs --wheelhouse and/or --site-packages")
            found_dists = collect_dists(args.wheelhouse, args.site_packages)
            if args.kit:
                if existing is None:
                    raise InventoryError("generate --kit needs the existing inventory")
                tags = read_rpm_licenses(args.rpm_licenses) if args.rpm_licenses else None
                merged, warnings = generate_kit(root, existing, found_dists, tags)
            else:
                merged, warnings = generate(root, existing, found_dists)
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
            json.dump(merged, sys.stdout, indent=2, sort_keys=True)
            print()
            return 0
        if existing is None:
            raise InventoryError("license inventory is missing")
        inventory = existing
        if args.command == "check" and args.rehearsal and not args.kit:
            raise InventoryError("--rehearsal needs --kit")
        if args.command == "notices":
            if args.kit and args.bundle_dir is None:
                raise InventoryError("notices --kit needs --bundle-dir KIT")
            text = render_kit_notices(root, inventory, args.bundle_dir) if args.kit else render_notices(
                root, inventory, args.bundle_dir)
            args.output.write_text(text, encoding="utf-8")
            print(f"license-inventory: wrote {args.output.name}")
            return 0
        found = collect_dists(args.wheelhouse, args.site_packages) if (args.wheelhouse or args.site_packages) else None
        problems, unresolved, state = run_check(root, inventory, found, args.release)
        if args.kit:
            kit_problems, kit_blockers, kit_state = run_kit_check(root, inventory, found)
            pending_kit = kit_unresolved(inventory)
            problems += kit_problems
            if args.rehearsal:
                open_kit = kit_blockers
            else:
                problems += kit_blockers
            kit = kit_section(inventory) or {"python": [], "components": []}
            kit_summary = (f" kit_python={len(kit['python'])} kit_components={len(kit['components'])} "
                           f"kit_unresolved={len(pending_kit)} kit_approval={kit_state}"
                           + (" kit_gate=rehearsal" if args.rehearsal else " kit_gate=enforced"))
    except (InventoryError, locks.LockError, OSError, KeyError, TypeError, zipfile.BadZipFile, tarfile.TarError):
        print("license-inventory: FAIL: license inventory or its inputs are invalid")
        return 2
    for problem in problems[:PROBLEM_LIMIT]:
        print(f"license-inventory: FAIL: {problem}")
    if len(problems) > PROBLEM_LIMIT:
        print(f"license-inventory: FAIL: ... and {len(problems) - PROBLEM_LIMIT} more")
    if not args.release:
        for item in unresolved + open_kit:
            print(f"license-inventory: UNRESOLVED: {item}")
    if args.kit:
        print(f"license-inventory: kit scope digest: {kit_scope_digest(inventory)}")
    summary = (f"python={len(inventory['python'])} components={len(inventory['components'])} "
               f"unresolved={len(unresolved)} release_approval={state}{kit_summary}")
    if problems:
        print(f"license-inventory: FAIL ({summary})")
        return 1
    print(f"license-inventory: OK ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
