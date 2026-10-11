#!/usr/bin/env python3
"""Manually owned Colab development runs: prepare, run, recover/stop.

No shell commands or Colab calls occur on import or during prepare. CLI
outputs may contain private paths; all raw outputs stay in the private run
directory. Only fixed diagnostics and the session-specific recovery command
are printed. Production model serving remains platform-owned.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import selectors
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from mainframe_rag.serve.colab_profiles import COLAB_PROFILES, admit, build_profile
from mainframe_rag.serve.colab_storage import prove_qdrant_storage

ROOT = Path(__file__).resolve().parents[1]
REMOTE_PROFILE = "/content/model-profile.json"
SESSION_LINE = re.compile(r"^\[([^\]]+)\] (\S+) \| Hardware: .* \| Shape: .* \| Variant: \S+$")
CAPTURE_LIMIT_BYTES = 64 * 1024
CAPTURE_POOLS = {"work": 4 * 1024 * 1024, "export": 256 * 1024, "release": 512 * 1024}
COMMAND_LOG_SLOTS = 32
MAX_CAPTURED_CHILDREN = 8
PIPE_DRAIN_GRACE_SECONDS = .2
SOURCE_LIMIT_BYTES = 32 * 1024 * 1024
SOURCE_FILE_LIMIT_BYTES = 4 * 1024 * 1024
SOURCE_ENTRY_LIMIT = 4096
PREREQUISITE_LIMITS = {"inference_lock": 1024 * 1024, "transport_receipt": 64 * 1024,
                       "kubeconfig": 1024 * 1024, "inventory": 16 * 1024 * 1024,
                       "golden": 16 * 1024 * 1024}
SOURCE_REQUIRED = ("scripts/colab.py", "scripts/colab_preflight.py", "scripts/colab_runtime.py",
                   "src/mainframe_rag/serve/colab_profiles.py", "scripts/tools/run-task.sh",
                   "scripts/run_local_stack.sh", "scripts/run_local_gateway.sh", "scripts/probe_gateway.py",
                   "taskfiles/local.yml", "Taskfile.yml", "pyproject.toml", "requirements.lock.txt")
TRANSPORT_TOOLS = ("colab", "ssh", "ssh_keygen", "docker")
NETWORK_RELEASE_SECONDS = 5
EVENT_HISTORY_LIMIT = 64
CU_HISTORY_LIMIT = 32
METADATA_PERSIST_SECONDS = 5
MANIFEST_LIMIT_BYTES = 64 * 1024
QDRANT_CONFIG_PATHS = frozenset(f"/qdrant/config/{filename}" for filename in
                               ("initialize.sh", "config.yaml", "production.yaml", "local.yaml"))


class LabError(RuntimeError):
    """Fixed local diagnostic; upstream text is preserved privately only."""


class CaptureBudget:
    """Persist raw-output reservations, including live children, across recovery.

    Work/export cannot spend the release pool. Exhausted or unwritable cleanup
    logs degrade to bounded memory capture so control commands still execute.
    These limits cover this controller's streams, not child-opened data files.
    """

    def __init__(self, state: Path):
        self.path = state / "capture-budget.json"
        self.lock = threading.Lock()
        self.used = {pool: 0 for pool in CAPTURE_POOLS}
        self.unavailable = False
        if self.path.exists() or self.path.is_symlink():
            try:
                if self.path.is_symlink() or self.path.stat().st_size > 4096:
                    raise ValueError("invalid capture budget")
                value = json.loads(self.path.read_text())
                if (type(value) is not dict or value.get("schema") != 1
                        or type(value.get("used")) is not dict or set(value["used"]) != set(CAPTURE_POOLS)):
                    raise ValueError("invalid capture budget")
                if not all(type(size) is int and 0 <= size <= CAPTURE_POOLS[pool]
                           for pool, size in value["used"].items()):
                    raise ValueError("invalid capture budget")
                self.used = value["used"]
            except (OSError, ValueError, KeyError, TypeError):
                self.unavailable = True

    def reserve(self, pool: str) -> int:
        with self.lock:
            available = 0 if self.unavailable else CAPTURE_POOLS[pool] - self.used[pool]
            size = min(2 * CAPTURE_LIMIT_BYTES, available)
            if pool == "work" and size < 2 * CAPTURE_LIMIT_BYTES:
                raise LabError("controller work output allowance exhausted; cleanup remains available")
            if not size:
                return 0
            self.used[pool] += size
            try:
                private_json(self.path, {"schema": 1, "used": self.used})
            except OSError:
                self.unavailable = True
                if pool == "work":
                    raise LabError("controller output budget cannot be persisted") from None
                return 0
            return size

    def finish(self, pool: str, reserved: int, written: int) -> None:
        with self.lock:
            self.used[pool] -= reserved - written
            if not self.unavailable:
                try:
                    private_json(self.path, {"schema": 1, "used": self.used})
                except OSError:
                    # The persisted reservation is conservative after a crash.
                    self.unavailable = True


class ProcessCapture:
    """Drain two nonblocking pipes; keep fixed prefixes and discard the rest."""

    def __init__(self, process: subprocess.Popen, paths: dict[str, Path],
                 budget: CaptureBudget, pool: str, reserved: int, *, retain: bool):
        self.process = process
        self.budget = budget
        self.pool = pool
        self.reserved = reserved
        self.retain = retain
        self.output = {name: bytearray() for name in ("stdout", "stderr")}
        self.truncated = {name: False for name in self.output}
        self.sizes = {name: 0 for name in self.output}
        self.written = 0
        self.log_failed = False
        self.held_pipes = False
        self.stop = threading.Event()
        self.streams = {}
        try:
            for name, path in paths.items():
                if not reserved:
                    continue
                try:
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                    self.streams[name] = os.fdopen(descriptor, "wb", buffering=0)
                except OSError:
                    if pool == "work":
                        raise
                    self.log_failed = True
            self.thread = threading.Thread(target=self.drain, daemon=True)
            self.thread.start()
        except BaseException:
            for stream in self.streams.values():
                stream.close()
            budget.finish(pool, reserved, 0)
            raise

    def drain(self) -> None:
        exited_at = None
        try:
            with selectors.DefaultSelector() as selector:
                for name in self.output:
                    pipe = getattr(self.process, name)
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                while selector.get_map() and not self.stop.is_set():
                    if self.process.poll() is not None:
                        if exited_at is None:
                            exited_at = time.monotonic()
                        elif time.monotonic() - exited_at >= PIPE_DRAIN_GRACE_SECONDS:
                            self.held_pipes = True
                            for key in selector.get_map().values():
                                self.truncated[key.data] = True
                            break
                    for key, _ in selector.select(.025):
                        try:
                            data = os.read(key.fd, 8192)
                        except BlockingIOError:
                            continue
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        name = key.data
                        kept = data[:max(0, CAPTURE_LIMIT_BYTES - self.sizes[name])]
                        self.truncated[name] |= len(kept) != len(data)
                        self.sizes[name] += len(kept)
                        if self.retain:
                            self.output[name].extend(kept)
                        stream = self.streams.get(name)
                        raw = kept[:max(0, self.reserved - self.written)]
                        if stream is not None and raw:
                            try:
                                self.written += stream.write(raw)
                            except OSError:
                                self.log_failed = True
                                stream.close()
                                del self.streams[name]
                if self.stop.is_set():
                    for key in selector.get_map().values():
                        self.truncated[key.data] = True
        except OSError:
            self.log_failed = True
            self.truncated = {name: True for name in self.output}
        finally:
            for name in self.output:
                getattr(self.process, name).close()
            for stream in self.streams.values():
                stream.close()
            self.budget.finish(self.pool, self.reserved, self.written)

    def finish(self) -> None:
        self.thread.join(PIPE_DRAIN_GRACE_SECONDS + .1)
        if self.thread.is_alive():
            self.stop.set()
            self.thread.join(.1)
        if self.thread.is_alive():
            raise LabError("owned output drain did not stop within its bounded grace")


@contextmanager
def assignment_lock():
    """Serialize Colab ownership across run states on this host, before CU checks.

    This is independent of the caller's existing host resource lock. Keep the
    inode stable: deleting a held lock would permit two concurrent owners.
    """
    directory = Path("/tmp") / f"mainframe-rag-colab-{os.geteuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise LabError("Colab assignment lock directory must be private and owned")
    descriptor = os.open(directory / "assignment.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise LabError("Colab assignment lock must be private and owned")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LabError("another controller owns Colab on this host; no allocation attempted") from exc
        yield


def private_json(path: Path, value: dict) -> None:
    encoded = json.dumps(value, indent=2) + "\n"
    if path.name == "manifest.json" and len(encoded.encode()) > MANIFEST_LIMIT_BYTES:
        raise LabError("controller manifest exceeded its bounded metadata envelope")
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(encoded)
    os.replace(temporary, path)


def external(path: Path) -> Path:
    path = path.expanduser().absolute()
    common = Path(subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True, timeout=5).strip()).parent
    if path.resolve().is_relative_to(ROOT) or path.resolve().is_relative_to(common):
        raise LabError("private state/corpus/evidence must be outside a Git checkout")
    return path


def executable(value: str) -> str:
    found = shutil.which(value)
    if not found:
        raise LabError("a required local executable is unavailable; prepare it explicitly")
    return str(Path(found).absolute())


def digest_file(path: Path, limit: int) -> str:
    """Hash an owned regular input in small chunks; never read corpora/weights."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise LabError("declared identity input must be a bounded regular file")
        digest = hashlib.sha256()
        consumed = 0
        while raw := stream.read(min(65536, limit - consumed + 1)):
            consumed += len(raw)
            if consumed > limit:
                raise LabError("declared identity input grew beyond its bound")
            digest.update(raw)
    return digest.hexdigest()


