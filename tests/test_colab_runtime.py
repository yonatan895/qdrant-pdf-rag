"""Remote supervisor contracts through its HTTP/model boundary, CPU only."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import colab_preflight
from scripts.colab_runtime import (
    ARCHIVE_LIMIT_BYTES,
    LOCK_LIMIT_BYTES,
    LOG_LIMIT_BYTES,
    MODEL_LIMIT_BYTES,
    POOLING_PROBE,
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


def synthetic_pooling_record(profile, software, candidate, source):
    """Original control-flow packet, never qualified native-library evidence."""
    server = profile["servers"][1]
    producer = {"development_sha": candidate,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest()}
    runtime = {name: software[name] for name in ("vllm", "transformers", "tokenizers", "torch")}
    files = {"tokenizer.json": {"bytes": 1, "sha256": "e" * 64}}
    window = int(server["argv"][server["argv"].index("--max-model-len") + 1])
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return {"schema": 1, "status": "observed", "trust": "unreviewed", "acceptance_certified": False,
            "manifest": {"schema": 1, "model": {"model": server["model_id"], "revision": server["revision"]},
                "tokenizer": {"implementation": "transformers.Qwen2TokenizerFast", "version": software["transformers"]},
                "runtime": runtime, "server_argv": server["argv"], "files": files,
                "observation": {"implementation": "vllm.entrypoints.pooling.embed.protocol.EmbeddingCompletionRequest",
                    "version": software["vllm"], "effective_window": window,
                    "counting_options": {"add_special_tokens": True, "do_lower_case": False, "truncation": False},
                    "model_config": {"max_model_len": window, "encoder_config": None,
                        "pooler_config": {"enable_chunked_processing": False, "max_embed_len": None,
                                          "seq_pooling_type": "LAST", "use_activation": False}}},
                "provenance": {"kind": "offline-vllm-pooling-observation", "producer": producer,
                    "resolved_revision": server["revision"], "files_sha256": digest(files),
                    "runtime_sha256": digest(runtime), "server_argv_sha256": digest(server["argv"])}},
            "facts": {"producer": producer, "packages": {name: software[name.replace("-", "_")]
                       for name in (*runtime, "huggingface-hub")}}}


def test_actual_inline_probe_refuses_invalid_identity_before_inference(tmp_path):
    result = subprocess.run([sys.executable, "-c", POOLING_PROBE, str(tmp_path), "invalid"],
                            capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"status": "failed", "code": "invalid_candidate_identity"}
    assert not list(tmp_path.iterdir())


@pytest.fixture
def remote_preflight(tmp_path, monkeypatch):
    from mainframe_rag.serve import colab_profiles

    content = tmp_path / "remote content"
    content.mkdir()
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 134217728 kB\n")
    monkeypatch.setattr(colab_preflight, "Path", lambda value: (
        content / Path(value).name if str(value).startswith("/content/")
        else meminfo if str(value) == "/proc/meminfo" else Path(value)))
    monkeypatch.setattr(colab_preflight.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(free=200000 * 1024**2))
    monkeypatch.setattr(colab_preflight.shutil, "which", lambda _name: None)
    monkeypatch.setattr(colab_preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(colab_preflight.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(colab_preflight.platform, "python_version", lambda: "3.13.16")
    monkeypatch.setattr(colab_preflight.platform, "python_implementation", lambda: "CPython")
    monkeypatch.setattr(colab_preflight.platform, "freedesktop_os_release",
                        lambda: {"ID": "ubuntu", "VERSION_ID": "22.04"})
    monkeypatch.setattr(colab_preflight.os, "confstr", lambda _name: "glibc 2.35")
    monkeypatch.setitem(sys.modules, "colab_profiles", colab_profiles)
    monkeypatch.setattr(colab_preflight, "observe_kernel_pip", lambda: {"version": "26.2.1", "error": None})
    profile = content / "profile.json"
    monkeypatch.setenv("RAG_COLAB_PROFILE", str(profile))

    def run(name):
        value = colab_profiles.build_profile(name)
        value["expires_at"] = colab_preflight.time.time() + 30
        profile.write_text(json.dumps(value))
        status = colab_preflight.main()
        return status, json.loads((content / "colab-preflight.json").read_text())

    return run


def test_cpu_preflight_records_actual_platform_without_inference_python(remote_preflight):
    status, observed = remote_preflight("COLAB_CPU_TRANSPORT")
    assert status == 0 and observed["admitted"] is True
    assert observed["python"] == "3.13.16"
    assert observed["python_implementation"] == "CPython"
    assert observed["platform"] == {"system": "Linux", "machine": "x86_64",
                                    "glibc": "glibc 2.35", "distribution_id": "ubuntu",
                                    "distribution_version": "22.04"}
    assert observed["inference_python"] == {"executable": None, "version": None,
                                            "implementation": None, "error": "not_found"}


@pytest.mark.parametrize("failure", [OSError("metadata absent"), ValueError("unsupported confstr"),
                                    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")])
def test_cpu_preflight_records_unknown_optional_platform_facts(remote_preflight, monkeypatch, failure):
    def unavailable(*args):
        raise failure

    monkeypatch.setattr(colab_preflight.platform, "freedesktop_os_release", unavailable)
    monkeypatch.setattr(colab_preflight.os, "confstr", unavailable)
    status, observed = remote_preflight("COLAB_CPU_TRANSPORT")
    assert status == 0 and observed["admitted"] is True
    assert observed["platform"] == {"system": "Linux", "machine": "x86_64", "glibc": None,
                                    "distribution_id": None, "distribution_version": None}


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("python3.12", 3),
                                    ValueError("malformed interpreter output")])
def test_cpu_preflight_survives_inference_probe_failure(remote_preflight, monkeypatch, failure):
    monkeypatch.setattr(colab_preflight.shutil, "which", lambda name: (
        "/usr/bin/python3.12" if name == "python3.12" else None))

    def probe(*args, **kwargs):
        raise failure

    monkeypatch.setattr(colab_preflight.subprocess, "run", probe)
    status, observed = remote_preflight("COLAB_CPU_TRANSPORT")
    assert status == 0 and observed["admitted"] is True
    assert observed["inference_python"]["error"] == (
        "timeout" if isinstance(failure, subprocess.TimeoutExpired) else "invalid_output")


@pytest.mark.parametrize("version,implementation,error,expected_status", [
    ("3.12.13", "CPython", None, 0), ("3.13.16", "CPython", None, 2),
    ("3.12.13", "PyPy", None, 2), (None, None, "not_found", 2),
    (None, None, "timeout", 2), (None, None, "probe_failed", 2),
    (None, None, "invalid_output", 2),
])
def test_gpu_preflight_checks_interpreter_before_setup_and_preserves_facts(
        remote_preflight, monkeypatch, version, implementation, error, expected_status):
    monkeypatch.setattr(colab_preflight, "observe_inference_python", lambda: {
        "executable": "/runtime with spaces/python3.12", "version": version,
        "implementation": implementation, "error": error})
    monkeypatch.setattr(colab_preflight.shutil, "which", lambda name: (
        "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None))
    monkeypatch.setattr(colab_preflight.subprocess, "run", lambda *args, **kwargs: (
        SimpleNamespace(stdout="NVIDIA RTX PRO 6000 Blackwell,98304,580.100\n")))
    status, observed = remote_preflight("COLAB_G4_BF16")
    assert status == expected_status
    assert observed["admitted"] is (expected_status == 0)
    assert observed["platform"]["glibc"] == "glibc 2.35"
    assert observed["python"] == "3.13.16"
    assert observed["gpu_name"] == "NVIDIA RTX PRO 6000 Blackwell"
    assert observed["inference_python"]["version"] == version
    if expected_status:
        assert "preinstalled CPython 3.12" in observed["error"]
    # A refusal must leave the next ordinary CPU transport independent of it.
    assert remote_preflight("COLAB_CPU_TRANSPORT")[0] == 0


def test_inference_interpreter_probe_uses_real_isolated_stdlib_process(tmp_path, monkeypatch):
    # The executable path crosses whitespace unchanged, even when its actual
    # version differs from the name the provider placed on PATH.
    executable = tmp_path / "runtime with spaces" / "python3.12"
    executable.parent.mkdir()
    executable.symlink_to(sys.executable)
    monkeypatch.setattr(colab_preflight.shutil, "which", lambda _name: str(executable))
    observed = colab_preflight.observe_inference_python()
    assert observed == {"executable": str(executable), "version": sys.version.split()[0],
                        "implementation": "CPython", "error": None}


@pytest.mark.parametrize("result,error", [
    (subprocess.TimeoutExpired("python3.12", 3), "timeout"),
    (OSError("cannot start"), "probe_failed"),
    (subprocess.CalledProcessError(1, "python3.12"), "probe_failed"),
    ("[]", "invalid_output"), ("not json", "invalid_output"),
    ('{"version": 312, "implementation": "CPython"}', "invalid_output"),
    ('{"version": "3.12.13"}', "invalid_output"),
])
def test_inference_interpreter_probe_reports_unknown_evidence(monkeypatch, result, error):
    monkeypatch.setattr(colab_preflight.shutil, "which", lambda _name: "/usr/bin/python3.12")

    def probe(argv, **kwargs):
        assert argv[:4] == ["/usr/bin/python3.12", "-I", "-S", "-B"]
        assert kwargs["timeout"] == 3 and kwargs["check"] is True
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(stdout=result)

    monkeypatch.setattr(colab_preflight.subprocess, "run", probe)
    assert colab_preflight.observe_inference_python() == {
        "executable": "/usr/bin/python3.12", "version": None,
        "implementation": None, "error": error}


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


@pytest.mark.parametrize("corruption", [None, "base_prefix", "prefix", "python", "implementation",
                                      "installer_kernel", "installer_old", "installer_exit", "check_exit"])
def test_inference_install_works_without_ensurepip_and_targets_fresh_environment(tmp_path, monkeypatch, corruption):
    from mainframe_rag.serve.colab_profiles import build_profile

    profile = build_profile("COLAB_G4_BF16")
    profile["expires_at"] = 10000
    lock = ("vllm==0.28.0 --hash=sha256:" + "a" * 64 + "\n"
            "transformers==5.15.0 --hash=sha256:" + "b" * 64 + "\n"
            "torch==2.12.0 --hash=sha256:" + "c" * 64 + "\n"
            "tokenizers==0.23.7 --hash=sha256:" + "e" * 64 + "\n"
            "huggingface-hub==1.31.0 --hash=sha256:" + "d" * 64 + "\n")
    (tmp_path / "inference.lock").write_text(lock)
    profile["inference_lock_sha256"] = hashlib.sha256(lock.encode()).hexdigest()
    monkeypatch.setattr("scripts.colab_runtime.shutil.which", lambda _: "/system/python3.12")
    for name, value in {"_PIP_RUNNING_IN_SUBPROCESS": "0", "PIP_TARGET": "/wrong-target",
                        "PIP_PREFIX": "/wrong-prefix", "PIP_ROOT": "/wrong-root",
                        "PIP_USER": "1", "PIP_CONFIG_FILE": "/wrong-config"}.items():
        monkeypatch.setenv(name, value)
    calls = []
    target = tmp_path / "rag-vllm/bin/python"
    provider_bin = tmp_path / "provider commands"
    provider_bin.mkdir()
    monkeypatch.setenv("PATH", str(provider_bin))

    class SetupProcess:
        def __init__(self, argv, **kwargs):
            self.pid, self.returncode = 400001 + len(calls), 0
            calls.append(argv)
            output = b""
            if argv[1:3] == ["-m", "venv"]:
                # The real Ubuntu symptom: venv works, its ensurepip component does not.
                if "--without-pip" not in argv:
                    self.returncode, output = 1, b"ensurepip is not available\n"
                else:
                    Path(argv[-1]).mkdir()
            elif "--python" in argv:
                assert argv[:6] == [sys.executable, "-I", "-B", "-m", "pip", "--python"]
                assert argv[6] == str(target)
                env = kwargs["env"]
                assert "_PIP_RUNNING_IN_SUBPROCESS" not in env
                assert all(name not in env for name in ("PIP_TARGET", "PIP_PREFIX", "PIP_ROOT", "PIP_USER"))
                assert env["PIP_CONFIG_FILE"] == os.devnull
                if "--version" in argv:
                    output = b"pip 26.2.1 from /kernel/pip (python 3.12)\n"
                    if corruption == "installer_kernel":
                        output = output.replace(b"3.12", b"3.13")
                    elif corruption == "installer_old":
                        output = output.replace(b"26.2.1", b"22.2")
                    elif corruption == "installer_exit":
                        self.returncode = 1
                elif "install" in argv:
                    assert {"--require-hashes", "--only-binary=:all:", "--no-deps", "--no-cache-dir"} <= set(argv)
                    target.parent.mkdir(parents=True)
                    helper = target.parent / "ninja"
                    helper.write_text("#!/bin/sh\nprintf 'qualified-inference-tool\\n'\n")
                    helper.chmod(0o700)
                else:
                    assert argv[-1] == "check"
                    if corruption == "check_exit":
                        self.returncode = 1
            elif "-c" in argv:
                if "sys.prefix" in argv[argv.index("-c") + 1]:
                    value = {"python": "3.12.3", "implementation": "CPython",
                             "prefix": str(target.parent.parent), "base_prefix": "/system",
                             "executable": str(target)}
                    if corruption in ("base_prefix", "prefix", "python", "implementation"):
                        value[corruption] = {"base_prefix": str(target.parent.parent), "prefix": "/wrong-target",
                                             "python": "3.13.3", "implementation": "PyPy"}[corruption]
                    output = json.dumps(value).encode() + b"\n"
                else:
                    value = {"python": "3.12.3", "vllm": "0.28.0", "transformers": "5.15.0",
                             "torch": "2.12.0", "huggingface_hub": "1.31.0", "tokenizers": "0.23.7",
                             "cuda_build": "13.0", "cuda_available": True}
                    Path(argv[-1]).write_text(json.dumps(value))
            else:
                raise AssertionError("unexpected setup operation")
            self.stdout = io.BytesIO(output)

        def poll(self):
            return self.returncode

        def wait(self, **kwargs):
            return self.returncode

    runtime = RemoteRuntime(profile, root=tmp_path, clock=lambda: 100, sleep=lambda _: None,
                            popen=SetupProcess, development_sha="a" * 40)
    if corruption:
        with pytest.raises(RuntimeError):
            runtime.install_inference()
        assert runtime.processes.env["PATH"] == str(provider_bin)
        assert not (tmp_path / "software-observed.json").exists()
        if corruption != "check_exit":
            assert not any("install" in argv for argv in calls)
        return
    assert runtime.install_inference() == target
    assert [argv[-1] for argv in calls if "--python" in argv] == ["--version", str(tmp_path / "inference.lock"), "check"]
    assert runtime.report["installer"]["version"] == "26.2.1"
    # Absolute model entrypoints still resolve their nested helpers through PATH.
    runtime.processes.popen = subprocess.Popen
    runtime.processes.clock, runtime.processes.sleep = time.monotonic, time.sleep
    argv = [sys.executable, "-I", "-B", "-c",
            "import subprocess; print(subprocess.check_output(['ninja'], text=True).strip())"]
    assert runtime.processes.run(argv, "qualified-helper", seconds=5) == b"qualified-inference-tool\n"
    provider_helper = provider_bin / "ninja"
    provider_helper.write_text("#!/bin/sh\nprintf 'provider-tool\\n'\n")
    provider_helper.chmod(0o700)
    assert runtime.processes.run(argv, "qualified-helper-again", seconds=5) == b"qualified-inference-tool\n"
    assert os.environ["PATH"] == str(provider_bin)


@pytest.mark.parametrize("version,error", [("22.2", "unsupported_version"), ("22.3", None),
    ("26.2.1", None), ("not-a-version", "invalid_version"), (None, "invalid_version"),
    (colab_preflight.metadata.PackageNotFoundError("pip"), "not_found"),
    (OSError("unreadable metadata"), "probe_failed")])
def test_preflight_records_kernel_pip_without_installing_or_bootstrapping(monkeypatch, version, error):
    def observe(name):
        assert name == "pip"
        if isinstance(version, Exception):
            raise version
        return version

    monkeypatch.setattr(colab_preflight.metadata, "version", observe)
    result = colab_preflight.observe_kernel_pip()
    assert result == {"version": version if error in (None, "unsupported_version") else None,
                      "error": error}


@pytest.mark.parametrize("error", ["not_found", "unsupported_version", "invalid_version"])
def test_missing_kernel_installer_refuses_gpu_but_next_cpu_remains_usable(remote_preflight, monkeypatch, error):
    monkeypatch.setattr(colab_preflight, "observe_inference_python", lambda: {
        "executable": "/usr/bin/python3.12", "version": "3.12.3", "implementation": "CPython", "error": None})
    monkeypatch.setattr(colab_preflight, "observe_kernel_pip", lambda: {"version": None, "error": error})
    monkeypatch.setattr(colab_preflight.shutil, "which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    monkeypatch.setattr(colab_preflight.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout="NVIDIA RTX PRO 6000 Blackwell,98304,580.100\n"))
    status, facts = remote_preflight("COLAB_G4_BF16")
    assert status == 2 and facts["admitted"] is False and facts["kernel_pip"]["error"] == error
    assert "kernel pip" in facts["error"]
    assert remote_preflight("COLAB_CPU_TRANSPORT")[0] == 0


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
                            observe=lambda gpu: observed, admission=admit, development_sha="a" * 40)
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
    ("stop", 2, False, ("candidate", None), "RuntimeError"),
    ("stop", 2, False, ("candidate", "wrong"), "RuntimeError"),
    ("stop", 2, False, ("pooling",), "TypeError"),
    ("stop", 2, False, ("pooling", "missing"), "FileNotFoundError"),
    ("stop", 2, False, ("pooling", "oversize"), "RuntimeError"),
    ("stop", 2, False, ("pooling", "timeout"), "RuntimeError"),
    ("stop", 2, False, ("pooling", "exit"), "RuntimeError"),
    ("stop", 2, False, ("pooling", "trust", "approved"), "RuntimeError"),
    ("stop", 2, False, ("pooling", "acceptance_certified", True), "RuntimeError"),
    ("stop", 2, False, ("pooling", "facts", None), "RuntimeError"),
    ("stop", 2, False, ("pooling", "facts", "packages", "tokenizers", "0.99"), "RuntimeError"),
    ("stop", 2, False, ("pooling", "manifest", "provenance", "producer", "source_sha256", "f" * 64), "RuntimeError"),
    ("stop", 2, False, ("pooling", "manifest", "model", "revision", "f" * 40), "RuntimeError"),
    ("stop", 2, False, ("pooling", "manifest", "files", "tokenizer.json", "sha256", "f" * 64), "RuntimeError"),
    ("stop", 2, False, ("pooling", "manifest", "observation", "counting_options", "truncation", True), "RuntimeError"),
    ("stop", 2, False, ("pooling", "manifest", "observation", "counting_options", "add_special_tokens", 1), "RuntimeError"),
    ("stop", 2, False, ("pooling", "manifest", "observation", "effective_window", 8192), "RuntimeError"),
    ("stop", 2, False, ("pooling", "manifest", "observation", "effective_window", float("nan")), "ValueError"),
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
            elif argv[1:4] == ["-I", "-B", "-c"]:
                self.stdout = io.BytesIO(json.dumps({"python": "3.12.3", "implementation": "CPython",
                    "prefix": str(tmp_path / "rag-vllm"), "base_prefix": "/system",
                    "executable": str(tmp_path / "rag-vllm/bin/python")}).encode() + b"\n")
            elif "--version" in argv:
                self.stdout = io.BytesIO(b"pip 26.2.1 from /kernel/pip (python 3.12)\n")
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
            elif argv[1] == "-c" and len(argv) == 5:
                assert argv[2] == POOLING_PROBE and argv[-2:] == [str(tmp_path), "a" * 40]
                software = json.loads((tmp_path / "software-observed.json").read_text())
                packet = synthetic_pooling_record(profile, software, argv[-1], argv[2])
                if malformed and malformed[0] == "pooling":
                    if len(malformed) == 1:
                        packet = []
                    elif malformed[1] == "missing":
                        return
                    elif malformed[1] == "oversize":
                        (tmp_path / "pooling-observed.json").write_text(" " * (64 * 1024 + 1))
                        return
                    elif malformed[1] in ("timeout", "exit"):
                        self.returncode = None if malformed[1] == "timeout" else 1
                        return
                    else:
                        target = packet
                        for field in malformed[1:-2]:
                            target = target[field]
                        target[malformed[-2]] = malformed[-1]
                (tmp_path / "pooling-observed.json").write_text(json.dumps(packet))
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
    clock.on_sleep = (lambda: None) if malformed and malformed[:2] == ("pooling", "timeout") else lambda: (tmp_path / "colab-stop").write_text("stop\n")
    runtime = RemoteRuntime(profile, root=tmp_path, clock=clock, sleep=clock.sleep,
                            popen=InferenceProcess, terminate=terminate, http=endpoint,
                            server_factory=server_factory, observe=lambda gpu: observed,
                            admission=admit, development_sha=(malformed[1] if malformed and malformed[0] == "candidate" else "a" * 40))
    assert runtime.run() == status
    started = [argv for kind, argv in events if kind == "process" and argv[1] == "serve"]
    model_count = 2 if embedding_started else 1
    if malformed and malformed[0] in ("software", "snapshot", "resources", "candidate", "pooling"):
        model_count = 0
    assert started == [server["argv"] for server in profile["servers"][:model_count]]
    reasoning_warmup = ("http", "http://127.0.0.1:8000/v1/chat/completions")
    assert (reasoning_warmup in events) is (malformed is None)
    if embedding_started:
        assert events.index(reasoning_warmup) < events.index(("process", profile["servers"][1]["argv"]))
    assert all(process.poll() is not None for process in processes)
    assert all(server.closed and server.stopped.is_set() for server in listeners)
    report = json.loads((tmp_path / "colab-diagnostics.json").read_text())
    if malformed is None or malformed[0] == "models":
        pooling_call = next(event for event in events if event[0] == "process" and event[1][1:3] == ["-c", POOLING_PROBE])
        assert events.index(pooling_call) < events.index(("process", profile["servers"][0]["argv"]))
        assert report["pooling_observation"]["trust"] == "unreviewed"
        assert report["pooling_observation"]["acceptance_certified"] is False
    for model in report.get("models", []):
        matching = [argv for argv in started if model["model_id"] in argv]
        if matching:
            exact_argv = json.dumps(matching[0], sort_keys=True, separators=(",", ":"), allow_nan=False)
            assert model["server_argv_sha256"] == hashlib.sha256(exact_argv.encode()).hexdigest()
        else:
            assert "server_argv_sha256" not in model
    if malformed and malformed[0] in ("resources", "candidate"):
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
