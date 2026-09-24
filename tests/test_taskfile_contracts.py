"""Task runner-boundary contracts for issue #402 (D-gate: final Task-only interface).

Exercises the ACTUAL pinned Task binary against the migrated Taskfiles with
inert process-boundary recorders in temporary workspaces. Expectations below
are hardcoded (independent of the YAML under test); the YAML is the
implementation, the recorder log is the evidence.

Make/Task parity and historical-pre-402 suites were retired with the Makefile
shim at the D-gate; the old→new map lives in docs/task-runner.md#inventory.

Needs a provisioned Task binary (scripts/tools/install-task.sh): TASK_BIN,
then .tools/bin/task, then PATH `task`, with `task --version` matching
scripts/tools/task-pin.txt. Without it these tests SKIP, unless
TASK_CONTRACTS_REQUIRE_RUNNER=1 is set (designated CI lane), where a missing
or mismatched binary FAILS instead of passing silently.

Hermetic: no venv installs, no Docker/GPU/model/cluster, no network, no
private config. Only stdlib (runnable with the system interpreter).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PIN_VERSION = ""
for _line in (REPO / "scripts/tools/task-pin.txt").read_text(encoding="utf-8").splitlines():
    if _line.startswith("version:"):
        PIN_VERSION = _line.split(":", 1)[1].strip().lstrip("v")
REQUIRE_RUNNER = os.environ.get("TASK_CONTRACTS_REQUIRE_RUNNER") == "1"

# Environment keys the recorders capture per invocation. Deliberately an
# allowlist (never the whole environment): failure output must not leak
# unrelated caller configuration, let alone secret-bearing variables.
LOGGED_ENV_KEYS = ("EMBED_MODE", "VENUE", "PYTHONPATH", "LLM_STREAM", "UI_ENABLED",
                   "ROLE", "MODEL", "SERVED_NAME", "CONTAINER_NAME", "PORT", "BUDGET_PROFILE", "BUDGET_PYTHON",
                   "GATEWAY_PORT", "CORPUS_DIR", "LOCAL_AGENT_PORT", "JAEGER_PORT",
                   "SIM_CONTAINER", "SIM_PORT",
                   "GPU_MEM", "MAX_LEN", "SEQS", "LOCAL_STACK_DRYRUN")

RECORDER = """#!/bin/sh
# Inert boundary recorder: appends one JSON line per invocation, then exits
# with $RECORDER_EXIT (default 0). Never touches the network or services.
python3 - "$RECORDER_LOG" "$RECORDER_TAG" "$@" <<'PYEOF'
import json, os, sys
log, tag, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
keys = ("EMBED_MODE", "VENUE", "PYTHONPATH", "LLM_STREAM", "UI_ENABLED",
        "ROLE", "MODEL", "SERVED_NAME", "CONTAINER_NAME", "PORT", "BUDGET_PROFILE", "BUDGET_PYTHON",
        "GATEWAY_PORT", "CORPUS_DIR", "LOCAL_AGENT_PORT", "JAEGER_PORT",
        "SIM_CONTAINER", "SIM_PORT",
        "GPU_MEM", "MAX_LEN", "SEQS", "LOCAL_STACK_DRYRUN")
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tag": tag, "argv": argv, "cwd": os.getcwd(),
                         "env": {k: os.environ.get(k) for k in keys}}) + "\\n")
PYEOF
exit "${RECORDER_EXIT:-0}"
"""

VENV_FAKE = """#!/bin/sh
# Fake .venv interpreter: answers `-V` from $FAKE_PY_VERSION without logging
# (status probes stay observable through rebuild/skip behavior), records all
# real build invocations as JSON. Produces deterministic stand-in members on success.
if [ "$1" = "-V" ]; then echo "${FAKE_PY_VERSION:-Python 3.14.5}"; exit 0; fi
python3 - "$RECORDER_LOG" "venv-python" "$@" <<'PYEOF'
import json, os, sys
log, tag, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
keys = ("EMBED_MODE", "VENUE", "PYTHONPATH", "LLM_STREAM", "UI_ENABLED",
        "ROLE", "MODEL", "SERVED_NAME", "CONTAINER_NAME", "PORT", "BUDGET_PROFILE", "BUDGET_PYTHON",
        "GATEWAY_PORT", "CORPUS_DIR", "LOCAL_AGENT_PORT", "JAEGER_PORT",
        "SIM_CONTAINER", "SIM_PORT",
        "GPU_MEM", "MAX_LEN", "SEQS", "LOCAL_STACK_DRYRUN")
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tag": tag, "argv": argv, "cwd": os.getcwd(),
                         "env": {k: os.environ.get(k) for k in keys}}) + "\\n")
ret = int(os.environ.get("RECORDER_EXIT", "0"))
if ret != 0:
    sys.exit(ret)
if "-m" in argv and "pip" in argv and "wheel" in argv and "-w" in argv:
    idx = argv.index("-w") + 1
    if idx < len(argv):
        os.makedirs(argv[idx], exist_ok=True)
        with open(os.path.join(argv[idx], "fake_pkg-1.0.0-py3-none-any.whl"), "w") as whl:
            whl.write("fake-wheel-content\\n")
if any("fetch_bm25_weights.py" in a for a in argv):
    if "--out" in argv:
        idx = argv.index("--out") + 1
        if idx < len(argv):
            snap = os.path.join(argv[idx], "models--fake", "snapshots", "snap1")
            os.makedirs(snap, exist_ok=True)
            with open(os.path.join(snap, "weights.bin"), "wb") as wf:
                wf.write(b"synthetic-weights-content\\n")
