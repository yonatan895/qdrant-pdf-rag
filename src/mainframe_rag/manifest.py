"""Run manifest logging for eval and bench iterations.

Appends a structured JSON manifest line for every eval/bench execution:
{timestamp, git_sha, settings_hash, model_ids, qdrant_version,
collection_snapshot_id, metrics} to evals/runs/{run_type}_runs.jsonl.
This is what makes iterations comparable across runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx2

from mainframe_rag.config import Settings
from mainframe_rag.ingest.context import CONTEXT_PROMPT_VERSION


def get_git_sha(repo_root: Path | None = None) -> str:
    """Returns full git SHA from git rev-parse HEAD or environment variables."""
    for env_var in ("GITHUB_SHA", "IMAGE_SHA", "GIT_SHA"):
        val = os.environ.get(env_var, "").strip()
        if len(val) == 40:
            return val
    try:
        cmd = ["git", "rev-parse", "HEAD"]
        cwd = str(repo_root) if repo_root else None
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=cwd)
        return res.stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def compute_settings_hash(settings: Settings) -> str:
    """Computes stable sha256 hex digest of settings model dump."""
    dumped = settings.model_dump_json()
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def get_qdrant_version(qdrant_url: str, timeout_s: float = 3.0) -> str:
    """Queries Qdrant server version via REST API root endpoint.

    Returns "unknown" when Qdrant is unreachable — never the pinned server
    version, which would forge comparability between run manifests."""
    try:
        resp = httpx2.get(qdrant_url.rstrip("/") + "/", timeout=timeout_s)
        if resp.status_code == 200:
            data = resp.json()
            return str(data.get("version") or "unknown")
    except Exception:  # noqa: BLE001, S110
        pass
    return "unknown"


def get_collection_snapshot_id(
    qdrant_url: str, collection: str, timeout_s: float = 3.0
) -> str | None:
    """Queries snapshots for collection, falling back to points count."""
    base = qdrant_url.rstrip("/")
    try:
        resp = httpx2.get(f"{base}/collections/{collection}/snapshots", timeout=timeout_s)
        if resp.status_code == 200:
            snaps = resp.json().get("result", [])
            if snaps and isinstance(snaps, list):
                return str(snaps[0].get("name") or snaps[0])
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        resp = httpx2.get(f"{base}/collections/{collection}", timeout=timeout_s)
        if resp.status_code == 200:
            pts = resp.json().get("result", {}).get("points_count")
            if pts is not None:
                return f"{collection}:{pts}"
    except Exception:  # noqa: BLE001, S110
        pass
    return None


def write_run_manifest(
    run_type: str,
    settings: Settings,
    metrics: dict[str, Any],
    runs_dir: Path | None = None,
) -> dict[str, Any]:
    """Builds and appends a run manifest line to evals/runs/{run_type}_runs.jsonl."""
    if runs_dir is None:
        runs_dir = Path("evals/runs")
    runs_dir.mkdir(parents=True, exist_ok=True)

    git_sha = get_git_sha()
    settings_hash = compute_settings_hash(settings)
    model_ids = {
        "embed": settings.embed_model,
        "llm_reasoning": settings.llm_model_reasoning,
        "llm_context": settings.context_llm_model,
        "context_prompt": CONTEXT_PROMPT_VERSION if settings.contextual_embed_enabled else None,
    }
    qdrant_ver = get_qdrant_version(settings.qdrant_url)
    snapshot_id = get_collection_snapshot_id(settings.qdrant_url, settings.qdrant_collection)

    manifest: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_type": run_type,
        "git_sha": git_sha,
        "settings_hash": settings_hash,
        "model_ids": model_ids,
        "qdrant_version": qdrant_ver,
        "collection_snapshot_id": snapshot_id,
        "metrics": metrics,
    }

    out_file = runs_dir / f"{run_type}_runs.jsonl"
    with out_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(manifest, ensure_ascii=False) + "\n")

    return manifest
