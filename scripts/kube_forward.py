#!/usr/bin/env python3
"""Supervised loopback `kubectl port-forward` to a namespaced Service (#751).

A bare port-forward exits on any pod-connection error and never returns, so a
sandbox stack loses its retained Qdrant mid-session. This owner binds
127.0.0.1 only, refuses an occupied port, restarts the forward with bounded
backoff when kubectl exits or its readiness probe keeps failing (a hung
forward), and stops its child on SIGINT/SIGTERM. The Service stays ClusterIP;
nothing is exposed beyond loopback. Output is fixed-format, never secrets.

Clients connect to a loopback relay, not to kubectl directly (#762). Through a
port-forward a client never sees the server close an idle keep-alive
connection (Qdrant/actix: 5 s), so reusing it near that moment sends a request
into a dead stream and can end kubectl. The relay keeps the client connection
and instead drops its forwarded stream once that stream has been idle
``--idle-close`` seconds after the server last sent data, below the server's
keep-alive. The client's next request opens a fresh stream, so no request is
ever sent into one the server may already have closed, and the client never
sees a close it could race. A stream waiting for a response (client spoke
last) is never dropped for idleness.
"""
from __future__ import annotations

import argparse
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
STABLE_SECONDS = 60.0
MAX_RELAYED = 64
CHUNK = 65536


def log(message: str) -> None:
    print(f"kube-forward: {message}", flush=True)


def port(value: str) -> int:
    number = int(value)
    if not 1024 <= number <= 65535:
        raise argparse.ArgumentTypeError("ports must be unprivileged (1024-65535)")
    return number


def name(value: str) -> str:
    if not NAME_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("namespace/service must be DNS-1123 labels")
    return value


