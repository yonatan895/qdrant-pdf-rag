"""run_local_vllm.sh Budget integration (serving-budget track, PR-B).

End-to-end through the real POSIX script with a stub `docker` on PATH that
records its argv instead of launching a container: asserts the exact vLLM
flags the script derives from Budget resolution, the explicit-env override
rule, and fail-closed behavior. Hermetic: no docker, no GPU, no network —
only the venv python resolver and /bin/sh.
"""

import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run_local_vllm.sh"


def _run_script(tmp_path: Path, extra_env: dict[str, str]) -> tuple[int, str, list[str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    out_file = tmp_path / "docker-argv"
    stub = bindir / "docker"
    stub.write_text(f"#!/bin/sh\necho \"$@\" > \"{out_file}\"\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = {
        **os.environ,
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
        argv = shlex.split(out_file.read_text())
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


def _run_script_with_stub_resolver(tmp_path: Path, extra_env: dict[str, str]) -> tuple[int, str, list[str]]:
    """BUDGET_PYTHON stub emitting canned assignments: exercises the script's
    flag conditionals independently of the Budget table."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    out_file = tmp_path / "docker-argv"
    stub_docker = bindir / "docker"
    stub_docker.write_text(f"#!/bin/sh\necho \"$@\" > \"{out_file}\"\n")
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
        "\"BUDGET_SEQS='1'\"\n"
    )
    stub_python.chmod(stub_python.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = {
        **os.environ,
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
        argv = shlex.split(out_file.read_text())
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
