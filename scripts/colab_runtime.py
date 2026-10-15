#!/usr/bin/env python3
"""Bounded, standard-library Colab inference supervisor for issue #706.

Executed as notebook source: configuration comes from RAG_COLAB_PROFILE, never
__file__ or notebook argv. CPU transport installs nothing. GPU bootstrap requires
an operator-supplied immutable, hash-locked wheel set and explicit transport
approval. Only bounded model diagnostics are exported; corpus data stays local.
"""
from __future__ import annotations

import csv
import hashlib
import http.server
import io
import json
import math
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

LOG_LIMIT_BYTES = 64 * 1024
DIAGNOSTIC_LIMIT_BYTES = 64 * 1024
ARCHIVE_LIMIT_BYTES = 1024 * 1024
HTTP_LIMIT_BYTES = 1024 * 1024
LOCK_LIMIT_BYTES = 1024 * 1024
MAX_DISK_GROWTH_BYTES = 112 * 1024**3
MIN_DISK_FREE_BYTES = 8 * 1024**3
MODEL_LIMIT_BYTES = 80 * 1024**3
MAX_CHILDREN = 12
SAMPLE_INTERVAL_SECONDS = 5
DIAGNOSTIC_INTERVAL_SECONDS = 30


class StopRequested(RuntimeError):
    """The owning host requested an ordinary bounded shutdown."""


def request_json(url: str, body: dict | None = None, *, timeout: float = 10) -> dict:
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError("model endpoint did not return HTTP 200")
        raw = response.read(HTTP_LIMIT_BYTES + 1)
    if len(raw) > HTTP_LIMIT_BYTES:
        raise RuntimeError("model response exceeded its bounded diagnostic size")
    # vLLM /health returns a successful empty response.
    result = json.loads(raw) if raw else {}
    if not isinstance(result, dict):
        raise TypeError("model endpoint did not return a JSON object")
    return result


def warmup_reasoning(server: dict, *, http=request_json, timeout: float = 30) -> None:
    result = http(f"http://127.0.0.1:{server['port']}/v1/chat/completions", {
        "model": server["model_id"], "messages": [{"role": "user", "content": "Return the word READY."}],
        "max_tokens": 512, "reasoning_effort": "low", "temperature": 0, "stream": False,
    }, timeout=timeout)
    choices = result.get("choices")
    first = choices[0] if isinstance(choices, list) and len(choices) == 1 else {}
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip() or first.get("finish_reason") != "stop":
        raise RuntimeError("reasoning warmup requires completed final content")


def warmup_embedding(server: dict, *, http=request_json, timeout: float = 30) -> None:
    result = http(f"http://127.0.0.1:{server['port']}/v1/embeddings", {
        "model": server["model_id"], "input": ["Colab embedding readiness."],
    }, timeout=timeout)
    data = result.get("data")
    first = data[0] if isinstance(data, list) and len(data) == 1 else {}
    vector = first.get("embedding") if isinstance(first, dict) else None
    if not isinstance(vector, list) or len(vector) != 4096:
        raise RuntimeError("embedding warmup requires native dimension 4096")
    if not all(type(value) in (int, float) and math.isfinite(value) for value in vector):
        raise RuntimeError("embedding warmup requires finite normalized vectors")
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isclose(norm, 1, abs_tol=1e-3):
        raise RuntimeError("embedding warmup requires normalized vectors")


def validate_inference_lock(path: Path, expected_digest: str | None) -> dict[str, str]:
    if not expected_digest or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise RuntimeError("GPU inference requires the supplied immutable lock hash")
    # Inspect the opened file, rather than a path that can change between stat
    # and read. A replaced FIFO must not block preallocation or bootstrap.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_size > LOCK_LIMIT_BYTES:
            raise RuntimeError("GPU inference requires a bounded supplied lock file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(LOCK_LIMIT_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > LOCK_LIMIT_BYTES:
        raise RuntimeError("GPU inference requires a bounded supplied lock file")
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise RuntimeError("inference lock hash does not match the approved profile")
    text = re.sub(r"\\\s*\n", " ", raw.decode())
    pins = {}
    for line in text.splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        # No editable/VCS/source URLs, unpinned dependencies or pip options that
        # can relax wheel/hash enforcement. Use the reference wheel index.
        match = re.fullmatch(
            r"([A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?)==([^\s]+)"
            r"((?:\s+--hash=sha256:[0-9a-f]{64})+)", line,
        )
        # Exact PEP 440 versions, including CUDA local versions, epochs and
        # pre/post/dev releases. Legacy arbitrary strings are not pip pins.
        if not match or not re.fullmatch(
            r"v?(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*"
            r"(?:[-_.]?(?:alpha|a|beta|b|preview|pre|c|rc)[-_.]?[0-9]*)?"
            r"(?:-[0-9]+|[-_.]?(?:post|rev|r)[-_.]?[0-9]*)?"
            r"(?:[-_.]?dev[-_.]?[0-9]*)?"
            r"(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?", match[2], re.IGNORECASE | re.ASCII,
        ):
            raise RuntimeError("every inference requirement must be pinned and sha256 hashed")
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in pins:
            raise RuntimeError("duplicate inference package pin")
        pins[name] = match[2]
    if pins.get("vllm") != "0.28.0" or pins.get("transformers") != "5.15.0":
        raise RuntimeError("inference lock does not contain the declared reference versions")
    if not {"torch", "huggingface-hub"}.issubset(pins):
        raise RuntimeError("inference lock lacks required runtime/model-download pins")
    return pins


class BoundedLog:
    """Write at most one fixed-size log; drain and discard excess pipe output."""

    def __init__(self, path: Path, limit: int = LOG_LIMIT_BYTES):
        self.path = path
        self.limit = limit
        self.output = bytearray()
        self.truncated = False
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(descriptor, "wb")

    def write(self, data: bytes) -> None:
        kept = data[:max(0, self.limit - len(self.output))]
        self.stream.write(kept)
        self.output.extend(kept)
        self.truncated |= len(kept) != len(data)

    def close(self) -> None:
        self.stream.close()


def package_diagnostics(root: Path, logs: list[Path]) -> Path:
    """Package explicit finite members only; never walk model/corpus directories."""
    destination = root / "colab-artifacts.tgz"
    temporary = root / "colab-artifacts.tgz.tmp"
    destination.unlink(missing_ok=True)
    members = [root / "colab-diagnostics.json", *logs]
    if len(logs) > MAX_CHILDREN:
        raise RuntimeError("too many bounded diagnostic logs")
    payloads = []
    for member in members:
        limit = DIAGNOSTIC_LIMIT_BYTES if member.name == "colab-diagnostics.json" else LOG_LIMIT_BYTES
        if member.is_symlink() or not member.is_file() or not member.resolve().is_relative_to(root.resolve()):
            raise RuntimeError("diagnostic members must be owned bounded files")
        if member.stat().st_size > limit:
            raise RuntimeError("diagnostic member exceeded its bounded size")
        raw = member.read_bytes()
        if len(raw) > limit:
            raise RuntimeError("diagnostic member grew beyond its bounded size")
        payloads.append((member.relative_to(root).as_posix(), raw))
    try:
        # All input members together are below 1 MiB; the in-memory compressed
        # object also has a fixed bound checked before any host download.
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, raw in payloads:
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(raw))
        if buffer.tell() > ARCHIVE_LIMIT_BYTES:
            raise RuntimeError("diagnostic archive exceeded its bounded size")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(buffer.getvalue())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    return destination


