"""Plan + fail-closed tests for scripts/run_local_stack.sh (local-dev only).

Hermetic: dry-run cases validate plans; actual-launcher cases use command and
service fakes without contacting Docker, models or network services.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run_local_stack.sh"


def _run(extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "LOCAL_STACK_DRYRUN": "1", **(extra_env or {})}
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)


def test_dryrun_plan_orders_stages():
    """Dry-run plan keeps the prod topology order (backends, Qdrant, Jaeger,
    gateway, probe, ingest, agent)."""
    r = _run()
    assert r.returncode == 0, r.stderr
    plan = "\n".join(ln for ln in r.stdout.splitlines() if ln.startswith("[plan]"))
    marks = ["backends", "qdrant", "run_local_jaeger.sh", "run_local_gateway.sh", "probe_gateway.py", "ingest", "uvicorn"]
    positions = [plan.index(m) for m in marks]
    assert positions == sorted(positions)
    assert "UI_ENABLED=true" in plan


def test_dryrun_plan_reflects_operator_overrides(tmp_path):
    r = _run({
        "GATEWAY_PROBE_MAX_TOKENS": "512", "UI_ENABLED": "false", "OTEL_TRACE_TIMEOUT": "7",
        "LOCAL_AGENT_PORT": "9099", "CORPUS_DIR": str(tmp_path), "RERANK_ENABLED": "false",
    })
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert "--max-tokens 512" in next(ln for ln in lines if "probe_gateway.py" in ln)
    assert "UI_ENABLED=false" in next(ln for ln in lines if "uvicorn" in ln)
    ingest = next(ln for ln in lines if "ingest" in ln)
    assert str(tmp_path) in ingest and "skipped" not in ingest
    # A shared service name would merge agent and ingest in one Jaeger.
    assert "OTEL_SERVICE_NAME=mainframe-rag-ingest" in ingest
    assert "timeout 7s" in r.stdout and "--port 9099" in r.stdout
    assert "8002" not in lines[0]


@pytest.mark.parametrize("bad", ["0", "4097", "bad"])
def test_bad_probe_budget_fails_before_stack_work(bad):
    r = _run({"GATEWAY_PROBE_MAX_TOKENS": bad})
    assert r.returncode != 0
    assert "GATEWAY_PROBE_MAX_TOKENS must be" in r.stderr
    assert "[plan]" not in r.stdout


def test_persistent_progress_reaches_ingest_after_stack_cleanup_and_restart(tmp_path):
    scripts = tmp_path / "checkout" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
    gateway = scripts / "run_local_gateway.sh"
    gateway.write_text('#!/bin/sh\nprintf "export EMBED_MODEL=fixture\\n" > "$GATEWAY_ENV_FILE"\nexec sleep 30\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text('#!/bin/sh\ncase "$*" in */healthz*) printf 503;; *) printf 200;; esac\n')
    (bindir / "docker").write_text('#!/bin/sh\nexit 99\n')
    consumer = bindir / "consumer-python"
    consumer.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
if sys.argv[1:3] == ["-m", "mainframe_rag.ingest.run_ingest"]:
    assert not any(name.casefold() in {"qdrant_api_key", "qdrant_ingest_api_key"} for name in os.environ)
    progress = sys.argv[sys.argv.index("--progress") + 1]
    with Path(progress).open("a") as target:
        target.write("retained checkpoint\\n")
    with open(os.environ["INGEST_CALLS"], "a") as target:
        target.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["-m", "uvicorn"]:
    assert not any(name.casefold() in {"qdrant_api_key", "qdrant_ingest_api_key"} for name in os.environ)
    sys.exit(23)
''')
    for path in bindir.iterdir():
        path.chmod(0o755)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    persistent = tmp_path / "persistent ' state ; Ω"
    persistent.mkdir()
    progress = persistent / "inventory.jsonl"
    calls = tmp_path / "calls.jsonl"
    keyless_env = {name: value for name, value in os.environ.items()
                   if name.casefold() not in {"qdrant_api_key", "qdrant_ingest_api_key"}}
    for attempt in range(2):
        logs = tmp_path / f"logs-{attempt}"
        logs.mkdir()
        handoff = tmp_path / f"gateway-{attempt}.env"
        result = subprocess.run(["sh", str(scripts / SCRIPT.name)], text=True,
            capture_output=True, check=False, timeout=15, env={**keyless_env,
            "PATH": f"{bindir}:{os.environ['PATH']}", "PY": str(consumer),
            "LOCAL_STACK_DRYRUN": "0", "LOCAL_STACK_LOG_DIR": str(logs),
            "LOCAL_INGEST_PROGRESS": str(progress), "INGEST_CALLS": str(calls),
            "CORPUS_DIR": str(corpus), "GATEWAY_ENV_FILE": str(handoff),
            "INGEST_WORKERS": "1", "FORCE_START": "1"})
        # Stop before any application runs; successful fake ingest precedes cleanup.
        assert result.returncode == 1 and "agent exited early" in result.stderr, result.stderr
        assert progress.read_text() == "retained checkpoint\n" * (attempt + 1)
        assert not handoff.exists()
        assert not (logs / "local-stack-ingest-progress.jsonl").exists()
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == 2
    assert [argv[argv.index("--progress") + 1] for argv in invocations] == [str(progress)] * 2


@pytest.mark.parametrize(
    ("reader", "writer", "exported_scratch", "unrelated_note"),
    [
        (" \tfixture-readonly-706|=literal\t ", " \tfixture-writer-706;$literal|=end\t ", True, None),
        (
            "fixture-readonly-706|=literal",
            "fixture-writer-706;$literal|=end",
            False,
            "text\nQDRANT_INGEST_API_KEY=not-an-env-entry",
        ),
    ],
    ids=["whitespace-and-inherited-scratch", "unrelated-multiline-value"],
)
def test_authenticated_stack_hands_writer_only_to_ingest_and_readonly_to_agent(
    tmp_path, monkeypatch, reader, writer, exported_scratch, unrelated_note
):
    if unrelated_note is not None:
        monkeypatch.setenv("NOTE", unrelated_note)
    scripts = tmp_path / "checkout" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
    (scripts / "run_local_gateway.sh").write_text('''#!/bin/sh
"$PY" --fixture-gateway
printf 'export EMBED_MODEL=fixture\\n' > "$GATEWAY_ENV_FILE"
exec sleep 30
''')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text('#!/bin/sh\ncase "$*" in */healthz*) printf 503;; *) printf 200;; esac\n')
    (bindir / "docker").write_text('#!/bin/sh\nexit 99\n')
    consumer = bindir / "consumer-python"
    # Parameters are independent original fixture values, never credentials.
    consumer.write_text(f"#!{sys.executable}\n" + f'''import json, os, sys
sys.path.insert(0, {str(REPO / "src")!r})
if sys.argv[1:3] == ["-m", "mainframe_rag.ingest.run_ingest"]:
    role = "ingest"
elif sys.argv[1:3] == ["-m", "uvicorn"]:
    role = "agent"
elif sys.argv[1:] == ["--fixture-gateway"]:
    role = "gateway"
else:
    role = "probe"
keys = {{name: value for name, value in os.environ.items()
        if name.casefold() in {{"qdrant_api_key", "qdrant_ingest_api_key"}}}}
resolved = None
if role in {{"ingest", "agent"}}:
    from mainframe_rag.config import Settings
    resolved = Settings(_env_file=None).qdrant_api_key
with open(os.environ["ROLE_CALLS"], "a") as target:
    target.write(json.dumps({{"role": role, "keys": keys, "resolved": resolved,
        "writer_present": any({writer!r} in value for value in os.environ.values())}}) + "\\n")
if role == "agent":
    sys.exit(23)
''')
    for path in bindir.iterdir():
        path.chmod(0o755)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    calls = tmp_path / "role-calls.jsonl"
    handoff = tmp_path / "gateway.env"
    clean = {name: value for name, value in os.environ.items()
             if name.casefold() not in {"qdrant_api_key", "qdrant_ingest_api_key"}
             and name not in _INGEST_VARS}
    # An inherited variable keeps its export attribute when reassigned unless
    # the supervisor explicitly unsets it, including the raw-env scratch copy.
    scratch = {name: writer for name in (
        "_STACK_QDRANT_WRITE_KEY", "_qdrant_value", "_qdrant_names"
    )} if exported_scratch else {}
    result = subprocess.run(["sh", str(scripts / SCRIPT.name)], text=True,
        capture_output=True, check=False, timeout=15, env={**clean, **scratch, **_host(tmp_path),
        "PATH": f"{bindir}:{os.environ['PATH']}", "PY": str(consumer),
        "LOCAL_STACK_DRYRUN": "0", "LOCAL_STACK_LOG_DIR": str(logs),
        "ROLE_CALLS": str(calls), "CORPUS_DIR": str(corpus),
        "GATEWAY_ENV_FILE": str(handoff), "INGEST_WORKERS": "1",
        "RERANK_ENABLED": "false",
        # Settings accepts one unambiguous mixed-case spelling per role.
        "qDrAnT_aPi_KeY": reader, "qDrAnT_iNgEsT_aPi_KeY": writer})
    assert result.returncode == 1 and "agent exited early" in result.stderr, result.stderr
    observed = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [row["role"] for row in observed] == ["gateway", "probe", "ingest", "agent"]
    children = {row["role"]: row for row in observed}
    assert children["ingest"]["resolved"] == writer
    assert children["ingest"]["keys"] == {"QDRANT_API_KEY": writer}
    assert children["agent"]["resolved"] == reader
    assert children["agent"]["keys"] == {"QDRANT_API_KEY": reader}
    for role in ("gateway", "probe", "agent"):
        assert not children[role]["writer_present"], role
        assert not any(name.casefold() == "qdrant_ingest_api_key" for name in children[role]["keys"]), role
    assert not handoff.exists()
    for path in (logs / "local-stack-gateway.log", logs / "local-stack-agent.log"):
        text = path.read_text()
        assert reader not in text and writer not in text
    assert reader not in result.stdout + result.stderr and writer not in result.stdout + result.stderr


def test_qdrant_credential_enumerator_failure_refuses_before_service_children(tmp_path):
    scripts = tmp_path / "checkout" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
    (scripts / "run_local_gateway.sh").write_text('#!/bin/sh\nprintf "gateway\\n" >> "$SERVICE_CALLS"\nexit 23\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "env").write_text('''#!/bin/sh
printf '%s\\n' "$qDrAnT_iNgEsT_aPi_KeY" >&2
exit 9
''')
    for name in ("curl", "docker", "consumer-python"):
        (bindir / name).write_text(f'#!/bin/sh\nprintf "{name}\\n" >> "$SERVICE_CALLS"\nexit 23\n')
    for path in bindir.iterdir():
        path.chmod(0o755)
    calls = tmp_path / "service-calls"
    reader = "fixture-enumerator-readonly-706"
    writer = "fixture-enumerator-writer-706"
    clean = {name: value for name, value in os.environ.items()
             if name.casefold() not in {"qdrant_api_key", "qdrant_ingest_api_key"}
             and name not in _INGEST_VARS}
    result = subprocess.run(["sh", str(scripts / SCRIPT.name)], text=True,
        capture_output=True, check=False, timeout=10, env={**clean, **_host(tmp_path),
        "PATH": f"{bindir}:{os.environ['PATH']}", "PY": str(bindir / "consumer-python"),
        "LOCAL_STACK_DRYRUN": "0", "LOCAL_STACK_LOG_DIR": str(tmp_path),
        "GATEWAY_ENV_FILE": str(tmp_path / "gateway.env"),
        "CORPUS_DIR": str(tmp_path), "INGEST_WORKERS": "1", "RERANK_ENABLED": "false",
        "SERVICE_CALLS": str(calls), "qDrAnT_aPi_KeY": reader, "qDrAnT_iNgEsT_aPi_KeY": writer})
    assert not calls.exists(), "credential prerequisite failure reached a service/probe child"
    assert result.returncode != 0
    assert "Qdrant credential environment is unavailable" in result.stderr
    assert reader not in result.stdout + result.stderr and writer not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("credentials", "diagnostic"),
    [
        ({"QDRANT_API_KEY": "fixture-readonly-706"},
         "authenticated Qdrant ingest requires a distinct writer credential"),
        ({"QDRANT_API_KEY": "fixture-shared-706", "QDRANT_INGEST_API_KEY": "fixture-shared-706"},
         "Qdrant requires distinct read-only and ingest credentials"),
        ({"QDRANT_API_KEY": "fixture-readonly-706", "qdrant_api_key": "fixture-other-reader-706",
          "QDRANT_INGEST_API_KEY": "fixture-writer-706"}, "Qdrant credential aliases conflict"),
    ],
    ids=["missing-writer", "equal-roles", "reader-alias-conflict"],
)
def test_unsafe_qdrant_role_inputs_refuse_before_service_children(tmp_path, credentials, diagnostic):
    scripts = tmp_path / "checkout" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
    (scripts / "run_local_gateway.sh").write_text('#!/bin/sh\nprintf "gateway\\n" >> "$SERVICE_CALLS"\nexit 23\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("curl", "docker", "consumer-python"):
        path = bindir / name
        path.write_text(f'#!/bin/sh\nprintf "{name}\\n" >> "$SERVICE_CALLS"\nexit 23\n')
        path.chmod(0o755)
    calls = tmp_path / "service-calls"
    clean = {name: value for name, value in os.environ.items()
             if name.casefold() not in {"qdrant_api_key", "qdrant_ingest_api_key"}
             and name not in _INGEST_VARS}
    result = subprocess.run(["sh", str(scripts / SCRIPT.name)], text=True,
        capture_output=True, check=False, timeout=10, env={**clean, **_host(tmp_path), **credentials,
        "PATH": f"{bindir}:{os.environ['PATH']}", "PY": str(bindir / "consumer-python"),
        "LOCAL_STACK_DRYRUN": "0", "LOCAL_STACK_LOG_DIR": str(tmp_path),
        "GATEWAY_ENV_FILE": str(tmp_path / "gateway.env"), "CORPUS_DIR": str(tmp_path),
        "INGEST_WORKERS": "1", "RERANK_ENABLED": "false", "SERVICE_CALLS": str(calls)})
    assert not calls.exists(), "unsafe credentials reached a service/probe child"
    assert result.returncode != 0 and diagnostic in result.stderr
    assert all(value not in result.stdout + result.stderr for value in credentials.values())


@pytest.mark.parametrize(
    ("var", "bad"),
    [("GATEWAY_PORT", "not-a-number"), ("DENSE_DIM", "0"), ("OTEL_TRACE_TIMEOUT", "0")],
)
def test_bad_numeric_fails_closed(var, bad):
    r = _run({var: bad})
    assert r.returncode != 0
    assert f"{var} must be" in r.stderr


@pytest.mark.parametrize("var", ["QDRANT_URL", "GATEWAY_RERANK_URL"])
def test_bare_hostname_fails_closed(var):
    r = _run({var: "gpu-box:8000"})
    assert r.returncode != 0
    assert f"{var} must begin with http:// or https://" in r.stderr


def test_env_file_inside_repo_fails_closed():
    r = _run({"GATEWAY_ENV_FILE": str(REPO / "dist" / "keys.env")})
    assert r.returncode != 0
    assert "must not live inside the repo" in r.stderr


def test_missing_corpus_dir_fails_closed():
    r = _run({"CORPUS_DIR": "/nonexistent/corpus"})
    assert r.returncode != 0
    assert "CORPUS_DIR is not a directory" in r.stderr


def test_invalid_rerank_flag_fails_before_launch():
    r = _run({'RERANK_ENABLED': 'flase'})
    assert r.returncode != 0
    assert 'RERANK_ENABLED' in r.stderr


def test_two_backend_live_path_preserves_flag_after_gateway_handoff(tmp_path):
    # Stop deliberately at the probe after recording its actual environment.
    # This traverses real backend validation and the gateway handoff, without I/O.
    scripts = tmp_path / 'checkout' / 'scripts'
    scripts.mkdir(parents=True)
    (scripts / SCRIPT.name).write_text(SCRIPT.read_text())
    (scripts / 'run_local_gateway.sh').write_text('''#!/bin/sh
printf 'export RERANK_ENABLED=true\\n' > "$GATEWAY_ENV_FILE"
''')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    log = tmp_path / 'calls'
    (bindir / 'curl').write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
printf '200'
''')
    (bindir / 'docker').write_text('#!/bin/sh\nexit 99\n')
    py = bindir / 'python'
    py.write_text('''#!/bin/sh
printf 'probe-rerank=%s\\n' "$RERANK_ENABLED" >> "$CALLS"
exit 23
''')
    for file in bindir.iterdir():
        file.chmod(0o755)
    result = subprocess.run(['sh', str(scripts / SCRIPT.name)], capture_output=True, text=True, check=False,
        env={**os.environ, 'PATH': str(bindir) + ':' + os.environ['PATH'], 'CALLS': str(log),
             'LOCAL_STACK_DRYRUN': '0', 'RERANK_ENABLED': 'false', 'PY': str(py),
             'GATEWAY_ENV_FILE': str(tmp_path / 'gateway.env'), 'LOCAL_STACK_LOG_DIR': str(tmp_path)})
    assert result.returncode == 23, result.stderr
    calls = log.read_text()
    assert '8000/v1/models' in calls and '8001/v1/models' in calls
    assert '8002' not in calls
    assert 'probe-rerank=false' in calls


# --- Local ingest host-RAM budget (issue #580) -------------------------------

_INGEST_VARS = (
    "INGEST_WORKERS",
    "LOCAL_INGEST_WORKER_MB",
    "HOST_MEM_HEADROOM_MB",
    "HOST_PSI_MAX",
    "HOST_MEMINFO",
    "HOST_PSI_DIR",
    "HOST_CPUS",
    "FORCE_START",
)


def _host(tmp_path: Path, avail_mb: int = 64000, psi: float = 0.0) -> dict[str, str]:
    """Stub host state (meminfo + PSI) and CPU count; tests never read the real host."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal: 99999999 kB\nMemAvailable: {avail_mb * 1024} kB\n")
    psi_dir = tmp_path / "pressure"
    psi_dir.mkdir(exist_ok=True)
    line = f"some avg10={psi:.2f} avg60=0.00 avg300=0.00 total=0\n"
    for resource in ("memory", "io"):
        (psi_dir / resource).write_text(line + line.replace("some", "full"))
    return {"HOST_MEMINFO": str(meminfo), "HOST_PSI_DIR": str(psi_dir), "HOST_CPUS": "8"}


