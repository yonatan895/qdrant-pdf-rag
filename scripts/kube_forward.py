#!/usr/bin/env python3
"""Supervised loopback `kubectl port-forward` to a namespaced Service (#751).

A bare port-forward exits on any pod-connection error and never returns, so a
sandbox stack loses its retained Qdrant mid-session. This owner binds
127.0.0.1 only, refuses an occupied port, restarts the forward with bounded
backoff when kubectl exits or its readiness probe keeps failing (a hung
forward), and stops its child on SIGINT/SIGTERM. The Service stays ClusterIP;
nothing is exposed beyond loopback. Output is fixed-format, never secrets.
"""
from __future__ import annotations

import argparse
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
STABLE_SECONDS = 60.0


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


def port_free(local_port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", local_port))
        except OSError:
            return False
    return True


def ready(local_port: int, path: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{local_port}{path}", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


class Supervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stopping = False
        self.child: subprocess.Popen | None = None

    def argv(self) -> list[str]:
        command = [self.args.kubectl]
        if self.args.kubeconfig:
            command += ["--kubeconfig", self.args.kubeconfig]
        if self.args.context:
            command += ["--context", self.args.context]
        return command + ["-n", self.args.namespace, "port-forward", "--address", "127.0.0.1",
                          f"service/{self.args.service}", f"{self.args.local_port}:{self.args.remote_port}"]

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
        self.child = subprocess.Popen(self.argv(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
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
        if not port_free(a.local_port):
            log(f"refused: 127.0.0.1:{a.local_port} is already in use")
            return 2
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
    args = parser.parse_args(argv)
    if not args.probe_path.startswith("/") or args.probe_failures < 1 or not (
            0 < args.backoff_min <= args.backoff_max and args.probe_interval > 0 and args.start_timeout > 0):
        parser.error("probe path must start with '/', and intervals, failures and backoff must be positive")
    supervisor = Supervisor(args)
    signal.signal(signal.SIGTERM, supervisor.stop)
    signal.signal(signal.SIGINT, supervisor.stop)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
