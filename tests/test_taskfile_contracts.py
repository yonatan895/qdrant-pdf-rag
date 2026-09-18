"""Task runner-boundary contracts for issue #402 (increments A: quality/context,
B: artifacts; eval/local/air-gap follow in later slices).

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

VENV_FAKE = """#!/bin/sh
# Fake .venv interpreter: answers `-V` from $FAKE_PY_VERSION without logging
# (status probes stay observable through rebuild/skip behavior), records all
# real build invocations as JSON. Produces deterministic stand-in members on success.
if [ "$1" = "-V" ]; then echo "${FAKE_PY_VERSION:-Python 3.14.5}"; exit 0; fi
python3 - "$RECORDER_LOG" "venv-python" "$@" <<'PYEOF'
import json, os, sys
log, tag, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tag": tag, "argv": argv, "cwd": os.getcwd()}) + "\\n")
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

    def pip_calls(self) -> list[dict]:
        return [c for c in self.calls() if c["tag"] == "venv-python"]

    def tool_calls(self, name: str) -> list[dict]:
        return [c for c in self.calls() if c["tag"] == f"tool-{name}"]

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

    def test_taskfile_wiring_stays_dispatch_only(self):
        root_text = (REPO / "Taskfile.yml").read_text(encoding="utf-8")
        quality_text = (REPO / "taskfiles/quality.yml").read_text(encoding="utf-8")
        dev_text = (REPO / "taskfiles/dev.yml").read_text(encoding="utf-8")
        artifacts_text = (REPO / "taskfiles/artifacts.yml").read_text(encoding="utf-8")
        combined = root_text + quality_text + dev_text + artifacts_text
        # Local required namespaced includes; one implementation per alias.
        self.assertIn("taskfile: ./taskfiles/quality.yml", root_text)
        self.assertIn("taskfile: ./taskfiles/dev.yml", root_text)
        self.assertIn("taskfile: ./taskfiles/artifacts.yml", root_text)
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
        self.assertNotIn("method:", code)
        self.assertNotIn("ignore_error", code)
        self.assertNotIn("export EMBED_MODE", code)
        self.assertNotIn("airgap.env", code)
        # Verification never caches (`sources:` fingerprints even write
        # state on `--list --json`); only artifact builds (plus the explicit
        # dev:setup presence check) may carry freshness state, proven by
        # content-bearing completion stamps re-verified on every run.
        verify_code = "\n".join(
            line for line in (root_text + quality_text).splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn("status:", verify_code)
        build_code = "\n".join(
            line for line in artifacts_text.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIn(".task-complete", build_code)
        self.assertIn("sha256sum", build_code)


if __name__ == "__main__":
    unittest.main()