def _plan_workers(tmp_path: Path, host: dict[str, str]) -> tuple[subprocess.CompletedProcess, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    clean = {k: v for k, v in os.environ.items() if k not in _INGEST_VARS}
    r = subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={**clean, "LOCAL_STACK_DRYRUN": "1", "CORPUS_DIR": str(corpus), **host},
    )
    ingest = next((ln for ln in r.stdout.splitlines() if "ingest" in ln), "")
    return r, ingest


@pytest.mark.parametrize(
    ("avail_mb", "workers"),
    [
        (64000, 7),  # plenty of RAM: the CPU-1 cap (8 CPUs) applies
        (4608, 2),  # a few GB free sizes down
        (3072, 1),  # exactly one worker + headroom
    ],
)
def test_ingest_workers_follow_available_ram(tmp_path, avail_mb, workers):
    r, ingest = _plan_workers(tmp_path, _host(tmp_path, avail_mb=avail_mb))
    assert r.returncode == 0, r.stderr
    assert f"--workers {workers} " in ingest
    assert "REFUSED" not in ingest and "REFUSED" not in r.stderr


def test_ingest_workers_env_overrides_sizing(tmp_path):
    r, ingest = _plan_workers(tmp_path, {**_host(tmp_path, avail_mb=64000), "INGEST_WORKERS": "2"})
    assert r.returncode == 0, r.stderr
    assert "--workers 2 " in ingest


