"""Budget resolve CLI (serving-budget track, PR-B).

Subprocess tests against the real CLI: stdout must be exactly the KEY='v'
assignment set (anything else breaks the script's eval), failures exit
nonzero with diagnostics on stderr and NOTHING eval-able on stdout.
sys.executable is the gate python (project venv), so no env/network/GPU.
"""

import re
import subprocess
import sys

from mainframe_rag.serve import PROFILES, resolve
from mainframe_rag.serve.budget import ProfileBundle

_ASSIGN = re.compile(r"^([A-Z_]+)='([A-Za-z0-9._-]*)'$")


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mainframe_rag.serve", *argv],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _assignments(proc: subprocess.CompletedProcess[str]) -> dict[str, str]:
    assert proc.returncode == 0, f"rc={proc.returncode} stderr={proc.stderr}"
    parsed: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        match = _ASSIGN.fullmatch(line)
        assert match is not None, f"non-assignment stdout line breaks script eval: {line!r}"
        parsed[match.group(1)] = match.group(2)
    return parsed


def test_resolve_embed_assignments():
    parsed = _assignments(_run("resolve", "--profile", "LOCAL_RT_8GB", "--role", "embed"))
    assert parsed == {
        "BUDGET_GPU_MEM": "0.33",
        "BUDGET_MAX_LEN": "4096",
        "BUDGET_RUNNER": "pooling",
        "BUDGET_CONVERT": "embed",
        "BUDGET_BATCHED_TOKENS": "4096",
        "BUDGET_EAGER": "1",
        "BUDGET_SEQS": "1",
    }


def test_resolve_reasoning_assignments():
    parsed = _assignments(_run("resolve", "--profile", "LOCAL_RT_8GB", "--role", "reasoning"))
    assert parsed == {
        "BUDGET_GPU_MEM": "0.64",
        "BUDGET_MAX_LEN": "4096",
        "BUDGET_RUNNER": "generate",
        "BUDGET_CONVERT": "none",
        "BUDGET_BATCHED_TOKENS": "",
        "BUDGET_EAGER": "0",
        "BUDGET_SEQS": "1",
    }


def test_cli_matches_library_for_every_profile_server():
    """Drift-guard twin: the CLI must emit exactly what resolve() computes,
    for every declared server — still true when the table changes."""
    for profile in PROFILES.values():
        for spec in profile.servers:
            parsed = _assignments(_run("resolve", "--profile", profile.name, "--role", spec.role))
            (server,) = resolve(
                ProfileBundle(name=profile.name, host=profile.host, servers=[spec])
            ).servers
            assert parsed["BUDGET_GPU_MEM"] == f"{server.gpu_memory_utilization:.2f}"
            assert parsed["BUDGET_MAX_LEN"] == str(server.max_model_len)
            assert parsed["BUDGET_RUNNER"] == server.runner
            assert parsed["BUDGET_CONVERT"] == server.convert
            assert parsed["BUDGET_BATCHED_TOKENS"] == str(server.max_num_batched_tokens or "")
            assert parsed["BUDGET_EAGER"] == ("1" if server.enforce_eager else "0")
            assert parsed["BUDGET_SEQS"] == str(server.max_num_seqs)


def test_unknown_profile_fails_closed():
    proc = _run("resolve", "--profile", "NOPE", "--role", "embed")
    assert proc.returncode == 2
    assert "NOPE" in proc.stderr
    assert "LOCAL_RT_8GB" in proc.stderr
    assert proc.stdout.strip() == ""


def test_unknown_role_fails_closed():
    proc = _run("resolve", "--profile", "LOCAL_RT_8GB", "--role", "rerank")
    assert proc.returncode == 2
    assert "rerank" in proc.stderr
    assert "embed" in proc.stderr
    assert proc.stdout.strip() == ""


def test_deficit_host_override_fails_closed_with_remedies():
    proc = _run(
        "resolve",
        "--profile",
        "LOCAL_RT_8GB",
        "--role",
        "embed",
        "--total-vram-mib",
        "2000",
    )
    assert proc.returncode == 1
    assert "BUDGET DEFICIT" in proc.stderr
    assert "Qwen/Qwen3-Embedding-0.6B" in proc.stderr
    assert proc.stdout.strip() == ""


def test_host_override_resolves_hypothetical_host():
    """--total-vram-mib sizes a what-if host: same rules, different total."""
    parsed = _assignments(
        _run(
            "resolve",
            "--profile",
            "LOCAL_RT_8GB",
            "--role",
            "embed",
            "--total-vram-mib",
            "16302",
        )
    )
    assert parsed["BUDGET_GPU_MEM"] == "0.17"
    assert parsed["BUDGET_MAX_LEN"] == "4096"
