#!/usr/bin/env python3
"""Shared Qdrant simulation-server lifecycle (docker, the images.txt pin).

Single owner for: resolving the pinned image, starting it on an ephemeral
loopback port, waiting for readiness, and cleanup. Used by the pytest
simulation fixture (tests/test_integration_sim.py) and by the benchmark
harness (scripts/benchmark.py) — two consumers, one implementation.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

READY_TIMEOUT_S = 60.0
QDRANT_PORT = 6333


class QdrantSimError(RuntimeError):
    """The simulation server could not be started or did not become ready."""


@dataclass
class QdrantSim:
    url: str
    container_id: str | None  # None when reusing an operator-provided server

    def stop(self) -> None:
        if self.container_id:
            subprocess.run(
                ["docker", "stop", self.container_id],
                capture_output=True, text=True, timeout=30, check=False,
            )


def _run(cmd: list[str], timeout: float = 300.0) -> subprocess.CompletedProcess:
    # Return codes are checked by each caller — docker failures raise below.
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def qdrant_image_pin(images_txt: Path) -> str:
    """Delegate to the single images.txt pin parser (scripts/qdrant_pin.py)."""
    try:
        from scripts.qdrant_pin import qdrant_image_pin as _pin
    except ImportError:  # script context: scripts/ itself is on sys.path
        from qdrant_pin import qdrant_image_pin as _pin
    try:
        return _pin(images_txt)
    except ValueError as exc:
        raise QdrantSimError(str(exc)) from exc


def wait_ready(url: str, timeout_s: float = READY_TIMEOUT_S) -> None:
    """Same readiness predicate as agent /healthz — one rule, not two."""
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url.rstrip('/')}/readyz", timeout=2.0)
            if r.status_code == 200 and r.text.strip().lower() == "all shards are ready":
                return
            last = f"/readyz {r.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)[:120]
        time.sleep(0.5)
    raise QdrantSimError(f"Qdrant at {url} not ready within {timeout_s:.0f}s (last: {last})")


def start_simulator(repo_root: Path, external_url: str | None = None) -> QdrantSim:
    """external_url (e.g. QDRANT_SIM_URL) reuses a running server; otherwise
    run the pinned image on an ephemeral loopback port. Raises QdrantSimError
    with an operator-readable reason when docker or the image is unavailable."""
    if external_url:
        url = external_url.rstrip("/")
        try:
            wait_ready(url)
        except QdrantSimError as exc:
            raise QdrantSimError(f"QDRANT_SIM_URL={external_url}: {exc}") from exc
        return QdrantSim(url=url, container_id=None)

    if shutil.which("docker") is None:
        raise QdrantSimError("docker CLI not found")
    info = _run(["docker", "info"], timeout=15)
    if info.returncode != 0:
        raise QdrantSimError("docker daemon not reachable")

    image = qdrant_image_pin(repo_root / "images.txt")
    if not _run(["docker", "images", "-q", image], timeout=30).stdout.strip():
        pull = _run(["docker", "pull", image])
        if pull.returncode != 0:
            raise QdrantSimError(f"could not pull {image}: {pull.stderr.strip()[:200]}")
    run = _run(["docker", "run", "-d", "--rm", "-p", f"127.0.0.1::{QDRANT_PORT}", image])
    if run.returncode != 0:
        raise QdrantSimError(f"could not start the qdrant container: {run.stderr.strip()[:200]}")
    cid = run.stdout.strip()
    deadline = time.monotonic() + 15
    port = ""
    while time.monotonic() < deadline:
        out = _run(["docker", "port", cid, f"{QDRANT_PORT}/tcp"], timeout=15).stdout.strip()
        if out:
            port = out.splitlines()[0].rsplit(":", 1)[1]
            break
        time.sleep(0.3)
    if not port:
        subprocess.run(["docker", "stop", cid], capture_output=True, timeout=30, check=False)
        raise QdrantSimError("container did not publish a host port in time")
    url = f"http://127.0.0.1:{port}"
    try:
        wait_ready(url)
    except QdrantSimError:
        subprocess.run(["docker", "stop", cid], capture_output=True, timeout=30, check=False)
        raise
    return QdrantSim(url=url, container_id=cid)