@pytest.mark.parametrize("bad", ["0", "lots"])
def test_invalid_ingest_workers_fails_closed(tmp_path, bad):
    r, _ = _plan_workers(tmp_path, {**_host(tmp_path), "INGEST_WORKERS": bad})
    assert r.returncode != 0
    assert "INGEST_WORKERS must be a positive integer" in r.stderr


def _live(tmp_path: Path, host: dict[str, str], extra: dict[str, str] | None = None):
    """Run the live path with stub gateway/curl/docker/python up to the ingest."""
    scripts = tmp_path / "checkout" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / SCRIPT.name).write_text(SCRIPT.read_text())
    (scripts / "run_local_gateway.sh").write_text(
        "#!/bin/sh\nprintf 'export RERANK_ENABLED=true\\n' > \"$GATEWAY_ENV_FILE\"\n"
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    (bindir / "curl").write_text("#!/bin/sh\nprintf '200'\n")
    (bindir / "docker").write_text("#!/bin/sh\nexit 99\n")
    py = bindir / "python"
    # Every python call is recorded; the agent health check then sees a 200
    # and the script stops, so the run ends right after the ingest.
    py.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\n")
    for file in bindir.iterdir():
        file.chmod(0o755)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    clean = {k: v for k, v in os.environ.items() if k not in _INGEST_VARS}
    result = subprocess.run(
        ["sh", str(scripts / SCRIPT.name)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **clean,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "CALLS": str(calls),
            "PY": str(py),
            "CORPUS_DIR": str(corpus),
            "GATEWAY_ENV_FILE": str(tmp_path / "gateway.env"),
            "LOCAL_STACK_LOG_DIR": str(tmp_path),
            **host,
            **(extra or {}),
        },
    )
    return result, (calls.read_text().splitlines() if calls.exists() else [])


def _ingest_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if "mainframe_rag.ingest.run_ingest" in c]


