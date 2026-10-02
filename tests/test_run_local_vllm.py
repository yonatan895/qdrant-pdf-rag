"""run_local_vllm.sh Budget integration (serving-budget track, PR-B).

End-to-end through the real POSIX script with a stub `docker` on PATH that
records its argv instead of launching a container: asserts the exact vLLM
flags the script derives from Budget resolution, the explicit-env override
rule, and fail-closed behavior. Hermetic: no docker, no GPU, no network —
only the venv python resolver and /bin/sh.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run_local_vllm.sh"


def _host(tmp_path: Path, avail_mb: int = 64000, psi: float = 0.0) -> dict[str, str]:
    """Stub host state (meminfo + PSI files) so tests never read the real host."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal: 99999999 kB\nMemAvailable: {avail_mb * 1024} kB\n")
    psi_dir = tmp_path / "pressure"
    psi_dir.mkdir(exist_ok=True)
    line = f"some avg10={psi:.2f} avg60=0.00 avg300=0.00 total=0\n"
    for resource in ("memory", "io"):
        (psi_dir / resource).write_text(line + line.replace("some", "full"))
    return {"HOST_MEMINFO": str(meminfo), "HOST_PSI_DIR": str(psi_dir)}


_HOST_VARS = (
    "HOST_MEM_MB",
    "HOST_MEM_HEADROOM_MB",
    "HOST_PSI_MAX",
    "HOST_MEMINFO",
    "HOST_PSI_DIR",
    "FORCE_START",
)


