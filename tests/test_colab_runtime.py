"""Remote supervisor contracts through its HTTP/model boundary, CPU only."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.colab_runtime import (
    ARCHIVE_LIMIT_BYTES,
    LOCK_LIMIT_BYTES,
    LOG_LIMIT_BYTES,
    MODEL_LIMIT_BYTES,
    SOFTWARE_PROBE,
    BoundedLog,
    ProcessSupervisor,
    RemoteRuntime,
    package_diagnostics,
    request_json,
    validate_inference_lock,
    warmup_embedding,
    warmup_reasoning,
)


def test_software_probe_observes_installed_tokenizers_version(tmp_path, monkeypatch):
    observed = tmp_path / "software.json"
    versions = {"vllm": "0.28.0", "transformers": "5.15.0", "torch": "2.12.0",
                "huggingface-hub": "1.31.0", "tokenizers": "0.23.7"}
    requested = []

    def installed_version(name):
        requested.append(name)
        return versions[name]

    monkeypatch.setattr("importlib.metadata.version", installed_version)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        version=SimpleNamespace(cuda="13.0"), cuda=SimpleNamespace(is_available=lambda: True)))
    monkeypatch.setattr(sys, "argv", ["software-probe", str(observed)])
    exec(SOFTWARE_PROBE, {})  # noqa: S102 - Execute the fixed repository probe with metadata fakes.
    assert "tokenizers" in requested
    assert json.loads(observed.read_text())["tokenizers"] == "0.23.7"


@pytest.mark.parametrize("raw", [b"[]", b"null", b'"unexpected"', b"42"])
def test_model_http_json_requires_an_object_and_next_response_succeeds(monkeypatch, raw):
    bodies = iter([raw, b'{"data": [{"id": "Qwen/Qwen3.8-27B"}]}'])

    def response(request, *, timeout):
        stream = io.BytesIO(next(bodies))
        stream.status = 200
        return stream

    monkeypatch.setattr("scripts.colab_runtime.urllib.request.urlopen", response)
    with pytest.raises(TypeError, match="JSON object"):
        request_json("http://127.0.0.1:8000/v1/models")
    assert request_json("http://127.0.0.1:8000/v1/models") == {
        "data": [{"id": "Qwen/Qwen3.8-27B"}]
    }


def test_reasoning_only_or_truncated_warmup_refuses_then_complete_request_succeeds():
    server = {"port": 8000, "model_id": "Qwen/Qwen3.8-27B"}
    calls = []

    def gateway(url, body=None, **kwargs):
        calls.append((url, body))
        return {"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning": "thinking"}}]}

    with pytest.raises(RuntimeError, match="completed final content"):
        warmup_reasoning(server, http=gateway)
    assert calls[0][1]["reasoning_effort"] == "low"
    assert calls[0][1]["max_tokens"] == 512

    def completed(url, body=None, **kwargs):
        return {"choices": [{"finish_reason": "stop", "message": {"content": "READY"}}]}

    warmup_reasoning(server, http=completed)


@pytest.mark.parametrize("content,finish", [("answer", "length"), ("   ", "stop"), (None, "stop")])
def test_reasoning_readiness_requires_completed_final_content(content, finish):
    def endpoint(url, body=None, **kwargs):
        return {"choices": [{"finish_reason": finish, "message": {"content": content}}]}

    with pytest.raises(RuntimeError, match="completed final content"):
        warmup_reasoning({"port": 8000, "model_id": "Qwen/Qwen3.8-27B"}, http=endpoint)


def test_embedding_readiness_checks_native_dimension_and_normalization():
    server = {"port": 8001, "model_id": "Qwen/Qwen3-Embedding-8B"}
    response = {"data": [{"embedding": [1.0] + [0.0] * 4095}]}
    calls = []

    def endpoint(url, body=None, **kwargs):
        calls.append((url, body))
        return response

    warmup_embedding(server, http=endpoint)
    assert calls == [("http://127.0.0.1:8001/v1/embeddings", {
        "model": "Qwen/Qwen3-Embedding-8B", "input": ["Colab embedding readiness."]
    })]
    response["data"][0]["embedding"] = [1.0] * 4096
    with pytest.raises(RuntimeError, match="normalized"):
        warmup_embedding(server, http=endpoint)
    response["data"][0]["embedding"] = [1.0] + [0.0] * 1023
    with pytest.raises(RuntimeError, match="4096"):
        warmup_embedding(server, http=endpoint)


def test_inference_lock_is_required_hash_bound_and_contains_reference_pins(tmp_path):
    path = tmp_path / "inference.lock"
    lock = ("vllm==0.28.0 --hash=sha256:" + "a" * 64 + "\n"
            "transformers==5.15.0 --hash=sha256:" + "b" * 64 + "\n"
            "torch==2.12.0 --hash=sha256:" + "c" * 64 + "\n"
            "huggingface-hub==1.31.0 --hash=sha256:" + "d" * 64 + "\n")
    path.write_text(lock)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert validate_inference_lock(path, digest)["vllm"] == "0.28.0"
    path.write_text(lock.replace("0.28.0", "0.28.1"))
    with pytest.raises(RuntimeError, match="hash"):
        validate_inference_lock(path, digest)
    path.write_text(lock.replace("vllm==0.28.0", "vllm>=0.28.0"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="pinned"):
        validate_inference_lock(path, digest)


@pytest.fixture
def supplied_inference_lock(tmp_path):
    path = tmp_path / "inference.lock"
    path.write_text("vllm==0.28.0 --hash=sha256:" + "a" * 64 + "\n"
                    "transformers==5.15.0 --hash=sha256:" + "b" * 64 + "\n"
                    "torch==2.12.0 --hash=sha256:" + "c" * 64 + "\n"
                    "huggingface-hub==1.31.0 --hash=sha256:" + "d" * 64 + "\n")
    return path


@pytest.mark.parametrize("requirement", [
    "extra-package==not-a-version", "extra-package==1..0", "extra-package==1!2!3",
    "extra-package==1.0+cuda++", "extra-package-==1.0", "extra-package>=1.0",
    "extra-package @ https://example.invalid/extra.whl", "--index-url=https://example.invalid",
])
def test_inference_lock_refuses_nonexact_or_unsupported_requirement_tokens(supplied_inference_lock, requirement):
    with supplied_inference_lock.open("a") as stream:
        stream.write(requirement + " --hash=sha256:" + "e" * 64 + "\n")
    digest = hashlib.sha256(supplied_inference_lock.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="pinned"):
        validate_inference_lock(supplied_inference_lock, digest)


def test_inference_lock_accepts_exact_local_versions_and_multiple_wheel_hashes(supplied_inference_lock):
    with supplied_inference_lock.open("a") as stream:
        stream.write("extra-package==1!2.3rc1.post2.dev3+cu130 \\\n"
                     "    --hash=sha256:" + "e" * 64 + " \\\n"
                     "    --hash=sha256:" + "f" * 64 + " # approved wheel alternatives\n")
    digest = hashlib.sha256(supplied_inference_lock.read_bytes()).hexdigest()
    assert validate_inference_lock(supplied_inference_lock, digest)["extra-package"] == "1!2.3rc1.post2.dev3+cu130"


def test_inference_lock_refuses_canonical_duplicate_pins(supplied_inference_lock):
    with supplied_inference_lock.open("a") as stream:
        stream.write("HuggingFace_Hub==1.31.0 --hash=sha256:" + "e" * 64 + "\n")
    digest = hashlib.sha256(supplied_inference_lock.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="duplicate"):
        validate_inference_lock(supplied_inference_lock, digest)


def test_inference_lock_refuses_unhashed_requirement(supplied_inference_lock):
    text = supplied_inference_lock.read_text()
    supplied_inference_lock.write_text(text.replace(" --hash=sha256:" + "c" * 64, ""))
    digest = hashlib.sha256(supplied_inference_lock.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="pinned"):
        validate_inference_lock(supplied_inference_lock, digest)


def test_inference_lock_refuses_growth_after_opened_file_metadata(supplied_inference_lock, monkeypatch):
    digest = hashlib.sha256(supplied_inference_lock.read_bytes()).hexdigest()
    original_fstat = os.fstat

    def grow_after_metadata(descriptor):
        observed = original_fstat(descriptor)
        with supplied_inference_lock.open("ab") as stream:
            stream.write(b"x" * LOCK_LIMIT_BYTES)
        return observed

    monkeypatch.setattr("scripts.colab_runtime.os.fstat", grow_after_metadata)
    with pytest.raises(RuntimeError, match="bounded"):
        validate_inference_lock(supplied_inference_lock, digest)


def test_inference_lock_refuses_symlink_and_nonregular_file(supplied_inference_lock, tmp_path):
    digest = hashlib.sha256(supplied_inference_lock.read_bytes()).hexdigest()
    link = tmp_path / "linked.lock"
    link.symlink_to(supplied_inference_lock)
    with pytest.raises((OSError, RuntimeError)):
        validate_inference_lock(link, digest)
    with pytest.raises(RuntimeError, match="bounded"):
        validate_inference_lock(tmp_path, digest)
    pipe = tmp_path / "pipe.lock"
    os.mkfifo(pipe)
    with pytest.raises(RuntimeError, match="bounded"):
        validate_inference_lock(pipe, digest)


def test_remote_output_and_archive_are_finite_and_exclude_model_files(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "reasoning.log"
    output = BoundedLog(log)
    for _ in range(32):
        output.write(b"x" * 8192)
    output.close()
    assert log.stat().st_size <= LOG_LIMIT_BYTES
    assert output.truncated
    (tmp_path / "colab-diagnostics.json").write_text('{"stage":"failed"}\n')
    (tmp_path / "weights.safetensors").write_bytes(b"must never enter diagnostics")
    (logs / "unowned.log").write_bytes(b"must never enter diagnostics")
    archive = package_diagnostics(tmp_path, [log])
    assert archive.stat().st_size <= ARCHIVE_LIMIT_BYTES
    with tarfile.open(archive, "r:gz") as bundle:
        assert set(bundle.getnames()) == {"colab-diagnostics.json", "logs/reasoning.log"}
        diagnostic = bundle.extractfile("colab-diagnostics.json")
        assert diagnostic is not None
        assert json.load(diagnostic) == {"stage": "failed"}


def test_diagnostic_packaging_rejects_oversized_or_unowned_members(tmp_path):
    (tmp_path / "colab-diagnostics.json").write_text("{}")
    log = tmp_path / "large.log"
    log.write_bytes(b"x" * (LOG_LIMIT_BYTES + 1))
    with pytest.raises(RuntimeError, match="bounded"):
        package_diagnostics(tmp_path, [log])
    assert not (tmp_path / "colab-artifacts.tgz").exists()


def test_real_child_output_is_drained_bounded_and_next_operation_works(tmp_path):
    processes = ProcessSupervisor(tmp_path, guard=lambda: None)
    try:
        output = processes.run([sys.executable, "-c",
                                "import sys; sys.stdout.buffer.write(b'x' * 262144)"],
                               "noisy", seconds=5)
        assert len(output) == LOG_LIMIT_BYTES
        assert (tmp_path / "noisy.log").stat().st_size == LOG_LIMIT_BYTES
        assert processes.children[0].log.truncated
        assert processes.run([sys.executable, "-c", "print('NEXT')"],
                             "next", seconds=5) == b"NEXT\n"
    finally:
        processes.stop_all()


def test_timed_out_child_is_reaped_before_the_next_operation(tmp_path):
    processes = ProcessSupervisor(tmp_path, guard=lambda: None)
    try:
        with pytest.raises(RuntimeError, match="timeout"):
            processes.run([sys.executable, "-c", "import time; time.sleep(30)"],
                          "slow", seconds=.05)
        assert all(child.process.poll() is not None for child in processes.children)
        assert processes.run([sys.executable, "-c", "print('RECOVERED')"],
                             "recovered", seconds=5) == b"RECOVERED\n"
    finally:
        processes.stop_all()


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.on_sleep = lambda: None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()


class FakeLoopbackServer:
    """TCP listener boundary without real network bindings or external services."""

    def __init__(self, address, handler):
        assert address[0] == "127.0.0.1"
        self.server_address = address
        self.stopped = threading.Event()
        self.closed = False

    def serve_forever(self, poll_interval):
        self.stopped.wait(timeout=2)

    def shutdown(self):
        self.stopped.set()

    def server_close(self):
        self.closed = True


def cpu_profile(clock):
    return {"name": "COLAB_CPU_TRANSPORT", "accelerator": None,
            "deadline_seconds": 30, "expires_at": clock() + 30, "servers": []}


def test_cpu_transport_never_installs_or_downloads_and_exports_after_stop(tmp_path):
    clock = Clock()
    servers = []

    def server_factory(address, handler):
        server = FakeLoopbackServer(address, handler)
        servers.append(server)
        return server

    def forbidden_process(*args, **kwargs):
        raise AssertionError("CPU transport must never invoke inference subprocesses")

    (tmp_path / "colab-transport-ok").write_text("verified\n")

    def stop_when_ready():
        report = json.loads((tmp_path / "colab-diagnostics.json").read_text())
        if report["stage"] == "ready":
            (tmp_path / "colab-stop").write_text("stop\n")

    clock.on_sleep = stop_when_ready
    runtime = RemoteRuntime(cpu_profile(clock), root=tmp_path, clock=clock, sleep=clock.sleep,
                            popen=forbidden_process, server_factory=server_factory)
    assert runtime.run() == 0
    report = json.loads((tmp_path / "colab-diagnostics.json").read_text())
    assert report["profile"] == "COLAB_CPU_TRANSPORT"
    assert report["transport_verified"] is True
    assert report["stage"] == "stopped"
    assert all(server.closed and server.stopped.is_set() for server in servers)
    assert (tmp_path / "colab-artifacts.tgz").stat().st_size <= ARCHIVE_LIMIT_BYTES
    assert not (tmp_path / "models").exists()


def test_partial_transport_start_failure_cleans_up_and_next_run_works(tmp_path):
    clock = Clock()
    servers = []

    def second_listener_fails(address, handler):
        if servers:
            raise OSError("original deterministic port collision")
        server = FakeLoopbackServer(address, handler)
        servers.append(server)
        return server

    first = RemoteRuntime(cpu_profile(clock), root=tmp_path / "first", clock=clock,
                          sleep=clock.sleep, server_factory=second_listener_fails)
    assert first.run() != 0
    assert servers[0].closed
    second_root = tmp_path / "second"
    second_root.mkdir()
    (second_root / "colab-transport-ok").write_text("verified\n")
    clock.on_sleep = lambda: (second_root / "colab-stop").write_text("stop\n")
    second = RemoteRuntime(cpu_profile(clock), root=second_root, clock=clock,
                           sleep=clock.sleep, server_factory=FakeLoopbackServer)
    assert second.run() == 0
    assert json.loads((second_root / "colab-diagnostics.json").read_text())["transport_verified"]


def test_cpu_deadline_refuses_readiness_without_transport_approval(tmp_path):
    clock = Clock()
    runtime = RemoteRuntime(cpu_profile(clock), root=tmp_path, clock=clock,
                            sleep=clock.sleep, server_factory=FakeLoopbackServer)
    assert runtime.run() != 0
    report = json.loads((tmp_path / "colab-diagnostics.json").read_text())
    assert report["transport_verified"] is False
    assert report["stage"] == "failed"
    assert "ready" not in [event["stage"] for event in report["events"]]
    assert clock() < 1031
    assert (tmp_path / "colab-artifacts.tgz").stat().st_size <= ARCHIVE_LIMIT_BYTES


def test_transport_approval_is_required_before_any_gpu_bootstrap(tmp_path):
    from mainframe_rag.serve.colab_profiles import admit, build_profile

    clock = Clock()
    profile = build_profile("COLAB_G4_BF16", deadline_seconds=30)
    profile["expires_at"] = clock() + 30
    for server in profile["servers"]:
        server["local_dir"] = server["local_dir"].replace("/content", str(tmp_path), 1)
        server["argv"] = [value.replace("/content", str(tmp_path), 1) for value in server["argv"]]
    lock = ("vllm==0.28.0 --hash=sha256:" + "a" * 64 + "\n"
            "transformers==5.15.0 --hash=sha256:" + "b" * 64 + "\n"
            "torch==2.12.0 --hash=sha256:" + "c" * 64 + "\n"
            "huggingface-hub==1.31.0 --hash=sha256:" + "d" * 64 + "\n")
    (tmp_path / "inference.lock").write_text(lock)
    profile["inference_lock_sha256"] = hashlib.sha256(lock.encode()).hexdigest()

    def forbidden_process(*args, **kwargs):
        raise AssertionError("no inference process before host transport approval")

    clock.on_sleep = lambda: (tmp_path / "colab-stop").write_text("stop\n")
    observed = {"gpu_name": "NVIDIA RTX PRO 6000 Blackwell Server Edition", "vram_mib": 98304,
                "driver_version": "580.100", "available_ram_mib": 131072, "free_disk_mib": 200000}
    runtime = RemoteRuntime(profile, root=tmp_path, clock=clock, sleep=clock.sleep,
                            popen=forbidden_process, server_factory=FakeLoopbackServer,
                            observe=lambda gpu: observed, admission=admit)
    assert runtime.run() == 0
    report = json.loads((tmp_path / "colab-diagnostics.json").read_text())
    assert report["transport_verified"] is False
    assert not (tmp_path / "rag-vllm").exists()


@pytest.mark.parametrize("finish,status,embedding_started,malformed,failure_type", [
    ("stop", 0, True, None, None), ("length", 2, False, None, "RuntimeError"),
    ("stop", 2, False, ("models",), "TypeError"),
    ("stop", 2, False, ("software",), "TypeError"),
    ("stop", 2, False, ("snapshot",), "TypeError"),
    ("stop", 2, False, ("software", "python", []), "TypeError"),
    ("stop", 2, False, ("software", "python", True), "TypeError"),
    ("stop", 2, False, ("software", "python", 312), "TypeError"),
    ("stop", 2, False, ("software", "python", "3.12."), "RuntimeError"),
    ("stop", 2, False, ("software", "cuda_available", "false"), "TypeError"),
    ("stop", 2, False, ("software", "cuda_available", 1), "TypeError"),
    ("stop", 2, False, ("software", "cuda_available", False), "RuntimeError"),
    ("stop", 2, False, ("software", "vllm", []), "TypeError"),
    ("stop", 2, False, ("software", "vllm", True), "TypeError"),
    ("stop", 2, False, ("software", "vllm", "0.28.1"), "RuntimeError"),
    ("stop", 2, False, ("software", "transformers", 5.15), "TypeError"),
    ("stop", 2, False, ("software", "tokenizers", []), "TypeError"),
    ("stop", 2, False, ("software", "tokenizers", "0.23.8"), "RuntimeError"),
    ("stop", 2, False, ("software", "torch", "2.12.1"), "RuntimeError"),
    ("stop", 2, False, ("software", "huggingface_hub", "1.31.1"), "RuntimeError"),
    ("stop", 2, False, ("software", "cuda_build", []), "TypeError"),
    ("stop", 2, False, ("software", "cuda_build", "12.8"), "RuntimeError"),
    ("stop", 2, False, ("resources", "driver_version", []), "TypeError"),
    ("stop", 2, False, ("resources", "driver_version", True), "TypeError"),
    ("stop", 2, False, ("resources", "driver_version", 580), "TypeError"),
    ("stop", 2, False, ("resources", "driver_version", "9" * 65), "RuntimeError"),
    ("stop", 2, False, ("snapshot", "model_id", []), "TypeError"),
    ("stop", 2, False, ("snapshot", "resolved_revision", True), "TypeError"),
    ("stop", 2, False, ("snapshot", "download_bytes", []), "TypeError"),
    ("stop", 2, False, ("snapshot", "download_bytes", True), "TypeError"),
    ("stop", 2, False, ("snapshot", "download_bytes", -1), "RuntimeError"),
    ("stop", 2, False, ("snapshot", "download_bytes", MODEL_LIMIT_BYTES + 1), "RuntimeError"),
    ("stop", 2, False, ("snapshot", "tensor_bytes", "100"), "TypeError"),
    ("stop", 2, False, ("snapshot", "tensor_bytes", True), "TypeError"),
    ("stop", 2, False, ("snapshot", "tensor_bytes", 101), "RuntimeError"),
    ("stop", 2, False, ("snapshot", "tensor_shards", True), "TypeError"),
    ("stop", 2, False, ("snapshot", "metadata_sha256", []), "TypeError"),
    ("stop", 2, False, ("snapshot", "metadata_sha256", {"tokenizer.json": True}), "RuntimeError"),
])
def test_gpu_startup_uses_exact_argv_and_warms_reasoning_before_embedding(
        tmp_path, monkeypatch, finish, status, embedding_started, malformed, failure_type):
    from mainframe_rag.serve.colab_profiles import admit, build_profile

    clock = Clock()
    profile = build_profile("COLAB_G4_BF16", deadline_seconds=30)
    profile["expires_at"] = clock() + 30
    for server in profile["servers"]:
        server["local_dir"] = server["local_dir"].replace("/content", str(tmp_path), 1)
        server["argv"] = [value.replace("/content", str(tmp_path), 1) for value in server["argv"]]
    lock = ("vllm==0.28.0 --hash=sha256:" + "a" * 64 + "\n"
            "transformers==5.15.0 --hash=sha256:" + "b" * 64 + "\n"
            "torch==2.12.0 --hash=sha256:" + "c" * 64 + "\n"
            "tokenizers==0.23.7 --hash=sha256:" + "e" * 64 + "\n"
            "huggingface-hub==1.31.0 --hash=sha256:" + "d" * 64 + "\n")
    (tmp_path / "inference.lock").write_text(lock)
    profile["inference_lock_sha256"] = hashlib.sha256(lock.encode()).hexdigest()
    (tmp_path / "colab-transport-ok").write_text("verified\n")
    monkeypatch.setattr("scripts.colab_runtime.shutil.which", lambda name: "/tools/python3.12")
    observed = {"gpu_name": "NVIDIA RTX PRO 6000 Blackwell Server Edition", "vram_mib": 98304,
                "gpu_used_mib": 72000, "driver_version": "580.100",
                "available_ram_mib": 131072, "free_disk_mib": 200000}
    if malformed and malformed[0] == "resources":
        observed[malformed[1]] = malformed[2]
    events = []
    processes = []
    listeners = []

    def server_factory(address, handler):
        assert not any(server.server_address == address and not server.closed for server in listeners)
        server = FakeLoopbackServer(address, handler)
        listeners.append(server)
        return server

    class InferenceProcess:
        def __init__(self, argv, **kwargs):
            events.append(("process", list(argv)))
            assert kwargs["start_new_session"] is True
            assert kwargs["stdin"] == subprocess.DEVNULL
            self.argv = argv
            self.returncode = None if argv[1] == "serve" else 0
            self.pid = 400000 + len(processes)
            self.stdout = io.BytesIO(b"bounded original inference diagnostic\n")
            processes.append(self)
            if argv[1:3] == ["-m", "venv"]:
                Path(argv[-1]).mkdir()
            elif "install" in argv:
                assert "--require-hashes" in argv
                assert "--only-binary=:all:" in argv
                assert "--no-deps" in argv
                assert "--no-cache-dir" in argv
            elif "check" in argv:
                pass
            elif argv[1] == "-c" and len(argv) == 4:
                software = {"python": "3.12.14", "vllm": "0.28.0",
                    "transformers": "5.15.0", "torch": "2.12.0", "huggingface_hub": "1.31.0", "tokenizers": "0.23.7",
                    "cuda_build": "13.0", "cuda_available": True}
                if malformed and malformed[0] == "software":
                    if len(malformed) == 1:
                        software = []
                    else:
                        software[malformed[1]] = malformed[2]
                Path(argv[-1]).write_text(json.dumps(software))
            elif argv[1] == "-c" and len(argv) == 8:
                snapshot = {"model_id": argv[3], "resolved_revision": argv[4],
                            "download_bytes": 100, "tensor_bytes": 100, "tensor_shards": 1,
                            "metadata_sha256": {"tokenizer.json": "e" * 64}}
                if malformed and malformed[0] == "snapshot":
                    if len(malformed) == 1:
                        snapshot = []
                    else:
                        snapshot[malformed[1]] = malformed[2]
                Path(argv[-1]).write_text(json.dumps(snapshot))
            else:
                assert argv in [server["argv"] for server in profile["servers"]]

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            return self.returncode

    def terminate(process):
        if process.returncode is None:
            process.returncode = -15

    def endpoint(url, body=None, **kwargs):
        events.append(("http", url))
        if url.endswith("/health"):
            return {}
        if url.endswith("/v1/models"):
            if malformed and malformed[0] == "models":
                return request_json(url, timeout=kwargs["timeout"])
            return {"data": [{"id": "Qwen/Qwen3.8-27B" if ":8000/" in url else "Qwen/Qwen3-Embedding-8B"}]}
        if url.endswith("/chat/completions"):
            return {"choices": [{"finish_reason": finish, "message": {"content": "READY"}}]}
        assert url.endswith("/embeddings")
        return {"data": [{"embedding": [1.0] + [0.0] * 4095}]}

    def malformed_response(request, *, timeout):
        stream = io.BytesIO(b"[]")
        stream.status = 200
        return stream

    monkeypatch.setattr("scripts.colab_runtime.urllib.request.urlopen", malformed_response)
    clock.on_sleep = lambda: (tmp_path / "colab-stop").write_text("stop\n")
    runtime = RemoteRuntime(profile, root=tmp_path, clock=clock, sleep=clock.sleep,
                            popen=InferenceProcess, terminate=terminate, http=endpoint,
                            server_factory=server_factory, observe=lambda gpu: observed,
                            admission=admit)
    assert runtime.run() == status
    started = [argv for kind, argv in events if kind == "process" and argv[1] == "serve"]
    model_count = 2 if embedding_started else 1
    if malformed and malformed[0] in ("software", "snapshot", "resources"):
        model_count = 0
    assert started == [server["argv"] for server in profile["servers"][:model_count]]
    reasoning_warmup = ("http", "http://127.0.0.1:8000/v1/chat/completions")
    assert (reasoning_warmup in events) is (malformed is None)
    if embedding_started:
        assert events.index(reasoning_warmup) < events.index(("process", profile["servers"][1]["argv"]))
    assert all(process.poll() is not None for process in processes)
    assert all(server.closed and server.stopped.is_set() for server in listeners)
    report = json.loads((tmp_path / "colab-diagnostics.json").read_text())
    for model in report.get("models", []):
        matching = [argv for argv in started if model["model_id"] in argv]
        if matching:
            exact_argv = json.dumps(matching[0], sort_keys=True, separators=(",", ":"), allow_nan=False)
            assert model["server_argv_sha256"] == hashlib.sha256(exact_argv.encode()).hexdigest()
        else:
            assert "server_argv_sha256" not in model
    if malformed and malformed[0] == "resources":
        assert not processes
    else:
        assert report["peak_resources"]["sampled_gpu_used_mib"] == 72000
    assert report["stage"] == ("stopped" if status == 0 else "failed")
    assert (tmp_path / "colab-artifacts.tgz").stat().st_size <= ARCHIVE_LIMIT_BYTES
    if malformed:
        assert report["failure_type"] == failure_type
        assert "ready" not in [event["stage"] for event in report["events"]]
        assert "cleanup_failure_type" not in report
        with tarfile.open(tmp_path / "colab-artifacts.tgz", "r:gz") as bundle:
            diagnostic = bundle.extractfile("colab-diagnostics.json")
            assert diagnostic is not None
            assert json.load(diagnostic)["failure_type"] == failure_type

        recovery_root = tmp_path / "next-session"
        recovery_root.mkdir()
        (recovery_root / "colab-transport-ok").write_text("verified\n")
        clock.on_sleep = lambda: (recovery_root / "colab-stop").write_text("stop\n")
        recovered = RemoteRuntime(cpu_profile(clock), root=recovery_root, clock=clock,
                                  sleep=clock.sleep, server_factory=server_factory)
        assert recovered.run() == 0
        recovery_report = json.loads((recovery_root / "colab-diagnostics.json").read_text())
        assert recovery_report["transport_verified"] is True
        assert recovery_report["stage"] == "stopped"
        assert all(server.closed and server.stopped.is_set() for server in listeners)