@pytest.mark.parametrize("value", [None, "1"])
def test_ingest_is_forced_only_when_reingest_is_selected(tmp_path, value):
    extra = {} if value is None else {"LOCAL_INGEST_REINGEST": value}
    r, calls = _live(tmp_path, _host(tmp_path), extra)
    ingest = _ingest_calls(calls)
    assert len(ingest) == 1, (r.stderr, calls)
    assert ("--reingest" in ingest[0]) == (value == "1")


@pytest.mark.parametrize("bad", ["true", " 1"])
def test_invalid_reingest_choice_fails_before_stack_work(tmp_path, bad):
    r = _run({"CORPUS_DIR": str(tmp_path), "LOCAL_INGEST_REINGEST": bad})
    assert r.returncode != 0
    assert "LOCAL_INGEST_REINGEST must be 0 or 1" in r.stderr
    assert "[plan]" not in r.stdout


def test_reingest_without_corpus_fails_before_stack_work():
    r = _run({"CORPUS_DIR": "", "LOCAL_INGEST_REINGEST": "1"})
    assert r.returncode != 0
    assert "LOCAL_INGEST_REINGEST=1 requires CORPUS_DIR" in r.stderr
    assert "[plan]" not in r.stdout


def test_live_low_ram_refuses_before_any_model_or_ingest_call(tmp_path):
    r, calls = _live(tmp_path, _host(tmp_path, avail_mb=3071))
    assert r.returncode == 75
    assert calls == [], "nothing may run (probe, ingest) on refusal"
    assert "REFUSED: host RAM too low" in r.stderr and "FORCE_START=1" in r.stderr