def terminate_group(process: subprocess.Popen) -> None:
    """Reap the child and descendants in the new session we created."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=2)
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    # The leader may have exited while a descendant ignored TERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


class Child:
    def __init__(self, process: subprocess.Popen, log: BoundedLog):
        self.process = process
        self.log = log
        self.error: str | None = None
        self.closed = False
        self.thread = threading.Thread(target=self.drain, daemon=True)
        self.thread.start()

    def drain(self) -> None:
        try:
            assert self.process.stdout is not None
            while data := self.process.stdout.read(8192):
                self.log.write(data)
        except OSError as exc:
            self.error = type(exc).__name__
        finally:
            self.log.close()


class ProcessSupervisor:
    """Finite process/output adapter; no shell, retry, or detached descendants."""

    def __init__(self, directory: Path, *, guard, popen=subprocess.Popen,
                 terminate=terminate_group, clock=time.time, sleep=time.sleep,
                 env: dict[str, str] | None = None):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.guard = guard
        self.popen = popen
        self.terminate = terminate
        self.clock = clock
        self.sleep = sleep
        self.env = env
        self.children: list[Child] = []

    def start(self, argv: list[str], name: str) -> Child:
        self.guard()
        if len(self.children) >= MAX_CHILDREN or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
            raise RuntimeError("remote subprocess count/name exceeds its bounded envelope")
        if any(child.log.path.name == name + ".log" for child in self.children):
            raise RuntimeError("remote diagnostic log identity already exists")
        log = BoundedLog(self.directory / (name + ".log"))
        try:
            process = self.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True,
                                 cwd=self.directory.parent, env=self.env)
        except BaseException:
            log.close()
            raise
        try:
            child = Child(process, log)
        except BaseException:
            self.terminate(process)
            if process.stdout is not None:
                process.stdout.close()
            log.close()
            raise
        self.children.append(child)
        return child

    def close(self, child: Child) -> None:
        if child.closed:
            return
        self.terminate(child.process)
        child.thread.join(timeout=2)
        if child.thread.is_alive():
            raise RuntimeError("remote child output drain did not stop")
        if child.process.stdout is not None:
            child.process.stdout.close()
        child.closed = True

    def run(self, argv: list[str], name: str, *, seconds: float) -> bytes:
        child = self.start(argv, name)
        until = self.clock() + seconds
        try:
            while child.process.poll() is None:
                self.guard()
                if child.error:
                    raise RuntimeError("remote child output drain failed")
                if self.clock() >= until:
                    raise RuntimeError("remote subprocess timeout")
                self.sleep(.05)
            if child.process.returncode:
                raise RuntimeError("remote subprocess failed; bounded diagnostic retained")
        finally:
            self.close(child)
        if child.error:
            raise RuntimeError("remote child output drain failed")
        return bytes(child.log.output)

    def stop_all(self) -> None:
        failed = False
        for child in reversed(self.children):
            try:
                self.close(child)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                failed = True
        if failed:
            raise RuntimeError("one or more owned remote process groups did not stop")


class TinyHTTPServer(http.server.HTTPServer):
    # Two listener threads total. A half-open client cannot hold a request forever.
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(1)
        return connection, address


class TransportHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        body = b'{"status":"ok","transport_only":true}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        # Request paths and bodies never enter model diagnostics.
        pass


def observe_resources(gpu: bool, *, root: Path = Path("/content")) -> dict[str, Any]:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0]) / 1024
    report: dict[str, Any] = {"python_supervisor": platform.python_version(),
                              "available_ram_mib": memory["MemAvailable"],
                              "free_disk_mib": shutil.disk_usage(root).free / 1024**2}
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            report["supervisor_peak_rss_kib"] = int(line.split()[1])
    if gpu:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
             "--format=csv,noheader,nounits"], check=True, capture_output=True, text=True, timeout=5,
        )
        rows = list(csv.reader(result.stdout.strip().splitlines()))
        if len(rows) != 1 or len(rows[0]) != 4:
            raise RuntimeError("reference runtime requires exactly one observed GPU")
        name, total, used, driver = rows[0]
        report.update(gpu_name=name.strip(), vram_mib=float(total), gpu_used_mib=float(used),
                      driver_version=driver.strip())
    return report


SOFTWARE_PROBE = r'''
import importlib.metadata as metadata, json, pathlib, platform, sys, torch
pathlib.Path(sys.argv[1]).write_text(json.dumps({"python": platform.python_version(), "vllm": metadata.version("vllm"),
                  "transformers": metadata.version("transformers"), "torch": metadata.version("torch"),
                  "tokenizers": metadata.version("tokenizers"),
                  "huggingface_hub": metadata.version("huggingface-hub"), "cuda_build": torch.version.cuda,
                  "cuda_available": torch.cuda.is_available()}))
'''

SNAPSHOT_FETCH = r'''
import fnmatch, hashlib, importlib.util, json, pathlib, shutil, sys, time
from huggingface_hub import HfApi, constants, snapshot_download
model, revision, destination, budget, report = sys.argv[1:]
budget = int(budget)
patterns = ["*.safetensors", "*.json", "*.txt", "*.model", "*.tiktoken", "*.jinja", "LICENSE*"]
info = HfApi().model_info(model, revision=revision, files_metadata=True)
if info.sha != revision:
    raise RuntimeError("immutable model revision did not resolve exactly")
selected = [item for item in info.siblings if any(fnmatch.fnmatch(item.rfilename, p) for p in patterns)]
if not selected or any(item.size is None for item in selected):
    raise RuntimeError("cannot bound the pinned snapshot download")
download_bytes = sum(item.size for item in selected)
if download_bytes > budget or shutil.disk_usage(pathlib.Path(destination).parent.parent).free < download_bytes + 8 * 1024**3:
    raise RuntimeError("pinned snapshot exceeds the admitted disk budget")
# Xet is the locked hub's own transfer path (hf-xet is in the inference lock);
# the elapsed time and transfer are recorded so each session measures startup.
transfer = "xet" if not constants.HF_HUB_DISABLE_XET and importlib.util.find_spec("hf_xet") else "http"
started = time.monotonic()
snapshot_download(model, revision=revision, local_dir=destination,
                  allow_patterns=patterns, max_workers=8)
download_seconds = time.monotonic() - started
root = pathlib.Path(destination)
index = json.loads((root / "model.safetensors.index.json").read_text())
shards = sorted(set(index["weight_map"].values()))
paths = [root / name for name in shards]
if any(path.is_symlink() or not path.resolve().is_relative_to(root.resolve())
       or not path.is_file() or path.stat().st_size <= 0 for path in paths):
    raise RuntimeError("pinned snapshot tensor coverage is incomplete")
tensor_bytes = sum(path.stat().st_size for path in paths)
if tensor_bytes < index["metadata"]["total_size"] or tensor_bytes > budget:
    raise RuntimeError("pinned snapshot tensor bytes are incompatible")
for name in ("tokenizer_config.json", "tokenizer.json", "config.json"):
    if not (root / name).is_file():
        raise RuntimeError("pinned snapshot tokenizer/config coverage is incomplete")
if model == "Qwen/Qwen3-Embedding-8B":
    modules = json.loads((root / "modules.json").read_text())
    pooling = [module["path"] for module in modules if module["type"].endswith(".Pooling")]
    if not pooling or any(not (root / path / "config.json").is_file() for path in pooling):
        raise RuntimeError("pinned snapshot pooling files are missing")
hashes = {}
hashed_bytes = 0
for path in sorted(root.rglob("*")):
    if not path.is_file() or ".cache" in path.relative_to(root).parts:
        continue
    if path.suffix not in (".json", ".txt", ".model", ".tiktoken", ".jinja"):
        continue
    if path.is_symlink() or path.stat().st_size > 32 * 1024**2:
        raise RuntimeError("snapshot metadata exceeds its bounded envelope")
    hashed_bytes += path.stat().st_size
    if hashed_bytes > 64 * 1024**2 or len(hashes) >= 64:
        raise RuntimeError("snapshot metadata exceeds its bounded envelope")
    with path.open("rb") as stream:
        hashes[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
pathlib.Path(report).write_text(json.dumps({"model_id": model, "resolved_revision": info.sha, "download_bytes": download_bytes,
                  "tensor_bytes": tensor_bytes, "tensor_shards": len(shards), "metadata_sha256": hashes,
                  "transfer": transfer, "download_seconds": download_seconds}))
'''


# Exact UTF-8 source executed by the qualified interpreter; its digest identifies
# the producer independently of its frozen wrapper/candidate. Facts grant no trust.
POOLING_PROBE = r'''
"""Private #706 inline producer body; execute only through the native supervisor.