PYEOF
exit "${RECORDER_EXIT:-0}"
"""


def find_task() -> str | None:
    candidates: list[str] = []
    env_bin = os.environ.get("TASK_BIN")
    if env_bin:
        candidates.append(env_bin)
    candidates.append(str(REPO / ".tools/bin/task"))
    which = shutil.which("task")
    if which:
        candidates.append(which)
    for candidate in candidates:
        try:
            proc = subprocess.run(
                [candidate, "--version"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=15, check=False, text=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and proc.stdout.strip() == PIN_VERSION:
            return candidate
    return None


class TaskContractsTests(unittest.TestCase):
    def setUp(self):
        self.task_bin = find_task()
        if self.task_bin is None:
            if REQUIRE_RUNNER:
                self.fail(
                    f"pinned Task v{PIN_VERSION} unavailable "
                    "(TASK_BIN, .tools/bin/task, PATH); required lane must fail, not skip"
                )
            self.skipTest(f"pinned Task v{PIN_VERSION} not provisioned; see scripts/tools/install-task.sh")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copy(REPO / "Taskfile.yml", self.root / "Taskfile.yml")
        shutil.copytree(REPO / "taskfiles", self.root / "taskfiles")
        (self.root / "scripts").mkdir()
        self.log = self.root / "calls.jsonl"

    def run_task(self, *args: str, cwd: Path | None = None, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("TASK_BIN", None)
        # Mirror the documented session-local setup (install script prints the
        # export): the pinned binary's directory leads PATH so nested
        # `task --list` discovery resolves to the same verified runner.
        env["PATH"] = os.path.dirname(self.task_bin) + os.pathsep + env.get("PATH", "")
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [self.task_bin, "--taskfile", str(self.root / "Taskfile.yml"),
             "--dir", str(cwd or self.root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=60, check=False, text=True, env=env, cwd=str(cwd or self.root),
        )

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def make_recorder(self, path: Path, tag: str = "PYREC", exit_code: int = 0) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(RECORDER, encoding="utf-8")
        path.chmod(0o755)
        self.recorder_env = {
            "RECORDER_LOG": str(self.log),
            "RECORDER_TAG": tag,
            "RECORDER_EXIT": str(exit_code),
        }

    def make_venv_fake(self, version: str = "Python 3.14.5") -> None:
        """Fake .venv interpreter with a controllable `-V` identity string."""
        path = self.root / ".venv/bin/python"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(VENV_FAKE, encoding="utf-8")
        path.chmod(0o755)
        self.recorder_env = {
            "RECORDER_LOG": str(self.log),
            "RECORDER_TAG": "venv-python",
            "RECORDER_EXIT": "0",
            "FAKE_PY_VERSION": version,
        }

    def make_tool_recorder(self, name: str) -> None:
        """Fake `helm`/`docker` on a workspace-local bin dir (argv recorded)."""
        bindir = self.root / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        path = bindir / name
        path.write_text(RECORDER.replace('"$RECORDER_TAG"', f'"tool-{name}"'), encoding="utf-8")
        path.chmod(0o755)

    def make_script_recorder(self, name: str) -> None:
        """Stand-in owner script (e.g. run_local_stack.sh) logging argv+env."""
        path = self.root / "scripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(RECORDER.replace('"$RECORDER_TAG"', f'"script-{name}"'), encoding="utf-8")
        path.chmod(0o755)

    def copy_repo_script(self, name: str) -> None:
        """Use the shipped script (tests the real artifact, not a copy)."""
        dest = self.root / "scripts" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "scripts" / name, dest)
        dest.chmod(0o755)

    def make_docker_fake(self) -> None:
        """Fake docker: `inspect` exits $DOCKER_INSPECT_EXIT (default 1)."""
        bindir = self.root / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        path = bindir / "docker"
        path.write_text(
            '#!/bin/sh\n'
            'python3 - "$RECORDER_LOG" "tool-docker" "$@" <<\'PYEOF\'\n'
            'import json, os, sys\n'
            'log, tag, argv = sys.argv[1], sys.argv[2], sys.argv[3:]\n'
            'with open(log, "a", encoding="utf-8") as fh:\n'
            '    fh.write(json.dumps({"tag": tag, "argv": argv, "cwd": os.getcwd()}) + "\\n")\n'
            'PYEOF\n'
            'if [ "$1" = "inspect" ]; then exit "${DOCKER_INSPECT_EXIT:-1}"; fi\n'
            'exit 0\n',
            encoding="utf-8")
        path.chmod(0o755)

    def make_sim_images_fixture(self) -> None:
        (self.root / "images.txt").write_text(
            "example.com/qdrant/qdrant:v9.9.9-unprivileged sha256:fixture\n", encoding="utf-8")

    def make_airgap_fixtures(self, file_env: dict | None = None) -> None:
        """Real common.sh (precedence under test) + fixture airgap.env."""
        airgap = self.root / "scripts/airgap"
        airgap.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "scripts/airgap/common.sh", airgap / "common.sh")
        lines = [f"{k}={v}" for k, v in (file_env or {}).items()]
        (self.root / "airgap.env").write_text("\n".join(lines) + "\n" if lines else "",
                                              encoding="utf-8")

    def make_airgap_stage_double(self, name: str = "deploy.sh") -> None:
        """Deploy/pipeline double: real precedence, inert stage, logs resolved keys."""
        airgap = self.root / "scripts/airgap"
        airgap.mkdir(parents=True, exist_ok=True)
        (airgap / name).write_text(
            '#!/bin/sh\n'
            '# Test double for scripts/airgap/<stage>.sh: sources the shipped\n'
            '# common.sh (precedence under test), then records resolved values.\n'
            '. "$(dirname "$0")/common.sh"\n'
            'python3 - "$@" <<\'PYEOF\'\n'
            'import json, os, sys\n'
            'keys = ("INTERNAL_REGISTRY", "NAMESPACE", "EMBED_MODE", "CORPUS_PVC",\n'
            '        "AIRGAP_DRYRUN", "IMAGE_SHA", "STORAGE_CLASS", "QUERY", "AIRGAP_ENV")\n'
            'with open(os.environ["RECORDER_LOG"], "a") as fh:\n'
            '    fh.write(json.dumps({"tag": "airgap-stage", "argv": sys.argv[1:],\n'
            '                         "cwd": os.getcwd(),\n'
            '                         "resolved": {k: os.environ.get(k) for k in keys}}) + "\\n")\n'
            'PYEOF\n',
            encoding="utf-8")
        (airgap / name).chmod(0o755)

    def airgap_calls(self) -> list[dict]:
        return [c for c in self.calls() if c.get("tag") == "airgap-stage"]

    def tool_env(self) -> dict:
        """Recorder env plus workspace bin dir leading PATH (task dir kept)."""
        env = dict(self.recorder_env)
        env["PATH"] = os.pathsep.join([
            str(self.root / "bin"),
            os.path.dirname(self.task_bin),
            os.environ.get("PATH", ""),
        ])
        return env

    def make_artifact_fixtures(self) -> None:
        """Minimal inputs the artifact tasks fingerprint (content inert)."""
        import hashlib
        (self.root / "requirements.lock.txt").write_text("qdrant-client==1.19.0\n", encoding="utf-8")
        data = b"synthetic-weights-content\n"
        digest = hashlib.sha256(data).hexdigest()
        (self.root / "bm25-weights.sha256").write_text(f"{digest}  weights.bin\n", encoding="utf-8")
        fetch = self.root / "scripts/fetch_bm25_weights.py"
        fetch.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "scripts/fetch_bm25_weights.py", fetch)

    def make_eval_fixtures(self) -> None:
        """Holdout file plus a matching sha256 manifest (tamperable)."""
        import hashlib
        data = b"frozen-holdout-fixture\n"
        evals = self.root / "evals"
        evals.mkdir(parents=True, exist_ok=True)
        (evals / "holdout.jsonl").write_bytes(data)
        (evals / "holdout.jsonl.sha256").write_text(
            f"{hashlib.sha256(data).hexdigest()}  evals/holdout.jsonl\n", encoding="utf-8")

    def pip_calls(self) -> list[dict]:
        return [c for c in self.calls() if c["tag"] == "venv-python"]

    def tool_calls(self, name: str) -> list[dict]:
        return [c for c in self.calls() if c["tag"] == f"tool-{name}"]

    def script_calls(self, name: str) -> list[dict]:
        return [c for c in self.calls() if c["tag"] == f"script-{name}"]

    def assertEnvSubset(self, call: dict, expected: dict) -> None:
        for key, value in expected.items():
            self.assertEqual(call["env"].get(key), value, key)

    def prepare_controlled_runner(self):
        self.copy_repo_script("tools/run-task.sh")
        installed = self.root / ".tools/bin/task"
        installed.parent.mkdir(parents=True)
        # A symlink keeps hermetic tests small; the launcher hashes its target.
        installed.symlink_to(self.task_bin)
        digest = hashlib.sha256(Path(self.task_bin).read_bytes()).hexdigest()
        (self.root / "scripts/tools/task-pin.txt").write_text(
            f"version: v{PIN_VERSION}\nbinary-sha256: {digest}\n")

    def run_controlled(self, *args, extra_env=None, cwd=None):
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["sh", str(self.root / "scripts/tools/run-task.sh"), *args],
            cwd=cwd or self.root, env=env, capture_output=True, text=True,
            timeout=30, check=False)

    def test_controlled_runner_ignores_inherited_controls_and_private_dotenv(self):
        self.prepare_controlled_runner()
        self.make_venv_fake()
        # The upstream binary reads .env experiments even without dotenv: in YAML.
        # Initialization in scripts/tools must never read this root private file.
        (self.root / ".env").write_text("TASK_X_ENV_PRECEDENCE=1\nTOKEN=SECRET-SENTINEL\n")
        values = {**self.recorder_env, "TASK_DRY": "1", "TASK_PY": "false",
                  "TASK_X_ENV_PRECEDENCE": "1", "TASK_TEMP_DIR": "/forbidden",
                  "TASK_CONCURRENCY": "invalid", "EMBED_MODE": "hash"}
        for _ in range(2):
            proc = self.run_controlled("eval:retrieval", "EMBED_MODE=vllm", extra_env=values)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("SECRET-SENTINEL", proc.stdout + proc.stderr)
        self.assertEqual(len(self.calls()), 2, "requested verification must rerun")
        for call in self.calls():
            self.assertEqual(call["env"]["EMBED_MODE"], "vllm")
            self.assertIn("evals/baseline-vllm.json", call["argv"])
            self.assertEqual(call["cwd"], str(self.root))

    def test_controlled_discovery_and_literal_focused_selection(self):
        self.prepare_controlled_runner()
        (self.root / "sub dir").mkdir()
        # A read of the private file would block; discovery must not open it.
        os.mkfifo(self.root / ".env")
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        for args in ((), ("help",), ("--list", "--json"), ("airgap:dryrun", "--summary")):
            proc = self.run_controlled(*args, cwd=self.root / "sub dir")
            self.assertEqual(proc.returncode, 0, proc.stderr)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.make_venv_fake()
        literal = "tests/a space;$(touch SENTINEL)אב.py"
        proc = self.run_controlled("qa:unit", "--", literal, "-q", extra_env=self.recorder_env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls()[-1]["argv"], ["-m", "pytest", literal, "-q"])
        self.assertFalse((self.root / "SENTINEL").exists())

    def test_controlled_runner_rejects_corruption_config_and_path_overrides(self):
        self.prepare_controlled_runner()
        for args in (("--taskfile=other.yml",), ("-tother.yml",), ("-gl",),
                     ("--parallel",), ("--force",), ("TASK_PY=false",)):
            proc = self.run_controlled(*args)
            self.assertEqual(proc.returncode, 2, proc.stderr)
        for relative in (".taskrc.yml", "scripts/tools/.env"):
            config = self.root / relative
            config.write_text("PRIVATE-SENTINEL [invalid config")
            proc = self.run_controlled("--list")
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertNotIn("PRIVATE-SENTINEL", proc.stdout + proc.stderr)
            config.unlink()
        installed = self.root / ".tools/bin/task"
        installed.unlink()
        installed.write_text("#!/bin/sh\ntouch SHOULD-NOT-RUN\n")
        installed.chmod(0o755)
        proc = self.run_controlled("--list")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("checksum mismatch", proc.stderr)
        self.assertFalse((self.root / "SHOULD-NOT-RUN").exists())
        installed.unlink()
        proc = self.run_controlled("--list")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("absent", proc.stderr)

    def test_multi_family_selection_does_not_leak_eval_defaults(self):
        self.make_venv_fake()
        self.make_airgap_fixtures({"EMBED_MODE": "vllm"})
        self.make_airgap_stage_double()
        proc = self.run_task("eval:retrieval", "airgap:deploy", extra_env=self.recorder_env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.calls()[0]["env"]["EMBED_MODE"], "hash")
        self.assertEqual(self.airgap_calls()[0]["resolved"]["EMBED_MODE"], "vllm")

    def test_controlled_cancellation_reaches_foreground_owner_and_cleanup(self):
        self.prepare_controlled_runner()
        self.make_venv_fake()
        owner = self.root / "scripts/run_local_vllm.sh"
        owner.write_text(
            '#!/bin/sh\nset -eu\n'
            'trap \'rm -f owned-resource; echo stopped > cleaned; exit 23\' INT TERM\n'
            'touch owned-resource ready\nwhile :; do sleep 0.1; done\n')
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig):
                proc = subprocess.Popen(
                    ["sh", str(self.root / "scripts/tools/run-task.sh"), "local:llm"],
                    cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True)
                try:
                    deadline = time.monotonic() + 10
                    while not (self.root / "ready").exists() and time.monotonic() < deadline:
                        if proc.poll() is not None:
                            self.fail(proc.communicate())
                        time.sleep(0.02)
                    self.assertTrue((self.root / "ready").exists())
                    os.killpg(proc.pid, sig)
                    proc.communicate(timeout=10)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertTrue((self.root / "cleaned").exists())
                    self.assertFalse((self.root / "owned-resource").exists())
                finally:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.communicate(timeout=10)
                (self.root / "ready").unlink()
                (self.root / "cleaned").unlink()

    def test_discovery_needs_no_venv_config_or_services(self):
        before = {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()}
        for args in (["--list"], ["--list", "--json"], ["help"], ["qa:context", "--summary"],
                     ["dev:doctor", "--summary"], ["qa:unit", "--summary"]):
            with self.subTest(args=args):
                proc = self.run_task(*args)
                self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("qa:context", self.run_task("--list").stdout)
        after = {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "discovery must not create or modify workspace files")
        self.assertFalse((self.root / ".venv").exists())

    def test_unknown_task_fails(self):
        proc = self.run_task("does-not-exist")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('Task "does-not-exist" does not exist', proc.stdout)

    def test_missing_venv_fails_closed_without_installing(self):
        for task_name in ("qa:lint", "qa:typecheck", "qa:unit"):
            with self.subTest(task=task_name):
                proc = self.run_task(task_name)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("missing development environment", proc.stdout)
                self.assertIn("task dev:setup", proc.stdout)
        self.assertFalse((self.root / ".venv").exists(), "verification must never create .venv")
        self.assertEqual(self.calls(), [])

    def test_unit_default_argv_runs_once(self):
        self.make_recorder(self.root / ".venv/bin/python")
        proc = self.run_task("qa:unit", extra_env=self.recorder_env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["argv"], ["scripts/agent_doctor.py", "--python", ".venv/bin/python"])
        self.assertEqual(calls[1]["argv"], ["-m", "pytest", "tests", "-v"])
        self.assertEqual(calls[0]["cwd"], str(self.root))

    def test_unit_focused_selection_replaces_default(self):
        self.make_recorder(self.root / ".venv/bin/python")
        proc = self.run_task("qa:unit", "--", "tests/test_agent_context.py", "-q",
                             extra_env=self.recorder_env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.calls()
        self.assertEqual(len(calls), 2, "one prerequisite check, one focused pytest invocation")
        self.assertEqual(calls[1]["argv"], ["-m", "pytest", "tests/test_agent_context.py", "-q"])

    def test_check_runs_lint_typecheck_unit_in_order(self):
        self.make_recorder(self.root / ".venv/bin/python")
        proc = self.run_task("qa:check", extra_env=self.recorder_env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = [c["argv"] for c in self.calls()]
        self.assertEqual(argv, [
            ["scripts/agent_doctor.py", "--python", ".venv/bin/python"],
            ["-m", "ruff", "check", "src", "tests"],
            ["-m", "mypy", "src"],
            ["scripts/agent_doctor.py", "--python", ".venv/bin/python"],
            ["-m", "pytest", "tests", "-v"],
        ])

    def test_failed_prerequisite_never_starts_pytest(self):
        self.make_recorder(self.root / ".venv/bin/python")
        for name in ("qa:unit", "qa:check"):
            if self.log.exists():
                self.log.unlink()
            proc = self.run_task(name, extra_env={**self.recorder_env, "RECORDER_EXIT": "2"})
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual([c["argv"] for c in self.calls()], [
                ["scripts/agent_doctor.py", "--python", ".venv/bin/python"],
            ])

    def test_cwd_is_workspace_root_from_subdirectory(self):
        self.make_recorder(self.root / ".venv/bin/python")
        sub = self.root / "sub"
        sub.mkdir()
        proc = self.run_task("qa:unit", cwd=sub, extra_env=self.recorder_env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.calls()[0]["cwd"], str(self.root))

    def test_profile_values_reach_doctor_exactly(self):
        self.make_recorder(self.root / "pyrec")
        for profile, expected in (("sim", "sim"), ("false", "false"), ("0", "0"), ("", "")):
            with self.subTest(profile=repr(profile)):
                if (self.log).exists():
                    self.log.unlink()
                proc = self.run_task("dev:doctor", f"PY={self.root / 'pyrec'}", f"PROFILE={profile}",
                                     extra_env=self.recorder_env)
                self.assertEqual(proc.returncode, 0, proc.stdout)
                calls = self.calls()
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["argv"], ["scripts/agent_doctor.py", "--profile", expected])

    def test_py_metacharacters_are_not_executed(self):
        sentinel = self.root / "PWNED"
        proc = self.run_task("qa:context", f"PY=$(touch {sentinel})")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(sentinel.exists(), "interpolated PY value must never execute")

    def test_exit_code_passthrough_documents_contracted_codes(self):
        self.make_recorder(self.root / ".venv/bin/python", exit_code=3)
        proc = self.run_task("--exit-code", "qa:lint", extra_env=self.recorder_env)
        self.assertEqual(proc.returncode, 3, proc.stdout)

    def test_artifacts_registered_in_discovery(self):
        proc = self.run_task("--list")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for name in ("artifacts:wheelhouse", "artifacts:bm25", "artifacts:chart-check",
                     "artifacts:chart-fetch", "artifacts:helm-render", "artifacts:helm-lint",
                     "artifacts:images"):
            self.assertIn(name, proc.stdout)

    def test_wheelhouse_freshness_cycle(self):
        self.make_venv_fake()
        self.make_artifact_fixtures()
        env = self.tool_env()
        proc = self.run_task("artifacts:wheelhouse", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual([c["argv"] for c in self.pip_calls()],
                         [["-m", "pip", "wheel", "-r", "requirements.lock.txt", "-w", "bundles/wheelhouse"]])
        stamp = self.root / "bundles/wheelhouse/.task-complete"
        self.assertTrue(stamp.is_file(), "completion stamp published only after success")
        # Fresh: skip without invoking pip again.
        proc = self.run_task("artifacts:wheelhouse", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("up to date", proc.stdout)
        self.assertEqual(len(self.pip_calls()), 1)
        # Content change rebuilds; mtime-only touch does not (checksum method).
        (self.root / "requirements.lock.txt").write_text("qdrant-client==1.19.0\n# comment\n", encoding="utf-8")
        proc = self.run_task("artifacts:wheelhouse", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(self.pip_calls()), 2)
        before = len(self.pip_calls())
        (self.root / "requirements.lock.txt").touch()
        proc = self.run_task("artifacts:wheelhouse", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(self.pip_calls()), before)
        # Missing stamp rebuilds even though the directory survives: an
        # existing directory is never proof of a finished build.
        stamp.unlink()
        (self.root / "bundles/wheelhouse/partial.txt").write_text("stale", encoding="utf-8")
        proc = self.run_task("artifacts:wheelhouse", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(self.pip_calls()), before + 1)
        self.assertFalse((self.root / "bundles/wheelhouse/partial.txt").exists())
        self.assertTrue(stamp.is_file())
        # TR432-F1: delete one expected member while retaining the stamp: rebuilds
        wheel = self.root / "bundles/wheelhouse/fake_pkg-1.0.0-py3-none-any.whl"
        self.assertTrue(wheel.is_file())
        wheel.unlink()
        proc = self.run_task("artifacts:wheelhouse", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(self.pip_calls()), before + 2, "deleted wheel member must rebuild")
        self.assertTrue(wheel.is_file())
        # TR432-F1: modify a member while preserving name and stamp: rebuilds
        wheel.write_text("corrupted-content\n")
        proc = self.run_task("artifacts:wheelhouse", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(self.pip_calls()), before + 3, "corrupted wheel member must rebuild")

    def test_wheelhouse_builder_failure_leaves_no_stamp(self):
        self.make_venv_fake()
        self.make_artifact_fixtures()
        env = self.tool_env()
        env["RECORDER_EXIT"] = "1"
        proc = self.run_task("artifacts:wheelhouse", extra_env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.root / "bundles/wheelhouse/.task-complete").exists())

    def test_wheelhouse_interpreter_change_rebuilds(self):
        self.make_venv_fake(version="Python 3.14.5")
        self.make_artifact_fixtures()
        env = self.tool_env()
        self.assertEqual(self.run_task("artifacts:wheelhouse", extra_env=env).returncode, 0)
        self.assertEqual(len(self.pip_calls()), 1)
        self.assertEqual(self.run_task("artifacts:wheelhouse", extra_env=env).returncode, 0)
        self.assertEqual(len(self.pip_calls()), 1)
        env = dict(env, FAKE_PY_VERSION="Python 3.14.6")
        proc = self.run_task("artifacts:wheelhouse", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(self.pip_calls()), 2, "interpreter change must not reuse cached wheels")

    def test_wheelhouse_bundle_dir_override(self):
        self.make_venv_fake()
        self.make_artifact_fixtures()
        env = self.tool_env()
        proc = self.run_task("artifacts:wheelhouse", "BUNDLE_DIR=alt", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual([c["argv"] for c in self.pip_calls()],
                         [["-m", "pip", "wheel", "-r", "requirements.lock.txt", "-w", "alt/wheelhouse"]])
        self.assertTrue((self.root / "alt/wheelhouse/.task-complete").is_file())
        self.assertFalse((self.root / "bundles").exists())

    def test_wheelhouse_missing_venv_fails_closed(self):
        self.make_artifact_fixtures()
        self.recorder_env = {}
        proc = self.run_task("artifacts:wheelhouse", extra_env={})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("missing development environment", proc.stdout)
        self.assertFalse((self.root / "bundles").exists(), "failed prep must leave no outputs")

    def test_bm25_model_selection_reaches_fetcher(self):
        self.make_venv_fake()
        self.make_artifact_fixtures()
        env = self.tool_env()
        proc = self.run_task("artifacts:bm25", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(
            [c["argv"] for c in self.pip_calls()],
            [["scripts/fetch_bm25_weights.py", "--model", "Qdrant/bm25",
              "--out", "bundles/bm25-weights", "--verify-manifest", "bm25-weights.sha256"]])
        stamp = (self.root / "bundles/bm25-weights/.task-complete").read_text(encoding="utf-8").strip()
        self.assertTrue(stamp.endswith(" Qdrant/bm25"), stamp)
        proc = self.run_task("artifacts:bm25", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(self.pip_calls()), 1, "fresh stamp must skip the fetch")
        proc = self.run_task("artifacts:bm25", "BM25_MODEL=Other/model", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(self.pip_calls()), 2, "model change must refetch, not reuse cached weights")
        self.assertIn("--model", self.pip_calls()[-1]["argv"])
        self.assertEqual(self.pip_calls()[-1]["argv"][2], "Other/model")
        # TR432-F1: delete one expected weight member while retaining stamp: refetches
        weight = self.root / "bundles/bm25-weights/models--fake/snapshots/snap1/weights.bin"
        self.assertTrue(weight.is_file())
        weight.unlink()
        proc = self.run_task("artifacts:bm25", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(self.pip_calls()), 3, "deleted weight member must refetch")
        self.assertTrue(weight.is_file())
        # TR432-F1: corrupt weight content: refetches
        weight.write_bytes(b"corrupted-weight-data\n")
        proc = self.run_task("artifacts:bm25", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(self.pip_calls()), 4, "corrupted weight member must refetch")

    def test_chart_check_fails_closed_then_passes(self):
        proc = self.run_task("artifacts:chart-check")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("charts/qdrant-*.tgz missing", proc.stdout)
        self.assertIn("task artifacts:chart-fetch", proc.stdout)
        charts = self.root / "charts"
        charts.mkdir()
        (charts / "qdrant-1.19.0.tgz").write_text("fixture", encoding="utf-8")
        proc = self.run_task("artifacts:chart-check")
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_helm_render_verifies_chart_first_with_fixed_sets(self):
        self.make_tool_recorder("helm")
        self.make_venv_fake()
        env = self.tool_env()
        charts = self.root / "charts"
        charts.mkdir()
        (charts / "qdrant-1.19.0.tgz").write_text("fixture", encoding="utf-8")
        proc = self.run_task("artifacts:helm-render", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.tool_calls("helm")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], [
            "template", "qdrant", "charts/qdrant-1.19.0.tgz", "-f", "overlays/openshift/values.yaml",
            "--set", "image.repository=PLACEHOLDER_REGISTRY/qdrant/qdrant",
            "--set", "imagePullSecrets[0].name=PLACEHOLDER_PULL_SECRET",
            "--set", "persistence.storageClassName=PLACEHOLDER_STORAGE_CLASS",
            "--set", "snapshotPersistence.storageClassName=PLACEHOLDER_STORAGE_CLASS",
        ])
        self.assertEqual(calls[0]["cwd"], str(self.root))

    def test_helm_render_refuses_without_chart(self):
        self.make_tool_recorder("helm")
        self.make_venv_fake()
        proc = self.run_task("artifacts:helm-render", extra_env=self.tool_env())
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.tool_calls("helm"), [], "helm must not run before chart verification")

    def test_images_builds_preparations_then_images_in_order(self):
        self.make_venv_fake()
        self.make_artifact_fixtures()
        self.make_tool_recorder("docker")
        env = self.tool_env()
        proc = self.run_task("artifacts:images", "IMAGE_TAG=abc123", "BUNDLE_DIR=alt", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(
            [c["argv"] for c in self.pip_calls()],
            [["-m", "pip", "wheel", "-r", "requirements.lock.txt", "-w", "alt/wheelhouse"],
             ["scripts/fetch_bm25_weights.py", "--model", "Qdrant/bm25",
              "--out", "alt/bm25-weights", "--verify-manifest", "bm25-weights.sha256"]])
        docker = [c["argv"] for c in self.tool_calls("docker")]
        self.assertEqual(len(docker), 2)
        self.assertEqual(docker[0][:7], ["build", "--build-context", "wheelhouse=alt/wheelhouse",
                                         "--build-context", "bm25=alt/bm25-weights",
                                         "-f", "images/Containerfile.ingest"])
        self.assertIn("mainframe-rag/ingest:abc123", docker[0])
        self.assertIn("images/Containerfile.agent", docker[1])
        self.assertIn("mainframe-rag/agent:abc123", docker[1])
        for call in self.tool_calls("docker"):
            self.assertEqual(call["cwd"], str(self.root))

    def test_eval_registered_in_discovery(self):
        proc = self.run_task("--list")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for name in ("eval:retrieval", "eval:gate-l1", "eval:paraphrase", "eval:holdout",
                     "eval:verify-golden", "eval:baseline", "eval:draft", "eval:capture-pool",
                     "eval:answers", "eval:chat", "eval:harness:gate", "eval:harness:baseline",
                     "eval:harness:l2", "eval:harness:l3", "eval:harness:l3-baseline",
                     "eval:harness:l4", "eval:harness:l4-record", "eval:report", "eval:html",
                     "eval:compare", "eval:bench-report", "eval:bench-html", "eval:bench-compare",
                     "eval:bench", "eval:bench-baseline", "eval:load"):
            self.assertIn(name, proc.stdout)

    def test_eval_mode_venue_defaults(self):
        self.make_venv_fake()
        proc = self.run_task("eval:retrieval", extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEqual(len(calls), 1)
        self.assertEnvSubset(calls[0], {"EMBED_MODE": "hash", "VENUE": "dev"})
        self.assertEqual(
            calls[0]["argv"],
            ["scripts/eval_retrieval.py", "--golden", "evals/golden.jsonl",
             "--check", "evals/baseline.json", "--out", "bundles/eval-report.json",
             "--summary", "bundles/eval-summary.md"])

    def test_eval_mode_override_cli_and_env_forms(self):
        self.make_venv_fake()
        env = self.tool_env()
        proc = self.run_task("eval:retrieval", "EMBED_MODE=vllm", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEnvSubset(calls[0], {"EMBED_MODE": "vllm", "VENUE": "dev"})
        self.assertIn("evals/baseline-vllm.json", calls[0]["argv"])
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("eval:retrieval", extra_env=dict(env, EMBED_MODE="vllm", VENUE="rc"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEnvSubset(calls[0], {"EMBED_MODE": "vllm", "VENUE": "rc"})
        self.assertIn("evals/baseline-vllm.json", calls[0]["argv"])
        if (self.log).exists():
            self.log.unlink()
        # Conflicting forms: CLI wins over ambient environment (TR433-F1)
        # 1. Ambient hash + CLI vllm -> child and baseline select vllm
        proc = self.run_task("eval:retrieval", "EMBED_MODE=vllm", extra_env=dict(env, EMBED_MODE="hash"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEnvSubset(calls[0], {"EMBED_MODE": "vllm", "VENUE": "dev"})
        self.assertIn("evals/baseline-vllm.json", calls[0]["argv"])
        if (self.log).exists():
            self.log.unlink()
        # 2. Ambient vllm + CLI hash -> child and baseline select hash
        proc = self.run_task("eval:retrieval", "EMBED_MODE=hash", extra_env=dict(env, EMBED_MODE="vllm"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEnvSubset(calls[0], {"EMBED_MODE": "hash", "VENUE": "dev"})
        self.assertIn("evals/baseline.json", calls[0]["argv"])
        if (self.log).exists():
            self.log.unlink()
        # 3. Ambient rc + CLI dev for VENUE -> child selects dev
        proc = self.run_task("eval:retrieval", "VENUE=dev", extra_env=dict(env, VENUE="rc"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEnvSubset(calls[0], {"EMBED_MODE": "hash", "VENUE": "dev"})

    def test_eval_explicit_empty_mode_preserved_with_hash_baseline(self):
        # Mirrors Make `$(filter vllm,"")` (hash branch) plus an empty export:
        # the baseline cannot disagree with the effective mode, and the empty
        # value reaches the script instead of silently becoming the default.
        self.make_venv_fake()
        proc = self.run_task("eval:retrieval", "EMBED_MODE=", extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEnvSubset(calls[0], {"EMBED_MODE": "", "VENUE": "dev"})
        self.assertIn("evals/baseline.json", calls[0]["argv"])
        if (self.log).exists():
            self.log.unlink()
        # Explicit empty CLI overrides nonempty ambient value (TR433-F1)
        proc = self.run_task("eval:retrieval", "EMBED_MODE=", extra_env=dict(self.tool_env(), EMBED_MODE="vllm"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEnvSubset(calls[0], {"EMBED_MODE": "", "VENUE": "dev"})
        self.assertIn("evals/baseline.json", calls[0]["argv"])

    def test_harness_golden_flag_iff_hash_mode(self):
        self.make_venv_fake()
        env = self.tool_env()
        proc = self.run_task("eval:harness:gate", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = self.pip_calls()[0]["argv"]
        self.assertIn("--golden", argv)
        self.assertEqual(argv[argv.index("--golden") + 1], "evals/golden.jsonl")
        self.assertIn("benchmarks/harness.json", argv)
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("eval:harness:gate", "EMBED_MODE=vllm", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = self.pip_calls()[0]["argv"]
        self.assertNotIn("--golden", argv)
        self.assertIn("benchmarks/harness-vllm.json", argv)

    def test_holdout_forces_rc_venue_and_verifies_first(self):
        self.make_venv_fake()
        self.make_eval_fixtures()
        env = self.tool_env()
        # CLI VENUE=dev cannot override holdout's VENUE=rc
        proc = self.run_task("eval:holdout", "VENUE=dev", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEqual(len(calls), 1, "sha256sum pre-check must precede the single python run")
        self.assertEnvSubset(calls[0], {"EMBED_MODE": "hash", "VENUE": "rc"})
        self.assertIn("evals/holdout.jsonl", calls[0]["argv"])
        if (self.log).exists():
            self.log.unlink()
        # Ambient VENUE=dev cannot override holdout's VENUE=rc (TR433-F1)
        proc = self.run_task("eval:holdout", extra_env=dict(env, VENUE="dev"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEnvSubset(calls[0], {"EMBED_MODE": "hash", "VENUE": "rc"})
        if (self.log).exists():
            self.log.unlink()
        # Ambient VENUE=dev AND CLI VENUE=dev together cannot override holdout's VENUE=rc (TR433-F1)
        proc = self.run_task("eval:holdout", "VENUE=dev", extra_env=dict(env, VENUE="dev"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEnvSubset(calls[0], {"EMBED_MODE": "hash", "VENUE": "rc"})
        if (self.log).exists():
            self.log.unlink()
        # Tampered holdout fails before any python invocation.
        with (self.root / "evals/holdout.jsonl").open("ab") as fh:
            fh.write(b"tampered\n")
        proc = self.run_task("eval:holdout", extra_env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.pip_calls(), [])

    def test_eval_count_inputs_default_and_override(self):
        self.make_venv_fake()
        env = self.tool_env()
        proc = self.run_task("eval:answers", "N=5", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = self.pip_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--max-queries") + 1], "5")
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("eval:answers", "N=", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = self.pip_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--max-queries") + 1], "24")
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("eval:chat", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = self.pip_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--limit") + 1], "12")

    def test_bench_verify_and_load_carry_no_mode_exports(self):
        self.make_venv_fake()
        self.make_tool_recorder("python3base")
        env = self.tool_env()
        for task_name in ("eval:bench", "eval:verify-golden"):
            with self.subTest(task=task_name):
                if (self.log).exists():
                    self.log.unlink()
                proc = self.run_task(task_name, extra_env=env)
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assertEnvSubset(self.pip_calls()[0], {"EMBED_MODE": None, "VENUE": None})
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("eval:load", "PY=python3base", "AGENT_URL=http://x:9999", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        base_calls = self.tool_calls("python3base")
        self.assertEqual(len(base_calls), 1)
        self.assertEqual(base_calls[0]["argv"], [
            "scripts/loadtest.py", "--url", "http://x:9999", "--endpoint", "search",
            "--concurrency", "8", "--duration", "30"])
        self.assertEnvSubset(base_calls[0], {"EMBED_MODE": None, "VENUE": None})

    def test_capture_pool_out_defaults_to_dated_bundle(self):
        import re
        self.make_venv_fake()
        env = self.tool_env()
        proc = self.run_task("eval:capture-pool", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = self.pip_calls()[0]["argv"]
        out = argv[argv.index("--out") + 1]
        self.assertTrue(re.fullmatch(r"bundles/pools-\d{8}\.jsonl", out), out)
        self.assertEqual(argv[argv.index("--golden") + 1], "evals/golden.jsonl")
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("eval:capture-pool", "OUT=/tmp/x.jsonl", "GOLDEN=g.jsonl", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = self.pip_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--out") + 1], "/tmp/x.jsonl")
        self.assertEqual(argv[argv.index("--golden") + 1], "g.jsonl")

    def test_report_inputs_default_and_override(self):
        self.make_venv_fake()
        env = self.tool_env()
        proc = self.run_task("eval:report", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = self.pip_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--report") + 1], "bundles/eval-report.json")
        self.assertEqual(argv[argv.index("--baseline") + 1], "evals/baseline.json")
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("eval:report", "REPORT=/r.json", "BASELINE=/b.json", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = self.pip_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--report") + 1], "/r.json")
        self.assertEqual(argv[argv.index("--baseline") + 1], "/b.json")

    def test_harness_l3_baseline_nesting(self):
        self.make_venv_fake()
        env = self.tool_env()
        proc = self.run_task("eval:harness:l3", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = self.pip_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--baseline") + 1], "benchmarks/harness-l3.json")
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("eval:harness:l3", "EMBED_MODE=vllm", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = self.pip_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--baseline") + 1], "benchmarks/harness-l3-vllm.json")
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("eval:harness:l3", "EMBED_MODE=vllm", "HARNESS_L3_BASELINE=c.json",
                             extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = self.pip_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--baseline") + 1], "c.json")

    def test_local_acceptance_and_repair_dispatch_exact_arguments(self):
        self.prepare_controlled_runner()
        self.make_venv_fake()
        for task, prefix in (("local:check", ["scripts/check_live.py"]),
                             ("local:repair-staging", ["-m", "mainframe_rag.ingest.repair"])):
            self.log.unlink(missing_ok=True)
            literal = "/private/a space;$(touch SENTINEL).json"
            result = self.run_controlled(task, "--", "--report", literal,
                                         extra_env=self.recorder_env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.calls()[0]["argv"], [*prefix, "--report", literal])
            self.assertFalse((self.root / "SENTINEL").exists())
            result = self.run_controlled(task, "--", "--help",
                extra_env={**self.recorder_env, "RECORDER_EXIT": "1"})
            self.assertNotEqual(result.returncode, 0)

    def test_local_registered_in_discovery(self):
        proc = self.run_task("--list")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for name in ("local:qdrant:up", "local:qdrant:down", "local:query", "local:ask",
                     "local:llm", "local:embed", "local:rerank", "local:gateway:up",
                     "local:gateway:down", "local:jaeger:up", "local:jaeger:down",
                     "local:stack", "local:agent", "local:check", "local:repair-staging", "qa:sim", "qa:load", "qa:vllm-e2e"):
            self.assertIn(name, proc.stdout)

    def test_query_optional_flags_and_literal_values(self):
        self.make_recorder(self.root / ".venv/bin/python")
        env = self.tool_env()
        proc = self.run_task("local:query", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.calls()
        self.assertEqual(calls[0]["argv"], ["scripts/query_demo.py", "--embed-mode", "hash"])
        self.assertEnvSubset(calls[0], {"PYTHONPATH": ".", "EMBED_MODE": "hash"})
        if (self.log).exists():
            self.log.unlink()
        sentinel = "a b$c;`echo PWNED`"
        proc = self.run_task("local:query", f"QUERY={sentinel}", "LIMIT=5", "LIMIT2=",
                             extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.calls()
        self.assertEqual(calls[0]["argv"], ["scripts/query_demo.py", "--query", sentinel,
                                            "--limit", "5", "--embed-mode", "hash"])
        self.assertFalse((self.root / "PWNED").exists())

    def test_ask_answer_flag_first(self):
        self.make_recorder(self.root / ".venv/bin/python")
        proc = self.run_task("local:ask", "QUERY=hi", extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.calls()[0]["argv"],
                         ["scripts/query_demo.py", "--answer", "--query", "hi",
                          "--embed-mode", "hash"])

    def test_vllm_launcher_env_exact(self):
        self.make_venv_fake()
        self.make_script_recorder("run_local_vllm.sh")
        env = self.tool_env()
        proc = self.run_task("local:llm", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.script_calls("run_local_vllm.sh")
        self.assertEqual(len(calls), 1)
        self.assertEnvSubset(calls[0], {"ROLE": "reasoning"})
        budget = calls[0]["env"].get("BUDGET_PYTHON")
        self.assertTrue(budget.startswith("/") and budget.endswith("/.venv/bin/python"), budget)
        if (self.log).exists():
            self.log.unlink()
        # Direct CLI form for local:llm forwards MODEL, PORT, GPU_MEM, MAX_LEN, SEQS, BUDGET_PROFILE (TR434-F2)
        proc = self.run_task("local:llm", "MODEL=approved-local-model", "PORT=8100",
                             "GPU_MEM=0.7", "MAX_LEN=4096", "SEQS=4", "BUDGET_PROFILE=custom-profile",
                             extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.script_calls("run_local_vllm.sh")
        self.assertEnvSubset(calls[0], {
            "ROLE": "reasoning",
            "MODEL": "approved-local-model",
            "PORT": "8100",
            "GPU_MEM": "0.7",
            "MAX_LEN": "4096",
            "SEQS": "4",
            "BUDGET_PROFILE": "custom-profile",
        })
        if (self.log).exists():
            self.log.unlink()
        # Conflicting ambient vs CLI: CLI wins (TR434-F2)
        proc = self.run_task("local:llm", "MODEL=cli-model", "PORT=8100",
                             extra_env=dict(env, MODEL="ambient-model", PORT="9999"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.script_calls("run_local_vllm.sh")
        self.assertEnvSubset(calls[0], {
            "ROLE": "reasoning",
            "MODEL": "cli-model",
            "PORT": "8100",
        })
        if (self.log).exists():
            self.log.unlink()
        # Ambient-only invocation is preserved (TR434-F2)
        proc = self.run_task("local:llm", extra_env=dict(env, MODEL="ambient-only-model", PORT="8200"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.script_calls("run_local_vllm.sh")
        self.assertEnvSubset(calls[0], {
            "ROLE": "reasoning",
            "MODEL": "ambient-only-model",
            "PORT": "8200",
        })
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("local:embed", "ROLE=x", "MODEL=m", "PORT=1234", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.script_calls("run_local_vllm.sh")
        self.assertEnvSubset(calls[0], {"ROLE": "x", "MODEL": "m", "PORT": "1234"})

    def test_local_model_served_name_round_trip(self):
        self.make_venv_fake()
        self.make_script_recorder("run_local_vllm.sh")
        name = "vendor/model with spaces|literal;$(touch PWNED)"
        for task in ("local:llm", "local:embed", "local:rerank"):
            with self.subTest(task=task):
                self.log.unlink(missing_ok=True)
                proc = self.run_task(task, "MODEL=/immutable/revision", f"SERVED_NAME={name}",
                                     extra_env=dict(self.tool_env(), SERVED_NAME="ambient-name"))
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assertEnvSubset(self.script_calls("run_local_vllm.sh")[0],
                                     {"MODEL": "/immutable/revision", "SERVED_NAME": name})
                self.assertFalse((self.root / "PWNED").exists())
                self.log.unlink(missing_ok=True)
                proc = self.run_task(task, extra_env=dict(self.tool_env(), SERVED_NAME="ambient-name"))
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assertEnvSubset(self.script_calls("run_local_vllm.sh")[0],
                                     {"SERVED_NAME": "ambient-name"})

    def test_local_model_container_name_round_trip(self):
        self.make_venv_fake()
        self.make_script_recorder("run_local_vllm.sh")
        for task in ("local:llm", "local:embed", "local:rerank"):
            with self.subTest(task=task):
                self.log.unlink(missing_ok=True)
                proc = self.run_task(task, "CONTAINER_NAME=rag-kind-model",
                                     extra_env=dict(self.tool_env(), CONTAINER_NAME="ambient-model"))
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assertEnvSubset(self.script_calls("run_local_vllm.sh")[0],
                                     {"CONTAINER_NAME": "rag-kind-model"})

    def test_gateway_down_ignores_absent_resources(self):
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "1"}
        self.make_tool_recorder("docker")
        proc = self.run_task("local:gateway:down", extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        docker = [c["argv"] for c in self.tool_calls("docker")]
        self.assertEqual(docker, [
            ["stop", "local-litellm-gateway", "local-litellm-gateway-pg"],
            ["network", "rm", "local-litellm-gateway-net"]])
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("local:gateway:down", "GATEWAY_NAME=g", "PG_NAME=p", "PG_NET=n",
                             extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        docker = [c["argv"] for c in self.tool_calls("docker")]
        self.assertEqual(docker, [["stop", "g", "p"], ["network", "rm", "n"]])

    def test_gateway_up_forwards_port_to_script(self):
        self.make_venv_fake()
        self.make_script_recorder("run_local_gateway.sh")
        env = self.tool_env()
        proc = self.run_task("local:gateway:up", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.script_calls("run_local_gateway.sh")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], [])
        self.assertEnvSubset(calls[0], {"GATEWAY_PORT": "4000"})
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("local:gateway:up", "GATEWAY_PORT=4321", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEnvSubset(self.script_calls("run_local_gateway.sh")[0], {"GATEWAY_PORT": "4321"})

    def test_jaeger_tasks(self):
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        self.make_script_recorder("run_local_jaeger.sh")
        self.make_tool_recorder("docker")
        env = self.tool_env()
        proc = self.run_task("local:jaeger:up", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(self.script_calls("run_local_jaeger.sh")), 1)
        if (self.log).exists():
            self.log.unlink()
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "1"}
        proc = self.run_task("local:jaeger:down", extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual([c["argv"] for c in self.tool_calls("docker")],
                         [["stop", "local-jaeger"]])

    def test_sim_helper_up_reuses_running_container(self):
        self.copy_repo_script("sim_qdrant.sh")
        self.copy_repo_script("qdrant_pin.py")
        self.make_sim_images_fixture()
        self.make_docker_fake()
        env = dict(os.environ, PATH=str(self.root / "bin") + os.pathsep + os.environ.get("PATH", ""),
                   RECORDER_LOG=str(self.log), RECORDER_EXIT="0", DOCKER_INSPECT_EXIT="0")
        proc = subprocess.run(["sh", str(self.root / "scripts/sim_qdrant.sh"), "up"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60, check=False, text=True, env=env, cwd=str(self.root))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("already running", proc.stdout)
        docker = [c["argv"] for c in self.calls()]
        self.assertEqual(docker, [["inspect", "qdrant-sim"]])

    def test_sim_helper_up_starts_pinned_image(self):
        self.copy_repo_script("sim_qdrant.sh")
        self.copy_repo_script("qdrant_pin.py")
        self.make_sim_images_fixture()
        self.make_docker_fake()
        env = dict(os.environ, PATH=str(self.root / "bin") + os.pathsep + os.environ.get("PATH", ""),
                   RECORDER_LOG=str(self.log), RECORDER_EXIT="0", DOCKER_INSPECT_EXIT="1")
        proc = subprocess.run(["sh", str(self.root / "scripts/sim_qdrant.sh"), "up"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60, check=False, text=True, env=env, cwd=str(self.root))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        docker = [c["argv"] for c in self.calls()]
        self.assertEqual(len(docker), 2)
        self.assertEqual(docker[0], ["inspect", "qdrant-sim"])
        self.assertIn("127.0.0.1:6333:6333", docker[1])
        self.assertIn("example.com/qdrant/qdrant:v9.9.9-unprivileged", docker[1])
        self.assertIn("QDRANT_SIM_URL=http://127.0.0.1:6333", proc.stdout)

    def test_sim_helper_rejects_empty_names(self):
        self.copy_repo_script("sim_qdrant.sh")
        self.copy_repo_script("qdrant_pin.py")
        self.make_sim_images_fixture()
        self.make_docker_fake()
        env = dict(os.environ, PATH=str(self.root / "bin") + os.pathsep + os.environ.get("PATH", ""),
                   RECORDER_LOG=str(self.log), RECORDER_EXIT="0",
                   SIM_CONTAINER="", DOCKER_INSPECT_EXIT="1")
        proc = subprocess.run(["sh", str(self.root / "scripts/sim_qdrant.sh"), "up"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60, check=False, text=True, env=env, cwd=str(self.root))
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("must not be empty", proc.stdout)
        self.assertEqual(self.calls(), [], "docker must not run on invalid input")
        proc = subprocess.run(["sh", str(self.root / "scripts/sim_qdrant.sh"), "bogus"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60, check=False, text=True, env=env, cwd=str(self.root))
        self.assertEqual(proc.returncode, 2)

    def test_qdrant_tasks_delegate_to_helper(self):
        self.copy_repo_script("sim_qdrant.sh")
        self.copy_repo_script("qdrant_pin.py")
        self.make_sim_images_fixture()
        self.make_docker_fake()
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        env = dict(self.tool_env(), DOCKER_INSPECT_EXIT="1")
        proc = self.run_task("local:qdrant:up", "SIM_CONTAINER=custom", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        docker = [c["argv"] for c in self.tool_calls("docker")]
        self.assertEqual(docker[0], ["inspect", "custom"])
        self.assertIn("--name", docker[1])
        self.assertIn("custom", docker[1])
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("local:qdrant:down", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual([c["argv"] for c in self.tool_calls("docker")], [["stop", "qdrant-sim"]])
        if (self.log).exists():
            self.log.unlink()
        # Conflicting ambient vs CLI: CLI wins over ambient SIM_CONTAINER (TR434-F1)
        proc = self.run_task("local:qdrant:down", "SIM_CONTAINER=requested-sim",
                             extra_env=dict(env, SIM_CONTAINER="ambient-sim"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual([c["argv"] for c in self.tool_calls("docker")], [["stop", "requested-sim"]])
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("local:qdrant:up", "SIM_CONTAINER=requested-sim", "SIM_PORT=6334",
                             extra_env=dict(env, SIM_CONTAINER="ambient-sim", SIM_PORT="6333"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        docker = [c["argv"] for c in self.tool_calls("docker")]
        self.assertEqual(docker[0], ["inspect", "requested-sim"])
        self.assertIn("requested-sim", docker[1])
        self.assertIn("127.0.0.1:6334:6333", docker[1])
        if (self.log).exists():
            self.log.unlink()
        # Explicit empty fails closed (TR434-F1)
        proc = self.run_task("local:qdrant:up", "SIM_CONTAINER=", extra_env=env)
        self.assertNotEqual(proc.returncode, 0)

    def test_stack_forwards_only_set_vars(self):
        self.make_venv_fake()
        self.make_script_recorder("run_local_stack.sh")
        env = self.tool_env()
        proc = self.run_task("local:stack", "CORPUS_DIR=/data", "GATEWAY_PORT=4001", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.script_calls("run_local_stack.sh")
        self.assertEqual(len(calls), 1)
        self.assertEnvSubset(calls[0], {"CORPUS_DIR": "/data", "GATEWAY_PORT": "4001",
                                        "LOCAL_AGENT_PORT": None, "JAEGER_PORT": None,
                                        "LOCAL_STACK_DRYRUN": None})
        if (self.log).exists():
            self.log.unlink()
        # Direct CLI LOCAL_STACK_DRYRUN=1 reaches script (TR434-F2)
        proc = self.run_task("local:stack", "LOCAL_STACK_DRYRUN=1", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.script_calls("run_local_stack.sh")
        self.assertEnvSubset(calls[0], {"LOCAL_STACK_DRYRUN": "1"})
        if (self.log).exists():
            self.log.unlink()
        # Ambient LOCAL_STACK_DRYRUN=1 reaches script (TR434-F2)
        proc = self.run_task("local:stack", extra_env=dict(env, LOCAL_STACK_DRYRUN="1"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.script_calls("run_local_stack.sh")
        self.assertEnvSubset(calls[0], {"LOCAL_STACK_DRYRUN": "1"})
        if (self.log).exists():
            self.log.unlink()
        # Conflicting ambient and CLI: CLI wins (TR434-F2)
        proc = self.run_task("local:stack", "LOCAL_STACK_DRYRUN=0", extra_env=dict(env, LOCAL_STACK_DRYRUN="1"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.script_calls("run_local_stack.sh")
        self.assertEnvSubset(calls[0], {"LOCAL_STACK_DRYRUN": "0"})

    def test_agent_stream_port_and_ui(self):
        self.make_venv_fake()
        env = self.tool_env()
        proc = self.run_task("local:agent", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEqual(len(calls), 1)
        self.assertEnvSubset(calls[0], {"LLM_STREAM": "true", "UI_ENABLED": None})
        self.assertIn("--port", calls[0]["argv"])
        self.assertEqual(calls[0]["argv"][calls[0]["argv"].index("--port") + 1], "8080")
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("local:agent", "UI_ENABLED=true", "PORT=9090", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.pip_calls()
        self.assertEnvSubset(calls[0], {"LLM_STREAM": "true", "UI_ENABLED": "true"})
        self.assertEqual(calls[0]["argv"][calls[0]["argv"].index("--port") + 1], "9090")

    def test_sim_and_load_use_fixed_tiers(self):
        self.make_venv_fake()
        env = self.tool_env()
        proc = self.run_task("qa:sim", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.pip_calls()[0]["argv"],
                         ["-m", "pytest", "-m", "integration",
                          "--ignore=tests/test_load_tier.py",
                          "--ignore=tests/test_ha_cluster.py", "-v", "-rs"])
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("qa:load", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.pip_calls()[0]["argv"],
                         ["-m", "pytest", "-m", "integration", "tests/test_load_tier.py", "-v"])
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("qa:ha", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.pip_calls()[0]["argv"],
                         ["-m", "pytest", "-m", "integration", "tests/test_ha_cluster.py", "-v"])

    def test_vllm_e2e_optional_flags(self):
        self.make_venv_fake()
        env = self.tool_env()
        proc = self.run_task("qa:vllm-e2e", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.pip_calls()[0]["argv"],
                         ["scripts/test_local_e2e_vllm.py", "--embed-mode", "hash"])
        if (self.log).exists():
            self.log.unlink()
        proc = self.run_task("qa:vllm-e2e", "MODEL=m", "DENSE_DIM=768", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.pip_calls()[0]["argv"],
                         ["scripts/test_local_e2e_vllm.py", "--model", "m",
                          "--dense-dim", "768", "--embed-mode", "hash"])

    def test_airgap_registered_in_discovery(self):
        proc = self.run_task("--list")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for name in ("airgap:pack", "airgap:load", "airgap:deploy", "airgap:ingest",
                     "airgap:smoke", "airgap:validate", "airgap:pipeline", "airgap:dryrun"):
            self.assertIn(name, proc.stdout)

    def test_operator_cli_beats_file(self):
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        self.make_airgap_fixtures({"INTERNAL_REGISTRY": "file-reg", "NAMESPACE": "file-ns"})
        self.make_airgap_stage_double("deploy.sh")
        proc = self.run_task("airgap:deploy", "INTERNAL_REGISTRY=cli-reg",
                             extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        resolved = self.airgap_calls()[0]["resolved"]
        self.assertEqual(resolved["INTERNAL_REGISTRY"], "cli-reg")
        self.assertEqual(resolved["NAMESPACE"], "file-ns")

    def test_operator_file_applies_when_unset(self):
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        self.make_airgap_fixtures({"INTERNAL_REGISTRY": "file-reg"})
        self.make_airgap_stage_double("deploy.sh")
        proc = self.run_task("airgap:deploy", extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.airgap_calls()[0]["resolved"]["INTERNAL_REGISTRY"], "file-reg")

    def test_operator_empty_stays_unset(self):
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        self.make_airgap_fixtures({"INTERNAL_REGISTRY": "file-reg"})
        self.make_airgap_stage_double("deploy.sh")
        proc = self.run_task("airgap:deploy", "INTERNAL_REGISTRY=", extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.airgap_calls()[0]["resolved"]["INTERNAL_REGISTRY"], "file-reg")

    def test_operator_unset_stays_empty_no_task_defaults(self):
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        self.make_airgap_fixtures({})
        self.make_airgap_stage_double("deploy.sh")
        proc = self.run_task("airgap:deploy", extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        resolved = self.airgap_calls()[0]["resolved"]
        self.assertEqual(resolved["QUERY"], "")
        self.assertEqual(resolved["EMBED_MODE"], "")

    def test_bridge_set_matches_operator_keys(self):
        import re
        common = (REPO / "scripts/airgap/common.sh").read_text(encoding="utf-8")
        expected = set(re.search(r'OPERATOR_ENV_KEYS="([^"]+)"', common).group(1).split())
        self.assertGreater(len(expected), 50, "operator key list unexpectedly small")
        self.assertIn("AIRGAP_ENV", expected, "AIRGAP_ENV must be an operator key")
        yml = (REPO / "taskfiles/airgap.yml").read_text(encoding="utf-8")
        blocks = re.findall(r"    env:\n((?:      TASK_[A-Z_]+: .*\n)+)", yml)
        # Seven bridged stages; dryrun carries fixed params instead of bridges.
        self.assertEqual(len(blocks), 7)
        for block in blocks:
            bridged = set(re.findall(r"^      TASK_([A-Z_]+): ", block, re.MULTILINE)) - {"OP_KEYS"}
            self.assertEqual(bridged, expected)

    def test_operator_precedence_three_way_conflict(self):
        # Three conflicting values: ambient env, CLI, and env file (TR435-F1)
        # CLI wins over ambient and file; ambient wins over file.
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        self.make_airgap_fixtures({"INTERNAL_REGISTRY": "file-reg", "NAMESPACE": "file-ns", "CORPUS_PVC": "file-pvc"})
        self.make_airgap_stage_double("deploy.sh")
        proc = self.run_task("airgap:deploy", "NAMESPACE=cli-ns",
                             extra_env=dict(self.tool_env(), NAMESPACE="ambient-ns", INTERNAL_REGISTRY="ambient-reg"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        resolved = self.airgap_calls()[0]["resolved"]
        self.assertEqual(resolved["NAMESPACE"], "cli-ns", "CLI must override ambient and file")
        self.assertEqual(resolved["INTERNAL_REGISTRY"], "ambient-reg", "ambient must override file when CLI unset")
        self.assertEqual(resolved["CORPUS_PVC"], "file-pvc", "file applies when CLI and ambient unset")

    def test_airgap_dryrun_cli_and_ambient_precedence(self):
        # TR435-F1: AIRGAP_DRYRUN selection must not be defeated by ambient env
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        self.make_airgap_fixtures({"AIRGAP_DRYRUN": "0"})
        self.make_airgap_stage_double("deploy.sh")
        # 1. Ambient 0 + CLI 1 -> child receives 1
        proc = self.run_task("airgap:deploy", "AIRGAP_DRYRUN=1",
                             extra_env=dict(self.tool_env(), AIRGAP_DRYRUN="0"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        resolved = self.airgap_calls()[0]["resolved"]
        self.assertEqual(resolved["AIRGAP_DRYRUN"], "1")
        if (self.log).exists():
            self.log.unlink()
        # 2. Ambient 1 + CLI 0 -> child receives 0
        proc = self.run_task("airgap:deploy", "AIRGAP_DRYRUN=0",
                             extra_env=dict(self.tool_env(), AIRGAP_DRYRUN="1"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        resolved = self.airgap_calls()[0]["resolved"]
        self.assertEqual(resolved["AIRGAP_DRYRUN"], "0")

    def test_airgap_env_selector_cli_and_ambient_precedence(self):
        # TR435-F2: AIRGAP_ENV selector directs common.sh to source the chosen file
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        # Default airgap.env
        self.make_airgap_fixtures({"INTERNAL_REGISTRY": "default-file-reg", "NAMESPACE": "default-file-ns"})
        # Custom file
        custom_env = self.root / "custom.env"
        custom_env.write_text("INTERNAL_REGISTRY=custom-file-reg\nNAMESPACE=custom-file-ns\n", encoding="utf-8")
        # Ambient env file
        ambient_env = self.root / "ambient.env"
        ambient_env.write_text("INTERNAL_REGISTRY=ambient-file-reg\nNAMESPACE=ambient-file-ns\n", encoding="utf-8")
        self.make_airgap_stage_double("deploy.sh")
        # CLI selector overrides ambient selector and default airgap.env
        proc = self.run_task("airgap:deploy", f"AIRGAP_ENV={custom_env}",
                             extra_env=dict(self.tool_env(), AIRGAP_ENV=str(ambient_env)))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        resolved = self.airgap_calls()[0]["resolved"]
        self.assertEqual(resolved["INTERNAL_REGISTRY"], "custom-file-reg")
        self.assertEqual(resolved["NAMESPACE"], "custom-file-ns")
        if (self.log).exists():
            self.log.unlink()
        # Ambient selector applies when CLI selector unset
        proc = self.run_task("airgap:deploy",
                             extra_env=dict(self.tool_env(), AIRGAP_ENV=str(ambient_env)))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        resolved = self.airgap_calls()[0]["resolved"]
        self.assertEqual(resolved["INTERNAL_REGISTRY"], "ambient-file-reg")
        self.assertEqual(resolved["NAMESPACE"], "ambient-file-ns")

    def test_airgap_env_missing_file_fails_task_invocation(self):
        # Issue #478: Task invocation parses through the same shipped
        # common.sh, so a missing selection refuses identically and the
        # stage double (recorder) is never reached — zero mutations.
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        self.make_airgap_fixtures({"INTERNAL_REGISTRY": "default-file-reg", "NAMESPACE": "default-file-ns"})
        self.make_airgap_stage_double("deploy.sh")
        missing = self.root / "poc-478-missing.env"
        self.assertFalse(missing.exists())
        proc = self.run_task("airgap:deploy", f"AIRGAP_ENV={missing}",
                             extra_env=dict(self.tool_env(), AIRGAP_ENV=str(missing)))
        self.assertNotEqual(proc.returncode, 0)
        # run_task merges stderr into stdout.
        self.assertIn(f"AIRGAP_ENV selects '{missing}'", proc.stdout)
        self.assertEqual(self.airgap_calls(), [])

    def test_dryrun_fixed_params_win(self):
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        self.make_airgap_fixtures({"INTERNAL_REGISTRY": "file-reg"})
        self.make_airgap_stage_double("pipeline.sh")
        proc = self.run_task("airgap:dryrun", "INTERNAL_REGISTRY=evil-reg",
                             extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.airgap_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], ["--dry-run"])
        resolved = calls[0]["resolved"]
        self.assertEqual(resolved["AIRGAP_DRYRUN"], "1")
        self.assertEqual(resolved["INTERNAL_REGISTRY"], "registry.example.internal/mainframe-rag")
        self.assertEqual(resolved["NAMESPACE"], "mainframe-rag")
        self.assertEqual(resolved["STORAGE_CLASS"], "gp3-csi")
        self.assertIsNone(resolved["QUERY"], "unrelated keys stay absent, never defaulted")

    def test_task_verification_fails_closed_without_autosetup(self):
        # Explicit setup stays separate from verification: Task diagnosis
        # fails closed on a missing .venv without auto-creating it.
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        shutil.rmtree(self.root / ".venv", ignore_errors=True)
        proc_task = self.run_task("qa:lint", extra_env=self.tool_env())
        self.assertNotEqual(proc_task.returncode, 0)
        self.assertIn("missing development environment: .venv/bin/python absent", proc_task.stdout)
        self.assertIn("task dev:setup", proc_task.stdout)
        self.assertFalse((self.root / ".venv").exists(), "Task must not auto-create .venv")

    def test_dev_setup_completion_stamp(self):
        # dev:setup requires both .venv/bin/python AND .venv/.setup-complete.
        # If .venv/bin/python exists but .setup-complete is missing (simulated failed pip install),
        # dev:setup must NOT skip; it must re-run cmds.
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        self.make_tool_recorder("fake-pip")
        py_path = self.root / "bin/fakepy"
        py_path.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
            '    mkdir -p .venv/bin\n'
            '    touch .venv/bin/python\n'
            '    chmod +x .venv/bin/python\n'
            '    exit 0\n'
            'fi\n'
            'exit 0\n',
            encoding="utf-8")
        py_path.chmod(0o755)

        # 1. First run: status fails, cmds run, stamp is created
        proc = self.run_task("dev:setup", "PY=fakepy", extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertTrue((self.root / ".venv/.setup-complete").is_file(), "stamp must be written on success")

        # 2. Status passes when stamp exists (up-to-date)
        proc = self.run_task("dev:setup", "PY=fakepy", extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn('Task "dev:setup" is up to date', proc.stdout)

        # 3. If stamp is removed while interpreter exists (partial setup), status fails and it re-runs
        (self.root / ".venv/.setup-complete").unlink()
        proc = self.run_task("dev:setup", "PY=fakepy", extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertNotIn('Task "dev:setup" is up to date', proc.stdout)
        self.assertTrue((self.root / ".venv/.setup-complete").is_file(), "stamp re-created on retry")

    def test_dev_demo_pdfs_and_clean(self):
        self.make_venv_fake()
        env = self.tool_env()
        proc = self.run_task("dev:demo-pdfs", extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(
            [c["argv"] for c in self.pip_calls()],
            [["scripts/make_synthetic_pdf.py", "--out", "output/demo-pdfs/SA22-0000-00_outline.pdf"],
             ["scripts/make_synthetic_pdf.py", "--plain", "--out", "output/demo-pdfs/plain-widget-notes.pdf"]])
        self.assertTrue((self.root / "output/demo-pdfs").is_dir())

    def test_dev_clean_bounded_retention(self):
        self.recorder_env = {"RECORDER_LOG": str(self.log), "RECORDER_TAG": "x", "RECORDER_EXIT": "0"}
        keepers = [".tools/bin/task", "dist/keep.tar", "airgap.env"]
        removable = [".venv/bin/python", ".pytest_cache/x", ".mypy_cache/x", ".ruff_cache/x",
                     "bundles/wheelhouse/f.whl", "output/demo-pdfs/a.pdf",
                     "dist/drop.txt", "dist/scratch/y"]
        for name in keepers + removable:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture", encoding="utf-8")
        (self.root / ".tools/bin/task").chmod(0o755)
        proc = self.run_task("dev:clean", extra_env=self.tool_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for name in removable:
            self.assertFalse((self.root / name).exists(), name)
        for name in keepers:
            self.assertTrue((self.root / name).is_file(), name)
        self.assertTrue((self.root / "Taskfile.yml").is_file(), "tracked sources survive clean")
        self.assertTrue((self.root / "dist").is_dir(), "dist survives for its kept archives")

    def test_taskfile_wiring_stays_dispatch_only(self):
        root_text = (REPO / "Taskfile.yml").read_text(encoding="utf-8")
        quality_text = (REPO / "taskfiles/quality.yml").read_text(encoding="utf-8")
        dev_text = (REPO / "taskfiles/dev.yml").read_text(encoding="utf-8")
        artifacts_text = (REPO / "taskfiles/artifacts.yml").read_text(encoding="utf-8")
        eval_text = (REPO / "taskfiles/eval.yml").read_text(encoding="utf-8")
        local_text = (REPO / "taskfiles/local.yml").read_text(encoding="utf-8")
        airgap_text = (REPO / "taskfiles/airgap.yml").read_text(encoding="utf-8")
        combined = root_text + quality_text + dev_text + artifacts_text + eval_text + local_text + airgap_text
        # Local required namespaced includes; one implementation per alias.
        self.assertIn("taskfile: ./taskfiles/quality.yml", root_text)
        self.assertIn("taskfile: ./taskfiles/dev.yml", root_text)
        self.assertIn("taskfile: ./taskfiles/artifacts.yml", root_text)
        self.assertIn("taskfile: ./taskfiles/eval.yml", root_text)
        self.assertIn("taskfile: ./taskfiles/local.yml", root_text)
        self.assertIn("taskfile: ./taskfiles/airgap.yml", root_text)
        for alias, canonical in (("task: qa:lint", "lint"), ("task: qa:check", "check"),
                                 ("task: qa:context", "context"), ("task: dev:doctor", "doctor")):
            self.assertIn(alias, root_text, canonical)
        # Forbidden in this migration: remote includes, private env loading,
        # parallel-dep orchestration, freshness caching on verification.
        # (Comments excluded: the Taskfiles document these prohibitions.)
        code = "\n".join(
            line for line in combined.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("dotenv", code)
        # No remote includes: every taskfile reference resolves locally.
        # (Plain http(s) defaults such as AGENT_URL are legitimate and
        # covered by exact-argv tests, not by this structural check.)
        for line in code.splitlines():
            if "taskfile:" in line:
                self.assertIn("./", line)
                self.assertNotIn("http", line)
        self.assertNotIn("deps:", code)
        # Optional `--flag value` pairs accumulate through positional
        # parameters: quotes nested inside `${VAR:+...}` do NOT survive outer
        # field splitting on any POSIX shell, so that idiom is forbidden.
        self.assertNotIn(":+--", code)
        self.assertNotIn("sources:", code)
        self.assertNotIn("method:", code)
        self.assertNotIn("ignore_error", code)
        self.assertNotIn("export EMBED_MODE", code)
        self.assertNotIn("airgap.env", code)
        # Verification and evaluation never cache (`sources:` fingerprints
        # even write state on `--list --json`); only artifact builds (plus
        # the explicit dev:setup presence check) may carry freshness state,
        # proven by content-bearing completion stamps re-verified on every run.
        verify_code = "\n".join(
            line for line in (root_text + quality_text + eval_text).splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn("status:", verify_code)
        build_code = "\n".join(
            line for line in artifacts_text.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIn(".task-complete", build_code)
        self.assertIn("sha256sum", build_code)
        dev_code = "\n".join(
            line for line in dev_text.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIn(".setup-complete", dev_code)
        # Mode/venue bridged under TASK_ prefixes and bound at the command boundary
        # so ambient environment variables cannot defeat CLI inputs.
        eval_code = "\n".join(
            line for line in eval_text.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIn("TASK_EMBED_MODE: '{{.EMBED_MODE}}'", eval_code)
        self.assertIn("TASK_VENUE: '{{.VENUE}}'", eval_code)
        self.assertIn('EMBED_MODE="$TASK_EMBED_MODE"', eval_code)
        self.assertIn('VENUE="$TASK_VENUE"', eval_code)


if __name__ == "__main__":
    unittest.main()