def read_bounded_input(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise LabError("declared prerequisite must be a bounded regular file")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise LabError("declared prerequisite grew beyond its bound")
    return raw


def source_identity(root: Path) -> dict:
    """Bind execution owners, including dirty/new source, without traversing data."""
    paths = {root / name for name in SOURCE_REQUIRED}
    pending = [root / name for name in ("scripts", "src/mainframe_rag", "taskfiles")]
    entries = 0
    while pending:
        with os.scandir(pending.pop()) as directory:
            for entry in directory:
                entries += 1
                if entries > SOURCE_ENTRY_LIMIT:
                    raise LabError("candidate source inventory exceeded its bounded entry count")
                if entry.name.startswith(".") or entry.name in ("__pycache__", "build", "dist"):
                    continue
                if entry.is_symlink():
                    raise LabError("candidate execution source cannot be a symlink")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif not entry.name.endswith((".pyc", ".pyo")):
                    paths.add(Path(entry.path))
    total = 0
    digest = hashlib.sha256()
    for path in sorted(paths):
        total += path.stat().st_size
        if total > SOURCE_LIMIT_BYTES:
            raise LabError("candidate execution source exceeded its bounded byte inventory")
        record = [path.relative_to(root).as_posix(), digest_file(path, SOURCE_FILE_LIMIT_BYTES)]
        digest.update(json.dumps(record, separators=(",", ":")).encode() + b"\n")
    return {"source_sha256": digest.hexdigest(), "source_files": len(paths), "source_bytes": total}


def candidate_identity() -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=10).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise LabError("cannot establish the exact prepared candidate")
    return {"development_sha": head, **source_identity(ROOT)}


def object_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def tool_identity(tools: dict) -> dict:
    return {name: digest_file(Path(path).resolve(strict=True), 128 * 1024 * 1024)
            for name, path in sorted(tools.items())}


def prerequisite_identity(config: dict) -> dict:
    return {name: digest_file(Path(config[name]), limit)
            for name, limit in PREREQUISITE_LIMITS.items() if config.get(name)}


def cpu_transport_profile(profile: dict) -> dict:
    return {key: value for key, value in profile.items()
            if key not in ("deadline_seconds", "expires_at", "inference_lock_sha256")}


def transport_identity(config: dict, bridge: str) -> dict:
    return {"verified": True, "bridge": bridge, "gateway_image": config["gateway_image"],
            "cli_version": "0.7.4", "network_name": config["gateway_network"],
            "gateway_name": config["gateway_name"],
            "reasoning_url": f"http://{bridge}:{config['reasoning_port']}/v1",
            "embedding_url": f"http://{bridge}:{config['embedding_port']}/v1",
            "forwards": [{"bind": address, "local_port": config[local],
                          "remote_host": "127.0.0.1", "remote_port": remote}
                         for local, remote in (("reasoning_port", 8000), ("embedding_port", 8001))
                         for address in ("127.0.0.1", bridge)]}


def validate_gpu_prerequisites(manifest: dict, bridge: str, state: Path) -> None:
    if not manifest["profile"]["accelerator"]:
        return
    config = manifest["config"]
    if not config.get("inference_lock") or not config.get("transport_receipt"):
        raise LabError("GPU allocation requires the approved inference lock and released CPU transport receipt")
    # Both import paths use the same stdlib runtime owner; no duplicate parser.
    if __package__:
        from scripts.colab_runtime import validate_inference_lock
    else:
        from colab_runtime import validate_inference_lock
    try:
        validate_inference_lock(state / "inference.lock", manifest["profile"].get("inference_lock_sha256"))
    except (OSError, RuntimeError, ValueError, TypeError):
        raise LabError("GPU inference lock failed immutable reference validation") from None
    raw = read_bounded_input(state / "cpu-transport-receipt.input.json", PREREQUISITE_LIMITS["transport_receipt"])
    receipt = json.loads(raw)
    expected_cpu = cpu_transport_profile(build_profile("COLAB_CPU_TRANSPORT"))
    if (type(receipt) is not dict or type(receipt.get("schema")) is not int or receipt.get("schema") != 1
            or receipt.get("kind") != "colab-cpu-transport"
            or receipt.get("candidate") != manifest["prepared_identity"]["candidate"]
            or object_digest(receipt.get("cpu_profile")) != object_digest(expected_cpu)
            or receipt.get("tools_sha256") != {name: manifest["prepared_identity"]["tools_sha256"][name]
                                              for name in TRANSPORT_TOOLS}
            or object_digest(receipt.get("transport")) != object_digest(transport_identity(config, bridge))
            or receipt.get("workload_succeeded") is not True or receipt.get("released") is not True):
        raise LabError("CPU transport receipt does not prove this candidate and probed transport identity")
    evidence = receipt.get("release_evidence")
    if (type(evidence) is not dict or evidence.get("absent") is not True
            or not re.fullmatch(r"rag-[0-9a-f]{16}", str(receipt.get("session", "")))
            or not isinstance(receipt.get("endpoint"), str) or not receipt["endpoint"]
            or evidence.get("session") != receipt["session"] or evidence.get("endpoint") != receipt["endpoint"]
            or type(evidence.get("observed_at")) not in (int, float)
            or type(receipt.get("completed_at")) not in (int, float)
            or not 0 < evidence["observed_at"] <= receipt["completed_at"] <= time.time() + 5):
        raise LabError("CPU transport receipt lacks exact successful owned-session absence proof")