Inputs: qualified_python -c SOURCE native_root frozen_candidate_sha.
No engines, tensor reads, downloads, connections, resource owner or approval.
"""
import contextlib
import dataclasses
import hashlib
import json
import os
import platform
import re
import stat
import sys
import tempfile
from importlib import metadata
from pathlib import Path

JSON_BYTES, FILE_BYTES, TOTAL_BYTES = 64 * 1024, 32 * 1024**2, 64 * 1024**2
PACKAGES = ("vllm", "transformers", "tokenizers", "torch", "huggingface-hub")
SUFFIXES = {".json", ".txt", ".model", ".tiktoken", ".jinja"}


class ObservationError(ValueError):
    """Only fixed codes constructed by this program may be exported."""


def require(condition, code):
    if not condition:
        raise ObservationError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read(path, maximum=JSON_BYTES):
    require(path.is_absolute() and path.resolve(strict=True) == path, "input_not_canonical")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= maximum, "input_unbounded")
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    require(len(raw) == before.st_size and stamp(before) == stamp(after), "input_changed")
    return raw


def decode(raw):
    def fields(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate_json_field")
            result[key] = value
        return result

    def invalid(_value):
        raise ObservationError("nonfinite_json")

    return json.loads(raw, object_pairs_hook=fields, parse_constant=invalid)


def inventory(directory):
    """Native snapshot metadata selection and bounds; never open tensor shards."""
    require(directory.resolve(strict=True) == directory, "model_directory_not_canonical")
    pending, names, entries = [directory], set(), 0
    while pending:
        with os.scandir(pending.pop()) as children:
            for item in children:
                entries += 1
                require(entries <= 256 and not item.is_symlink(), "snapshot_entry_unsupported")
                path = Path(item.path)
                relative = path.relative_to(directory)
                if ".cache" in relative.parts:
                    continue
                if item.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif item.is_file(follow_symlinks=False) and path.suffix in SUFFIXES:
                    names.add(relative.as_posix())
    require(1 <= len(names) <= 64, "snapshot_metadata_set_unbounded")
    result, total = {}, 0
    for name in sorted(names):
        path = directory / name
        require(path.resolve(strict=True) == path, "snapshot_metadata_not_canonical")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= FILE_BYTES,
                    "snapshot_metadata_file_unbounded")
            total += before.st_size
            require(total <= TOTAL_BYTES, "snapshot_metadata_total_unbounded")
            hasher, size = hashlib.sha256(), 0
            while chunk := stream.read(65536):
                size += len(chunk)
                require(size <= before.st_size, "snapshot_metadata_changed")
                hasher.update(chunk)
            after = os.fstat(stream.fileno())
        require(size == before.st_size and stamp(before) == stamp(after), "snapshot_metadata_changed")
        result[name] = {"bytes": size, "sha256": hasher.hexdigest()}
    return result


def install(path, value):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    require(len(raw) <= JSON_BYTES, "output_exceeds_bound")
    descriptor, temporary = tempfile.mkstemp(prefix=".pooling-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Exclusive complete-record publication; never replace.
    finally:
        temporary.unlink(missing_ok=True)


def observe():
    invocation = tuple(sys.orig_argv)
    require(len(invocation) == 5 and invocation[1] == "-c"
            and sys.argv == ["-c", invocation[3], invocation[4]], "unsupported_invocation")
    root, candidate = Path(sys.argv[1]), sys.argv[2]
    require(root.is_absolute() and root.resolve(strict=True) == root and root.is_dir(), "invalid_native_root")
    require(re.fullmatch(r"[0-9a-f]{40}", candidate), "invalid_candidate_identity")
    producer = {"development_sha": candidate,
                "source_sha256": hashlib.sha256(invocation[2].encode("utf-8")).hexdigest()}
    output = root / "pooling-observed.json"
    require(not output.exists() and not output.is_symlink(), "output_already_exists")
    paths = {name: root / filename for name, filename in (
        ("profile", "model-profile.json"), ("software", "software-observed.json"),
        ("snapshot", "snapshot-embed.json"))}
    inputs = {name: read(path) for name, path in paths.items()}
    profile, software, snapshot = (decode(inputs[name]) for name in ("profile", "software", "snapshot"))
    require(all(type(value) is dict for value in (profile, software, snapshot)), "native_inputs_not_objects")
    require((profile.get("name"), profile.get("accelerator")) in (
        ("COLAB_G4_BF16", "G4"), ("COLAB_A100_80GB_BF16", "A100"))
        and profile.get("vllm_version") == "0.28.0" and profile.get("transformers_version") == "5.15.0"
        and profile.get("python_version") == "3.12" and profile.get("dense_dim") == 4096,
        "unsupported_native_profile")
    servers = [server for server in profile["servers"] if server["role"] == "embed"]
    require(len(servers) == 1, "native_embedding_server_ambiguous")
    server, directory = servers[0], root / "models/embedding"
    argv = server["argv"]
    require(server["local_dir"] == str(directory) and type(argv) is list
            and all(type(value) is str and "\0" not in value for value in argv)
            and argv[:3] == [str(root / "rag-vllm/bin/vllm"), "serve", str(directory)]
            and argv.count("--max-model-len") == 1, "native_embedding_argv_unsupported")
    window = int(argv[argv.index("--max-model-len") + 1])
    require(4096 <= window <= 32768, "native_window_unbounded")
    require(type(server["model_id"]) is str and re.fullmatch(r"[0-9a-f]{40}", server["revision"])
            and snapshot["model_id"] == server["model_id"]
            and snapshot["resolved_revision"] == server["revision"], "snapshot_identity_differs")
    lock = read(root / "inference.lock", 1024**2)
    lock_sha256 = hashlib.sha256(lock).hexdigest()
    require(lock_sha256 == profile["inference_lock_sha256"], "native_lock_identity_differs")
    files = inventory(directory)
    require(snapshot["metadata_sha256"] == {name: row["sha256"] for name, row in files.items()},
            "actual_snapshot_metadata_differs")

    def packages():
        result = {name: metadata.version(name) for name in PACKAGES}
        require(result == {name: software[name.replace("-", "_")] for name in PACKAGES}
                and result["vllm"] == "0.28.0" and result["transformers"] == "5.15.0"
                and platform.python_implementation() == "CPython" and sys.version_info[:2] == (3, 12)
                and platform.python_version() == software["python"] and Path(sys.prefix) == root / "rag-vllm",
                "qualified_runtime_identity_differs")
        return result

    installed = packages()
    require(all(name not in sys.modules for name in ("vllm", "transformers", "huggingface_hub")),
            "inference_library_loaded_before_offline_controls")
    permitted = {"VLLM_NO_USAGE_STATS": "1", "VLLM_DO_NOT_TRACK": "1", "VLLM_LOGGING_LEVEL": "ERROR"}
    require(all(not name.startswith("VLLM_") or name in permitted for name in os.environ),
            "unsupported_vllm_environment_override")
    plugins = list(metadata.entry_points(group="vllm.general_plugins"))
    require({entry.name: entry.value for entry in plugins} == {
        "lora_filesystem_resolver": "vllm.plugins.lora_resolvers.filesystem_resolver:register_filesystem_resolver",
        "lora_hf_hub_resolver": "vllm.plugins.lora_resolvers.hf_hub_resolver:register_hf_hub_resolver"}
        and len(plugins) == 2 and all(entry.dist is not None and entry.dist.metadata["Name"] == "vllm"
            and entry.dist.version == installed["vllm"] for entry in plugins)
        and not metadata.entry_points(group="vllm.platform_plugins"), "unreviewed_vllm_plugin_present")
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                       "DO_NOT_TRACK": "1", "TOKENIZERS_PARALLELISM": "false", **permitted})
    from huggingface_hub import constants as hub_constants
    from transformers import Qwen2Tokenizer, Qwen2TokenizerFast
    from vllm.engine.arg_utils import EngineArgs
    from vllm.entrypoints.openai.cli_args import make_arg_parser, validate_parsed_serve_args
    from vllm.entrypoints.pooling.embed.protocol import EmbeddingCompletionRequest
    from vllm.exceptions import VLLMValidationError
    from vllm.tokenizers.hf import CachedHfTokenizer
    from vllm.utils.argparse_utils import FlexibleArgumentParser

    require(hub_constants.HF_HUB_OFFLINE is True, "actual_hub_offline_mode_unavailable")
    args = make_arg_parser(FlexibleArgumentParser(description="Offline pooling observation")).parse_args(argv[2:])
    validate_parsed_serve_args(args)
    require(args.model_tag == str(directory), "native_model_tag_missing")
    args.model = args.model_tag  # Native ServeSubcommand precedence; never dispatch serve.
    require(args.trust_remote_code is False and args.runner == "pooling" and args.convert == "embed"
            and args.tokenizer is None and args.hf_config_path is None and args.config is None
            and args.tokenizer_mode == "auto", "unsupported_native_config_options")
    model_config = EngineArgs.from_cli_args(args).create_model_config()  # Never create_engine_config/LLM.
    require(model_config.trust_remote_code is False
            and Path(model_config.model).resolve(strict=True) == directory
            and Path(model_config.tokenizer).resolve(strict=True) == directory, "actual_model_config_escaped_inputs")
    request = EmbeddingCompletionRequest(model=server["model_id"], input="Original MiXeD café α <|im_end|> fixture.")
    params, pooler = request.build_tok_params(model_config), model_config.pooler_config
    require(pooler is not None and model_config.encoder_config is None
            and type(model_config.max_model_len) is int and type(params.max_input_tokens) is int
            and params.max_input_tokens == model_config.max_model_len == window
            and params.truncate_prompt_tokens is None and params.pad_prompt_tokens is None
            and params.do_lower_case is False and params.add_special_tokens is True, "observed_pooling_semantics_unsupported")
    require(Qwen2TokenizerFast is Qwen2Tokenizer, "actual_fast_compatibility_alias_differs")
    local = Qwen2TokenizerFast.from_pretrained(str(directory), local_files_only=True, trust_remote_code=False)
    native = CachedHfTokenizer.from_pretrained(str(directory), local_files_only=True, trust_remote_code=False)
    require(isinstance(local, Qwen2TokenizerFast) and isinstance(native, Qwen2TokenizerFast)
            and local.is_fast is True and native.is_fast is True, "actual_tokenizer_unsupported")
    initial_truncation = {"alias": local.backend_tokenizer.truncation, "native": native.backend_tokenizer.truncation}
    for tokenizer in (local, native):
        tokenizer.backend_tokenizer.no_truncation()
        require(tokenizer.backend_tokenizer.truncation is None, "backend_truncation_remains_enabled")

    def encode(tokenizer, text):
        ids = tokenizer.encode(text, add_special_tokens=params.add_special_tokens, truncation=False)
        require(type(ids) is list and ids and all(type(token) is int and token >= 0 for token in ids)
                and tokenizer.backend_tokenizer.truncation is None, "actual_encoding_unsupported")
        return ids

    text = request.input
    pre = params.apply_pre_tokenization(native, {"prompt": text})["prompt"]
    require(pre == text, "native_plain_string_was_transformed")
    short_ids = encode(local, text)
    require(short_ids == encode(native, pre) and short_ids != encode(local, text.lower())
            and local.backend_tokenizer.encode(text, add_special_tokens=True).ids == short_ids,
            "actual_special_token_or_case_semantics_differ")
    require(params.apply_post_tokenization(native, {"prompt_token_ids": list(short_ids)})["prompt_token_ids"]
            == short_ids, "native_short_tokens_changed")
    local_backend, native_backend = local.backend_tokenizer.to_str(), native.backend_tokenizer.to_str()
    require(len(local_backend.encode("utf-8")) <= FILE_BYTES and len(native_backend.encode("utf-8")) <= FILE_BYTES,
            "backend_serialization_unbounded")
    require(decode(local_backend) == decode(native_backend), "native_and_alias_backends_differ")
    long_text = "OriginalBoundaryMiXeD17 " * (window + 32)  # Window was bounded before multiplication.
    require(len(long_text) <= 2 * 1024**2, "original_probe_exceeds_character_bound")
    long_ids = encode(local, long_text)
    require(len(long_ids) > window, "original_probe_did_not_exceed_window")
    encode_kwargs = params.get_encode_kwargs()
    sentinel_ids = native.encode(long_text, **encode_kwargs)
    require(len(sentinel_ids) == window + 1, "native_refusal_sentinel_differs")
    try:
        params.apply_post_tokenization(native, {"prompt_token_ids": sentinel_ids})
    except VLLMValidationError as exc:
        require(exc.parameter == "input_tokens" and exc.value == len(sentinel_ids), "unexpected_native_overflow_refusal")
    else:
        raise ObservationError("native_oversize_input_was_accepted")
    runtime = {name: installed[name] for name in ("vllm", "transformers", "tokenizers", "torch")}
    observation = {
        "implementation": type(request).__module__ + "." + type(request).__qualname__, "version": installed["vllm"],
        "model_config": {"max_model_len": model_config.max_model_len, "encoder_config": model_config.encoder_config,
                         "pooler_config": {name: getattr(pooler, name) for name in (
                             "enable_chunked_processing", "max_embed_len", "seq_pooling_type", "use_activation")}},
        "counting_options": {"add_special_tokens": params.add_special_tokens, "do_lower_case": params.do_lower_case,
                             "truncation": params.truncate_prompt_tokens is not None},
        "effective_window": params.max_input_tokens,
    }
    manifest = {
        "schema": 1, "model": {"model": server["model_id"], "revision": snapshot["resolved_revision"]},
        "tokenizer": {"implementation": "transformers.Qwen2TokenizerFast", "version": installed["transformers"]},
        "runtime": runtime, "server_argv": argv, "files": files, "observation": observation,
        "provenance": {"kind": "offline-vllm-pooling-observation", "producer": producer,
                       "resolved_revision": snapshot["resolved_revision"], "files_sha256": digest(files),
                       "runtime_sha256": digest(runtime), "server_argv_sha256": digest(argv)},
    }
    facts = {
        "producer": producer, "packages": installed, "python": platform.python_version(),
        **{name + "_sha256": hashlib.sha256(raw).hexdigest() for name, raw in inputs.items()},
        "inference_lock_sha256": lock_sha256, "actual_hub_offline": hub_constants.HF_HUB_OFFLINE,
        "actual_trust_remote_code": model_config.trust_remote_code,
        "actual_runner_type": model_config.runner_type, "actual_convert_type": model_config.convert_type,
        "tokenizer_alias_target": Qwen2TokenizerFast.__module__ + "." + Qwen2TokenizerFast.__qualname__,
        "native_tokenizer_class": type(native).__module__ + "." + type(native).__qualname__,
        "tokenize_params": dataclasses.asdict(params), "native_encode_kwargs": encode_kwargs,
        "initial_backend_truncation": initial_truncation, "backend_equivalence": True,
        "short_tokens": len(short_ids), "short_ids_sha256": digest(short_ids),
        "full_nontruncated_probe_tokens": len(long_ids), "full_probe_ids_sha256": digest(long_ids),
        "native_refusal_sentinel_tokens": len(sentinel_ids), "oversize_native_refusal": True,
        "logical_truncation_requested": False, "metadata_files": len(files),
        "metadata_bytes": sum(row["bytes"] for row in files.values()),
    }
    require(inventory(directory) == files and packages() == installed and tuple(sys.orig_argv) == invocation
            and all(read(paths[name]) == raw for name, raw in inputs.items())
            and read(root / "inference.lock", 1024**2) == lock, "inputs_or_producer_changed_during_observation")
    install(output, {"schema": 1, "status": "observed", "trust": "unreviewed", "acceptance_certified": False,
                     "manifest": manifest, "facts": facts})


try:
    # Library diagnostics are not evidence; supervisor still bounds/drains the OS stream.
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        observe()
except BaseException as exc:  # noqa: BLE001 - fixed failures; the supervisor owns interruption.
    print(json.dumps({"status": "failed", "code": str(exc) if isinstance(exc, ObservationError)
                      else "actual_observation_failed"}, sort_keys=True))
    raise SystemExit(1)
print(json.dumps({"status": "observed", "acceptance_certified": False}, sort_keys=True))
'''


class RemoteRuntime:
    """One session's transport/bootstrap/model/process lifecycle."""

    def __init__(self, profile: dict, *, root: Path = Path("/content"), http=request_json,
                 popen=subprocess.Popen, server_factory=TinyHTTPServer,
                 observe=None, admission=None, terminate=terminate_group,
                 clock=time.time, sleep=time.sleep, development_sha: str | None = None):
        self.profile = profile
        self.root = root
        self.development_sha = development_sha
        self.http = http
        self.server_factory = server_factory
        self.observe = observe or (lambda gpu: observe_resources(gpu, root=root))
        self.admission = admission
        self.clock = clock
        self.sleep = sleep
        self.gpu = bool(profile.get("accelerator"))
        self.deadline = float(profile.get("expires_at", 0))
        self.transport_servers: list[tuple[Any, threading.Thread]] = []
        self.background: list[Child] = []
        self.start_free = 0
        self.last_sample = -math.inf
        self.last_report = -math.inf
        self.report_writes = 0
        self.report: dict[str, Any] = {
            "schema": 1, "profile": profile.get("name"), "stage": "starting",
            "transport_verified": False, "started_at": clock(), "expires_at": self.deadline,
            "events": [], "peak_resources": {},
            "limits": {"log_bytes": LOG_LIMIT_BYTES, "archive_bytes": ARCHIVE_LIMIT_BYTES,
                       "diagnostic_bytes": DIAGNOSTIC_LIMIT_BYTES,
                       "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
                       "disk_growth_bytes": MAX_DISK_GROWTH_BYTES if self.gpu else 16 * 1024**2},
        }
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith("PIP_")
                       and name not in {"_PIP_RUNNING_IN_SUBPROCESS", "PYTHONHOME", "PYTHONPATH"}}
        # Xet downloads with high-performance concurrency, no chunk cache
        # (the snapshot itself is the only copy) and no transfer telemetry.
        environment.update(HF_HOME=str(root / "hf-cache"), HF_HUB_DISABLE_TELEMETRY="1",
                           HF_HUB_DISABLE_PROGRESS_BARS="1", HF_HUB_DISABLE_XET="0",
                           HF_XET_HIGH_PERFORMANCE="1", HF_XET_CHUNK_CACHE_SIZE_BYTES="0",
                           HF_XET_TELEMETRY_ENABLED="false",
                           HF_HUB_ETAG_TIMEOUT="10", HF_HUB_DOWNLOAD_TIMEOUT="30",
                           PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1",
                           PIP_CONFIG_FILE=os.devnull,
                           VLLM_LOGGING_LEVEL="INFO",
                           TMPDIR=str(root / "rag-inference-tmp"))
        self.processes = ProcessSupervisor(root / "rag-colab-logs", guard=self.check,
                                           popen=popen, terminate=terminate,
                                           clock=clock, sleep=sleep, env=environment)

    def write_report(self, stage: str | None = None) -> None:
        if stage is not None:
            self.report["stage"] = stage
            self.report["events"].append({"stage": stage, "time": self.clock()})
        if self.report_writes >= 512 or len(self.report["events"]) > 32:
            raise RuntimeError("remote diagnostic update count exceeded its bounded envelope")
        self.report["updated_at"] = self.clock()
        raw = (json.dumps(self.report, sort_keys=True) + "\n").encode()
        if len(raw) > DIAGNOSTIC_LIMIT_BYTES:
            raise RuntimeError("remote diagnostic size exceeded its bounded envelope")
        temporary = self.root / "colab-diagnostics.json.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        os.replace(temporary, self.root / "colab-diagnostics.json")
        self.report_writes += 1
        self.last_report = self.clock()

    def check(self) -> None:
        if self.clock() >= self.deadline:
            raise RuntimeError("remote absolute wall deadline reached")
        stop = self.root / "colab-stop"
        if stop.exists():
            raise StopRequested("owning host requested stop")
        for child in self.background:
            if child.process.poll() is not None or child.error:
                raise RuntimeError("owned model process exited before session shutdown")
        if self.start_free:
            free = shutil.disk_usage(self.root).free
            growth = self.start_free - free
            limit = MAX_DISK_GROWTH_BYTES if self.gpu else 16 * 1024**2
            floor = MIN_DISK_FREE_BYTES if self.gpu else 1024**2
            if free < floor or growth > limit:
                raise RuntimeError("remote disk headroom/write envelope reached")

    def tick(self) -> None:
        self.check()
        if self.clock() - self.last_sample >= SAMPLE_INTERVAL_SECONDS:
            observed = self.observe(self.gpu)
            if not isinstance(observed, dict):
                raise TypeError("observed remote resources must be an object")
            for field in ("available_ram_mib", "free_disk_mib", *(('vram_mib',) if self.gpu else ())):
                value = observed.get(field)
                if type(value) not in (int, float):
                    raise RuntimeError("cannot verify finite observed remote resources")
                assert isinstance(value, (int, float))
                if not math.isfinite(value) or value <= 0:
                    raise RuntimeError("cannot verify finite observed remote resources")
            if self.gpu:
                driver = observed.get("driver_version")
                if not isinstance(driver, str):
                    raise TypeError("observed NVIDIA driver version must be a string")
                if len(driver) > 64 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", driver):
                    raise RuntimeError("cannot verify the observed NVIDIA driver version")
            if not self.report.get("initial_resources"):
                self.report["initial_resources"] = observed
            self.report["latest_resources"] = observed
            peaks = self.report["peak_resources"]
            for field in ("gpu_used_mib", "supervisor_peak_rss_kib"):
                value = observed.get(field)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    peaks["sampled_" + field] = max(peaks.get("sampled_" + field, 0), value)
            self.last_sample = self.clock()
            if self.gpu and observed.get("available_ram_mib", 0) < 2048:
                raise RuntimeError("remote available RAM safety reserve reached")
        if self.clock() - self.last_report >= DIAGNOSTIC_INTERVAL_SECONDS:
            self.write_report()

    def start_transport(self) -> None:
        for port in (8000, 8001):
            self.check()
            server = self.server_factory(("127.0.0.1", port), TransportHandler)
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": .05}, daemon=True)
            try:
                thread.start()
            except BaseException:
                server.server_close()
                raise
            self.transport_servers.append((server, thread))
        self.write_report("transport_ready")

    def stop_transport(self) -> None:
        failed = False
        for server, thread in reversed(self.transport_servers):
            try:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                failed |= thread.is_alive()
            except OSError:
                failed = True
        self.transport_servers.clear()
        if failed:
            raise RuntimeError("owned transport listener did not stop")

    def wait_transport_approval(self) -> None:
        control = self.root / "colab-transport-ok"
        while True:
            self.tick()
            if control.exists():
                if control.is_symlink() or control.stat().st_size > 64 or control.read_text().strip() != "verified":
                    raise RuntimeError("invalid explicit transport approval")
                self.report["transport_verified"] = True
                return
            self.sleep(.1)

    def install_inference(self) -> Path:
        self.check()
        python = shutil.which("python3.12")
        if python is None:
            raise RuntimeError("preinstalled Python 3.12 is required; no paid bootstrap fallback")
        lock = self.root / "inference.lock"
        pins = validate_inference_lock(lock, self.profile.get("inference_lock_sha256"))
        (self.root / "rag-inference-tmp").mkdir(exist_ok=True)
        venv = self.root / "rag-vllm"
        if venv.exists():
            raise RuntimeError("inference environment already exists; refusing ambiguous reuse")
        self.processes.run([python, "-m", "venv", "--without-pip", str(venv)], "venv", seconds=120)
        interpreter = venv / "bin/python"
        identity = json.loads(self.processes.run(
            [str(interpreter), "-I", "-B", "-c",
             ("import json, platform, sys; print(json.dumps({'python': platform.python_version(), "
             "'implementation': platform.python_implementation(), 'prefix': sys.prefix, "
              "'base_prefix': sys.base_prefix, 'executable': sys.executable}))")],
            "interpreter", seconds=5))
        if (type(identity) is not dict or any(type(identity.get(name)) is not str
                for name in ("python", "implementation", "prefix", "base_prefix", "executable"))
                or not re.fullmatch(r"3\.12\.[0-9]+", identity["python"])
                or identity["implementation"] != "CPython" or identity["prefix"] != str(venv)
                or identity["base_prefix"] == identity["prefix"]
                or identity["executable"] != str(interpreter)):
            raise RuntimeError("fresh inference interpreter identity could not be verified")
        pip = [sys.executable, "-I", "-B", "-m", "pip", "--python", str(interpreter)]
        capability = self.processes.run([*pip, "--version"], "installer", seconds=10)
        version = re.fullmatch(rb"pip ([0-9]+\.[0-9]+(?:\.[0-9]+)?) from [^\r\n]+ \(python 3\.12\)\r?\n?", capability)
        if version is None or tuple(int(part) for part in version[1].split(b".")[:2]) < (22, 3):
            raise RuntimeError("preinstalled kernel pip cannot manage the inference interpreter")
        self.report["installer"] = {"version": version[1].decode(), "target": str(interpreter),
                                    "launcher": sys.executable, "venv": identity}
        self.write_report("inference_environment_ready")
        self.processes.run([*pip, "install", "--require-hashes",
                            "--only-binary=:all:", "--no-deps", "--no-cache-dir", "-r", str(lock)],
                           "install", seconds=1200)
        self.processes.run([*pip, "check"], "dependencies", seconds=30)
        observed = self.root / "software-observed.json"
        self.processes.run([str(interpreter), "-c", SOFTWARE_PROBE, str(observed)], "software", seconds=30)
        software = self.read_observed(observed)
        expected = {"vllm": "0.28.0", "transformers": "5.15.0", "torch": pins["torch"],
                    "huggingface_hub": pins["huggingface-hub"], "cuda_build": "13.0"}
        if "tokenizers" in pins:
            expected["tokenizers"] = pins["tokenizers"]
        for field in ("python", "tokenizers", *expected):
            if not isinstance(software.get(field), str):
                raise TypeError("observed inference software versions must be strings")
        if type(software.get("cuda_available")) is not bool:
            raise TypeError("observed CUDA availability must be a boolean")
        if (len(software["python"]) > 64 or not re.fullmatch(r"3\.12\.[0-9]+", software["python"])
                or not 1 <= len(software["tokenizers"]) <= 64
                or any(software[field] != version for field, version in expected.items())
                or software["cuda_available"] is not True):
            raise RuntimeError("observed inference software does not match the admitted reference")
        # An absolute vLLM entrypoint does not activate its nested console tools.
        assert self.processes.env is not None
        inherited_path = self.processes.env.get("PATH") or os.defpath
        self.processes.env["PATH"] = os.pathsep.join((str(interpreter.parent), inherited_path))
        self.report["software"] = software
        self.write_report("inference_ready")
        return interpreter

    def download_models(self, interpreter: Path) -> None:
        manifests = []
        budget = MODEL_LIMIT_BYTES
        for server in self.profile["servers"]:
            self.tick()
            local = self.root / "models" / ("reasoning" if server["role"] == "reasoning" else "embedding")
            if str(local) != server["local_dir"]:
                raise RuntimeError("model destination differs from the exact profile argv")
            observed = self.root / ("snapshot-" + server["role"] + ".json")
            self.processes.run(
                [str(interpreter), "-c", SNAPSHOT_FETCH, server["model_id"], server["revision"],
                 str(local), str(budget), str(observed)], "snapshot-" + server["role"], seconds=1200,
            )
            manifest = self.read_observed(observed)
            for field in ("model_id", "resolved_revision"):
                if not isinstance(manifest.get(field), str):
                    raise TypeError("observed snapshot identity fields must be strings")
            if manifest["resolved_revision"] != server["revision"] or manifest["model_id"] != server["model_id"]:
                raise RuntimeError("downloaded model identity differs from the immutable profile")
            for field in ("download_bytes", "tensor_bytes", "tensor_shards"):
                if type(manifest.get(field)) is not int:
                    raise TypeError("observed snapshot sizes and shard count must be integers")
            if not (0 < manifest["tensor_shards"] <= manifest["tensor_bytes"]
                    <= manifest["download_bytes"] <= budget):
                raise RuntimeError("downloaded model sizes exceed the declared snapshot envelope")
            if manifest.get("transfer") not in ("xet", "http"):
                raise RuntimeError("observed snapshot transfer is invalid")
            seconds = manifest.get("download_seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                raise TypeError("observed snapshot download time must be a number")
            if not 0 <= seconds <= 1200:
                raise RuntimeError("observed snapshot download time is outside its bound")
            hashes = manifest.get("metadata_sha256")
            if not isinstance(hashes, dict):
                raise TypeError("observed snapshot metadata hashes must be an object")
            if not 1 <= len(hashes) <= 64 or not all(
                    type(name) is str and name and type(digest) is str
                    and re.fullmatch(r"[0-9a-f]{64}", digest) for name, digest in hashes.items()):
                raise RuntimeError("observed snapshot metadata hashes are invalid")
            budget -= manifest["download_bytes"]
            manifests.append(manifest)
        self.report["models"] = manifests
        self.write_report("models_downloaded")

    def read_observed(self, path: Path) -> dict:
        if path.is_symlink() or path.stat().st_size > DIAGNOSTIC_LIMIT_BYTES:
            raise RuntimeError("observed inference metadata exceeded its bounded envelope")
        with path.open("rb") as stream:
            raw = stream.read(DIAGNOSTIC_LIMIT_BYTES + 1)
        if len(raw) > DIAGNOSTIC_LIMIT_BYTES:
            raise RuntimeError("observed inference metadata exceeded its bounded envelope")
        observed = json.loads(raw)
        if not isinstance(observed, dict):
            raise TypeError("observed inference metadata must be a JSON object")
        return observed

    def observe_pooling(self, interpreter: Path) -> None:
        """Generate attributable, unreviewed facts through the existing owner."""
        self.tick()
        candidate = self.development_sha
        if type(candidate) is not str or not re.fullmatch(r"[0-9a-f]{40}", candidate):
            raise RuntimeError("pooling observation requires frozen candidate identity")
        output = self.root / "pooling-observed.json"
        if output.exists() or output.is_symlink():
            raise RuntimeError("pooling observation output already exists")
        seconds = min(120, self.deadline - self.clock())
        if seconds <= 0:
            raise RuntimeError("remote absolute wall deadline reached")
        self.processes.run([str(interpreter), "-c", POOLING_PROBE, str(self.root), candidate],
                           "pooling", seconds=seconds)
        observed = self.read_observed(output)
        # Nested NaN/Infinity must not become diagnostic or manifest facts.
        json.dumps(observed, allow_nan=False)
        if (set(observed) != {"schema", "status", "trust", "acceptance_certified", "manifest", "facts"}
                or type(observed["schema"]) is not int or observed["schema"] != 1
                or observed["status"] != "observed" or observed["trust"] != "unreviewed"
                or observed["acceptance_certified"] is not False):
            raise RuntimeError("pooling observation record is unsupported")
        manifest, facts = observed["manifest"], observed["facts"]
        software = self.report["software"]
        server, = (server for server in self.profile["servers"] if server["role"] == "embed")
        snapshot, = (item for item in self.report["models"] if item["model_id"] == server["model_id"])
        producer = {"development_sha": candidate,
                    "source_sha256": hashlib.sha256(POOLING_PROBE.encode("utf-8")).hexdigest()}
        runtime = {name: software[name] for name in ("vllm", "transformers", "tokenizers", "torch")}
        if (not isinstance(manifest, dict) or not isinstance(facts, dict)
                or set(manifest) != {"schema", "model", "tokenizer", "runtime", "server_argv", "files", "observation", "provenance"}
                or type(manifest["schema"]) is not int or manifest["schema"] != 1
                or manifest["model"] != {"model": server["model_id"], "revision": server["revision"]}
                or manifest["runtime"] != runtime or manifest["server_argv"] != server["argv"]
                or manifest["tokenizer"] != {"implementation": "transformers.Qwen2TokenizerFast", "version": software["transformers"]}
                or not isinstance(manifest["observation"], dict) or facts.get("producer") != producer
                or facts.get("packages") != {name: software[name.replace("-", "_")] for name in (*runtime, "huggingface-hub")}):
            raise RuntimeError("pooling observation differs from qualified native inputs")
        observation = manifest["observation"]
        window = int(server["argv"][server["argv"].index("--max-model-len") + 1])
        if (set(observation) != {"implementation", "version", "model_config", "counting_options", "effective_window"}
                or observation["implementation"] != "vllm.entrypoints.pooling.embed.protocol.EmbeddingCompletionRequest"
                or observation["version"] != software["vllm"]
                or type(observation["effective_window"]) is not int or observation["effective_window"] != window
                or observation["counting_options"] != {"add_special_tokens": True, "do_lower_case": False, "truncation": False}
                or any(type(value) is not bool for value in observation["counting_options"].values())):
            raise RuntimeError("pooling observation request semantics are unsupported")
        config = observation["model_config"]
        if (not isinstance(config, dict) or set(config) != {"max_model_len", "encoder_config", "pooler_config"}
                or type(config["max_model_len"]) is not int or config["max_model_len"] != window
                or config["encoder_config"] is not None or not isinstance(config["pooler_config"], dict)
                or set(config["pooler_config"]) != {"enable_chunked_processing", "max_embed_len", "seq_pooling_type", "use_activation"}):
            raise RuntimeError("pooling observation model configuration is unsupported")
        files = manifest["files"]
        if (not isinstance(files, dict) or not 1 <= len(files) <= 64
                or any(type(name) is not str or not isinstance(row, dict)
                       or set(row) != {"bytes", "sha256"} or type(row["bytes"]) is not int
                       or not 0 < row["bytes"] <= 32 * 1024**2 or type(row["sha256"]) is not str
                       or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) for name, row in files.items())
                or sum(row["bytes"] for row in files.values()) > 64 * 1024**2
                or {name: row["sha256"] for name, row in files.items()} != snapshot["metadata_sha256"]):
            raise RuntimeError("pooling observation metadata differs from the downloaded snapshot")
        def digest(value):
            raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            return hashlib.sha256(raw).hexdigest()
        if manifest["provenance"] != {
                "kind": "offline-vllm-pooling-observation", "producer": producer,
                "resolved_revision": server["revision"], "files_sha256": digest(files),
                "runtime_sha256": digest(runtime), "server_argv_sha256": digest(server["argv"])}:
            raise RuntimeError("pooling observation provenance differs from its executed source")
        self.report["pooling_observation"] = observed
        self.write_report("pooling_observed")

    def wait_model(self, server: dict, child: Child, *, startup_deadline: float) -> None:
        until = min(self.deadline, startup_deadline)
        while self.clock() < until:
            self.tick()
            if child.process.poll() is not None:
                raise RuntimeError("model server exited before readiness")
            try:
                timeout = min(5, max(.01, until - self.clock()))
                self.http(f"http://127.0.0.1:{server['port']}/health", timeout=timeout)
                models = self.http(f"http://127.0.0.1:{server['port']}/v1/models", timeout=timeout)
            except (OSError, ValueError):
                self.sleep(.2)
                continue
            data = models.get("data")
            if not isinstance(data, list) or not any(
                    isinstance(model, dict) and model.get("id") == server["model_id"] for model in data):
                raise RuntimeError("model readiness returned an unexpected served alias")
            return
        raise RuntimeError("model readiness exceeded the remote deadline")

    def start_models(self) -> None:
        for server in self.profile["servers"]:
            self.tick()
            startup_deadline = min(self.deadline, self.clock() + 1200)
            argv = list(server["argv"])
            child = self.processes.start(argv, server["role"])
            self.background.append(child)
            manifest = next(model for model in self.report["models"] if model["model_id"] == server["model_id"])
            exact_argv = json.dumps(argv, sort_keys=True, separators=(",", ":"), allow_nan=False)
            manifest["server_argv_sha256"] = hashlib.sha256(exact_argv.encode()).hexdigest()
            self.wait_model(server, child, startup_deadline=startup_deadline)
            self.check()
            timeout = startup_deadline - self.clock()
            if timeout <= 0:
                raise RuntimeError("model startup exceeded its readiness and warmup deadline")
            if server["role"] == "reasoning":
                warmup_reasoning(server, http=self.http, timeout=timeout)
                stage = "reasoning_warmed"
            else:
                warmup_embedding(server, http=self.http, timeout=timeout)
                stage = "embedding_warmed"
            self.check()
            if self.clock() >= startup_deadline:
                raise RuntimeError("model startup exceeded its readiness and warmup deadline")
            self.write_report(stage)

    def validate_profile(self) -> None:
        duration = self.profile.get("deadline_seconds")
        if (type(duration) is not int or not 1 <= duration <= 7200 or not math.isfinite(self.deadline)
                or not self.clock() < self.deadline <= self.clock() + 7200):
            raise RuntimeError("remote requires a finite absolute deadline within 120 minutes")
        self.deadline = min(self.deadline, self.clock() + duration)
        if not self.gpu:
            if self.profile.get("name") != "COLAB_CPU_TRANSPORT" or self.profile.get("servers"):
                raise RuntimeError("CPU transport cannot contain model processes")
            return
        if (self.profile.get("name"), self.profile.get("accelerator")) not in (
                ("COLAB_G4_BF16", "G4"), ("COLAB_A100_80GB_BF16", "A100")):
            raise RuntimeError("unknown named GPU reference")
        if self.profile.get("python_version") != "3.12" or self.profile.get("cuda_build") != "13.0":
            raise RuntimeError("unsupported inference Python/CUDA reference")
        servers = self.profile.get("servers", [])
        if [server.get("role") for server in servers] != ["reasoning", "embed"]:
            raise RuntimeError("reference requires reasoning followed by embedding")
        for server, model, port, leaf in zip(servers,
                ("Qwen/Qwen3.8-27B", "Qwen/Qwen3-Embedding-8B"), (8000, 8001), ("reasoning", "embedding")):
            if (server["model_id"] != model or server["port"] != port
                    or not re.fullmatch(r"[0-9a-f]{40}", server["revision"])
                    or server["local_dir"] != str(self.root / "models" / leaf)
                    or server["argv"][:3] != [str(self.root / "rag-vllm/bin/vllm"), "serve", server["local_dir"]]
                    or not all(isinstance(value, str) and "\x00" not in value for value in server["argv"])):
                raise RuntimeError("model launch identity differs from the exact reference profile")
            for flag, expected in (("--host", "127.0.0.1"), ("--port", str(port)),
                                   ("--served-model-name", model), ("--dtype", "bfloat16"),
                                   ("--tensor-parallel-size", "1")):
                argv = server["argv"]
                if argv.count(flag) != 1 or argv.index(flag) + 1 == len(argv) or argv[argv.index(flag) + 1] != expected:
                    raise RuntimeError("model argv weakened the loopback/reference identity")
        validate_inference_lock(self.root / "inference.lock", self.profile.get("inference_lock_sha256"))

    def run(self) -> int:
        status = 0
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.validate_profile()
            if self.gpu and (type(self.development_sha) is not str
                             or not re.fullmatch(r"[0-9a-f]{40}", self.development_sha)):
                raise RuntimeError("GPU pooling observation requires frozen candidate identity")
            self.start_free = shutil.disk_usage(self.root).free
            self.tick()
            if self.gpu:
                if self.admission is None:
                    # Uploaded portable owner; deliberately no app dependency.
                    from colab_profiles import admit
                    self.admission = admit
                self.admission(self.profile, self.report["initial_resources"])
            self.start_transport()
            self.wait_transport_approval()
            if self.gpu:
                self.stop_transport()
                interpreter = self.install_inference()
                self.download_models(interpreter)
                self.observe_pooling(interpreter)
                self.start_models()
            self.write_report("ready")
            while True:
                self.tick()
                self.sleep(.1)
        except StopRequested:
            self.write_report("stopping")
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, ImportError, subprocess.SubprocessError) as exc:
            status = 2
            self.report["failure_type"] = type(exc).__name__
            self.write_report("failed")
        except KeyboardInterrupt:
            status = 130
            self.write_report("stopping")
        finally:
            for cleanup in (self.processes.stop_all, self.stop_transport):
                try:
                    cleanup()
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    status = status or 2
                    self.report["cleanup_failure_type"] = type(exc).__name__
            self.report["logs"] = [{"name": child.log.path.name, "truncated": child.log.truncated,
                                     "returncode": child.process.returncode} for child in self.processes.children]
            self.report["finished_at"] = self.clock()
            self.write_report("failed" if status else "stopped")
            try:
                package_diagnostics(self.root, [child.log.path for child in self.processes.children])
            except (OSError, RuntimeError, tarfile.TarError):
                status = status or 2
                self.report["archive_failed"] = True
                self.write_report("failed")
        return status


def main() -> int:
    os.umask(0o077)
    try:
        path = Path(os.environ["RAG_COLAB_PROFILE"])
        if path.is_symlink() or path.stat().st_size > DIAGNOSTIC_LIMIT_BYTES:
            raise RuntimeError("profile must be a bounded supplied JSON file")
        profile = json.loads(path.read_text())
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        status = RemoteRuntime(profile, development_sha=os.environ.get("RAG_COLAB_DEVELOPMENT_SHA")).run()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        status = 2
    print(json.dumps({"remote_runtime_exit": status}), flush=True)
    return status


if __name__ == "__main__":
    result = main()
    if result:
        raise SystemExit(result)
