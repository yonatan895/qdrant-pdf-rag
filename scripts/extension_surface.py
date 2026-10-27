#!/usr/bin/env python3
"""The upstream/gap extension surface (ADR-0009 §9): derive it from the code,
compare it with the committed snapshot, refresh the snapshot.

Every category is derived from its owner, never from a hand list:

- airgap_env_keys: every key airgap.env.example assigns or shows commented out;
- retired_env_keys: the keys in model_config.RETIRED_KEYS;
- task_names: every task `task --list` shows (a `desc`, not internal);
- chart_values: the leaf paths map_values.py writes for an input that selects
  every optional block, plus the Qdrant `--set-string` paths of deploy.sh and
  model_config.py --qdrant-sizing;
- script_flags: the flags in the top-level argument loop of each script an
  airgap task runs, plus the inputs of the ADR-0009 import script;
- gap_paths: the site-layer table of ADR-0009 §3;
- bundle_layout: the tarball and member names and MANIFEST fields of pack.sh.

An item that leaves the code needs a `- removed` line in docs/release-notes.md
whose migration this module verifies (a retired-key refusal, an alias or a
documented refusal). Refresh: .venv/bin/python scripts/extension_surface.py --write
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = Path("tests/fixtures/extension-surface.json")
RELEASE_NOTES = Path("docs/release-notes.md")
ADR = Path("docs/adr/0009-gap-fork.md")
EXAMPLE = Path("airgap.env.example")
MODEL_CONFIG = Path("scripts/airgap/model_config.py")
PACK = Path("scripts/airgap/pack.sh")
DEPLOY = Path("scripts/airgap/deploy.sh")
AIRGAP_TASKS = Path("taskfiles/airgap.yml")
ROUTE_CA = Path("charts/mainframe-rag/ci/openwebui-route-ca.pem")
# The files the derivation reads (tests copy exactly these).
INPUTS = (EXAMPLE, Path("scripts/__init__.py"), Path("scripts/airgap"), Path("Taskfile.yml"),
          Path("taskfiles"), ADR, ROUTE_CA, RELEASE_NOTES, SNAPSHOT)
CATEGORIES = ("airgap_env_keys", "retired_env_keys", "task_names", "chart_values",
              "script_flags", "gap_paths", "bundle_layout")
REFRESH = ".venv/bin/python scripts/extension_surface.py --write"
ADDED = "extension surface: {category} `{item}` was added; refresh the snapshot: " + REFRESH
REMOVED = ("extension surface: {category} `{item}` was removed or renamed without a migration "
           "and a release note; keep the old name working or refuse it with a fixed message "
           "naming the new one, add a `- removed` line to docs/release-notes.md, then refresh "
           "the snapshot (ADR-0009 §9): " + REFRESH)
UNREFRESHED = ("extension surface: {category} `{item}` was removed with its migration and release "
               "note; refresh the snapshot: " + REFRESH)
BROKEN = ("extension surface: the release note for {category} `{item}` names a migration the "
          "code does not provide: {reason}")
NOTE = re.compile(r"^- removed `(?P<category>[a-z_]+):(?P<item>[^`]+)`"
                  r"(?:; replaced by `(?P<replacement>[^`]+)`)?"
                  r"; migration: (?P<kind>retired-key|alias|refusal) in `(?P<evidence>[^`]+)`\s*$",
                  re.MULTILINE)

# An input that selects every optional block of the values mapping (route,
# Open WebUI with IdP CA, feedback, bundled tracing and metrics, ingest Job with
# every option, reasoning and request overrides, sizing, digests).
_DIGEST = "sha256:" + "0" * 64
MAXIMAL_INPUT = {
    "INTERNAL_REGISTRY": "registry.example.invalid", "NAMESPACE": "surface", "QDRANT_RELEASE": "qdrant",
    "IMAGE_SHA": "0" * 40, "EMBED_BASE_URL": "http://embed.example.invalid/v1", "EMBED_MODEL": "embed",
    "EMBED_MODEL_REVISION": "rev", "DENSE_DIM": "8", "STORAGE_CLASS": "block", "PULL_SECRET": "pull",
    "AGENT_ROUTE": "true", "UI_ENABLED": "true", "METRICS_ENABLED": "true", "SERVICEMONITOR_ENABLED": "true",
    "OTEL_DEPLOYMENT_ENVIRONMENT": "env", "OTEL_SERVICE_NAME": "svc", "OPENOBSERVE_RUN_AS_ID": "1000",
    "AGENT_NETWORK_POLICY": "true", "AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES": "monitoring",
    "FEEDBACK_ENABLED": "true", "FEEDBACK_SECRET": "feedback",
    "OPENWEBUI_ENABLED": "true", "OPENWEBUI_SECRET": "owui", "OPENWEBUI_HOST": "docs.example.invalid",
    "OPENWEBUI_OIDC_PROVIDER_URL": "https://idp.example.invalid/.well-known/openid-configuration",
    "OPENWEBUI_OIDC_CLIENT_ID": "client", "OPENWEBUI_OIDC_ALLOWED_ROLES": "users",
    "OPENWEBUI_OIDC_ADMIN_ROLES": "admins", "OPENWEBUI_IDP_EGRESS_CIDRS": "192.0.2.0/24",
    "LLM_BASE_URL": "http://llm.example.invalid/v1", "LLM_MODEL_REASONING": "reasoner",
    "CHAT_CONDENSE_ENABLED": "true", "LLM_MAX_MODEL_LEN": "4096", "LLM_RESERVED_OUTPUT_TOKENS": "256",
    "LLM_THINKING_RESERVE_TOKENS_SIMPLE": "0", "LLM_THINKING_RESERVE_TOKENS_COMPLEX": "0",
    "LLM_REASONING_EFFORT_SIMPLE": "medium", "LLM_REASONING_EFFORT_COMPLEX": "medium",
    "LLM_REASONING_EFFORTS": "low,medium", "LLM_STREAM": "true",
    "REQUEST_DEADLINE_S": "200", "REQUEST_MAX_CONCURRENT": "4", "REQUEST_QUEUE_MAX": "4",
    "REQUEST_QUEUE_WAIT_S": "30", "RERANK_ENABLED": "true", "RERANK_BASE_URL": "http://rerank.example.invalid/v1",
    "GATEWAY_API_KEY_SECRET": "gw", "GATEWAY_API_KEY_SECRET_KEY": "key", "GATEWAY_CA_CONFIGMAP": "ca",
    "CORPUS_PVC": "corpus", "INGEST_WORKERS": "2", "INGEST_WORK_SIZE": "10Gi", "INGEST_REINGEST": "true",
    "INGEST_RETIRE_DOCS": "doc", "INGEST_AUTHORIZE_FROM_CATALOG": "true", "CONTEXTUAL_EMBED_ENABLED": "true",
    "CONTEXT_LLM_BASE_URL": "http://context.example.invalid/v1", "CONTEXT_LLM_MODEL": "context",
    "QDRANT_PEER_URLS": "http://peer.example.invalid:6333", "QDRANT_SHARD_NUMBER": "1",
    "QDRANT_REPLICATION_FACTOR": "1", "QDRANT_WRITE_CONSISTENCY_FACTOR": "1",
    "QDRANT_DATA_SIZE": "1Gi", "QDRANT_SNAPSHOT_SIZE": "1Gi",
    **{f"{prefix}_{res}_{kind}": ("1" if res == "CPU" else "1Gi")
       for prefix in ("QDRANT", "INGEST") for res in ("CPU", "MEMORY") for kind in ("REQUEST", "LIMIT")},
    **{f"IMAGE_DIGEST_{role}": _DIGEST
       for role in ("AGENT", "INGEST", "OAUTH_PROXY", "OPENWEBUI", "OPENWEBUI_PROXY")},
}


def retired_keys(root: Path) -> dict[str, str]:
    """RETIRED_KEYS of the tree at `root` ({key: migration text}), read without importing it."""
    tree = ast.parse((root / MODEL_CONFIG).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(isinstance(t, ast.Name) and t.id == "RETIRED_KEYS" for t in targets):
                assert node.value is not None
                return {key: text for key, _ref, text in ast.literal_eval(node.value)}
    raise ValueError("model_config.py defines no RETIRED_KEYS")


def env_keys(root: Path) -> list[str]:
    text = (root / EXAMPLE).read_text(encoding="utf-8")
    return sorted(set(re.findall(r"(?m)^#?[ \t]*([A-Z][A-Z0-9_]*)=", text)))


def task_names(root: Path) -> list[str]:
    names: list[str] = []

    def add(prefix: str, document: dict) -> None:
        for name, task in (document.get("tasks") or {}).items():
            if isinstance(task, dict) and task.get("desc") and not task.get("internal"):
                names.extend(prefix + alias for alias in (name, *(task.get("aliases") or ())))

    taskfile = yaml.safe_load((root / "Taskfile.yml").read_text(encoding="utf-8"))
    add("", taskfile)
    for namespace, include in (taskfile.get("includes") or {}).items():
        path = include["taskfile"] if isinstance(include, dict) else include
        add(namespace + ":", yaml.safe_load((root / path).read_text(encoding="utf-8")))
    return sorted(set(names))


def _leaf_paths(node: object, prefix: str) -> list[str]:
    if isinstance(node, dict) and node:
        return [path for key, value in node.items() for path in _leaf_paths(value, f"{prefix}.{key}")]
    return [prefix]


def chart_values(root: Path) -> list[str]:
    import certifi  # A real public CA certificate, so the optional IdP CA block is selected.

    bundle = Path(certifi.where()).read_text(encoding="utf-8")
    end = "-----END CERTIFICATE-----\n"
    certificate = bundle[bundle.index("-----BEGIN CERTIFICATE-----"):bundle.index(end) + len(end)]
    with tempfile.TemporaryDirectory() as scratch:
        idp_ca = Path(scratch) / "idp-ca.pem"
        idp_ca.write_text(certificate, encoding="utf-8")
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(root),
                       "ROUTE_DESTINATION_CA_FILE": str(root / ROUTE_CA),
                       "OPENWEBUI_IDP_CA_FILE": str(idp_ca)}
        # A key the tree retires refuses: leave it out, as an operator would.
        retired = retired_keys(root)
        environment.update({key: value for key, value in MAXIMAL_INPUT.items() if key not in retired})
        out = Path(scratch) / "values.yaml"
        mapped = subprocess.run([sys.executable, "-m", "scripts.airgap.map_values", "--out", str(out)],
                                cwd=root, env=environment, capture_output=True, text=True, check=False)
        if mapped.returncode != 0:
            raise ValueError("map_values.py refused the maximal input: " + mapped.stderr.strip()[-400:])
        values = yaml.safe_load(out.read_text(encoding="utf-8"))
        sizing = subprocess.run([sys.executable, "-m", "scripts.airgap.model_config", "--qdrant-sizing"],
                                cwd=root, env=environment, capture_output=True, text=True, check=False)
        if sizing.returncode != 0:
            raise ValueError("model_config.py --qdrant-sizing refused the maximal input")
    app = [path[1:] for path in _leaf_paths(values, "")]
    deploy = (root / DEPLOY).read_text(encoding="utf-8")
    qdrant = set(re.findall(r'--set-string "([A-Za-z][A-Za-z0-9_.]*)=', deploy))
    qdrant |= {line.split("=", 1)[0] for line in sizing.stdout.split()}
    return sorted([f"mainframe-rag:{path}" for path in app] + [f"qdrant:{path}" for path in qdrant])


def script_flags(root: Path) -> list[str]:
    tasks = (root / AIRGAP_TASKS).read_text(encoding="utf-8")
    items: list[str] = []
    for script in sorted(set(re.findall(r"sh scripts/airgap/([a-z_]+\.sh)", tasks))):
        text = (root / "scripts/airgap" / script).read_text(encoding="utf-8")
        loop = re.search(r'(?ms)^(?:for \w+ in "\$@"|while \[ "\$#" -gt 0 \]); do$(.*?)^done$', text)
        flags: set[str] = set()
        for label in re.findall(r"(?m)^\s*(-[-A-Za-z0-9|]*)\)", loop.group(1) if loop else ""):
            flags.update(part for part in label.split("|") if part.startswith("-"))
        items.extend(f"{script} {flag}" for flag in sorted(flags))
    adr = (root / ADR).read_text(encoding="utf-8")
    procedure = adr[adr.index("## 4. Release import procedure"):adr.index("<!-- import-script:start -->")]
    items.extend(f"import-script {name}" for name in re.findall(r"(?m)^export ([A-Z_]+)=", procedure))
    return sorted(items)


def gap_paths(root: Path) -> list[str]:
    adr = (root / ADR).read_text(encoding="utf-8")
    section = adr[adr.index("## 3. Site layer"):adr.index("## 4. Release import procedure")]
    paths: set[str] = set()
    for first_cell in re.findall(r"(?m)^\| ([^|]*`[^|]*) \|", section):
        paths.update(re.findall(r"`([^`]+)`", first_cell))
    return sorted(paths)


def bundle_layout(root: Path) -> list[str]:
    text = (root / PACK).read_text(encoding="utf-8")
    joined = text.replace("\\\n", " ")
    assignments = dict(re.findall(r'(?m)^\s*([A-Z_]+)="([^"$]+)"', text))

    def name(token: str) -> str:
        token = token.strip('"')
        whole = re.fullmatch(r"\$\{?([A-Z_]+)\}?", token)
        if whole and whole[1] in assignments:
            return assignments[whole[1]]
        return re.sub(r'"?\$\{?([A-Z_]+)\}?"?', r"{\1}", token)

    command = re.search(r'(?m)^tar -C "\$DIST" -cf "\$OUT_TARBALL" (.*)$', joined)
    tarball = re.search(r'(?m)^OUT_TARBALL="\$DIST/([^"]+)"', text)
    manifest = re.search(r'(?ms)^echo "==> MANIFEST"$(.*?)^\} > "\$DIST/MANIFEST.txt"$', text)
    if not command or not tarball or not manifest:
        raise ValueError("pack.sh no longer has the tar command, OUT_TARBALL or the MANIFEST block")
    outer = name(tarball[1])
    items = [f"tarball {outer}", f"tarball {outer}.sha256"]
    items += [f"member {name(token)}" for token in command[1].split()]
    items += [f"manifest {field}" for field in re.findall(r'echo "([a-z0-9_]+): ', manifest[1])]
    return sorted(set(items))


def derive(root: Path = ROOT) -> dict[str, list[str]]:
    return {
        "airgap_env_keys": env_keys(root),
        "retired_env_keys": sorted(retired_keys(root)),
        "task_names": task_names(root),
        "chart_values": chart_values(root),
        "script_flags": script_flags(root),
        "gap_paths": gap_paths(root),
        "bundle_layout": bundle_layout(root),
    }


def _name(category: str, item: str) -> str:
    """The name inside an item that code spells: `pipeline.sh --dry-run` -> `--dry-run`."""
    if category in ("script_flags", "bundle_layout"):
        return item.split(" ", 1)[1]
    if category == "chart_values":
        return item.split(":", 1)[1]
    return item


def migration_problem(root: Path, note: re.Match[str]) -> str | None:
    """Why the code does not provide the migration a release note names, or None."""
    item, replacement = _name(note["category"], note["item"]), note["replacement"]
    evidence = root / note["evidence"]
    if not evidence.is_file() or not evidence.resolve().is_relative_to(root.resolve()):
        return f"`{note['evidence']}` is not a file in the repository"
    if note["kind"] == "retired-key":
        retired = retired_keys(root)
        if note["category"] != "airgap_env_keys" or note["evidence"] != MODEL_CONFIG.as_posix():
            return f"a retired-key refusal is an airgap_env_keys entry in `{MODEL_CONFIG}`"
        if item not in retired:
            return f"`{item}` is not in RETIRED_KEYS"
        if replacement and replacement not in retired[item]:
            return f"the RETIRED_KEYS message for `{item}` does not name `{replacement}`"
        return None
    text = evidence.read_text(encoding="utf-8")
    missing = [name for name in (item, replacement) if name and name not in text]
    if missing:
        return f"`{note['evidence']}` does not name " + ", ".join(f"`{name}`" for name in missing)
    return None


def release_notes(root: Path) -> dict[tuple[str, str], re.Match[str]]:
    path = root / RELEASE_NOTES
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {(note["category"], note["item"]): note for note in NOTE.finditer(text)}


def load_snapshot(root: Path) -> dict[str, dict[str, list[str]]]:
    path = root / SNAPSHOT
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"current": {}, "removed": {}}
    return {part: {category: data[part].get(category, []) for category in CATEGORIES}
            for part in ("current", "removed")}


def _problems(root: Path, current: dict[str, list[str]]) -> list[tuple[bool, str]]:
    """(blocks a refresh, fixed message) for each difference between the code and the snapshot."""
    snapshot = load_snapshot(root)
    notes = release_notes(root)
    problems: list[tuple[bool, str]] = []
    for category in CATEGORIES:
        now, before = set(current[category]), set(snapshot["current"][category])
        problems += [(False, ADDED.format(category=category, item=item)) for item in sorted(now - before)]
        for item in sorted(before - now):
            note = notes.get((category, item))
            if note is None or migration_problem(root, note) is not None:
                problems.append((True, REMOVED.format(category=category, item=item)))
            else:
                problems.append((False, UNREFRESHED.format(category=category, item=item)))
        for item in sorted(set(snapshot["removed"][category]) - now):
            note = notes.get((category, item))
            reason = "no `- removed` line" if note is None else migration_problem(root, note)
            if reason:
                problems.append((True, BROKEN.format(category=category, item=item, reason=reason)))
    return problems


def compare(root: Path = ROOT) -> list[str]:
    """Fixed-message problems between the code and the snapshot; empty means equal."""
    return [message for _blocking, message in _problems(root, derive(root))]


def write(root: Path = ROOT) -> list[str]:
    """Refresh the snapshot; refuse (and write nothing) while a removal lacks its migration and note."""
    current = derive(root)
    blocking = [message for blocks, message in _problems(root, current) if blocks]
    if blocking:
        return blocking
    snapshot = load_snapshot(root)
    removed = {category: sorted((set(snapshot["removed"][category]) | set(snapshot["current"][category]))
                                - set(current[category])) for category in CATEGORIES}
    document = {
        "_about": ("The upstream/gap extension surface (ADR-0009 section 9), derived by "
                   "scripts/extension_surface.py. Do not edit by hand; refresh: " + REFRESH),
        "current": current,
        "removed": removed,
    }
    (root / SNAPSHOT).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="refresh the committed snapshot")
    args = parser.parse_args(argv)
    problems = write() if args.write else compare()
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems and args.write:
        print(f"wrote {SNAPSHOT}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
