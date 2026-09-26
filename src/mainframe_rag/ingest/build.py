"""Versioned immutable build binding, independent of representation and chunk IDs."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

BUILD_SCHEMA = 1


def canonical_build_id(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("invalid build identity")
    parsed = uuid.UUID(value)
    if str(parsed) != value or parsed.int == 0:
        raise ValueError("invalid build identity")
    return value


def build_aliases(alias: str, build_id: str) -> tuple[str, str]:
    canonical_build_id(build_id)
    data = f"{alias}__build_{build_id}"
    return data, data + "__completions"


@dataclass(frozen=True)
class BuildBinding:
    build_id: str
    alias: str
    physical: str
    gen_fp: str
    corpus_fp: str


def decode_build_binding(payload: dict, control: str) -> BuildBinding | None:
    """Absent build fields mean the supported pre-build format, never a new ID."""
    fields = {"build_schema", "build_id", "logical_alias", "data_collection"}
    if not fields.intersection(payload):
        return None
    if type(payload.get("build_schema")) is not int or payload["build_schema"] != BUILD_SCHEMA:
        raise ValueError("unsupported build schema")
    if (
        payload.get("record_type") != "publication-metadata"
        or payload.get("target_collection") != control
    ):
        raise ValueError("invalid build control binding")
    for key in ("logical_alias", "data_collection", "gen_fp", "corpus_fp"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError("invalid build control fields")
    if payload["data_collection"] + "__completions" != control:
        raise ValueError("invalid build control pair")
    return BuildBinding(
        canonical_build_id(payload.get("build_id")),
        payload["logical_alias"],
        payload["data_collection"],
        payload["gen_fp"],
        payload["corpus_fp"],
    )


def build_phase(binding: BuildBinding, aliases: Mapping[str, str]) -> str:
    """Infer sealed/published/retained from the one atomic publication operation."""
    data, control = build_aliases(binding.alias, binding.build_id)
    if data not in aliases and control not in aliases:
        if aliases.get(binding.alias) == binding.physical:
            raise ValueError("serving build lacks its immutable aliases")
        return "sealed"
    if (
        aliases.get(data) != binding.physical
        or aliases.get(control) != binding.physical + "__completions"
    ):
        raise ValueError("inconsistent immutable build aliases")
    return "published" if aliases.get(binding.alias) == binding.physical else "retained"


def require_published_binding(
    binding: BuildBinding | None,
    configured: str,
    physical: str,
    aliases: Mapping[str, str],
) -> None:
    if binding is None:
        if any(
            "__build_" in name and target in (physical, physical + "__completions")
            for name, target in aliases.items()
        ):
            raise ValueError("immutable build aliases lack their control record")
        return  # Completed legacy generations keep their existing read contract.
    if binding.physical != physical or configured not in (binding.alias, physical):
        raise ValueError("build control names another logical or physical corpus")
    if build_phase(binding, aliases) not in ("published", "retained"):
        raise ValueError("unpublished build cannot serve")
