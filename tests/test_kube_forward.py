"""Supervised loopback port-forward (#751): hermetic, fake kubectl, no cluster."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/kube_forward.py"

FAKE_KUBECTL = r'''
import http.server, os, pathlib, sys, threading, time
local = int(sys.argv[-1].split(":")[0])
state = pathlib.Path(os.environ["FAKE_FORWARD_STATE"])
first = not (state / "started-once").exists()
(state / "started-once").touch()
with (state / "launches").open("a") as log:
    log.write(f"{os.getpid()} {' '.join(sys.argv[1:])}\n")
mode = os.environ.get("FAKE_FORWARD_MODE", "steady")
if mode == "never_ready":
    time.sleep(0.3); sys.exit(1)
healthy = {"ok": True}
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if healthy["ok"] and self.path == "/readyz" else 503); self.end_headers()
    def log_message(self, *a): pass
server = http.server.ThreadingHTTPServer(("127.0.0.1", local), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
if first and mode == "exit_once":
    time.sleep(0.6); sys.exit(1)
if first and mode == "hang_once":
    time.sleep(0.6); healthy["ok"] = False
while True:
    time.sleep(0.1)
'''


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=0.5) as response:
            return response.status == 200
    except OSError:
        return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def forward(tmp_path):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\n" + FAKE_KUBECTL)
    kubectl.chmod(0o755)
    started: list[subprocess.Popen] = []

    def start(mode: str, port: int | None = None, *extra: str) -> tuple[subprocess.Popen, int]:
        port = port or _free_port()
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--kubectl", str(kubectl), "--kubeconfig", "/private/kubeconfig",
             "--context", "kind-rag-kind", "--namespace", "rag-colab-706-4096", "--service", "qdrant",
             "--local-port", str(port), "--remote-port", "6333", "--probe-interval", "0.2",
             "--probe-timeout", "0.3", "--probe-failures", "2", "--start-timeout", "3",
             "--backoff-min", "0.2", "--backoff-max", "0.4", *extra],
            env={**os.environ, "FAKE_FORWARD_STATE": str(tmp_path), "FAKE_FORWARD_MODE": mode},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        started.append(proc)
        return proc, port

    yield start, tmp_path
    for proc in started:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def _launches(root: Path) -> list[tuple[int, str]]:
    path = root / "launches"
    if not path.exists():
        return []
    return [(int(pid), argv) for pid, argv in (line.split(" ", 1) for line in path.read_text().splitlines())]


def _wait(predicate, seconds: float = 10.0) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


@pytest.mark.parametrize("mode", ["exit_once", "hang_once"])
def test_forward_restarts_after_exit_or_hang_and_recovers_then_stops_cleanly(forward, mode):
    start, root = forward
    proc, port = start(mode)
    _wait(lambda: len(_launches(root)) >= 2 and _ready(port))
    first_pid, argv = _launches(root)[0]
    assert argv.split() == ["--kubeconfig", "/private/kubeconfig", "--context", "kind-rag-kind",
                            "-n", "rag-colab-706-4096", "port-forward", "--address", "127.0.0.1",
                            "service/qdrant", f"{port}:6333"]
    _wait(lambda: not _alive(first_pid))  # a hung forward is replaced, not leaked
    proc.send_signal(signal.SIGTERM)
    output, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0, output
    reason = "child exit" if mode == "exit_once" else "2 failed readiness probes"
    assert f"restart 1 after {reason}" in output and "kube-forward: stopped" in output
    assert all(not _alive(pid) for pid, _ in _launches(root))
    assert not _ready(port)


def test_never_ready_forward_keeps_bounded_backoff_until_stopped(forward):
    start, root = forward
    proc, _ = start("never_ready")
    _wait(lambda: len(_launches(root)) >= 4)  # three completed failures logged their backoff
    proc.send_signal(signal.SIGINT)
    output, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0, output
    waits = [float(line.rsplit(" in ", 1)[1].rstrip("s")) for line in output.splitlines() if "next attempt in" in line]
    assert waits[:3] == [0.2, 0.4, 0.4]  # doubled, then capped at --backoff-max
    assert all(not _alive(pid) for pid, _ in _launches(root))


def test_occupied_local_port_is_refused_before_spawning(forward):
    start, root = forward
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        proc, _ = start("steady", holder.getsockname()[1])
        output, _ = proc.communicate(timeout=10)
    assert proc.returncode == 2 and "already in use" in output
    assert _launches(root) == []


@pytest.mark.parametrize("bad", [
    ["--namespace", "Bad_NS"], ["--service", "qdrant;rm"], ["--local-port", "80"],
    ["--remote-port", "70000"], ["--probe-path", "readyz"], ["--backoff-min", "0"],
])
def test_invalid_inputs_refuse_before_spawning(forward, bad):
    start, root = forward
    proc, _ = start("steady", None, *bad)
    proc.communicate(timeout=10)
    assert proc.returncode == 2
    assert _launches(root) == []
