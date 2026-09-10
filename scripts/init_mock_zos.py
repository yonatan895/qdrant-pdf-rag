"""Generate a deterministic mock z/OS fixture tree (never committed).

Runtime-only test data for the FTP MCP bridge: the same bytes on every
run (sorted iteration, fixed contents) so golden assertions can pin
substrings. Layout matches mainframe_rag.mcp.mock expectations.

Presets: minimal (1 failed job + 1 dataset, fast unit path) and default
(full set: failed + active jobs, PDS+JCL, sequential, USS file).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

JCL_MEMBER = """//PAYROLL JOB (ACCT),'NIGHTLY PAY',CLASS=A,MSGCLASS=X
//STEP1 EXEC PGM=PAYCALC,PARM='MODE=PROD'
//INFILE DD DSN=USER.PAYROLL.INPUT,DISP=SHR
//OUTFILE DD SYSOUT=*
//SYSIN DD *
RATES 2026-09-01
/*
"""

SPOOL_LOG = """IEF142I PAYROLL JOB00023 - STEP STEP1 - RC=8
IEF403I PAYROLL - STARTED - TIME=02.14.00
IGW015I PAYCALC ABEND S0C4 AT OFFSET 00001A2C
IEA995I SYMPTOM DUMP OUTPUT 512K BYTES
IEF404I PAYROLL - ENDED - TIME=02.14.07 - RC=8
"""

SPOOL_JCL = """//PAYROLL JOB (ACCT),'NIGHTLY PAY',CLASS=A,MSGCLASS=X
//STEP1 EXEC PGM=PAYCALC,PARM='MODE=PROD'
"""

PARMLIB = """LFAREA=(1G,64M)
IEASYSxx parmlib fragment for mock reads.
APF ADD DSNAME=SYS1.USER.LINKLST,VOLUME=VOL123
"""

USS_REPORT = """Nightly operations report (mock).
Jobs ended: PAYROLL RC=8, BACKUP RC=0.
"""

JOBS = [
    {
        "job_id": "JOB00023",
        "name": "PAYROLL",
        "owner": "READER",
        "status": "OUTPUT",
        "return_code": "0008",
        "spool": {"1": "JOB00023.1", "2": "JOB00023.2"},
    },
    {
        "job_id": "JOB00024",
        "name": "BACKUP",
        "owner": "READER",
        "status": "ACTIVE",
        "return_code": None,
        "spool": {},
    },
]


def build_mock_tree(root: Path, minimal: bool = False) -> dict:
    """Write the fixture tree deterministically. Returns counts for logs."""
    root.mkdir(parents=True, exist_ok=True)
    datasets = root / "datasets"
    datasets.mkdir(exist_ok=True)
    (datasets / "USER.PARMLIB").write_text(PARMLIB, encoding="utf-8")
    if not minimal:
        pds = datasets / "USER.JCL"
        pds.mkdir(exist_ok=True)
        (pds / "JOBCARD.jcl").write_text(JCL_MEMBER, encoding="utf-8")
        (pds / "_meta.json").write_text(
            json.dumps({"dsorg": "PO", "recfm": "FB", "lrecl": 80}, indent=1) + "\n",
            encoding="utf-8",
        )
    uss_dir = root / "uss" / "u" / "ops"
    uss_dir.mkdir(parents=True, exist_ok=True)
    if not minimal:
        (uss_dir / "report.txt").write_text(USS_REPORT, encoding="utf-8")
    jobs = JOBS if not minimal else [JOBS[0]]
    jobs_dir = root / "jobs" / "spool"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (root / "jobs" / "jobs.json").write_text(json.dumps(jobs, indent=1) + "\n", encoding="utf-8")
    (jobs_dir / "JOB00023.1").write_text(SPOOL_JCL, encoding="utf-8")
    (jobs_dir / "JOB00023.2").write_text(SPOOL_LOG, encoding="utf-8")
    return {"datasets": 1 if minimal else 2, "uss_files": 0 if minimal else 1, "jobs": len(jobs)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output directory (created)")
    parser.add_argument("--preset", choices=("minimal", "default"), default="default")
    args = parser.parse_args(argv)
    counts = build_mock_tree(Path(args.out), minimal=args.preset == "minimal")
    print(f"mock z/OS tree at {args.out}: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
