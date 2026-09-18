"""Task runner-boundary contracts for issue #402 increment A.

Exercises the ACTUAL pinned Task binary against the migrated Taskfiles with
inert process-boundary recorders in temporary workspaces. Expectations below
are hardcoded (independent of the YAML under test); the YAML is the
implementation, the recorder log is the evidence.

Needs a provisioned Task binary (scripts/tools/install-task.sh): TASK_BIN,
then .tools/bin/task, then PATH `task`, with `task --version` matching
scripts/tools/task-pin.txt. Without it these tests SKIP, unless
TASK_CONTRACTS_REQUIRE_RUNNER=1 is set (designated CI lane), where a missing
or mismatched binary FAILS instead of passing silently.

Hermetic: no venv installs, no Docker/GPU/model/cluster, no network, no
private config. Only stdlib (runnable with the system interpreter).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PIN_VERSION = ""
for _line in (REPO / "scripts/tools/task-pin.txt").read_text(encoding="utf-8").splitlines():
    if _line.startswith("version:"):
        PIN_VERSION = _line.split(":", 1)[1].strip().lstrip("v")
REQUIRE_RUNNER = os.environ.get("TASK_CONTRACTS_REQUIRE_RUNNER") == "1"

RECORDER = """#!/bin/sh
# Inert boundary recorder: appends one JSON line per invocation, then exits
# with $RECORDER_EXIT (default 0). Never touches the network or services.
python3 - "$RECORDER_LOG" "$RECORDER_TAG" "$@" <<'PYEOF'
import json, os, sys
log, tag, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tag": tag, "argv": argv, "cwd": os.getcwd()}) + "\\n")
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
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], ["-m", "pytest", "tests", "-v"])
        self.assertEqual(calls[0]["cwd"], str(self.root))

    def test_unit_focused_selection_replaces_default(self):
        self.make_recorder(self.root / ".venv/bin/python")
        proc = self.run_task("qa:unit", "--", "tests/test_agent_context.py", "-q",
                             extra_env=self.recorder_env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        calls = self.calls()
        self.assertEqual(len(calls), 1, "focused selection must run once, not default+selection")
        self.assertEqual(calls[0]["argv"], ["-m", "pytest", "tests/test_agent_context.py", "-q"])

    def test_check_runs_lint_typecheck_unit_in_order(self):
        self.make_recorder(self.root / ".venv/bin/python")
        proc = self.run_task("qa:check", extra_env=self.recorder_env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        argv = [c["argv"] for c in self.calls()]
        self.assertEqual(argv, [
            ["-m", "ruff", "check", "src", "tests"],
            ["-m", "mypy", "src"],
            ["-m", "pytest", "tests", "-v"],
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

    def test_taskfile_wiring_stays_dispatch_only(self):
        root_text = (REPO / "Taskfile.yml").read_text(encoding="utf-8")
        quality_text = (REPO / "taskfiles/quality.yml").read_text(encoding="utf-8")
        dev_text = (REPO / "taskfiles/dev.yml").read_text(encoding="utf-8")
        combined = root_text + quality_text + dev_text
        # Local required namespaced includes; one implementation per alias.
        self.assertIn("taskfile: ./taskfiles/quality.yml", root_text)
        self.assertIn("taskfile: ./taskfiles/dev.yml", root_text)
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
        self.assertNotIn("http://", code)
        self.assertNotIn("https://", code)
        self.assertNotIn("deps:", code)
        self.assertNotIn("sources:", code)
        self.assertNotIn("ignore_error", code)
        self.assertNotIn("export EMBED_MODE", code)
        self.assertNotIn("airgap.env", code)


if __name__ == "__main__":
    unittest.main()
