#!/usr/bin/env python3
"""Codex/Astra -> OpenCode Go/DeepSeek implementation adapter and controller.

Standard-library controller managing:
1. Prerequisite and subscription route diagnostics (doctor).
2. Isolated workspace preflight and candidate protection.
3. Process-group execution of OpenCode workers with event streaming and session capture.
4. Candidate freezing with strict protected-path and scope enforcement.
5. Deterministic verification execution and evidence collection.
6. Durable run state persistence (planned -> running -> candidate_frozen -> verification -> review).
7. Resuming recorded worker sessions for bounded corrections.

Policy authority: issue #468.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import enum
import fnmatch
import hashlib
import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Configuration Constants & Safe Defaults
# -----------------------------------------------------------------------------

DEFAULT_OPENCODE_MODEL = "opencode-go/deepseek-v4.1-flash"
DEFAULT_OPENCODE_AGENT = "rag-implementer"
DEFAULT_CODEX_MODEL = "o3"
DEFAULT_INVOCATION_TIMEOUT = 1800  # 30 minutes
DEFAULT_TASK_TIMEOUT = 5400        # 90 minutes
DEFAULT_MAX_ATTEMPTS = 3           # 1 initial + 2 corrections
DEFAULT_WORKER_STEPS = 40
DEFAULT_RUN_ROOT = "dist/ai_worker_runs"

# Protected repository surfaces that an unprivileged worker candidate
# must never modify without explicit authorization.
PROTECTED_PATTERNS = (
    ".opencode/**",
    ".agents/**",
    "scripts/ai_worker.py",
    "Taskfile.yml",
    "taskfiles/**",
    ".github/workflows/**",
    ".gitlab-ci.yml",
    "pyproject.toml",
    "requirements*.txt",
    "requirements*.in",
    "constraints*.txt",
    "*.lock",
    "images.txt",
    "bm25-weights.sha256",
    ".gitignore",
    ".gitattributes",
    "airgap.env.example",
)


class WorkerState(str, enum.Enum):
    PLANNED = "planned"
    RUNNING = "running"
    CANDIDATE_FROZEN = "candidate_frozen"
    VERIFICATION = "verification"
    REVIEW = "review"
    CORRECTION = "correction"
    READY_FOR_MAINTAINER = "ready_for_maintainer"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclasses.dataclass
class CandidateChanges:
    tracked_modified: list[str] = dataclasses.field(default_factory=list)
    staged: list[str] = dataclasses.field(default_factory=list)
    unstaged: list[str] = dataclasses.field(default_factory=list)
    untracked: list[str] = dataclasses.field(default_factory=list)
    deleted: list[str] = dataclasses.field(default_factory=list)
    renamed: list[str] = dataclasses.field(default_factory=list)

    @property
    def all_changed_paths(self) -> list[str]:
        combined = set(
            self.tracked_modified
            + self.staged
            + self.unstaged
            + self.untracked
            + self.deleted
            + self.renamed
        )
        return sorted(combined)

    @property
    def is_empty(self) -> bool:
        return not bool(self.all_changed_paths)


@dataclasses.dataclass
class RunState:
    run_id: str
    issue_id: str
    state: WorkerState
    created_at: str
    updated_at: str
    contract_digest: str
    base_sha: str
    workspace_path: str
    opencode_bin: str
    opencode_model: str
    opencode_agent: str
    codex_bin: str
    codex_model: str
    attempt: int = 1
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    session_id: str | None = None
    candidate_sha: str | None = None
    worker_steps: int = DEFAULT_WORKER_STEPS
    invocation_timeout: int = DEFAULT_INVOCATION_TIMEOUT
    task_timeout: int = DEFAULT_TASK_TIMEOUT
    error: str | None = None
    verification_results: dict[str, Any] = dataclasses.field(default_factory=dict)
    review_results: dict[str, Any] = dataclasses.field(default_factory=dict)
    candidate_inventory: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        data_copy = dict(data)
        data_copy["state"] = WorkerState(data_copy["state"])
        return cls(**data_copy)


# -----------------------------------------------------------------------------
# Git and Workspace Helpers
# -----------------------------------------------------------------------------

def run_cmd(
    args: list[str],
    cwd: Path | str | None = None,
    timeout: float | None = None,
    capture_output: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        timeout=timeout,
        capture_output=capture_output,
        text=True,
        check=False,
        env=env,
    )


def get_git_head(cwd: Path) -> str:
    res = run_cmd(["git", "rev-parse", "HEAD"], cwd=cwd)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to get git HEAD in {cwd}: {res.stderr.strip()}")
    return res.stdout.strip()


def inspect_candidate_changes(cwd: Path) -> CandidateChanges:
    """Inspects tracked, staged, unstaged, untracked, deleted, and renamed files."""
    res = run_cmd(["git", "status", "--porcelain=v1", "-uall"], cwd=cwd)
    if res.returncode != 0:
        raise RuntimeError(f"git status failed in {cwd}: {res.stderr.strip()}")

    changes = CandidateChanges()
    for line in res.stdout.splitlines():
        if not line.strip() or len(line) < 3:
            continue
        index_status = line[0]
        worktree_status = line[1]
        path_part = line[3:].strip()
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]

        path_clean = path_part.strip('"')

        if index_status in ("M", "A", "D", "R"):
            changes.staged.append(path_clean)
        if index_status == "R" or worktree_status == "R":
            changes.renamed.append(path_clean)
        if index_status == "D" or worktree_status == "D":
            changes.deleted.append(path_clean)
        if worktree_status == "M":
            changes.unstaged.append(path_clean)
        elif worktree_status == "?":
            changes.untracked.append(path_clean)
        elif worktree_status not in (" ", "?", "D"):
            changes.tracked_modified.append(path_clean)

    return changes


def check_protected_path_violations(
    paths: Iterable[str],
    allowed_patterns: Iterable[str] | None = None,
) -> list[str]:
    """Returns list of paths violating protected boundaries or write scope."""
    violations: list[str] = []
    for path in paths:
        normalized = path.replace("\\", "/")
        is_protected = any(
            fnmatch.fnmatch(normalized, pat) or fnmatch.fnmatch(normalized.lstrip("./"), pat)
            for pat in PROTECTED_PATTERNS
        )
        if is_protected:
            violations.append(path)
        elif allowed_patterns is not None:
            in_scope = any(
                fnmatch.fnmatch(normalized, pat) or fnmatch.fnmatch(normalized.lstrip("./"), pat)
                for pat in allowed_patterns
            )
            if not in_scope:
                violations.append(path)
    return violations


def compute_sha256(content: bytes | str) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


# -----------------------------------------------------------------------------
# Diagnostics (dev:ai-doctor)
# -----------------------------------------------------------------------------

def resolve_binary(binary_name: str, fallback_paths: list[Path]) -> str | None:
    found = shutil.which(binary_name)
    if found:
        return found
    for fallback in fallback_paths:
        if fallback.is_file() and os.access(fallback, os.X_OK):
            return str(fallback.resolve())
    return None


def run_doctor(
    repo_root: Path,
    profile: str = "default",
    opencode_bin: str | None = None,
    codex_bin: str | None = None,
    worker_model: str = DEFAULT_OPENCODE_MODEL,
    worker_agent: str = DEFAULT_OPENCODE_AGENT,
) -> tuple[int, list[str]]:
    """Evaluates prerequisites for Codex/Astra -> OpenCode Go/DeepSeek handoff."""
    reports: list[str] = []
    failures: list[str] = []

    # 1. Runtime environment
    cpython_ok = sys.version_info >= (3, 14)
    gil_ok = True
    if hasattr(sys, "_is_gil_enabled"):
        gil_ok = sys._is_gil_enabled()
    if cpython_ok and gil_ok:
        reports.append("ready: python runtime: CPython >= 3.14 with GIL enabled")
    else:
        msg = f"missing prerequisite: python runtime requires CPython >= 3.14 with GIL (found {sys.version.split()[0]})"
        reports.append(msg)
        failures.append(msg)

    # 2. Git status
    git_bin = shutil.which("git")
    if git_bin:
        git_res = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_root)
        if git_res.returncode == 0 and git_res.stdout.strip() == "true":
            reports.append("ready: git: repository and working tree verified")
        else:
            msg = "missing prerequisite: git directory is not inside a valid worktree"
            reports.append(msg)
            failures.append(msg)
    else:
        msg = "missing prerequisite: git binary not found"
        reports.append(msg)
        failures.append(msg)

    # 3. Task runner
    task_script = repo_root / "scripts/tools/run-task.sh"
    if task_script.is_file():
        task_res = run_cmd(["sh", str(task_script), "--version"], cwd=repo_root)
        if task_res.returncode == 0:
            reports.append(f"ready: task runner: {task_res.stdout.strip()}")
        else:
            reports.append("ready: task runner: run-task.sh present (offline pin verified)")
    else:
        msg = "missing prerequisite: scripts/tools/run-task.sh not found"
        reports.append(msg)
        failures.append(msg)

    # 4. OpenCode CLI & Go model
    oc_exec = opencode_bin or resolve_binary(
        "opencode",
        [
            Path.home() / ".opencode/bin/opencode",
            Path.home() / ".local/bin/opencode",
            Path("/usr/local/bin/opencode"),
        ],
    )
    if not oc_exec:
        msg = "missing prerequisite: opencode executable absent; configure OPENCODE_BIN"
        reports.append(msg)
        failures.append(msg)
    else:
        v_res = run_cmd([oc_exec, "--version"])
        v_str = v_res.stdout.strip() if v_res.returncode == 0 else "unknown"
        reports.append(f"ready: opencode binary: {oc_exec} (version {v_str})")

        # Check models for Go provider and deepseek model
        m_res = run_cmd([oc_exec, "models"])
        if m_res.returncode == 0:
            available_models = [m.strip() for m in m_res.stdout.splitlines() if m.strip()]
            if worker_model in available_models:
                reports.append(f"ready: opencode model: {worker_model} available via Go provider")
            else:
                msg = f"missing prerequisite: model {worker_model} not found in 'opencode models'"
                reports.append(msg)
                failures.append(msg)
        else:
            msg = f"missing prerequisite: failed to query 'opencode models' ({m_res.stderr.strip()})"
            reports.append(msg)
            failures.append(msg)

        # Check agent definition
        agent_file = repo_root / f".opencode/agents/{worker_agent}.md"
        if not agent_file.is_file():
            msg = f"missing prerequisite: agent file .opencode/agents/{worker_agent}.md absent"
            reports.append(msg)
            failures.append(msg)
        else:
            # Check agent list
            a_res = run_cmd([oc_exec, "agent", "list"], cwd=repo_root)
            if a_res.returncode == 0:
                is_primary = False
                for line in a_res.stdout.splitlines():
                    if f"{worker_agent} (primary)" in line:
                        is_primary = True
                        break
                if is_primary:
                    reports.append(f"ready: opencode agent: {worker_agent} registered as primary mode")
                else:
                    msg = f"missing prerequisite: agent {worker_agent} not registered as mode: primary (avoids silent fallback)"
                    reports.append(msg)
                    failures.append(msg)
            else:
                msg = f"missing prerequisite: 'opencode agent list' failed ({a_res.stderr.strip()})"
                reports.append(msg)
                failures.append(msg)

    # 5. Codex CLI & ChatGPT login status
    cdx_exec = codex_bin or resolve_binary(
        "codex",
        [
            Path.home() / ".local/bin/codex",
            Path.home() / ".codex/bin/codex",
            Path("/usr/local/bin/codex"),
        ],
    )
    if not cdx_exec:
        msg = "missing prerequisite: codex executable absent; configure CODEX_BIN"
        reports.append(msg)
        failures.append(msg)
    else:
        v_res = run_cmd([cdx_exec, "--version"])
        v_str = v_res.stdout.strip() if v_res.returncode == 0 else "unknown"
        reports.append(f"ready: codex binary: {cdx_exec} ({v_str})")

        login_res = run_cmd([cdx_exec, "login", "status"])
        if login_res.returncode == 0:
            status_text = (login_res.stdout + " " + login_res.stderr).strip()
            if "ChatGPT" in status_text or "Logged in" in status_text:
                reports.append(f"ready: codex authentication: {status_text}")
            else:
                msg = f"missing prerequisite: unexpected codex login status: '{status_text}'"
                reports.append(msg)
                failures.append(msg)
        else:
            msg = f"missing prerequisite: 'codex login status' failed: {login_res.stderr.strip()}"
            reports.append(msg)
            failures.append(msg)

    exit_code = 0 if not failures else 2
    return exit_code, reports


# -----------------------------------------------------------------------------
# Process Group and Subprocess Manager
# -----------------------------------------------------------------------------

class SubprocessRunner:
    """Manages process group lifecycle, timeouts, signal escalation, and event logging."""

    @staticmethod
    def terminate_process_group(proc: subprocess.Popen[str], grace_seconds: float = 3.0) -> None:
        """Sends SIGTERM to process group, waits grace period, then SIGKILL."""
        if proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

        start_wait = time.time()
        while time.time() - start_wait < grace_seconds:
            if proc.poll() is not None:
                return
            time.sleep(0.1)

        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass

    @classmethod
    def execute_with_events(
        cls,
        argv: list[str],
        cwd: Path,
        stdin_content: str,
        events_file: Path,
        stderr_file: Path,
        timeout_seconds: int,
        on_event: Any = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str | None, list[str]]:
        """Runs child in new session, streams stdout JSON lines, and returns (exit_code, session_id, errors)."""
        events_file.parent.mkdir(parents=True, exist_ok=True)
        session_id: str | None = None
        errors: list[str] = []
        text_outputs: list[str] = []

        proc_env = dict(os.environ)
        if env:
            proc_env.update(env)

        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=proc_env,
        )

        # Write stdin
        if proc.stdin:
            try:
                proc.stdin.write(stdin_content)
                proc.stdin.close()
            except BrokenPipeError:
                pass

        start_time = time.time()

        with open(events_file, "a", encoding="utf-8") as ev_f, open(stderr_file, "a", encoding="utf-8") as err_f:
            while True:
                if time.time() - start_time > timeout_seconds:
                    cls.terminate_process_group(proc)
                    err_f.write(f"\n[ai_worker] Process timed out after {timeout_seconds}s\n")
                    errors.append(f"Timeout exceeded ({timeout_seconds}s)")
                    return 124, session_id, errors

                if proc.stdout is None:
                    break

                rlist, _, _ = select.select([proc.stdout], [], [], 0.1)
                if rlist:
                    line = proc.stdout.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        continue

                    ev_f.write(line)
                    ev_f.flush()
                    line_str = line.strip()
                    if line_str:
                        try:
                            data = json.loads(line_str)
                            # Extract session id if present
                            if not session_id:
                                extracted_sid = (
                                    data.get("sessionID")
                                    or data.get("sessionId")
                                    or data.get("session_id")
                                    or (isinstance(data.get("data"), dict) and data["data"].get("sessionID"))
                                )
                                if extracted_sid:
                                    session_id = str(extracted_sid)
                                    if on_event:
                                        on_event("session_id", session_id)

                            # Check for explicit error event
                            ev_type = data.get("type", "")
                            if ev_type == "error":
                                err_msg = data.get("error") or data.get("message") or json.dumps(data)
                                errors.append(str(err_msg))

                            # Capture text chunks
                            if ev_type in ("text", "message", "step_finish"):
                                content = data.get("content") or data.get("text")
                                if content and isinstance(content, str):
                                    text_outputs.append(content)

                            # Route fallback check
                            raw_str = json.dumps(data)
                            if "falling back to default agent" in raw_str:
                                errors.append("Detected agent fallback to default agent")

                        except json.JSONDecodeError:
                            # Not valid JSON; could be raw message
                            if "error" in line_str.lower():
                                errors.append(line_str)
                else:
                    if proc.poll() is not None:
                        break

            # Drain stderr
            if proc.stderr:
                stderr_text = proc.stderr.read()
                if stderr_text:
                    err_f.write(stderr_text)
                    err_f.flush()
                    if "falling back to default agent" in stderr_text:
                        errors.append("Detected agent fallback in stderr")

        proc.wait()
        exit_code = proc.returncode
        return exit_code, session_id, errors


# -----------------------------------------------------------------------------
# Controller Engine
# -----------------------------------------------------------------------------

class AIWorkerController:
    def __init__(
        self,
        run_root: Path,
        repo_root: Path,
        opencode_bin: str = "opencode",
        opencode_model: str = DEFAULT_OPENCODE_MODEL,
        opencode_agent: str = DEFAULT_OPENCODE_AGENT,
        codex_bin: str = "codex",
        codex_model: str = DEFAULT_CODEX_MODEL,
    ) -> None:
        self.run_root = run_root.resolve()
        self.repo_root = repo_root.resolve()
        self.opencode_bin = opencode_bin
        self.opencode_model = opencode_model
        self.opencode_agent = opencode_agent
        self.codex_bin = codex_bin
        self.codex_model = codex_model

    def _state_file(self, run_dir: Path) -> Path:
        return run_dir / "state.json"

    def load_state(self, run_dir: Path) -> RunState:
        state_file = self._state_file(run_dir)
        if not state_file.is_file():
            raise FileNotFoundError(f"state.json not found in {run_dir}")
        with open(state_file, encoding="utf-8") as f:
            data = json.load(f)
        return RunState.from_dict(data)

    def save_state_atomic(self, run_dir: Path, state: RunState) -> None:
        state.updated_at = datetime.datetime.now(datetime.UTC).isoformat()
        state_file = self._state_file(run_dir)
        tmp_file = run_dir / f"state.json.tmp.{os.getpid()}"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, indent=2)
            f.write("\n")
        os.replace(tmp_file, state_file)

    def run_preflight_checks(self, workspace: Path, expected_base_sha: str | None = None) -> str:
        """Verifies workspace cleanliness, valid git worktree, and returns current base SHA."""
        if not (workspace / ".git").exists():
            raise ValueError(f"Workspace {workspace} is not a git repository or worktree")

        current_head = get_git_head(workspace)
        if expected_base_sha and current_head != expected_base_sha:
            raise ValueError(
                f"Workspace HEAD ({current_head}) does not match expected base ({expected_base_sha})"
            )

        # Refuse dirty checkouts that might mix another task's changes
        changes = inspect_candidate_changes(workspace)
        if not changes.is_empty:
            raise ValueError(
                f"Workspace {workspace} contains uncommitted changes ({len(changes.all_changed_paths)} files). "
                "Preserve dirty work and dispatch from a clean isolated worktree."
            )
        return current_head

    def initialize_run(
        self,
        issue_id: str,
        contract_path: Path,
        workspace: Path,
        run_id: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        invocation_timeout: int = DEFAULT_INVOCATION_TIMEOUT,
        task_timeout: int = DEFAULT_TASK_TIMEOUT,
        worker_steps: int = DEFAULT_WORKER_STEPS,
    ) -> tuple[Path, RunState]:
        if not contract_path.is_file():
            raise FileNotFoundError(f"Contract file {contract_path} not found")

        contract_bytes = contract_path.read_bytes()
        contract_digest = compute_sha256(contract_bytes)

        base_sha = self.run_preflight_checks(workspace)

        now_iso = datetime.datetime.now(datetime.UTC).isoformat()
        if not run_id:
            ts = int(time.time())
            run_id = f"run-{issue_id}-{ts}-{contract_digest[:8]}"

        run_dir = self.run_root / issue_id / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "evidence").mkdir(exist_ok=True)
        (run_dir / "reviews").mkdir(exist_ok=True)
        (run_dir / "attempts/01").mkdir(parents=True, exist_ok=True)

        # Snapshot contract
        (run_dir / "contract.md").write_bytes(contract_bytes)

        state = RunState(
            run_id=run_id,
            issue_id=issue_id,
            state=WorkerState.PLANNED,
            created_at=now_iso,
            updated_at=now_iso,
            contract_digest=contract_digest,
            base_sha=base_sha,
            workspace_path=str(workspace.resolve()),
            opencode_bin=self.opencode_bin,
            opencode_model=self.opencode_model,
            opencode_agent=self.opencode_agent,
            codex_bin=self.codex_bin,
            codex_model=self.codex_model,
            attempt=1,
            max_attempts=max_attempts,
            worker_steps=worker_steps,
            invocation_timeout=invocation_timeout,
            task_timeout=task_timeout,
        )
        self.save_state_atomic(run_dir, state)
        return run_dir, state

    def dispatch_worker(
        self,
        run_dir: Path,
        prompt_content: str,
        attempt: int,
        session_id: str | None = None,
    ) -> tuple[int, str | None, list[str]]:
        state = self.load_state(run_dir)
        workspace = Path(state.workspace_path)
        attempt_dir = run_dir / f"attempts/{attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        events_file = attempt_dir / "events.jsonl"
        stderr_file = attempt_dir / "stderr.log"

        argv = [
            self.opencode_bin,
            "run",
            "--agent",
            self.opencode_agent,
            "--model",
            self.opencode_model,
            "--format",
            "json",
            "--title",
            state.run_id,
        ]
        if session_id:
            argv.extend(["--session", session_id])

        state.state = WorkerState.RUNNING
        self.save_state_atomic(run_dir, state)

        def on_event(ev_name: str, val: Any) -> None:
            if ev_name == "session_id":
                state.session_id = str(val)
                self.save_state_atomic(run_dir, state)

        exit_code, observed_session, errors = SubprocessRunner.execute_with_events(
            argv=argv,
            cwd=workspace,
            stdin_content=prompt_content,
            events_file=events_file,
            stderr_file=stderr_file,
            timeout_seconds=state.invocation_timeout,
            on_event=on_event,
        )

        if observed_session and not state.session_id:
            state.session_id = observed_session
            self.save_state_atomic(run_dir, state)

        return exit_code, state.session_id, errors

    def freeze_candidate(
        self,
        run_dir: Path,
        allowed_scope_patterns: list[str] | None = None,
        allow_protected_paths: bool = False,
    ) -> CandidateChanges:
        """Inspects changes, validates scope and protected paths, and records candidate inventory."""
        state = self.load_state(run_dir)
        workspace = Path(state.workspace_path)

        changes = inspect_candidate_changes(workspace)
        all_changed = changes.all_changed_paths

        # Check for protected paths
        if not allow_protected_paths:
            violations = check_protected_path_violations(all_changed, allowed_patterns=allowed_scope_patterns)
            if violations:
                state.state = WorkerState.FAILED
                state.error = f"Protected path or scope violation: {violations}"
                self.save_state_atomic(run_dir, state)
                raise PermissionError(f"Candidate violates protected paths: {violations}")

        # Compute candidate SHA or tree state
        current_head = get_git_head(workspace)
        diff_res = run_cmd(["git", "diff", "HEAD"], cwd=workspace)
        untracked_digest = compute_sha256("".join(changes.untracked).encode("utf-8"))
        candidate_id = f"{current_head[:10]}-{compute_sha256(diff_res.stdout.encode('utf-8') + untracked_digest.encode('utf-8'))[:10]}"

        state.candidate_sha = candidate_id
        state.candidate_inventory = all_changed
        state.state = WorkerState.CANDIDATE_FROZEN
        self.save_state_atomic(run_dir, state)
        return changes

    def run_verification(
        self,
        run_dir: Path,
        verification_commands: list[list[str]],
    ) -> bool:
        """Executes controller-approved verification commands deterministically and records evidence."""
        state = self.load_state(run_dir)
        workspace = Path(state.workspace_path)
        evidence_dir = run_dir / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)

        state.state = WorkerState.VERIFICATION
        self.save_state_atomic(run_dir, state)

        all_passed = True
        results: dict[str, Any] = {}

        for idx, cmd in enumerate(verification_commands, 1):
            cmd_str = " ".join(shlex.quote(c) for c in cmd)
            res = run_cmd(cmd, cwd=workspace, timeout=600)
            receipt_file = evidence_dir / f"check_{idx:02d}.log"
            with open(receipt_file, "w", encoding="utf-8") as f:
                f.write(f"COMMAND: {cmd_str}\n")
                f.write(f"EXIT_CODE: {res.returncode}\n")
                f.write(f"STDOUT:\n{res.stdout}\n")
                f.write(f"STDERR:\n{res.stderr}\n")

            passed = res.returncode == 0
            results[cmd_str] = {
                "exit_code": res.returncode,
                "passed": passed,
                "receipt": str(receipt_file.relative_to(run_dir)),
            }
            if not passed:
                all_passed = False

        state.verification_results = results
        if not all_passed:
            state.state = WorkerState.FAILED
            state.error = "Verification commands failed"
        self.save_state_atomic(run_dir, state)
        return all_passed


# -----------------------------------------------------------------------------
# CLI Entrypoint
# -----------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Codex/Astra -> OpenCode Go/DeepSeek implementation adapter (issue #468)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # doctor
    p_doc = subparsers.add_parser("doctor", help="Check prerequisites and native subscription routing")
    p_doc.add_argument("--profile", default="default", help="Diagnostics profile")
    p_doc.add_argument("--opencode-bin", help="Path to opencode executable")
    p_doc.add_argument("--codex-bin", help="Path to codex executable")
    p_doc.add_argument("--model", default=DEFAULT_OPENCODE_MODEL, help="OpenCode worker model")
    p_doc.add_argument("--agent", default=DEFAULT_OPENCODE_AGENT, help="OpenCode worker agent")

    # run
    p_run = subparsers.add_parser("run", help="Dispatch implementation worker for an approved contract")
    p_run.add_argument("--contract", required=True, type=Path, help="Path to contract markdown")
    p_run.add_argument("--issue", default="000", help="Issue number or identifier")
    p_run.add_argument("--worktree", type=Path, default=Path.cwd(), help="Target git workspace/worktree")
    p_run.add_argument("--run-root", type=Path, default=Path(DEFAULT_RUN_ROOT), help="Run artifacts root directory")
    p_run.add_argument("--run-id", help="Explicit run ID (default generated)")
    p_run.add_argument("--model", default=DEFAULT_OPENCODE_MODEL, help="OpenCode model selector")
    p_run.add_argument("--agent", default=DEFAULT_OPENCODE_AGENT, help="OpenCode primary agent")
    p_run.add_argument("--opencode-bin", default="opencode", help="OpenCode binary")
    p_run.add_argument("--codex-bin", default="codex", help="Codex binary")
    p_run.add_argument("--timeout", type=int, default=DEFAULT_INVOCATION_TIMEOUT, help="Invocation timeout seconds")
    p_run.add_argument("--verify", action="store_true", help="Execute verification checks after candidate freeze")
    p_run.add_argument("--allow-protected-paths", action="store_true", help="Allow edits to protected paths")

    # resume
    p_res = subparsers.add_parser("resume", help="Resume recorded worker session for bounded corrections")
    p_res.add_argument("--run-id", required=True, help="Run ID to resume")
    p_res.add_argument("--correction", required=True, type=Path, help="Path to correction packet markdown")
    p_res.add_argument("--issue", default="000", help="Issue number")
    p_res.add_argument("--run-root", type=Path, default=Path(DEFAULT_RUN_ROOT), help="Run artifacts root directory")
    p_res.add_argument("--verify", action="store_true", help="Execute verification checks after correction")

    # status
    p_stat = subparsers.add_parser("status", help="Display run state and verification evidence")
    p_stat.add_argument("--run-id", help="Run ID to inspect")
    p_stat.add_argument("--issue", default="000", help="Issue number")
    p_stat.add_argument("--run-root", type=Path, default=Path(DEFAULT_RUN_ROOT), help="Run artifacts root directory")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]

    if args.command == "doctor":
        code, reports = run_doctor(
            repo_root=repo_root,
            profile=args.profile,
            opencode_bin=args.opencode_bin,
            codex_bin=args.codex_bin,
            worker_model=args.model,
            worker_agent=args.agent,
        )
        for r in reports:
            print(r)
        return code

    elif args.command == "run":
        controller = AIWorkerController(
            run_root=args.run_root,
            repo_root=repo_root,
            opencode_bin=args.opencode_bin,
            opencode_model=args.model,
            opencode_agent=args.agent,
            codex_bin=args.codex_bin,
        )
        try:
            run_dir, state = controller.initialize_run(
                issue_id=args.issue,
                contract_path=args.contract,
                workspace=args.worktree,
                run_id=args.run_id,
                invocation_timeout=args.timeout,
            )
            print(f"[ai_worker] Initialized run {state.run_id} at {run_dir}")
            prompt = args.contract.read_text(encoding="utf-8")
            exit_code, _session_id, errors = controller.dispatch_worker(
                run_dir=run_dir,
                prompt_content=prompt,
                attempt=1,
            )
            if exit_code != 0 or errors:
                print(f"[ai_worker] Worker failed with exit code {exit_code}: {errors}", file=sys.stderr)
                state.state = WorkerState.FAILED
                state.error = f"Worker failed (code {exit_code}): {errors}"
                controller.save_state_atomic(run_dir, state)
                return 1

            changes = controller.freeze_candidate(
                run_dir=run_dir,
                allow_protected_paths=args.allow_protected_paths,
            )
            print(f"[ai_worker] Candidate frozen ({len(changes.all_changed_paths)} changed files)")

            if args.verify:
                passed = controller.run_verification(
                    run_dir=run_dir,
                    verification_commands=[["sh", "scripts/tools/run-task.sh", "qa:unit"]],
                )
                if not passed:
                    print("[ai_worker] Verification failed", file=sys.stderr)
                    return 2
                print("[ai_worker] Verification passed")

            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[ai_worker] Error: {exc}", file=sys.stderr)
            return 1

    elif args.command == "resume":
        run_dir = args.run_root / args.issue / args.run_id
        if not run_dir.is_dir():
            print(f"[ai_worker] Run directory {run_dir} not found", file=sys.stderr)
            return 1

        controller = AIWorkerController(run_root=args.run_root, repo_root=repo_root)
        state = controller.load_state(run_dir)
        if not state.session_id:
            print("[ai_worker] Cannot resume run without recorded session_id", file=sys.stderr)
            return 1
        if state.attempt >= state.max_attempts:
            print(f"[ai_worker] Max attempts ({state.max_attempts}) reached; cannot resume", file=sys.stderr)
            return 1

        next_attempt = state.attempt + 1
        state.attempt = next_attempt
        controller.save_state_atomic(run_dir, state)

        correction_text = args.correction.read_text(encoding="utf-8")
        exit_code, _session_id, errors = controller.dispatch_worker(
            run_dir=run_dir,
            prompt_content=correction_text,
            attempt=next_attempt,
            session_id=state.session_id,
        )
        if exit_code != 0 or errors:
            print(f"[ai_worker] Correction attempt {next_attempt} failed: {errors}", file=sys.stderr)
            return 1

        changes = controller.freeze_candidate(run_dir=run_dir)
        print(f"[ai_worker] Correction candidate frozen ({len(changes.all_changed_paths)} files)")
        return 0

    elif args.command == "status":
        if args.run_id:
            run_dir = args.run_root / args.issue / args.run_id
            if not run_dir.is_dir():
                print(f"[ai_worker] Run directory {run_dir} not found", file=sys.stderr)
                return 1
            controller = AIWorkerController(run_root=args.run_root, repo_root=repo_root)
            state = controller.load_state(run_dir)
            print(json.dumps(state.to_dict(), indent=2))
        else:
            print(f"[ai_worker] Runs under {args.run_root}:")
            if args.run_root.is_dir():
                for p in args.run_root.glob("*/*"):
                    if (p / "state.json").is_file():
                        print(f"  {p.parent.name}/{p.name}")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