def _run_script(tmp_path: Path, extra_env: dict[str, str]) -> tuple[int, str, list[str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    out_file = tmp_path / "docker-argv"
    stub = bindir / "docker"
    stub.write_text(f"#!/bin/sh\nprintf '%s\\0' \"$@\" > \"{out_file}\"\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    base_env = dict(os.environ)
    for var in (
        "MODEL",
        "PORT",
        "ROLE",
        "GPU_MEM",
        "MAX_LEN",
        "SEQS",
        "BUDGET_PROFILE",
        "SERVED_NAME",
        "CONTAINER_NAME",
        "VLLM_IMAGE",
        "TASK",
        "CHAT_TEMPLATE",
        *_HOST_VARS,
    ):
        if var not in extra_env:
            base_env.pop(var, None)
    env = {
        **base_env,
        **({} if "HOST_MEMINFO" in extra_env else _host(tmp_path)),
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "BUDGET_PYTHON": sys.executable,
        **extra_env,
    }
    proc = subprocess.run(
        ["sh", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    argv: list[str] = []
    if out_file.exists():
        argv = out_file.read_bytes().decode().split('\0')[:-1]
    return proc.returncode, proc.stderr, argv


def _pairs(argv: list[str]) -> dict[str, str]:
    """Map --flag value pairs; boolean flags map to '1' when present."""
    pairs: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                pairs[argv[i]] = argv[i + 1]
                i += 2
            else:
                pairs[argv[i]] = "1"
                i += 1
        else:
            i += 1
    return pairs


def test_embed_server_flags_come_from_budget(tmp_path: Path):
    rc, stderr, argv = _run_script(
        tmp_path, {"MODEL": "Qwen/Qwen3-Embedding-0.6B", "PORT": "8001"}
    )
    assert rc == 0, stderr
    pairs = _pairs(argv)
    assert pairs["--gpu-memory-utilization"] == "0.33"
    assert pairs["--max-model-len"] == "4096"
    assert pairs["--runner"] == "pooling"
    assert pairs["--convert"] == "embed"
    assert pairs["--max-num-batched-tokens"] == "4096"
    assert pairs["--enforce-eager"] == "1"
    assert pairs["--max-num-seqs"] == "1"
    assert "--enable-prefix-caching" not in argv
    assert "Qwen/Qwen3-Embedding-0.6B" in argv


def test_reasoning_server_flags_come_from_budget(tmp_path: Path):
    rc, stderr, argv = _run_script(tmp_path, {"PORT": "8000"})
    assert rc == 0, stderr
    pairs = _pairs(argv)
    # Calibration change called out in the PR body: 0.65 -> 0.64 (Budget
    # upper-bound fit; soak envelope proven by the 6977 MiB measured peak).
    assert pairs["--gpu-memory-utilization"] == "0.64"
    assert pairs["--max-model-len"] == "4096"
    assert pairs["--max-num-seqs"] == "1"
    assert "--runner" not in pairs
    assert "--convert" not in pairs
    assert "--enforce-eager" not in pairs
    assert "--enable-prefix-caching" in pairs
    assert pairs["--reasoning-parser"] == "gemma4"


def test_role_derived_from_model_name_without_make(tmp_path: Path):
    """Direct invocation (no ROLE): the *embed* match selects the embed server."""
    rc, stderr, argv = _run_script(tmp_path, {"MODEL": "my-org/foo-embed-bar", "PORT": "8001"})
    assert rc == 0, stderr
    pairs = _pairs(argv)
    assert pairs["--gpu-memory-utilization"] == "0.33"
    assert pairs["--runner"] == "pooling"


def test_explicit_role_selects_server_not_name(tmp_path: Path):
    rc, stderr, argv = _run_script(
        tmp_path,
        {"MODEL": "Qwen/Qwen3-Embedding-0.6B", "PORT": "8000", "ROLE": "reasoning"},
    )
    assert rc == 0, stderr
    pairs = _pairs(argv)
    assert pairs["--gpu-memory-utilization"] == "0.64"
    assert "--runner" not in pairs


def test_explicit_env_wins_over_budget(tmp_path: Path):
    rc, stderr, argv = _run_script(
        tmp_path,
        {
            "MODEL": "Qwen/Qwen3-Embedding-0.6B",
            "PORT": "8001",
            "GPU_MEM": "0.90",
            "MAX_LEN": "2048",
            "SEQS": "4",
        },
    )
    assert rc == 0, stderr
    pairs = _pairs(argv)
    assert pairs["--gpu-memory-utilization"] == "0.90"
    assert pairs["--max-model-len"] == "2048"
    assert pairs["--max-num-seqs"] == "4"
    # Budget serving shape is retained, on the safe side: the batched cap
    # does not follow the operator's smaller window upward.
    assert pairs["--max-num-batched-tokens"] == "4096"
    assert pairs["--enforce-eager"] == "1"


def test_budget_failure_fails_closed_before_docker(tmp_path: Path):
    rc, stderr, argv = _run_script(tmp_path, {"BUDGET_PROFILE": "NOPE", "PORT": "8000"})
    assert rc != 0
    assert "serving-budget" in stderr
    assert argv == [], "container runtime must never exec on resolve failure"


def test_local_model_path_is_one_mount_argument_and_token_stays_off_argv(tmp_path: Path):
    model = tmp_path / 'model weights [local]'
    model.mkdir()
    rc, stderr, argv = _run_script(tmp_path, {
        'MODEL': str(model), 'ROLE': 'reasoning', 'HF_TOKEN': 'private-test-token',
        'HF_HOME': str(tmp_path / 'cache directory'),
    })
    assert rc == 0, stderr
    assert str(model) + ':/model:ro' in argv
    assert str(tmp_path / 'cache directory') + ':/root/.cache/huggingface' in argv
    assert 'HF_TOKEN' in argv
    assert 'private-test-token' not in ' '.join(argv) + stderr


def _run_script_with_stub_resolver(tmp_path: Path, extra_env: dict[str, str]) -> tuple[int, str, list[str]]:
    """BUDGET_PYTHON stub emitting canned assignments: exercises the script's
    flag conditionals independently of the Budget table."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    out_file = tmp_path / "docker-argv"
    stub_docker = bindir / "docker"
    stub_docker.write_text(f"#!/bin/sh\nprintf '%s\\0' \"$@\" > \"{out_file}\"\n")
    stub_docker.chmod(stub_docker.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    stub_python = bindir / "stub-python"
    stub_python.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' "
        "\"BUDGET_GPU_MEM='0.50'\" "
        "\"BUDGET_MAX_LEN='4096'\" "
        "\"BUDGET_RUNNER='generate'\" "
        "\"BUDGET_CONVERT='none'\" "
        "\"BUDGET_BATCHED_TOKENS=''\" "
        "\"BUDGET_EAGER='0'\" "
        "\"BUDGET_PREFIX_CACHE='${STUB_PREFIX_CACHE:-0}'\" "
        "\"BUDGET_SEQS='1'\" "
        "\"BUDGET_HOST_MEM_MB='${STUB_HOST_MEM_MB-4000}'\"\n"
    )
    stub_python.chmod(stub_python.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    base_env = dict(os.environ)
    for var in (
        "MODEL",
        "PORT",
        "ROLE",
        "GPU_MEM",
        "MAX_LEN",
        "SEQS",
        "BUDGET_PROFILE",
        "SERVED_NAME",
        "CONTAINER_NAME",
        "VLLM_IMAGE",
        "TASK",
        "CHAT_TEMPLATE",
        *_HOST_VARS,
    ):
        if var not in extra_env:
            base_env.pop(var, None)
    env = {
        **base_env,
        **({} if "HOST_MEMINFO" in extra_env else _host(tmp_path)),
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "BUDGET_PYTHON": str(stub_python),
        **extra_env,
    }
    proc = subprocess.run(
        ["sh", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    argv: list[str] = []
    if out_file.exists():
        argv = out_file.read_bytes().decode().split('\0')[:-1]
    return proc.returncode, proc.stderr, argv


def test_prefix_cache_flag_follows_budget_resolution(tmp_path: Path):
    rc, stderr, argv = _run_script_with_stub_resolver(tmp_path, {"STUB_PREFIX_CACHE": "1"})
    assert rc == 0, stderr
    pairs = _pairs(argv)
    assert pairs["--gpu-memory-utilization"] == "0.50"
    assert "--enable-prefix-caching" in argv


def test_prefix_cache_flag_absent_by_default(tmp_path: Path):
    rc, stderr, argv = _run_script_with_stub_resolver(tmp_path, {})
    assert rc == 0, stderr
    assert "--enable-prefix-caching" not in argv


def test_crc_reasoning_launch_is_eager_and_text_only(tmp_path):
    rc, stderr, argv = _run_script(tmp_path, {'BUDGET_PROFILE': 'LOCAL_CRC_32GB', 'ROLE': 'reasoning'})
    assert rc == 0, stderr
    pairs = _pairs(argv)
    assert pairs['--gpu-memory-utilization'] == '0.54'
    assert '--enforce-eager' in argv
    assert '--language-model-only' in argv
    assert pairs['--mm-processor-cache-gb'] == '0.0'
    assert pairs['--max-model-len'] == '4096'
    assert pairs['--max-num-seqs'] == '1'


def test_crc_embed_explicitly_disables_caches(tmp_path):
    rc, stderr, argv = _run_script(tmp_path, {'BUDGET_PROFILE': 'LOCAL_CRC_32GB', 'ROLE': 'embed',
                                           'MODEL': 'Qwen/Qwen3-Embedding-0.6B'})
    assert rc == 0, stderr
    assert _pairs(argv)['--gpu-memory-utilization'] == '0.43'
    assert '--enforce-eager' in argv
    assert '--no-enable-prefix-caching' in argv
    assert '--no-enable-chunked-prefill' in argv
    assert '--language-model-only' not in argv


def test_container_name_is_one_runtime_argument(tmp_path: Path):
    name = "rag-kind-model;$(touch SHOULD_NOT_EXIST)"
    rc, stderr, argv = _run_script(tmp_path, {"CONTAINER_NAME": name})
    assert rc == 0, stderr
    assert argv[:4] == ["run", "--rm", "--name", name]
    assert not (REPO_ROOT / "SHOULD_NOT_EXIST").exists()


def test_container_name_remains_optional(tmp_path: Path):
    rc, stderr, argv = _run_script(tmp_path, {})
    assert rc == 0, stderr
    assert "--name" not in argv


# --- Host-RAM budget (issue #580) ------------------------------------------


def _cap(argv: list[str]) -> tuple[str | None, str | None]:
    caps = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in argv if a.startswith("--memory")}
    return caps.get("--memory"), caps.get("--memory-swap")


def test_container_memory_cap_follows_budget_role(tmp_path: Path):
    expected = {
        ("LOCAL_RT_8GB", "reasoning", None): "6400m",
        ("LOCAL_RT_8GB", "embed", "Qwen/Qwen3-Embedding-0.6B"): "2600m",
        ("TRIPLE_8GB", "reasoning", "Qwen/Qwen2.5-0.5B-Instruct"): "4500m",
        ("TRIPLE_8GB", "rerank", "BAAI/bge-reranker-v2-m3"): "4700m",
    }
    for (profile, role, model), cap in expected.items():
        env = {"BUDGET_PROFILE": profile, "ROLE": role}
        if model:
            env["MODEL"] = model
        case = tmp_path / f"{profile}-{role}"
        case.mkdir()
        rc, stderr, argv = _run_script(case, env)
        assert rc == 0, stderr
        # Equal --memory-swap means no swap for the container.
        assert _cap(argv) == (cap, cap), (profile, role)
        # The cap is a runtime option, before the image and the vllm args.
        assert argv.index(f"--memory={cap}") < argv.index("--gpu-memory-utilization")


def test_host_mem_mb_env_overrides_budget_cap(tmp_path: Path):
    rc, stderr, argv = _run_script(tmp_path, {"HOST_MEM_MB": "3072"})
    assert rc == 0, stderr
    assert _cap(argv) == ("3072m", "3072m")


def test_invalid_host_mem_mb_fails_closed_before_docker(tmp_path: Path):
    for bad in ("lots", "0", "12g", "-5"):
        case = tmp_path / f"bad{abs(hash(bad))}"
        case.mkdir()
        rc, stderr, argv = _run_script(case, {"HOST_MEM_MB": bad})
        assert rc != 0, bad
        assert "HOST_MEM_MB" in stderr
        assert argv == []


def test_refuses_when_available_ram_below_cap_plus_headroom(tmp_path: Path):
    # 6400 cap + 2048 headroom = 8448 MiB needed.
    rc, stderr, argv = _run_script(tmp_path, _host(tmp_path, avail_mb=8447))
    assert rc == 75
    assert argv == [], "container runtime must never exec on refusal"
    assert "REFUSED: host RAM too low" in stderr
    assert "8447 MiB available" in stderr
    assert "FORCE_START=1" in stderr


def test_admits_at_exactly_cap_plus_headroom(tmp_path: Path):
    rc, stderr, argv = _run_script(tmp_path, _host(tmp_path, avail_mb=8448))
    assert rc == 0, stderr
    assert _cap(argv)[0] == "6400m"


def test_headroom_env_is_respected(tmp_path: Path):
    rc, _, argv = _run_script(tmp_path, _host(tmp_path, avail_mb=7000))
    assert rc == 75 and argv == []
    case = tmp_path / "relaxed"
    case.mkdir()
    rc, stderr, argv = _run_script(case, {**_host(case, avail_mb=7000), "HOST_MEM_HEADROOM_MB": "512"})
    assert rc == 0, stderr


def test_refuses_on_high_memory_or_io_pressure(tmp_path: Path):
    for resource in ("memory", "io"):
        case = tmp_path / resource
        case.mkdir()
        host = _host(case)
        (Path(host["HOST_PSI_DIR"]) / resource).write_text(
            "some avg10=10.00 avg60=0.00 avg300=0.00 total=0\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        )
        rc, stderr, argv = _run_script(case, host)
        assert rc == 75, resource
        assert argv == []
        assert f"REFUSED: host {resource} pressure is high" in stderr


def test_pressure_just_below_threshold_is_admitted(tmp_path: Path):
    rc, stderr, argv = _run_script(tmp_path, _host(tmp_path, psi=9.99))
    assert rc == 0, stderr
    case = tmp_path / "strict"
    case.mkdir()
    rc, _, argv = _run_script(case, {**_host(case, psi=9.99), "HOST_PSI_MAX": "5"})
    assert rc == 75 and argv == []


def test_missing_pressure_files_skip_that_check_with_notice(tmp_path: Path):
    host = _host(tmp_path)
    host["HOST_PSI_DIR"] = str(tmp_path / "no-such-dir")
    rc, stderr, argv = _run_script(tmp_path, host)
    assert rc == 0, stderr
    assert "skipping the memory pressure check" in stderr
    assert _cap(argv)[0] == "6400m"


def test_force_start_skips_admission_but_keeps_the_cap(tmp_path: Path):
    host = {**_host(tmp_path, avail_mb=100, psi=80.0), "FORCE_START": "1"}
    rc, stderr, argv = _run_script(tmp_path, host)
    assert rc == 0, stderr
    assert "FORCE_START=1" in stderr
    assert _cap(argv) == ("6400m", "6400m")


def test_stub_resolver_without_cap_requires_host_mem_mb(tmp_path: Path):
    rc, stderr, argv = _run_script_with_stub_resolver(tmp_path, {"STUB_HOST_MEM_MB": ""})
    assert rc != 0 and argv == []
    assert "HOST_MEM_MB" in stderr
    case = tmp_path / "explicit"
    case.mkdir()
    rc, stderr, argv = _run_script_with_stub_resolver(
        case, {"STUB_HOST_MEM_MB": "", "HOST_MEM_MB": "1500"}
    )
    assert rc == 0, stderr
    assert _cap(argv) == ("1500m", "1500m")