@pytest.mark.parametrize("resource", ["memory", "io"])
def test_live_pressure_refuses(tmp_path, resource):
    host = _host(tmp_path)
    (Path(host["HOST_PSI_DIR"]) / resource).write_text(
        "some avg10=10.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    r, calls = _live(tmp_path, host)
    assert r.returncode == 75 and calls == []
    assert f"REFUSED: host {resource} pressure is high" in r.stderr


def test_live_pressure_just_below_threshold_is_admitted(tmp_path):
    r, calls = _live(tmp_path, _host(tmp_path, psi=9.99))
    assert _ingest_calls(calls), r.stderr


def test_live_explicit_workers_are_still_admission_checked(tmp_path):
    # 7 workers x 1024 + 2048 = 9216 MiB needed.
    low = tmp_path / "low"
    low.mkdir()
    r, calls = _live(low, _host(low, avail_mb=9215), {"INGEST_WORKERS": "7"})
    assert r.returncode == 75 and calls == []
    ok = tmp_path / "ok"
    ok.mkdir()
    r, calls = _live(ok, _host(ok, avail_mb=9216), {"INGEST_WORKERS": "7"})
    ingest = _ingest_calls(calls)
    assert ingest and "--workers 7 " in ingest[0]


def test_live_force_start_skips_refusal_but_still_sizes_workers(tmp_path):
    host = _host(tmp_path, avail_mb=100, psi=80.0)
    r, calls = _live(tmp_path, host, {"FORCE_START": "1"})
    ingest = _ingest_calls(calls)
    assert ingest and "--workers 1 " in ingest[0], r.stderr
    assert "FORCE_START=1" in r.stderr


def test_missing_host_files_skip_checks_with_notice(tmp_path):
    host = {"HOST_MEMINFO": str(tmp_path / "none"), "HOST_PSI_DIR": str(tmp_path / "nodir"), "HOST_CPUS": "4"}
    r, ingest = _plan_workers(tmp_path, host)
    assert r.returncode == 0, r.stderr
    assert "--workers 3 " in ingest  # falls back to the CPU-1 default
    assert "skipping the RAM admission check" in r.stderr
    assert "skipping the memory pressure check" in r.stderr
