"""Check gateway configuration by consumer identity, retaining only safe diagnostics."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

CONSUMERS = (
    ("Deployment", "rag-agent", "agent", (
        ("EMBED_BASE_URL", "EMBED_API_KEY", "embed-api-key"),
        ("LLM_BASE_URL", "LLM_API_KEY", "llm-api-key"),
        ("RERANK_BASE_URL", "RERANK_API_KEY", "rerank-api-key"),
    )),
    ("Job", "ingest", "ingest", (
        ("EMBED_BASE_URL", "EMBED_API_KEY", "embed-api-key"),
        ("CONTEXT_LLM_BASE_URL", "CONTEXT_LLM_API_KEY", "context-llm-api-key"),
    )),
)
URL_KEYS = ("EMBED_BASE_URL", "LLM_BASE_URL", "RERANK_BASE_URL", "CONTEXT_LLM_BASE_URL")
SOURCE_KEYS = (*URL_KEYS, "GATEWAY_BASE_URL", "GATEWAY_API_KEY_SECRET",
               "GATEWAY_API_KEY_SECRET_KEY", "EMBED_MODEL", "LLM_MODEL_REASONING")


class RenderingInputError(ValueError):
    pass


def source_environment(path: Path) -> dict[str, str]:
    selected = {}
    for line in path.read_text().splitlines():
        words = shlex.split(line, comments=True)
        if words and words[0] == "export":
            words = words[1:]
        if len(words) == 1 and "=" in words[0]:
            name, value = words[0].split("=", 1)
            if name in SOURCE_KEYS:
                selected[name] = value
    return selected


def load_render(path: Path, label: str) -> list[dict]:
    if re.search(r"__[A-Z][A-Z0-9_]*__", path.read_text()):
        raise RenderingInputError(f"{label}: unresolved placeholder")
    result = subprocess.run(
        ["kubectl", "patch", "--local", "--type=merge", "-p", "{}", "-f", str(path), "-o", "json"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode:
        raise RenderingInputError(f"{label}: cannot decode rendered Kubernetes YAML")
    decoder = json.JSONDecoder()
    pending = result.stdout.strip()
    documents = []
    while pending:
        document, offset = decoder.raw_decode(pending)
        if not isinstance(document, dict):
            raise RenderingInputError(f"{label}: expected Kubernetes object")
        documents.extend(document["items"] if document.get("kind") == "List" else [document])
        pending = pending[offset:].lstrip()
    return documents


def safe_reference(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {name: entry if isinstance(entry, str) and len(entry) <= 253
            and re.fullmatch(r"[A-Za-z0-9._-]+", entry) else "<invalid>"
            for name in ("name", "key") if (entry := value.get(name)) is not None}


def safe_url(value) -> str:
    if not isinstance(value, str):
        return "<missing>"
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or len(parsed.hostname) > 253 or not re.fullmatch(r"[A-Za-z0-9.:-]+", parsed.hostname)):
            return "<invalid>"
        origin = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            origin += f":{parsed.port}"
        return origin + (parsed.path if parsed.path in ("", "/", "/v1", "/v1/") else "/<redacted-path>")
    except ValueError:
        return "<invalid>"


def consumer_env_entry(entries: list[dict], name: str, identity: str, errors: list[str]) -> dict:
    matches = [entry for entry in entries if entry.get("name") == name]
    if len(matches) != 1:
        errors.append(f"{identity} env {name}: expected exactly one entry")
        return {}
    return matches[0]


def check_consumers(documents: list[dict], expected_urls: dict[str, str],
                    secret_name: str, secret_key: str, embed_model: str,
                    reasoning_model: str) -> tuple[list[str], list[dict]]:
    errors = []
    evidence = []
    for kind, resource_name, container_name, legs in CONSUMERS:
        identity = f"{kind}/{resource_name} container {container_name}"
        resources = [doc for doc in documents if doc.get("kind") == kind
                     and doc.get("metadata", {}).get("name") == resource_name]
        if len(resources) != 1:
            errors.append(f"{identity}: expected exactly one resource")
            continue
        containers = [entry for entry in resources[0].get("spec", {}).get("template", {}).get("spec", {})
                      .get("containers", []) if entry.get("name") == container_name]
        if len(containers) != 1:
            errors.append(f"{identity}: expected exactly one container")
            continue
        entries = containers[0].get("env", [])

        for url_name, key_name, legacy_key in legs:
            url_entry = consumer_env_entry(entries, url_name, identity, errors)
            key_entry = consumer_env_entry(entries, key_name, identity, errors)
            reference = key_entry.get("valueFrom", {}).get("secretKeyRef", {})
            evidence.append({"resource": f"{kind}/{resource_name}", "container": container_name,
                             "url_env": url_name, "url": safe_url(url_entry.get("value")),
                             "key_env": key_name, "secretKeyRef": safe_reference(reference)})
            if url_entry.get("value") != expected_urls[url_name] or "valueFrom" in url_entry:
                errors.append(f"{identity} env {url_name}: resolved URL differs from expected precedence")
            if (reference.get("name") != secret_name or reference.get("key") != (secret_key or legacy_key)
                    or reference.get("optional", False) is not False or "value" in key_entry):
                errors.append(f"{identity} env {key_name}: required Secret name/key reference differs")
        models = {"EMBED_MODEL": embed_model}
        if kind == "Deployment":
            models["LLM_MODEL_REASONING"] = reasoning_model
        for name, value in models.items():
            entry = consumer_env_entry(entries, name, identity, errors)
            if entry.get("value") != value or "valueFrom" in entry:
                errors.append(f"{identity} env {name}: model alias differs")
    return errors, evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--agent-render", type=Path, required=True)
    parser.add_argument("--ingest-render", type=Path, required=True)
    parser.add_argument("--shared-url", required=True)
    parser.add_argument("--secret-name", required=True)
    parser.add_argument("--secret-key", default="")
    parser.add_argument("--embed-model", required=True)
    parser.add_argument("--reasoning-model", required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    for name in URL_KEYS:
        parser.add_argument("--" + name.lower().replace("_base_url", "-url").replace("_", "-"), default="")
    args = parser.parse_args(argv)
    expected_source = {
        "GATEWAY_BASE_URL": args.shared_url, "GATEWAY_API_KEY_SECRET": args.secret_name,
        "GATEWAY_API_KEY_SECRET_KEY": args.secret_key,
        "EMBED_MODEL": args.embed_model, "LLM_MODEL_REASONING": args.reasoning_model,
        **{name: getattr(args, name.lower().replace("_base_url", "_url")) for name in URL_KEYS},
    }
    expected_urls = {name: expected_source[name] or args.shared_url for name in URL_KEYS}
    errors = []
    evidence = []
    try:
        source = source_environment(args.env_file)
        for name, expected in expected_source.items():
            if source.get(name, "") != expected:
                errors.append(f"operator configuration {name}: selected value differs from expected mode")
        documents = load_render(args.agent_render, "agent render")
        documents.extend(load_render(args.ingest_render, "ingest render"))
        failures, evidence = check_consumers(documents, expected_urls, args.secret_name, args.secret_key,
                                            args.embed_model, args.reasoning_model)
        errors.extend(failures)
    except RenderingInputError as exc:
        errors.append(str(exc))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        errors.append("rendering inputs: unavailable, malformed, or unresolved placeholder")
    args.diagnostics.parent.mkdir(parents=True, exist_ok=True)
    args.diagnostics.write_text(json.dumps({"passed": not errors, "errors": errors, "consumers": evidence}, indent=2) + "\n")
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if not errors:
        print("PASS: gateway source mode, resolved URLs, model aliases and all five consumer Secret references")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
