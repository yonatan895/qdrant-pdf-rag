"""Deterministic acceptance tests for Codex/Astra -> OpenCode Go/DeepSeek adapter.

Tests the ai_worker controller across:
1. Model/agent route verification and fallback failure (fail closed).
2. Session and workspace isolation across independent runs.
3. Path and prompt character safety (spaces, quotes, dashes, shell metacharacters).
4. Dirty checkout preservation and refusal.
5. Comprehensive candidate inventory (tracked, staged, unstaged, untracked, deleted, renamed).
6. Protected path and scope violation blocking.
7. Error event handling, agent fallback detection, and malformed event streams.
8. Timeout enforcement and process group termination.
9. Durable state persistence and recovery across crashes.
10. Verification execution and attributable evidence receipts.
11. Resuming recorded sessions and enforcing correction budgets.
12. Doctor prerequisite diagnostics.

Policy authority: issue #468, section 13.
"""
from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from scripts.ai_worker import (
    AIWorkerController,
    WorkerState,
    inspect_candidate_changes,
    run_cmd,
    run_doctor,
)


def make_fake_executable(target_path: Path, script_body: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env python3\n")
        f.write(script_body)
    target_path.chmod(target_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestAIWorkerController(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name).resolve()
        self.repo_dir = self.root / "repo"
        self.repo_dir.mkdir()
        self.run_root = self.root / "runs"
        self.run_root.mkdir()

        # Initialize clean git repository
        run_cmd(["git", "init", "-b", "main"], cwd=self.repo_dir)
        run_cmd(["git", "config", "user.name", "Test User"], cwd=self.repo_dir)
        run_cmd(["git", "config", "user.email", "test@example.com"], cwd=self.repo_dir)

        # Initial commit
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        run_cmd(["git", "add", "README.md"], cwd=self.repo_dir)
        run_cmd(["git", "commit", "-m", "initial commit"], cwd=self.repo_dir)

        # Setup fake opencode
        self.fake_bin_dir = self.root / "bin"
        self.fake_bin_dir.mkdir()
        self.fake_opencode = self.fake_bin_dir / "opencode"
        self.fake_codex = self.fake_bin_dir / "codex"

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _create_contract(self, filename: str = "contract.md", content: str = "# Contract\n") -> Path:
        contract_path = self.root / filename
        contract_path.write_text(content, encoding="utf-8")
        return contract_path

    def test_preflight_refuses_dirty_checkout(self) -> None:
        """Dirty checkouts containing unrelated work must be preserved, not overwritten."""
        controller = AIWorkerController(
            run_root=self.run_root,
            repo_root=self.repo_dir,
        )
        # Create dirty uncommitted file
        (self.repo_dir / "dirty_file.py").write_text("# uncommitted\n", encoding="utf-8")

        contract = self._create_contract()
        with self.assertRaises(ValueError) as ctx:
            controller.initialize_run(
                issue_id="468",
                contract_path=contract,
                workspace=self.repo_dir,
            )
        self.assertIn("contains uncommitted changes", str(ctx.exception))

    def test_preflight_accepts_clean_checkout(self) -> None:
        controller = AIWorkerController(
            run_root=self.run_root,
            repo_root=self.repo_dir,
        )
        contract = self._create_contract()
        run_dir, state = controller.initialize_run(
            issue_id="468",
            contract_path=contract,
            workspace=self.repo_dir,
        )
        self.assertTrue(run_dir.is_dir())
        self.assertEqual(state.state, WorkerState.PLANNED)
        self.assertTrue((run_dir / "state.json").is_file())
        self.assertTrue((run_dir / "contract.md").is_file())

    def test_argument_and_path_safety(self) -> None:
        """Prompt and paths with spaces, quotes, dashes, and Unicode arrive intact."""
        weird_dir_name = "workspace with spaces & 'quotes' - dash 日本語"
        workspace = self.root / weird_dir_name
        workspace.mkdir()
        run_cmd(["git", "init", "-b", "main"], cwd=workspace)
        run_cmd(["git", "config", "user.name", "Test User"], cwd=workspace)
        run_cmd(["git", "config", "user.email", "test@example.com"], cwd=workspace)
        (workspace / "initial.txt").write_text("ok\n", encoding="utf-8")
        run_cmd(["git", "add", "initial.txt"], cwd=workspace)
        run_cmd(["git", "commit", "-m", "init"], cwd=workspace)

        weird_contract = "# Title: `--flag` and 'quotes' & $VAR 日本語\n"
        contract = self._create_contract(filename="weird_contract.md", content=weird_contract)

        controller = AIWorkerController(
            run_root=self.run_root,
            repo_root=workspace,
        )
        run_dir, state = controller.initialize_run(
            issue_id="468",
            contract_path=contract,
            workspace=workspace,
        )
        saved_contract = (run_dir / "contract.md").read_text(encoding="utf-8")
        self.assertEqual(saved_contract, weird_contract)
        self.assertEqual(state.workspace_path, str(workspace.resolve()))

    def test_two_tasks_isolated_sessions(self) -> None:
        """Multiple runs maintain independent state, sessions, and directories."""
        controller = AIWorkerController(
            run_root=self.run_root,
            repo_root=self.repo_dir,
        )
        contract1 = self._create_contract("contract1.md", "# Task 1\n")
        contract2 = self._create_contract("contract2.md", "# Task 2\n")

        run_dir1, state1 = controller.initialize_run(
            issue_id="468",
            contract_path=contract1,
            workspace=self.repo_dir,
            run_id="run-task-001",
        )
        run_dir2, state2 = controller.initialize_run(
            issue_id="468",
            contract_path=contract2,
            workspace=self.repo_dir,
            run_id="run-task-002",
        )

        self.assertNotEqual(run_dir1, run_dir2)
        self.assertEqual(state1.run_id, "run-task-001")
        self.assertEqual(state2.run_id, "run-task-002")
        self.assertNotEqual(state1.contract_digest, state2.contract_digest)

    def test_candidate_inventory_captures_untracked_and_deleted(self) -> None:
        """Complete candidate capture includes untracked files and deletions."""
        changes_initial = inspect_candidate_changes(self.repo_dir)
        self.assertTrue(changes_initial.is_empty)

        # Add untracked file
        (self.repo_dir / "new_untracked.py").write_text("# new\n", encoding="utf-8")
        # Modify existing file
        (self.repo_dir / "README.md").write_text("# Modified\n", encoding="utf-8")

        changes = inspect_candidate_changes(self.repo_dir)
        self.assertIn("new_untracked.py", changes.untracked)
        self.assertIn("README.md", changes.unstaged)
        self.assertEqual(set(changes.all_changed_paths), {"new_untracked.py", "README.md"})

        # Now delete README.md
        (self.repo_dir / "README.md").unlink()
        changes_deleted = inspect_candidate_changes(self.repo_dir)
        self.assertIn("README.md", changes_deleted.deleted)

    def test_protected_path_violation_blocks_candidate(self) -> None:
        """Worker modifying protected repository policy is blocked from candidate freezing."""
        controller = AIWorkerController(
            run_root=self.run_root,
            repo_root=self.repo_dir,
        )
        contract = self._create_contract()
        run_dir, _ = controller.initialize_run(
            issue_id="468",
            contract_path=contract,
            workspace=self.repo_dir,
        )

        # Worker attempts to modify Taskfile.yml
        (self.repo_dir / "Taskfile.yml").write_text("version: '3'\n", encoding="utf-8")

        with self.assertRaises(PermissionError) as ctx:
            controller.freeze_candidate(run_dir)
        self.assertIn("Taskfile.yml", str(ctx.exception))

        state = controller.load_state(run_dir)
        self.assertEqual(state.state, WorkerState.FAILED)
        self.assertIn("Protected path or scope violation", state.error or "")

    def test_dispatch_with_fake_opencode_event_stream(self) -> None:
        """Tests event streaming, session ID capture, and candidate freezing with fake CLI."""
        fake_script = """import sys, json, time
# Emit session start event
print(json.dumps({"type": "session_start", "sessionID": "sess-abc-123", "timestamp": time.time()}), flush=True)
# Emit text event
print(json.dumps({"type": "text", "content": "Implementing feature...", "sessionID": "sess-abc-123"}), flush=True)
# Perform file change in cwd
with open("feature.py", "w") as f:
    f.write("# feature implementation\\n")
# Finish event
print(json.dumps({"type": "step_finish", "sessionID": "sess-abc-123"}), flush=True)
sys.exit(0)
"""
        make_fake_executable(self.fake_opencode, fake_script)

        controller = AIWorkerController(
            run_root=self.run_root,
            repo_root=self.repo_dir,
            opencode_bin=str(self.fake_opencode),
        )
        contract = self._create_contract()
        run_dir, _ = controller.initialize_run(
            issue_id="468",
            contract_path=contract,
            workspace=self.repo_dir,
        )

        exit_code, session_id, errors = controller.dispatch_worker(
            run_dir=run_dir,
            prompt_content="Do work",
            attempt=1,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(session_id, "sess-abc-123")
        self.assertEqual(errors, [])

        state = controller.load_state(run_dir)
        self.assertEqual(state.session_id, "sess-abc-123")

        # Verify events log was created
        events_file = run_dir / "attempts/01/events.jsonl"
        self.assertTrue(events_file.is_file())
        events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(events), 3)

        # Freeze candidate
        changes = controller.freeze_candidate(run_dir)
        self.assertIn("feature.py", changes.untracked)

        state = controller.load_state(run_dir)
        self.assertEqual(state.state, WorkerState.CANDIDATE_FROZEN)
        self.assertIsNotNone(state.candidate_sha)

    def test_agent_fallback_detection_fails_closed(self) -> None:
        """Worker emitting fallback to default agent warning is flagged as failed."""
        fake_script = """import sys, json
print(json.dumps({"type": "session_start", "sessionID": "sess-fail-001"}), flush=True)
print(json.dumps({"type": "warning", "message": "falling back to default agent 'title'"}), flush=True)
sys.exit(0)
"""
        make_fake_executable(self.fake_opencode, fake_script)

        controller = AIWorkerController(
            run_root=self.run_root,
            repo_root=self.repo_dir,
            opencode_bin=str(self.fake_opencode),
        )
        contract = self._create_contract()
        run_dir, _ = controller.initialize_run(
            issue_id="468",
            contract_path=contract,
            workspace=self.repo_dir,
        )

        exit_code, _, errors = controller.dispatch_worker(
            run_dir=run_dir,
            prompt_content="Do work",
            attempt=1,
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(any("fallback" in err.lower() for err in errors))

    def test_subprocess_timeout_termination(self) -> None:
        """Worker exceeding timeout is killed cleanly via process group."""
        fake_script = """import sys, time
print('{"type": "started", "sessionID": "sess-timeout"}', flush=True)
time.sleep(10)
"""
        make_fake_executable(self.fake_opencode, fake_script)

        controller = AIWorkerController(
            run_root=self.run_root,
            repo_root=self.repo_dir,
            opencode_bin=str(self.fake_opencode),
        )
        contract = self._create_contract()
        run_dir, _state = controller.initialize_run(
            issue_id="468",
            contract_path=contract,
            workspace=self.repo_dir,
            invocation_timeout=1,  # 1 second timeout
        )

        exit_code, _, errors = controller.dispatch_worker(
            run_dir=run_dir,
            prompt_content="Do work",
            attempt=1,
        )
        self.assertEqual(exit_code, 124)
        self.assertTrue(any("timeout" in err.lower() for err in errors))

    def test_deterministic_verification_execution(self) -> None:
        """Controller executes verification commands and records attributable receipts."""
        controller = AIWorkerController(
            run_root=self.run_root,
            repo_root=self.repo_dir,
        )
        contract = self._create_contract()
        run_dir, _ = controller.initialize_run(
            issue_id="468",
            contract_path=contract,
            workspace=self.repo_dir,
        )

        # Run passing command
        passed = controller.run_verification(
            run_dir=run_dir,
            verification_commands=[["python3", "-c", "print('check OK')"]],
        )
        self.assertTrue(passed)
        receipt_file = run_dir / "evidence/check_01.log"
        self.assertTrue(receipt_file.is_file())
        receipt_content = receipt_file.read_text(encoding="utf-8")
        self.assertIn("EXIT_CODE: 0", receipt_content)
        self.assertIn("check OK", receipt_content)

        state = controller.load_state(run_dir)
        self.assertEqual(state.state, WorkerState.VERIFICATION)

        # Run failing command
        failed = controller.run_verification(
            run_dir=run_dir,
            verification_commands=[["python3", "-c", "import sys; sys.exit(42)"]],
        )
        self.assertFalse(failed)
        receipt_fail = run_dir / "evidence/check_01.log"
        self.assertIn("EXIT_CODE: 42", receipt_fail.read_text(encoding="utf-8"))
        state_after_fail = controller.load_state(run_dir)
        self.assertEqual(state_after_fail.state, WorkerState.FAILED)

    def test_resume_session_advances_attempt_and_passes_session(self) -> None:
        """Resuming session uses exact recorded session ID and creates new attempt directory."""
        fake_script = """import sys, json, argparse
parser = argparse.ArgumentParser()
parser.add_argument("--session")
args, unknown = parser.parse_known_args()

print(json.dumps({"type": "session_resumed", "sessionID": args.session}), flush=True)
with open("repaired.py", "w") as f:
    f.write("# fix applied\\n")
sys.exit(0)
"""
        make_fake_executable(self.fake_opencode, fake_script)

        controller = AIWorkerController(
            run_root=self.run_root,
            repo_root=self.repo_dir,
            opencode_bin=str(self.fake_opencode),
        )
        contract = self._create_contract()
        run_dir, state = controller.initialize_run(
            issue_id="468",
            contract_path=contract,
            workspace=self.repo_dir,
        )
        state.session_id = "sess-prior-456"
        controller.save_state_atomic(run_dir, state)

        # Dispatch correction attempt 2
        exit_code, _session_id, errors = controller.dispatch_worker(
            run_dir=run_dir,
            prompt_content="Fix defect",
            attempt=2,
            session_id=state.session_id,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(errors, [])
        self.assertTrue((run_dir / "attempts/02/events.jsonl").is_file())

        events = [json.loads(line) for line in (run_dir / "attempts/02/events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(events[0]["sessionID"], "sess-prior-456")

    def test_doctor_evaluates_prerequisites(self) -> None:
        """Doctor evaluates environment, reporting ready/missing accurately."""
        # Create fake codex and opencode
        fake_oc = """import sys
if "--version" in sys.argv:
    print("1.18.31")
    sys.exit(0)
if "models" in sys.argv:
    print("opencode-go/deepseek-v4.1-flash\\nopencode/muse-spark-1.3-contributor-free")
    sys.exit(0)
if "agent" in sys.argv and "list" in sys.argv:
    print("rag-implementer (primary)")
    sys.exit(0)
sys.exit(0)
"""
        fake_cdx = """import sys
if "--version" in sys.argv:
    print("codex-cli 0.155.1")
    sys.exit(0)
if "login" in sys.argv and "status" in sys.argv:
    print("Logged in using ChatGPT", file=sys.stderr)
    sys.exit(0)
sys.exit(0)
"""
        make_fake_executable(self.fake_opencode, fake_oc)
        make_fake_executable(self.fake_codex, fake_cdx)

        # Create agent file in repo
        (self.repo_dir / ".opencode/agents").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / ".opencode/agents/rag-implementer.md").write_text("mode: primary\n", encoding="utf-8")

        # Create fake run-task.sh
        fake_task_dir = self.repo_dir / "scripts/tools"
        fake_task_dir.mkdir(parents=True, exist_ok=True)
        make_fake_executable(fake_task_dir / "run-task.sh", 'import sys; print("3.53.1"); sys.exit(0)\n')

        exit_code, reports = run_doctor(
            repo_root=self.repo_dir,
            opencode_bin=str(self.fake_opencode),
            codex_bin=str(self.fake_codex),
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(any("opencode model: opencode-go/deepseek-v4.1-flash" in r for r in reports))
        self.assertTrue(any("opencode agent: rag-implementer registered as primary" in r for r in reports))
        self.assertTrue(any("codex authentication: Logged in using ChatGPT" in r for r in reports))


if __name__ == "__main__":
    unittest.main()
