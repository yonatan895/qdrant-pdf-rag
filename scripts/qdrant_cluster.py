#!/usr/bin/env python3
"""Disposable multi-peer Qdrant fixture (issue #360).

Starts the pinned Qdrant image as a three-peer cluster on loopback with
separate storage, for the placement/node-loss acceptance exercises. This is
a local, disposable fixture: the production tuple is selected by the caller
(6/3/2); the fixture never changes any repository default. Real production
placement is three independent workers; three containers on one host prove
distributed software behavior only.

Lifecycle is owned here (start/stop/wait); the same functions back the
integration test and the operator CLI:

    python3 scripts/qdrant_cluster.py up
    python3 scripts/qdrant_cluster.py down --prefix qdrant-ha-1234
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx2

HTTP_PORT = 6333
GRPC_PORT = 6334
P2P_PORT = 6335
DEFAULT_PEERS = 3
DEFAULT_PREFIX = "qdrant-ha"
DEFAULT_BASE_PORT = 7433
READY_TIMEOUT_S = 120.0


class QdrantClusterError(RuntimeError):
    """The cluster fixture could not be started, reached, or torn down."""


@dataclass(frozen=True)
class QdrantCluster:
    prefix: str
    network: str
    urls: tuple[str, ...]

    def stop(self) -> None:
        for index in range(1, len(self.urls) + 1):
            _run(["docker", "rm", "-f", "-v", f"{self.prefix}-{index}"], check=False)
        _run(["docker", "network", "rm", self.network], check=False)


def _run(cmd: list[str], *, timeout: float = 300.0, check: bool = True):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode != 0:
        raise QdrantClusterError(
            f"{' '.join(cmd)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[:400]}"
        )
    return result


def qdrant_image(repo_root: Path) -> str:
    """The images.txt pin — the same image every other Qdrant lane uses."""
    try:
        from scripts.qdrant_pin import qdrant_image_pin
    except ImportError:  # script context: scripts/ itself is on sys.path
        from qdrant_pin import qdrant_image_pin

    return qdrant_image_pin(repo_root / "images.txt")


def require_docker(image: str) -> None:
    if shutil.which("docker") is None:
        raise QdrantClusterError("docker is required for the multi-peer fixture")
    if _run(["docker", "image", "inspect", image], check=False).returncode == 0:
        return
    # Same implicit behavior as the single-node sim (docker run would pull);
    # in the air gap there is no network, so absence fails with a clear path.
    pulled = _run(["docker", "pull", image], check=False, timeout=900.0)
    if pulled.returncode != 0:
        raise QdrantClusterError(
            f"pinned image {image} is not present and could not be pulled "
            f"({(pulled.stderr or pulled.stdout).strip()[:200]}); fetch it on a "
            "connected host first (for example: sh scripts/tools/run-task.sh "
            "local:qdrant:up) and re-run"
        )


def free_port(start: int) -> int:
    for port in range(start, start + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise QdrantClusterError(f"no free loopback port in [{start}, {start + 200})")


def cluster_peer_count(url: str, *, timeout: float = 5.0) -> int:
    response = httpx2.get(f"{url.rstrip('/')}/cluster", timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("result", {}).get("status") != "enabled":
        return 0
    return len(payload["result"].get("peers") or {})


def wait_cluster_ready(urls: tuple[str, ...], *, timeout_s: float = READY_TIMEOUT_S) -> None:
    """Every published endpoint answers and the consensus sees all peers."""
    deadline = time.monotonic() + timeout_s
    last = "no attempt"
    while time.monotonic() < deadline:
        counts = []
        answers = True
        for url in urls:
            try:
                counts.append(cluster_peer_count(url, timeout=3.0))
            except Exception as exc:  # noqa: BLE001 - retried until the deadline
                answers = False
                last = f"{url}: {type(exc).__name__}: {exc}"
                break
        if answers and counts and all(count == len(urls) for count in counts):
            return
        if answers:
            last = f"cluster peer counts {counts}, expected {len(urls)}"
        time.sleep(1.0)
    raise QdrantClusterError(
        f"cluster not ready within {timeout_s:.0f}s (last: {last})"
    )


def start_cluster(
    repo_root: Path,
    *,
    prefix: str | None = None,
    base_port: int | None = None,
    peers: int = DEFAULT_PEERS,
    timeout_s: float = READY_TIMEOUT_S,
) -> QdrantCluster:
    """Start `peers` cluster-mode containers with separate anonymous storage.

    The first peer supplies its own URI; the rest bootstrap from it. Only the
    HTTP API is published (loopback); replication traffic stays on the docker
    network. Restarting a stopped container keeps its storage; `stop()`
    removes containers and their anonymous volumes.
    """
    if peers < 1:
        raise QdrantClusterError(f"peers must be >= 1, got {peers}")
    image = qdrant_image(repo_root)
    require_docker(image)
    prefix = prefix or f"{DEFAULT_PREFIX}-{int(time.time())}"
    base = base_port if base_port is not None else free_port(DEFAULT_BASE_PORT)
    network = f"{prefix}-net"
    _run(["docker", "network", "create", network])
    urls: list[str] = []
    try:
        for index in range(1, peers + 1):
            name = f"{prefix}-{index}"
            host_port = base + (index - 1) * 10
            command = [
                "docker", "run", "-d", "--name", name,
                "--network", network,
                "-p", f"127.0.0.1:{host_port}:{HTTP_PORT}",
                "-e", "QDRANT__CLUSTER__ENABLED=true",
                "-e", f"QDRANT__CLUSTER__P2P__PORT={P2P_PORT}",
                "-e", f"QDRANT__SERVICE__HTTP_PORT={HTTP_PORT}",
                "-e", f"QDRANT__SERVICE__GRPC_PORT={GRPC_PORT}",
                "--entrypoint", "/qdrant/qdrant",
                image,
            ]
            if index == 1:
                command += ["--uri", f"http://{name}:{P2P_PORT}"]
            else:
                command += ["--bootstrap", f"http://{prefix}-1:{P2P_PORT}"]
            _run(command)
            urls.append(f"http://127.0.0.1:{host_port}")
        wait_cluster_ready(tuple(urls), timeout_s=timeout_s)
    except Exception:
        QdrantCluster(prefix=prefix, network=network, urls=tuple(urls)).stop()
        raise
    return QdrantCluster(prefix=prefix, network=network, urls=tuple(urls))


def stop_cluster(prefix: str, peers: int = DEFAULT_PEERS) -> None:
    QdrantCluster(
        prefix=prefix,
        network=f"{prefix}-net",
        urls=tuple(f"http://127.0.0.1:{DEFAULT_BASE_PORT + i * 10}" for i in range(peers)),
    ).stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="start the disposable three-peer cluster")
    up.add_argument("--prefix", default=None)
    up.add_argument("--base-port", type=int, default=None)
    down = sub.add_parser("down", help="remove the fixture's containers and network")
    down.add_argument("--prefix", required=True)
    down.add_argument("--peers", type=int, default=DEFAULT_PEERS)
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        if args.command == "up":
            cluster = start_cluster(
                repo_root, prefix=args.prefix, base_port=args.base_port
            )
            export = " ".join(cluster.urls)
            print(f"prefix={cluster.prefix}")
            print(f"peer_urls={export}")
            print(
                "select the production tuple explicitly, e.g.: "
                f"QDRANT_SHARD_NUMBER=6 QDRANT_REPLICATION_FACTOR=3 "
                f"QDRANT_WRITE_CONSISTENCY_FACTOR=2 "
                f"QDRANT_URL={cluster.urls[0]} python3 scripts/verify_placement.py "
                "--production "
                + " ".join(f"--peer-url {url}" for url in cluster.urls)
            )
        else:
            stop_cluster(args.prefix, peers=args.peers)
            print(f"removed {args.prefix}")
    except QdrantClusterError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