def ephemeral_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def ready(local_port: int, path: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{local_port}{path}", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


class Relay:
    """Loopback TCP relay from the public local port to the current kubectl port."""

    def __init__(self, listener: socket.socket, idle_close: float) -> None:
        self.listener = listener
        self.idle_close = idle_close
        self.upstream_port = 0
        self.stopping = threading.Event()
        self.slots = threading.BoundedSemaphore(MAX_RELAYED)
        self.lock = threading.Lock()
        self.open: set[socket.socket] = set()
        self.accepter = threading.Thread(target=self.accept_loop, daemon=True)

    def connect(self) -> socket.socket:
        upstream = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=2)
        upstream.settimeout(None)  # only the connect is bounded; backpressured writes may take longer
        self.track(upstream, add=True)
        return upstream

    def drop(self, sock: socket.socket | None) -> None:
        if sock is not None:
            self.track(sock, add=False)
            sock.close()

    def start(self) -> None:
        self.listener.settimeout(0.2)
        self.accepter.start()

    def accept_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=self.pump, args=(client,), daemon=True).start()

    def track(self, *sockets: socket.socket, add: bool) -> None:
        with self.lock:
            for sock in sockets:
                (self.open.add if add else self.open.discard)(sock)

    def pump(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            self.track(client, add=True)
            # True while the client has spoken last, i.e. a request awaits its response.
            awaiting, last = False, time.monotonic()
            while not self.stopping.is_set():
                watched = [client] if upstream is None else [client, upstream]
                readable, _, _ = select.select(watched, [], [], 0.1)
                if not readable:
                    if upstream is not None and not awaiting and time.monotonic() - last >= self.idle_close:
                        self.drop(upstream)
                        upstream = None
                    continue
                for source in readable:
                    data = source.recv(CHUNK)
                    if not data:
                        return  # client done, or the stream ended (server or kubectl): close the client
                    if source is client:
                        if upstream is None:
                            upstream = self.connect()  # OSError while kubectl restarts closes the client
                        upstream.sendall(data)
                    else:
                        client.sendall(data)
                    awaiting, last = source is client, time.monotonic()
        except OSError:
            return
        finally:
            self.drop(upstream)
            self.drop(client)
            self.slots.release()

    def stop(self) -> None:
        self.stopping.set()
        self.listener.close()
        self.accepter.join(timeout=2)
        with self.lock:
            for sock in list(self.open):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class Supervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stopping = False
        self.child: subprocess.Popen | None = None
        self.relay: Relay | None = None

    def argv(self, forward_port: int) -> list[str]:
        command = [self.args.kubectl]
        if self.args.kubeconfig:
            command += ["--kubeconfig", self.args.kubeconfig]
        if self.args.context:
            command += ["--context", self.args.context]
        return command + ["-n", self.args.namespace, "port-forward", "--address", "127.0.0.1",
                          f"service/{self.args.service}", f"{forward_port}:{self.args.remote_port}"]

    def stop(self, *_: object) -> None:
        self.stopping = True

    def wait(self, seconds: float) -> None:
        until = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < until:
            time.sleep(min(0.1, max(0.0, until - time.monotonic())))

    def end_child(self) -> None:
        child, self.child = self.child, None
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    def supervise_once(self) -> str:
        """Run one forward until it exits, hangs or a stop is requested."""
        a = self.args
        assert self.relay is not None
        self.relay.upstream_port = forward_port = ephemeral_port()
        self.child = subprocess.Popen(self.argv(forward_port), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + a.start_timeout
        while not ready(a.local_port, a.probe_path, a.probe_timeout):
            if self.stopping:
                return "stopped"
            if self.child.poll() is not None:
                return "child exit before ready"
            if time.monotonic() >= deadline:
                return "not ready within start timeout"
            self.wait(min(0.2, a.probe_interval))
        log(f"ready 127.0.0.1:{a.local_port} -> {a.namespace}/service/{a.service}:{a.remote_port}")
        failures = 0
        while not self.stopping:
            if self.child.poll() is not None:
                return "child exit"
            failures = 0 if ready(a.local_port, a.probe_path, a.probe_timeout) else failures + 1
            if failures >= a.probe_failures:
                return f"{failures} failed readiness probes"
            self.wait(a.probe_interval)
        return "stopped"

    def run(self) -> int:
        a = self.args
        listener = socket.socket()
        # Relay-side closes (an ended stream, a stop) leave TIME_WAIT on this port; a
        # prompt restart must still bind. Linux still refuses a port being listened on.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", a.local_port))
        except OSError:
            listener.close()
            log(f"refused: 127.0.0.1:{a.local_port} is already in use")
            return 2
        listener.listen(MAX_RELAYED)
        self.relay = Relay(listener, a.idle_close)
        self.relay.start()
        backoff, restarts = a.backoff_min, 0
        try:
            while not self.stopping:
                started = time.monotonic()
                reason = self.supervise_once()
                self.end_child()
                if self.stopping:
                    break
                if time.monotonic() - started >= STABLE_SECONDS:
                    backoff = a.backoff_min
                restarts += 1
                log(f"restart {restarts} after {reason}; next attempt in {backoff:g}s")
                self.wait(backoff)
                backoff = min(a.backoff_max, backoff * 2)
        finally:
            self.end_child()
            self.relay.stop()
        log("stopped")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--kubeconfig", default="")
    parser.add_argument("--context", default="")
    parser.add_argument("--namespace", type=name, required=True)
    parser.add_argument("--service", type=name, required=True)
    parser.add_argument("--local-port", type=port, required=True)
    parser.add_argument("--remote-port", type=port, required=True)
    parser.add_argument("--probe-path", default="/readyz")
    parser.add_argument("--probe-interval", type=float, default=2.0)
    parser.add_argument("--probe-timeout", type=float, default=2.0)
    parser.add_argument("--probe-failures", type=int, default=3)
    parser.add_argument("--start-timeout", type=float, default=15.0)
    parser.add_argument("--backoff-min", type=float, default=1.0)
    parser.add_argument("--backoff-max", type=float, default=10.0)
    parser.add_argument("--idle-close", type=float, default=4.0,
                        help="replace a relayed stream idle this long after the server last sent data; "
                             "keep it below the server's keep-alive (Qdrant: 5 s)")
    args = parser.parse_args(argv)
    if not args.probe_path.startswith("/") or args.probe_failures < 1 or not (
            0 < args.backoff_min <= args.backoff_max and args.probe_interval > 0 and args.start_timeout > 0
            and args.idle_close > 0):
        parser.error("probe path must start with '/', and intervals, failures, backoff and idle close must be positive")
    supervisor = Supervisor(args)
    signal.signal(signal.SIGTERM, supervisor.stop)
    signal.signal(signal.SIGINT, supervisor.stop)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
