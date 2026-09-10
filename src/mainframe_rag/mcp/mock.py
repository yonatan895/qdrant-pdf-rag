"""Filesystem-backed mock z/OS backend for the FTP bridge (test-only).

Mirrors the upstream mock-data idea: a fixture directory stands in for a
mainframe so the full bridge stack (framing, tools, caps, error mapping)
executes with zero network. `MockFTPSession` duck-types the ftplib surface
`bridge.py` uses — the bridge cannot tell mock from wire.

Layout (built by scripts/init_mock_zos.py, never committed):
    root/
      datasets/USER.PARMLIB          # sequential dataset → file
      datasets/USER.JCL/             # PDS → directory, members are files
        JOBCARD.jcl
      uss/u/ops/report.txt
      jobs/jobs.json                 # [{job_id, name, owner, status,
                                     #   return_code, spool: {file_id: name}}]
      jobs/spool/JOB00023.2

Static jobs only (deterministic content for golden assertions). SITE
FILETYPE=JES toggles JES mode; JESOWNER/JESJOBNAME/JESSTATUS filter the
listing. Unknown paths raise 550 exactly like the wire. Read-only by
construction: no write method exists on this class.
"""

from __future__ import annotations

import ftplib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mainframe_rag.mcp.bridge import FTPConfig

_CHUNK = 8192


class MockFTPSession:
    """Fake FTP server rooted at a fixture directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self.commands: list[str] = []
        self._jes_mode = False
        self._filters: dict[str, str] = {}
        self.aborted = False

    # ---------------------------------------------------------- wire verbs
    def sendcmd(self, cmd: str) -> str:
        self.commands.append(cmd)
        verb, _, rest = cmd.partition(" ")
        verb = verb.upper()
        if verb == "TYPE":
            return "200 Type set"
        if verb == "SITE":
            return self._site(rest.strip())
        raise ftplib.error_perm("500 Unknown command")

    def _site(self, args: str) -> str:
        # SITE args arrive as KEY=value (no space): JESOWNER=*, FILETYPE=JES.
        key, eq, value = args.partition("=")
        if not eq:
            key, _, value = args.partition(" ")
        key = key.strip().upper()
        value = value.strip()
        if key == "FILETYPE" and value.upper() == "JES":
            self._jes_mode = True
            return "200 JES mode"
        if key in ("JESOWNER", "JESJOBNAME", "JESSTATUS"):
            self._filters[key] = value.strip()
            return "200 filter set"
        if key.startswith("JES"):
            raise ftplib.error_perm("550 JES interface not available")
        return "200 ok"

    def retrbinary(self, cmd: str, callback) -> str:
        self.commands.append(cmd)
        _, _, arg = cmd.partition(" ")
        data = self._read_bytes(arg.strip())
        for i in range(0, len(data), _CHUNK):
            callback(data[i : i + _CHUNK])
        return "226 done"

    def retrlines(self, cmd: str, callback) -> str:
        self.commands.append(cmd)
        if not self._jes_mode:
            raise ftplib.error_perm("550 Not in JES mode")
        for line in self._job_lines():
            callback(line)
        return "226 done"

    def nlst(self, arg: str = "") -> list[str]:
        self.commands.append(f"NLST {arg}")
        target = self._datasets() / arg.strip().strip("'")
        if target.is_dir():
            return sorted(
                p.name for p in target.iterdir() if p.is_file() and p.name != "_meta.json"
            )
        raise ftplib.error_perm("550 Not a PDS")

    def quit(self) -> str:
        self.commands.append("QUIT")
        return "221 bye"

    def abort(self) -> str:
        self.aborted = True
        return "226 aborted"

    # ---------------------------------------------------------- fixture IO
    def _datasets(self) -> Path:
        return self._root / "datasets"

    def _read_bytes(self, arg: str) -> bytes:
        if self._jes_mode and "." in arg and "/" not in arg:
            job_id, _, spool_id = arg.upper().partition(".")
            return self._spool_bytes(job_id, spool_id)
        if arg.startswith("/"):
            target = self._root / "uss" / arg.lstrip("/")
            if target.is_file():
                return target.read_bytes()
            raise ftplib.error_perm("550 File not found")
        target = self._datasets() / arg.strip().strip("'")
        if "(" in arg and arg.rstrip().endswith(")"):
            head, _, tail = arg.strip().partition("(")
            name = head.strip().strip("'").upper()
            member = tail.removesuffix(")")
            member = member.strip().upper()
            candidate = self._datasets() / name / (member + ".jcl")
            plain = self._datasets() / name / member
            for path in (candidate, plain):
                if path.is_file():
                    return path.read_bytes()
            raise ftplib.error_perm("550 Member not found")
        if target.is_file():
            return target.read_bytes()
        raise ftplib.error_perm("550 File not found")

    def _jobs(self) -> list[dict[str, Any]]:
        path = self._root / "jobs" / "jobs.json"
        if not path.is_file():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def _job_lines(self) -> list[str]:
        lines = []
        for job in self._jobs():
            owner = self._filters.get("JESOWNER", "*")
            name = self._filters.get("JESJOBNAME", "*")
            status = self._filters.get("JESSTATUS", "ALL").upper()
            if owner not in ("*", job.get("owner", "")):
                continue
            if name != "*" and name.rstrip("*") not in job.get("name", ""):
                continue
            if status != "ALL" and status != str(job.get("status", "")).upper():
                continue
            line = f"{job['name']}  {job['job_id']} {job['owner']}   {job['status']}"
            if job.get("return_code"):
                line += f" RC={job['return_code']}"
            lines.append(line)
        return lines

    def _spool_bytes(self, job_id: str, spool_id: str) -> bytes:
        for job in self._jobs():
            if job.get("job_id", "").upper() == job_id:
                filename = (job.get("spool") or {}).get(spool_id)
                if filename:
                    target = self._root / "jobs" / "spool" / filename
                    if target.is_file():
                        return target.read_bytes()
                raise ftplib.error_perm("550 Spool file not found")
        raise ftplib.error_perm("550 Job not found")


def mock_session_factory(root: str | Path) -> Callable[[FTPConfig], MockFTPSession]:
    """Connect factory for __main__ --mock: ignores credentials (there is
    no wire to authenticate) and roots every session at the fixture dir."""
    root_path = Path(root)

    def _open(_config: FTPConfig) -> MockFTPSession:
        if not root_path.is_dir():
            raise ftplib.error_perm(f"550 mock root not found: {root_path}")
        return MockFTPSession(root_path)

    return _open
