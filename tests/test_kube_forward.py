"""Supervised loopback port-forward (#751, #762): hermetic, fake kubectl, no cluster."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import httpx
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
if mode == "stale_keepalive":
    # Like kubectl: once the pod side of an idle keep-alive stream is gone (0.5 s
    # here, Qdrant 5 s), the client is not told; its next request gets no response.
    import socket
    def serve(conn):
        buf, last = b"", time.monotonic()
        while (data := conn.recv(65536)) and time.monotonic() - last <= 0.5:
            buf += data
            while b"\r\n\r\n" in buf:
                head, buf = buf.split(b"\r\n\r\n", 1)
                if head.split(b" ")[1] == b"/slow":
                    time.sleep(1.0)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                last = time.monotonic()
        conn.close()
    listener = socket.create_server(("127.0.0.1", local))
    while True:
        threading.Thread(target=serve, args=(listener.accept()[0],), daemon=True).start()
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
            proc.terminate()  # lets the supervisor stop its child; a failed test must not leak kubectl
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
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
    *fixed, mapping = argv.split()
    assert fixed == ["--kubeconfig", "/private/kubeconfig", "--context", "kind-rag-kind",
                     "-n", "rag-colab-706-4096", "port-forward", "--address", "127.0.0.1", "service/qdrant"]
    forward_port, remote = mapping.split(":")
    assert remote == "6333" and int(forward_port) != port  # kubectl sits behind the relay (#762)
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
    ["--namespace", "Bad_NS"], ["--service", "qdrant;rm"], ["--local-port", "80"], ["--idle-close", "0"],
])
def test_invalid_inputs_refuse_before_spawning(forward, bad):
    start, root = forward
    proc, _ = start("steady", None, *bad)
    proc.communicate(timeout=10)
    assert proc.returncode == 2
    assert _launches(root) == []


def _reuse_after_idle(port: int, idle: float, path: str = "/readyz") -> httpx.Response:
    # One pooled connection, as qdrant-client's httpx pool would reuse it.
    limits = httpx.Limits(max_connections=1, keepalive_expiry=60)
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", limits=limits, timeout=5) as client:
        client.get(path).raise_for_status()
        time.sleep(idle)
        return client.get(path)


def test_relay_replaces_an_idle_stream_so_reuse_never_hits_a_dead_one(forward):
    start, _ = forward
    proc, port = start("stale_keepalive", None, "--idle-close", "5")
    _wait(lambda: _ready(port))
    with pytest.raises(httpx.RemoteProtocolError):  # control: the fake reproduces the #762 failure
        _reuse_after_idle(port, 0.8)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=10)

    proc, port = start("stale_keepalive", None, "--idle-close", "0.3")
    _wait(lambda: _ready(port))
    assert _reuse_after_idle(port, 0.8).status_code == 200
    proc.send_signal(signal.SIGTERM)
    output, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0 and "restart" not in output, output  # no kubectl restart was needed


def test_relay_never_closes_a_connection_awaiting_its_response(forward):
    start, _ = forward
    proc, port = start("stale_keepalive", None, "--idle-close", "0.3")
    _wait(lambda: _ready(port))
    response = _reuse_after_idle(port, 0.1, "/slow")  # each response takes 1.0 s, beyond --idle-close
    assert response.status_code == 200 and response.text == "ok"
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=10)


def test_restart_on_the_same_port_right_after_relay_closed_connections(forward):
    # Relay-initiated closes leave TIME_WAIT on the public port; a prompt restart must still bind it.
    start, _ = forward
    proc, port = start("stale_keepalive", None, "--idle-close", "0.3")
    _wait(lambda: _ready(port))
    assert _reuse_after_idle(port, 0.8).status_code == 200
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=10)
    proc, _ = start("stale_keepalive", port, "--idle-close", "0.3")
    _wait(lambda: _ready(port))
    proc.send_signal(signal.SIGTERM)
    output, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0 and "already in use" not in output, output


def test_client_connection_survives_the_server_idle_close_on_a_fresh_stream(forward):
    # The relay never closes the client for idleness, so there is no client-side close to race.
    start, root = forward
    proc, port = start("stale_keepalive", None, "--idle-close", "0.3")
    _wait(lambda: _ready(port))
    request = b"GET /readyz HTTP/1.1\r\nHost: x\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        for idle in (0.0, 0.8, 1.6):
            time.sleep(idle)
            client.sendall(request)
            assert client.recv(4096).endswith(b"\r\n\r\nok")
    proc.send_signal(signal.SIGTERM)
    output, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0 and "restart" not in output, output
    assert len(_launches(root)) == 1  # one kubectl; only its streams were replaced