def prepare(args: argparse.Namespace) -> int:
    state = external(args.state)
    if state.exists() or state.is_symlink():
        raise LabError("run state already exists; use a fresh per-run directory")
    profile = build_profile(args.profile, embed_max_len=args.embedding_max_len,
                            deadline_seconds=args.deadline_seconds)
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", args.gateway_image):
        raise LabError("gateway image must be the existing gateway owner's digest pin")
    for port in (args.reasoning_port, args.embedding_port, args.qdrant_port):
        if not 1024 <= port <= 65535:
            raise LabError("local ports must be distinct unprivileged TCP ports")
    if len({args.reasoning_port, args.embedding_port, args.qdrant_port}) != 3:
        raise LabError("local ports must be distinct unprivileged TCP ports")
    if not math.isfinite(args.cu_cap) or not 0 < args.cu_cap <= 200:
        raise LabError("CU cap must be finite, positive and at most 200")
    if not math.isfinite(args.cu_floor) or not 0 <= args.cu_floor < 200:
        raise LabError("CU floor must be finite and within the monthly envelope")
    if type(args.poll_seconds) not in (int, float) or not math.isfinite(args.poll_seconds) or not 1 <= args.poll_seconds <= 5:
        raise LabError("poll interval must be finite and between one and five seconds")
    gateway_name = os.environ.get("GATEWAY_NAME") or "local-litellm-gateway"
    gateway_network = os.environ.get("PG_NET") or f"{gateway_name}-net"
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
               for value in (gateway_name, gateway_network)):
        raise LabError("gateway and network names must be bounded Docker object names")
    config = {"dataset": args.dataset, "gateway_image": args.gateway_image,
              "gateway_name": gateway_name, "gateway_network": gateway_network,
              "gateway_debug": "0",
              "reasoning_port": args.reasoning_port, "embedding_port": args.embedding_port,
              "qdrant_port": args.qdrant_port, "cu_cap": args.cu_cap, "cu_floor": args.cu_floor,
              "namespace": args.namespace, "qdrant_service": args.qdrant_service,
              "kube_context": args.kube_context, "poll_seconds": args.poll_seconds}
    for key in ("corpus", "golden", "inventory", "data_state", "kubeconfig",
                "inference_lock", "transport_receipt"):
        value = getattr(args, key)
        config[key] = str(external(value)) if value else None
    if args.dataset == "production":
        if not all(config[key] for key in ("corpus", "golden", "inventory", "data_state",
                                           "kubeconfig", "kube_context", "namespace", "qdrant_service")):
            raise LabError("production requires private corpus/golden/inventory and explicit retained Kind PVC service inputs")
        if not Path(config["corpus"]).is_dir() or not Path(config["golden"]).is_file():
            raise LabError("production corpus directory and grounded evaluation file must already exist")
        inventory = json.loads(read_bounded_input(Path(config["inventory"]), PREREQUISITE_LIMITS["inventory"]))
        lengths = (inventory["max_document_tokens"], inventory["max_query_tokens"])
        if not all(type(value) is int and value > 0 for value in lengths):
            raise LabError("inventory must contain measured positive token lengths")
        if max(lengths) > args.embedding_max_len:
            raise LabError("measured embedding inputs exceed the selected window; truncation is forbidden")
        config["inventory_sha256"] = digest_file(Path(config["inventory"]), PREREQUISITE_LIMITS["inventory"])
        data = Path(config["data_state"])
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        if data.is_symlink() or data.stat().st_mode & 0o077:
            raise LabError("persistent local data state must be private and not a symlink")
    for key in ("inference_lock", "transport_receipt", "kubeconfig"):
        if config[key] and not Path(config[key]).is_file():
            raise LabError("a declared private prerequisite file is missing")
    if config["inference_lock"]:
        profile["inference_lock_sha256"] = digest_file(Path(config["inference_lock"]), PREREQUISITE_LIMITS["inference_lock"])
    tools = {key: executable(getattr(args, key)) for key in ("colab", "ssh", "docker", "ssh_keygen")}
    if args.dataset == "production":
        tools["kubectl"] = executable(args.kubectl)
    state.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    candidate = candidate_identity()
    manifest = {"schema": 1, "session": "rag-" + uuid.uuid4().hex[:16], "endpoint": None,
                "phase": "prepared", "allocation_attempted": False,
                "created_at": time.time(), "development_sha": candidate["development_sha"],
                "config": config, "profile": profile, "tools": tools, "events": []}
    private_json(state / "model-profile.json", profile)
    snapshots = {}
    for name, filename in (("inference_lock", "inference.lock"),
                           ("transport_receipt", "cpu-transport-receipt.input.json")):
        if config.get(name):
            raw = read_bounded_input(Path(config[name]), PREREQUISITE_LIMITS[name])
            descriptor = os.open(state / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
            snapshots[name] = hashlib.sha256(raw).hexdigest()
    prerequisites = prerequisite_identity(config)
    if any(digest != prerequisites[name] for name, digest in snapshots.items()):
        raise LabError("private prerequisite changed while preparing its owned snapshot")
    manifest["prepared_identity"] = {
        "candidate": candidate, "config_sha256": object_digest(config), "profile_sha256": object_digest(profile),
        "tools_sha256": tool_identity(tools), "tools_config_sha256": object_digest(tools),
        "prerequisites_sha256": prerequisites, "snapshots_sha256": snapshots,
        "model_profile_sha256": digest_file(state / "model-profile.json", 64 * 1024),
    }
    private_json(state / "manifest.json", manifest)
    print(f"Prepared private run: {state}")
    print("No runtime allocated. Run requires explicit --approve-cu and prior maintainer authorization.")
    return 0


class Run:
    def __init__(self, state: Path):
        self.state = external(state)
        if self.state.is_symlink() or self.state.stat().st_uid != os.geteuid() or self.state.stat().st_mode & 0o077:
            raise LabError("run state must be private and owned by the current user")
        self.manifest = json.loads(read_bounded_input(self.state / "manifest.json", MANIFEST_LIMIT_BYTES))
        if self.manifest.get("schema") != 1 or not re.fullmatch(r"rag-[0-9a-f]{16}", self.manifest.get("session", "")):
            raise LabError("invalid run ownership manifest")
        self.config = self.manifest["config"]
        self.profile = self.manifest["profile"]
        self.tools = self.manifest["tools"]
        self.children: list[subprocess.Popen] = []
        self.runtime: subprocess.Popen | None = None
        self.tunnel: subprocess.Popen | None = None
        self.workload: subprocess.Popen | None = None
        self.qdrant_forward: subprocess.Popen | None = None
        self.expiry = math.inf
        self.work_expiry = math.inf
        self.cleanup_phase_expiry: float | None = None
        self.sequence = 0
        self.output_budget = CaptureBudget(self.state)
        self.captures: dict[subprocess.Popen, ProcessCapture] = {}
        self.reaped_groups: set[subprocess.Popen] = set()
        self.last_metadata_persist: float | None = None
        self.cleanup_in_progress = False
        self.cleanup_metadata_failed = False
        self.metadata_warning_printed = False

    def save(self, event: str) -> None:
        now = time.time()
        self.record_history("events", "event_history", {"event": event, "time": now}, EVENT_HISTORY_LIMIT)
        previous = getattr(self, "last_metadata_persist", None)
        if (event == "cu_observed_best_effort" and previous is not None
                and 0 <= now - previous < METADATA_PERSIST_SECONDS):
            return
        try:
            private_json(self.state / "manifest.json", self.manifest)
        except (OSError, LabError):
            if not getattr(self, "cleanup_in_progress", False):
                raise
            self.cleanup_metadata_failed = True
            self.manifest["metadata_persistence_failed"] = True
            if not getattr(self, "metadata_warning_printed", False):
                print("Cleanup metadata could not be persisted; exact VM release continues without acceptance.",
                      file=sys.stderr)
                self.metadata_warning_printed = True
            return
        self.last_metadata_persist = now

    def record_history(self, key: str, summary_key: str, value: dict, limit: int) -> dict:
        history = self.manifest.setdefault(key, [])
        summary = self.manifest.setdefault(summary_key, {"total": len(history), "dropped": 0})
        history.append(value)
        summary["total"] += 1
        excess = max(0, len(history) - limit)
        del history[:excess]
        summary["dropped"] += excess
        return summary

    def validate_prepared(self, *, running: bool = False) -> None:
        prepared = self.manifest.get("prepared_identity", {})
        profile = dict(self.profile)
        if running and profile.pop("expires_at", None) != self.work_expiry:
            raise LabError("remote deadline no longer matches the owned work deadline")
        snapshots = {name: digest_file(self.state / filename, PREREQUISITE_LIMITS[name])
                     for name, filename in (("inference_lock", "inference.lock"),
                                            ("transport_receipt", "cpu-transport-receipt.input.json"))
                     if self.config.get(name)}
        if (prepared.get("candidate") != candidate_identity()
                or self.manifest.get("development_sha") != prepared["candidate"]["development_sha"]
                or prepared.get("config_sha256") != object_digest(self.config)
                or prepared.get("profile_sha256") != object_digest(profile)
                or prepared.get("tools_config_sha256") != object_digest(self.tools)
                or prepared.get("tools_sha256") != tool_identity(self.tools)
                or prepared.get("prerequisites_sha256") != prerequisite_identity(self.config)
                or prepared.get("snapshots_sha256") != snapshots
                or prepared.get("model_profile_sha256") != digest_file(self.state / "model-profile.json", 64 * 1024)):
            raise LabError("prepared candidate/configuration/prerequisite bytes changed; prepare a fresh run")

    def capture_report(self, capture: ProcessCapture) -> None:
        report = self.manifest.setdefault("controller_capture", {
            "stream_limit_bytes": CAPTURE_LIMIT_BYTES, "pool_limits_bytes": CAPTURE_POOLS,
            "stdout_truncated": False, "stderr_truncated": False, "log_failed": False,
            "descendant_pipes_incomplete": False,
        })
        for name in ("stdout", "stderr"):
            report[name + "_truncated"] |= capture.truncated[name]
        report["log_failed"] |= capture.log_failed or self.output_budget.unavailable
        report["descendant_pipes_incomplete"] |= capture.held_pipes

    def capture(self, process: subprocess.Popen, paths: dict[str, Path], pool: str,
                reserved: int, *, retain: bool) -> ProcessCapture:
        try:
            capture = ProcessCapture(process, paths, self.output_budget, pool, reserved, retain=retain)
        except BaseException:
            self.terminate(process)
            for name in ("stdout", "stderr"):
                getattr(process, name).close()
            raise
        self.captures[process] = capture
        return capture

    def command(self, argv: list[str], *, seconds: float = 30, cleanup: bool = False,
                allow_failure: bool = False,
                require_complete_stdout: bool = True, sensitive: bool = False,
                env: dict | None = None, stdout_observer: Callable[[str], None] | None = None,
                cleanup_deadline: float | None = None) -> subprocess.CompletedProcess:
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            raise LabError("local tool timeout must be finite and positive")
        deadline = (min(self.expiry, self.cleanup_phase_expiry if self.cleanup_phase_expiry is not None else math.inf)
                    if cleanup else self.work_expiry)
        if cleanup_deadline is not None:
            now = time.time()
            if (not cleanup or type(cleanup_deadline) not in (int, float)
                    or not math.isfinite(cleanup_deadline)
                    or not now < cleanup_deadline <= now + NETWORK_RELEASE_SECONDS):
                raise LabError("independent network cleanup requires its bounded control deadline")
            deadline = cleanup_deadline
        remaining = deadline - time.time()
        if remaining <= 0:
            raise LabError("run deadline reached")
        pool = ("export" if cleanup and (self.cleanup_phase_expiry is not None or cleanup_deadline is not None)
                else "release" if cleanup else "work")
        reserved = self.output_budget.reserve(pool)
        self.sequence += 1
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True, cwd=ROOT, env=env)
        except BaseException:
            self.output_budget.finish(pool, reserved, 0)
            raise
        stem = f"command-{pool}-{self.sequence % COMMAND_LOG_SLOTS:02}"
        paths = {} if sensitive else {name: self.state / f"{stem}.{name}.log"
                                      for name in ("stdout", "stderr")}
        capture = self.capture(process, paths, pool, reserved, retain=True)
        timeout = min(seconds, remaining)
        until = time.monotonic() + timeout
        observed_bytes = 0

        def observe_stdout() -> None:
            nonlocal observed_bytes
            size = len(capture.output["stdout"])
            if stdout_observer is not None and size != observed_bytes:
                observed_bytes = size
                stdout_observer(bytes(capture.output["stdout"]).decode("utf-8", errors="replace"))

        try:
            while process.poll() is None:
                observe_stdout()
                if require_complete_stdout and capture.truncated["stdout"]:
                    raise LabError("local tool stdout exceeded its bound; partial identity is refused")
                if time.monotonic() >= until:
                    raise subprocess.TimeoutExpired(argv, timeout)
                time.sleep(.01)
        finally:
            # Signal the owned group even when its root already exited: an
            # inherited pipe held by a descendant must not keep capture alive.
            try:
                try:
                    if process.poll() is not None:
                        capture.finish()
                    observe_stdout()
                finally:
                    self.terminate(process)
            finally:
                # Ownership is emitted before the network probe. Preserve that
                # bounded observation even when timeout or Ctrl-C interrupts it.
                observe_stdout()
        stdout, stderr = (bytes(capture.output[name]).decode("utf-8", errors="replace")
                          for name in ("stdout", "stderr"))
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        if require_complete_stdout and capture.truncated["stdout"]:
            raise LabError("local tool stdout exceeded its bound; partial identity is refused")
        if capture.log_failed and not cleanup:
            raise LabError("controller output could not be retained within its bounded log")
        if result.returncode and not allow_failure:
            raise LabError("local tool operation failed; details retained in private run state")
        return result

    def colab(self, command: str, *args: str, **kwargs) -> subprocess.CompletedProcess:
        # Only these pinned CLI stdout contracts establish identity/account
        # state. Progress from upload/download/new/exec is diagnostic output.
        kwargs.setdefault("require_complete_stdout", command in ("version", "sessions", "usage"))
        return self.command([self.tools["colab"], "--auth", "oauth2", command, *args], **kwargs)

    def sessions(self, *, cleanup: bool = False) -> dict[str, str]:
        output = self.colab("sessions", cleanup=cleanup, seconds=5 if cleanup else 30).stdout
        found = {}
        for line in output.splitlines():
            match = SESSION_LINE.fullmatch(line.strip())
            if match:
                name, endpoint = match.groups()
                if name in found:
                    raise LabError("duplicate active session identity")
                found[name] = endpoint
        if not found and "[colab] No active sessions found on server." not in output:
            raise LabError("unable to parse pinned Colab session output")
        return found

    def owned(self, *, cleanup: bool = False) -> bool:
        endpoint = self.sessions(cleanup=cleanup).get(self.manifest["session"])
        if endpoint is None:
            return False
        if self.manifest["endpoint"] is None:
            if not self.manifest["allocation_attempted"]:
                raise LabError("refusing to adopt a session not allocated by this run")
            self.manifest["endpoint"] = endpoint
            self.save("session_bound")
        elif endpoint != self.manifest["endpoint"]:
            raise LabError("owned session endpoint changed; automatic stop is refused")
        return True

    def budget(self, *, initial: bool = False) -> None:
        self.check_qdrant()
        output = self.colab("usage").stdout
        try:
            balance = float(re.search(r"Current balance: ([0-9.]+) compute units", output)[1])
            rate = float(re.search(r"Usage rate: ([0-9.]+)/hr", output)[1])
            assignments = int(re.search(r"Active assignments: ([0-9]+)", output)[1])
        except (TypeError, ValueError) as exc:
            raise LabError("cannot verify current CU balance/rate") from exc
        if not all(math.isfinite(value) for value in (balance, rate)):
            raise LabError("cannot verify current CU balance/rate")
        existing = self.manifest.get("cu_observations", [])
        if "cu_summary" not in self.manifest and existing:
            self.manifest["cu_summary"] = {
                "total": len(existing), "dropped": 0, "first": existing[0], "last": existing[-1],
                "min_balance": min(item["balance"] for item in existing),
                "max_rate_per_hour": max(item["rate_per_hour"] for item in existing),
            }
        observation = {"time": time.time(), "balance": balance, "rate_per_hour": rate, "assignments": assignments}
        summary = self.record_history("cu_observations", "cu_summary", observation, CU_HISTORY_LIMIT)
        summary.setdefault("first", observation)
        summary.update(last=observation, min_balance=min(summary.get("min_balance", balance), balance),
                       max_rate_per_hour=max(summary.get("max_rate_per_hour", rate), rate))
        if initial:
            if assignments:
                raise LabError("another Colab assignment is active; this controller never stops it")
            self.manifest["starting_balance"] = balance
            if balance < self.config["cu_floor"] + self.config["cu_cap"]:
                raise LabError("CU balance cannot cover the approved cap plus reserve floor")
        elif assignments > 1:
            raise LabError("additional account assignments prevent attributable CU budgeting")
        if balance <= self.config["cu_floor"]:
            raise LabError("CU reserve floor reached")
        spent = max(0.0, self.manifest["starting_balance"] - balance)
        if spent >= self.config["cu_cap"]:
            raise LabError("approved CU cap reached")
        if rate > 0:
            remaining_cu = min(self.config["cu_cap"] - spent,
                               balance - self.config["cu_floor"])
            self.work_expiry = min(self.work_expiry, time.time() + remaining_cu / rate * 3600 - self.cleanup_reserve)
        self.save("cu_observed_best_effort")

    def spawn(self, argv: list[str], name: str, *, env: dict | None = None) -> subprocess.Popen:
        if len(self.children) >= MAX_CAPTURED_CHILDREN or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
            raise LabError("owned child/log identity limit reached")
        reserved = self.output_budget.reserve("work")
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, start_new_session=True, cwd=ROOT)
        except BaseException:
            self.output_budget.finish("work", reserved, 0)
            raise
        self.capture(child, {stream: self.state / f"{name}.{stream}.log"
                             for stream in ("stdout", "stderr")}, "work", reserved, retain=False)
        self.children.append(child)
        return child

    def terminate(self, child: subprocess.Popen) -> None:
        try:
            if child not in self.reaped_groups:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if child.poll() is None:
                    try:
                        child.wait(timeout=min(2, max(.01, self.expiry - time.time())))
                    except subprocess.TimeoutExpired:
                        pass
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=.25)
                self.reaped_groups.add(child)
        finally:
            capture = self.captures.pop(child, None)
            if capture is not None:
                capture.finish()
                self.capture_report(capture)

    def free_port(self, address: str, port: int) -> None:
        with socket.socket() as listener:
            # Match a reusable transport listener after a prior owned run has
            # quiesced; TIME_WAIT is not an active listener or new allocation.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((address, port))
            except OSError as exc:
                raise LabError("required local address/port is unavailable; no runtime allocated") from exc

    def kubectl(self, *args: str) -> list[str]:
        return [self.tools["kubectl"], "--kubeconfig", self.config["kubeconfig"],
                "--context", self.config["kube_context"], "--namespace", self.config["namespace"], *args]

    def persistent_qdrant(self) -> None:
        """Bind the inspected PVC producer to this run's owned loopback forward."""
        service = self.qdrant_object("service", self.config["qdrant_service"])
        metadata = service.get("metadata")
        if (not isinstance(metadata, dict) or metadata.get("name") != self.config["qdrant_service"]
                or metadata.get("namespace") != self.config["namespace"]):
            raise LabError("selected Qdrant service identity changed")
        spec = service.get("spec")
        if not isinstance(spec, dict):
            raise LabError("selected Qdrant service selector is unavailable")
        selector = spec.get("selector")
        if (not isinstance(selector, dict) or not selector
                or not all(isinstance(key, str) and isinstance(value, str) for key, value in selector.items())):
            raise LabError("selected Qdrant service selector is unavailable")
        labels = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
        listing = json.loads(self.command(self.kubectl("get", "pods", "-l", labels, "-o", "json"),
                                          sensitive=True).stdout)
        if not isinstance(listing, dict) or not isinstance(listing.get("items"), list):
            raise LabError("selected Qdrant pod inventory is incomplete")
        pods = listing["items"]
        proof = prove_qdrant_storage(
            service, pods,
            read_pvc=lambda name: self.qdrant_object("pvc", name),
            read_pv=lambda name: self.qdrant_object("pv", name),
            read_storage_class=lambda name: self.qdrant_object("storageclass", name),
            read_node=lambda name: self.qdrant_object("node", name),
            read_container_file=self.qdrant_config,
        )
        port = self.config["qdrant_port"]
        self.free_port("127.0.0.1", port)
        self.qdrant_forward = self.spawn(self.kubectl("port-forward", "--address", "127.0.0.1",
                                                     f"pod/{proof['pod_name']}", f"{port}:6333"), "qdrant-forward")
        self.wait_http(f"http://127.0.0.1:{port}/readyz", self.qdrant_forward)
        self.check_qdrant(proof)
        self.manifest["persistent_qdrant"] = {**proof, "url": f"http://127.0.0.1:{port}"}
        self.save("persistent_qdrant_admitted")

    def qdrant_object(self, kind: str, name: str) -> dict:
        # Pod specifications/annotations may also contain private configuration.
        value = json.loads(self.command(self.kubectl("get", kind, name, "-o", "json"),
                                       sensitive=True).stdout)
        if not isinstance(value, dict):
            raise LabError("Qdrant API object is incomplete")
        return value

    def qdrant_config(self, pod: str, container: str, path: str) -> str:
        if path not in QDRANT_CONFIG_PATHS:
            raise LabError("Qdrant config read is outside the fixed proof paths")
        return self.command(self.kubectl("exec", pod, "-c", container, "--", "cat", path),
                            seconds=10, sensitive=True).stdout

    def check_qdrant(self, proof: dict | None = None) -> None:
        proof = self.manifest.get("persistent_qdrant") if proof is None else proof
        if proof is None and self.config["dataset"] != "production":
            return
        if not proof or self.qdrant_forward is None or self.qdrant_forward.poll() is not None:
            raise LabError("owned retained Qdrant forward is unavailable")
        pod = self.qdrant_object("pod", proof["pod_name"])
        metadata, status = pod.get("metadata", {}), pod.get("status", {})
        if not isinstance(metadata, dict) or not isinstance(status, dict):
            raise LabError("retained Qdrant producer identity or readiness changed")
        statuses, conditions = status.get("containerStatuses", []), status.get("conditions", [])
        if (not isinstance(statuses, list) or not isinstance(conditions, list)
                or any(not isinstance(item, dict) for item in (*statuses, *conditions))):
            raise LabError("retained Qdrant producer identity or readiness changed")
        containers = [item for item in statuses
                      if item.get("name") == proof["container_name"]]
        # Volume/spec/mount identity is established by the storage owner for
        # this pod/container lifetime. Administrator mutation is not a lease.
        if (metadata.get("name") != proof["pod_name"] or metadata.get("uid") != proof["pod_uid"]
                or metadata.get("namespace") != proof["namespace"] or metadata.get("deletionTimestamp")
                or status.get("phase") != "Running"
                or not any(item.get("type") == "Ready" and item.get("status") == "True"
                           for item in conditions)
                or len(containers) != 1 or containers[0].get("ready") is not True
                or not isinstance(containers[0].get("state"), dict)
                or "running" not in containers[0].get("state", {})
                or containers[0].get("containerID") != proof["container_id"]
                or containers[0].get("imageID") != proof["image_id"]
                or self.qdrant_forward.poll() is not None):
            raise LabError("retained Qdrant producer identity or readiness changed")

    def wait_http(self, url: str, child: subprocess.Popen) -> None:
        until = min(self.work_expiry, time.time() + 20)
        while time.time() < until and child.poll() is None:
            try:
                with urllib.request.urlopen(url, timeout=min(2, max(.01, until - time.time()))) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(.05)
        raise LabError("owned local transport did not become reachable")

    def diagnostic_stage(self, stage: str) -> None:
        until = min(self.work_expiry, time.time() + 1200)
        destination = self.state / "remote-diagnostics.json"
        while time.time() < until:
            if self.runtime and self.runtime.poll() is not None:
                raise LabError("remote runtime exited before readiness; diagnostics retained privately")
            if self.tunnel and self.tunnel.poll() is not None:
                raise LabError("owned SSH tunnel was lost")
            result = self.colab("download", "-s", self.manifest["session"], "/content/colab-diagnostics.json",
                                str(destination), seconds=10, allow_failure=True)
            if result.returncode == 0:
                diagnostic = json.loads(destination.read_text())
                if diagnostic["stage"] == stage:
                    return
                if diagnostic["stage"] == "failed":
                    raise LabError("remote runtime reported a failure")
            self.budget()
            time.sleep(self.config["poll_seconds"])
        raise LabError("remote readiness exceeded the run budget")

    def transport(self, bridge: str) -> None:
        if not self.owned():
            raise LabError("owned session vanished before SSH; refusing auto-allocation")
        key = self.state / "id_ed25519_colab_rag"
        self.command([self.tools["ssh_keygen"], "-q", "-t", "ed25519", "-N", "", "-f", str(key), "-C", self.manifest["session"]])
        key.chmod(0o600)
        proxy = shlex.join([self.tools["colab"], "--auth", "oauth2", "ssh", "--proxy-mode",
                            "-s", self.manifest["session"], "--identity", str(key)])
        configuration = self.state / "ssh_config"
        configuration.write_text("Host rag-colab\n  HostName colab-runtime\n  User root\n"
                                 f"  IdentityFile {json.dumps(str(key))}\n  IdentitiesOnly yes\n"
                                 f"  ProxyCommand {proxy}\n  StrictHostKeyChecking accept-new\n"
                                 f"  UserKnownHostsFile {json.dumps(str(self.state / 'known_hosts'))}\n"
                                 "  ExitOnForwardFailure yes\n  ServerAliveInterval 10\n  ServerAliveCountMax 2\n")
        argv = [self.tools["ssh"], "-F", str(configuration), "-N"]
        for local, remote in ((self.config["reasoning_port"], 8000), (self.config["embedding_port"], 8001)):
            for address in ("127.0.0.1", bridge):
                argv += ["-L", f"{address}:{local}:127.0.0.1:{remote}"]
        argv.append("rag-colab")
        self.tunnel = self.spawn(argv, "ssh")
        for port in (self.config["reasoning_port"], self.config["embedding_port"]):
            self.wait_http(f"http://127.0.0.1:{port}/health", self.tunnel)
        self.probe_gateway_network(bridge)
        self.manifest["transport"] = transport_identity(self.config, bridge)
        self.save("host_and_gateway_network_transport_verified")

    def gateway_environment(self, bridge: str) -> dict[str, str]:
        env = dict(os.environ)
        identity = transport_identity(self.config, bridge)
        ownership = self.manifest.get("gateway_network", {})
        env.update(PG_NET=self.config["gateway_network"], GATEWAY_NAME=self.config["gateway_name"],
                   LITELLM_IMAGE=identity["gateway_image"], GATEWAY_DOCKER_BIN=self.tools["docker"],
                   GATEWAY_DEBUG=self.config["gateway_debug"],
                   GATEWAY_REASONING_URL=identity["reasoning_url"], GATEWAY_EMBED_URL=identity["embedding_url"],
                   GATEWAY_TRANSPORT_NETWORK_ID=ownership.get("network_id", ""),
                   GATEWAY_TRANSPORT_NETWORK_CREATED="1" if ownership.get("created") is True else "0")
        return env

    def observe_gateway_network(self, raw: str, bridge: str) -> bool:
        try:
            value = json.loads(raw)
        except ValueError:
            return False
        expected = transport_identity(self.config, bridge)
        if (type(value) is not dict
                or set(value) != {"network_name", "network_id", "created", "gateway_image", "reasoning_url", "embedding_url"}
                or type(value["created"]) is not bool or type(value["network_id"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", value["network_id"])
                or any(value[key] != expected[key]
                       for key in ("network_name", "gateway_image", "reasoning_url", "embedding_url"))):
            return False
        previous = self.manifest.get("gateway_network")
        if previous is not None and value != previous:
            return False
        if previous is None:
            self.manifest["gateway_network"] = value
            self.manifest["gateway_network_bridge"] = bridge
            self.save("gateway_network_ownership_observed")
        return True

    def probe_gateway_network(self, bridge: str) -> None:
        accepted = False

        def observe(raw: str) -> None:
            nonlocal accepted
            accepted = self.observe_gateway_network(raw, bridge)

        result = self.command(["sh", str(ROOT / "scripts/run_local_gateway.sh"), "transport-probe"],
                              env=self.gateway_environment(bridge), seconds=20, allow_failure=True,
                              stdout_observer=observe)
        if result.returncode or not accepted:
            raise LabError("gateway owner could not prove the exact network and backend addresses")

    def release_gateway_network(self) -> None:
        ownership = self.manifest.get("gateway_network")
        if ownership is None:
            return
        # Mandatory VM stop/absence runs first. This separate bounded control
        # window may finish after that absolute deadline and cannot spend the
        # Colab release capture pool or postpone its mandatory operations.
        self.command(["sh", str(ROOT / "scripts/run_local_gateway.sh"), "transport-release"],
                     env=self.gateway_environment(self.manifest["gateway_network_bridge"]),
                     seconds=NETWORK_RELEASE_SECONDS, cleanup=True,
                     cleanup_deadline=time.time() + NETWORK_RELEASE_SECONDS,
                     require_complete_stdout=False)
        self.manifest["gateway_network_cleanup"] = {"completed": True,
                                                     "owned_removed_or_absent": ownership["created"],
                                                     "preexisting_preserved": not ownership["created"]}
        self.save("gateway_network_cleanup_complete")

    def environment(self, bridge: str) -> dict[str, str]:
        env = self.gateway_environment(bridge)
        if self.profile["accelerator"]:
            env.update(GATEWAY_REASONING_URL=f"http://{bridge}:{self.config['reasoning_port']}/v1",
                       GATEWAY_EMBED_URL=f"http://{bridge}:{self.config['embedding_port']}/v1",
                       GATEWAY_REASONING_MODEL=self.profile["servers"][0]["model_id"],
                       GATEWAY_EMBED_MODEL=self.profile["servers"][1]["model_id"],
                       GATEWAY_EMBED_MODEL_REVISION=self.profile["servers"][1]["revision"],
                       DENSE_DIM="4096", RERANK_ENABLED="false", LLM_MAX_MODEL_LEN="16384",
                       LLM_REASONING_EFFORT_SIMPLE="low", LLM_REASONING_EFFORT_COMPLEX="medium",
                       GATEWAY_PROBE_MAX_TOKENS="512", LITELLM_IMAGE=self.config["gateway_image"],
                       GATEWAY_ENV_FILE=str(self.state / "gateway.env"),
                       LOCAL_STACK_LOG_DIR=str(self.state))
        if self.config["dataset"] == "production":
            env.update(CORPUS_DIR=self.config["corpus"], PRODUCTION_CORPUS_DIR=self.config["corpus"],
                       PRODUCTION_GOLDEN_PATH=self.config["golden"],
                       QDRANT_URL=self.manifest["persistent_qdrant"]["url"],
                       LOCAL_INGEST_PROGRESS=str(Path(self.config["data_state"]) /
                                                 f"ingest-{self.config['inventory_sha256']}.json"))
        # Only controlled, non-secret values are written for a second local terminal.
        keys = {key for key in env if key not in os.environ or env[key] != os.environ[key]}
        (self.state / "handoff.env").write_text("".join(f"export {key}={shlex.quote(env[key])}\n" for key in sorted(keys)))
        return env

    def cleanup(self) -> bool:
        self.cleanup_in_progress = True
        self.cleanup_metadata_failed = False
        try:
            return self._cleanup()
        finally:
            # Later ordinary saves must fail closed again. Historical failure
            # remains in the manifest; a new durable recovery may still finish.
            self.cleanup_in_progress = False

    def _cleanup(self) -> bool:
        ok = True
        session = self.manifest["session"]
        # Quiesce ingest/evaluation before stopping the model or exporting a
        # report. The transport remains alive until the writer has exited.
        if self.workload is not None:
            try:
                self.terminate(self.workload)
                self.save("local_workload_quiesced")
            except (LabError, OSError, subprocess.SubprocessError):
                ok = False
                self.save("local_workload_stop_failed")
        if self.manifest["allocation_attempted"]:
            # Export gets at most a third of the remaining cleanup window and
            # at most 20 seconds. Preserve time for independent release even
            # when its first ownership check or download stalls.
            remaining = max(0.0, self.expiry - time.time())
            self.cleanup_phase_expiry = min(time.time() + 20,
                                            self.expiry - min(30, remaining * 2 / 3))
            try:
                if self.owned(cleanup=True):
                    control = self.state / "stop-control"
                    control.write_text("stop\n")
                    self.colab("upload", "-s", session, str(control), "/content/colab-stop", cleanup=True,
                               seconds=5, allow_failure=True, require_complete_stdout=False)
                    if self.runtime:
                        wait = min(5, self.cleanup_phase_expiry - time.time())
                        if wait > 0:
                            try:
                                self.runtime.wait(timeout=wait)
                            except subprocess.TimeoutExpired:
                                pass
                    result = self.colab("download", "-s", session, "/content/colab-artifacts.tgz",
                                        str(self.state / "colab-artifacts.tgz"), cleanup=True, seconds=10,
                                        allow_failure=True, require_complete_stdout=False)
                    if result.returncode:
                        ok = False
                        self.save("artifact_export_failed")
            except (LabError, OSError, subprocess.SubprocessError):
                ok = False
                self.save("artifact_export_unavailable")
            finally:
                self.cleanup_phase_expiry = None
            # Export failure is isolated from the mandatory VM release attempt.
            try:
                if self.owned(cleanup=True):
                    self.colab("stop", "-s", session, cleanup=True, seconds=10, require_complete_stdout=False)
                if self.owned(cleanup=True):
                    raise LabError("owned session is still active after stop")
                self.manifest["released"] = True
                self.manifest["release_evidence"] = {"session": session, "endpoint": self.manifest["endpoint"],
                                                      "absent": True, "observed_at": time.time()}
                self.save("session_release_absence_verified")
            except (LabError, OSError, subprocess.SubprocessError):
                ok = False
                self.manifest["released"] = False
                self.save("session_release_unverified")
        for child in reversed(self.children):
            try:
                self.terminate(child)
            except (LabError, OSError, subprocess.SubprocessError):
                ok = False
        try:
            self.release_gateway_network()
        except (LabError, OSError, ValueError, KeyError, subprocess.SubprocessError):
            ok = False
            self.manifest["gateway_network_cleanup"] = {"completed": False}
            self.save("gateway_network_cleanup_failed")
        if self.manifest.get("released"):
            for filename in ("id_ed25519_colab_rag", "id_ed25519_colab_rag.pub"):
                (self.state / filename).unlink(missing_ok=True)
        self.manifest["phase"] = "released" if self.manifest.get("released") else "cleanup_required"
        ok = ok and not self.cleanup_metadata_failed
        self.save("cleanup_complete" if ok else "cleanup_incomplete")
        ok = ok and not self.cleanup_metadata_failed
        if not ok:
            print("Cleanup/export incomplete; inspect private evidence and use the recovery command.", file=sys.stderr)
        return ok

    def run(self, approval: float, workload: list[str]) -> int:
        if self.manifest["phase"] != "prepared" or approval != self.config["cu_cap"]:
            raise LabError("fresh prepared run and exact per-run CU approval are required")
        self.validate_prepared()
        start = time.time()
        duration = self.profile["deadline_seconds"]
        self.cleanup_reserve = min(90, duration / 3)
        self.expiry = start + duration
        self.work_expiry = self.expiry - self.cleanup_reserve
        self.manifest.update(started_at=start, expires_at=self.expiry, approved_cu=approval)
        self.profile["expires_at"] = self.work_expiry
        self.save("run_approved")
        print("Manual recovery: " + shlex.join([self.tools["colab"], "--auth", "oauth2", "stop", "-s", self.manifest["session"]]), flush=True)
        status = 1
        try:
            if not re.search(r"\b0\.7\.4\b", self.colab("version").stdout):
                raise LabError("google-colab-cli 0.7.4 is required")
            bridge = self.command([self.tools["docker"], "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"]).stdout.strip()
            address = ipaddress.ip_address(bridge)
            if address.version != 4 or address.is_unspecified or address.is_multicast or bridge == "127.0.0.1":
                raise LabError("Docker bridge gateway must be a specific IPv4 address")
            for port in (self.config["reasoning_port"], self.config["embedding_port"]):
                for host in ("127.0.0.1", bridge):
                    self.free_port(host, port)
            self.command([self.tools["docker"], "image", "inspect", self.config["gateway_image"]],
                         require_complete_stdout=False)
            if self.config["dataset"] == "production":
                self.persistent_qdrant()
            self.budget(initial=True)
            if self.manifest["session"] in self.sessions():
                raise LabError("generated session name already exists; refusing to adopt it")
            self.profile["expires_at"] = self.work_expiry
            self.validate_prepared(running=True)
            validate_gpu_prerequisites(self.manifest, bridge, self.state)
            self.manifest["allocation_attempted"] = True
            self.manifest["phase"] = "allocating"
            self.save("allocation_attempted")
            argv = ["-s", self.manifest["session"]]
            if self.profile["accelerator"]:
                argv += ["--gpu", self.profile["accelerator"]]
            self.colab("new", *argv, seconds=180)
            if not self.owned():
                raise LabError("new runtime was not found in active assignments")
            self.budget()
            self.profile["expires_at"] = self.work_expiry
            private_json(self.state / "model-profile.json", self.profile)
            for local, remote in ((self.state / "model-profile.json", REMOTE_PROFILE),
                                  (ROOT / "src/mainframe_rag/serve/colab_profiles.py", "/content/colab_profiles.py")):
                self.colab("upload", "-s", self.manifest["session"], str(local), remote)
            if self.config["inference_lock"]:
                self.colab("upload", "-s", self.manifest["session"], str(self.state / "inference.lock"), "/content/inference.lock")
            preflight = self.colab("exec", "-s", self.manifest["session"], "--timeout", "180", "--env",
                                   f"RAG_COLAB_PROFILE={REMOTE_PROFILE}", "-f", str(ROOT / "scripts/colab_preflight.py"),
                                   seconds=180, allow_failure=True)
            census = self.state / "preflight.json"
            self.colab("download", "-s", self.manifest["session"], "/content/colab-preflight.json", str(census))
            observed = json.loads(census.read_text())
            admit(self.profile, observed)
            if preflight.returncode or not observed.get("admitted"):
                raise LabError("remote preflight refused the assigned resources")
            self.save("remote_resources_admitted")
            self.runtime = self.spawn([self.tools["colab"], "--auth", "oauth2", "exec", "-s", self.manifest["session"],
                                       "--timeout", str(max(1, int(self.work_expiry - time.time()))), "--env",
                                       f"RAG_COLAB_PROFILE={REMOTE_PROFILE}", "-f", str(ROOT / "scripts/colab_runtime.py")], "runtime")
            self.diagnostic_stage("transport_ready")
            self.transport(bridge)
            control = self.state / "transport-control"
            control.write_text("verified\n")
            self.colab("upload", "-s", self.manifest["session"], str(control), "/content/colab-transport-ok")
            self.diagnostic_stage("ready")
            child = self.spawn(workload or (["/bin/true"] if not self.profile["accelerator"]
                                            else ["sh", "scripts/tools/run-task.sh", "local:stack"]), "workload", env=self.environment(bridge))
            self.workload = child
            self.manifest["phase"] = "running"
            self.save("local_workload_started")
            next_budget = time.time()
            while child.poll() is None:
                if time.time() >= self.work_expiry:
                    raise LabError("run work deadline/CU envelope reached")
                if self.tunnel.poll() is not None or self.runtime.poll() is not None:
                    raise LabError("owned model transport/runtime was lost")
                if time.time() >= next_budget:
                    self.budget()
                    next_budget = time.time() + 30
                time.sleep(self.config["poll_seconds"])
            if self.tunnel.poll() is not None or self.runtime.poll() is not None:
                raise LabError("owned model transport/runtime was lost before workload completion")
            self.check_qdrant()
            status = child.returncode
            self.manifest["workload_succeeded"] = status == 0
            self.save("local_workload_finished")
        except (LabError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            status = 1
            self.manifest["failure_type"] = type(exc).__name__
            self.save("run_failed")
            print("Colab run failed; private evidence and exact recovery command retained.", file=sys.stderr)
        except KeyboardInterrupt:
            status = 130
            self.save("run_interrupted")
        finally:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            cleanup_ok = self.cleanup()
            if not cleanup_ok:
                status = status or 1
            if (status == 0 and cleanup_ok and not self.profile["accelerator"]
                    and self.manifest.get("workload_succeeded") is True and self.manifest.get("released") is True
                    and self.manifest.get("release_evidence", {}).get("absent") is True):
                try:
                    receipt = {"schema": 1, "kind": "colab-cpu-transport",
                               "candidate": self.manifest["prepared_identity"]["candidate"],
                               "cpu_profile": cpu_transport_profile(self.profile),
                               "tools_sha256": {name: self.manifest["prepared_identity"]["tools_sha256"][name]
                                                for name in TRANSPORT_TOOLS},
                               "transport": self.manifest["transport"], "session": self.manifest["session"],
                               "endpoint": self.manifest["endpoint"], "workload_succeeded": True,
                               "released": True, "release_evidence": self.manifest["release_evidence"],
                               "completed_at": time.time()}
                    if len(json.dumps(receipt).encode()) > PREREQUISITE_LIMITS["transport_receipt"] // 2:
                        raise LabError("CPU transport receipt exceeded its bounded envelope")
                    private_json(self.state / "transport-receipt.json", receipt)
                    self.save("cpu_transport_receipt_created_after_release")
                except (LabError, OSError, KeyError, ValueError):
                    status = 1
                    self.save("cpu_transport_receipt_unavailable")
        return status


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="local files only; never allocate or authenticate")
    prep.add_argument("--state", type=Path, required=True)
    prep.add_argument("--profile", choices=COLAB_PROFILES, default="COLAB_G4_BF16")
    prep.add_argument("--dataset", choices=("production", "synthetic"), default="production")
    prep.add_argument("--gateway-image", required=True)
    for name in ("corpus", "golden", "inventory", "data-state", "kubeconfig", "inference-lock", "transport-receipt"):
        prep.add_argument("--" + name, type=Path)
    for name in ("kube-context", "namespace", "qdrant-service"):
        prep.add_argument("--" + name)
    for name, default in (("colab", "colab"), ("ssh", "ssh"), ("ssh-keygen", "ssh-keygen"),
                          ("docker", "docker"), ("kubectl", "kubectl")):
        prep.add_argument("--" + name, default=default)
    prep.add_argument("--reasoning-port", type=int, default=8000)
    prep.add_argument("--embedding-port", type=int, default=8001)
    prep.add_argument("--qdrant-port", type=int, default=16333)
    prep.add_argument("--embedding-max-len", type=int, default=4096)
    prep.add_argument("--deadline-seconds", type=int, default=7200)
    prep.add_argument("--cu-cap", type=float, default=20)
    prep.add_argument("--cu-floor", type=float, default=10)
    prep.add_argument("--poll-seconds", type=float, default=1)
    for name in ("run", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--state", type=Path, required=True)
        if name == "run":
            command.add_argument("--approve-cu", type=float, required=True)
            command.add_argument("workload", nargs=argparse.REMAINDER)
    return result


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare":
            return prepare(args)
        run = Run(args.state)
        with assignment_lock(), (run.state / "run.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LabError("another controller owns this run; interrupt it before recovery") from exc
            if args.command == "stop":
                run.expiry = time.time() + 60
                return 0 if run.cleanup() else 1
            signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            workload = args.workload[1:] if args.workload[:1] == ["--"] else args.workload
            return run.run(args.approve_cu, workload)
    except (LabError, OSError, ValueError, KeyError, subprocess.SubprocessError):
        print("Colab controller refused invalid/missing prerequisites; no acceptance claimed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
