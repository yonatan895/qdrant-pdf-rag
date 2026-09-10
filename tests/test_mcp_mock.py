"""Mock z/OS backend tests (ADR-0002 mock phase). Hermetic except the sim
tier file below: fixtures generate at runtime (never committed), the mock
session replaces the wire, and a hygiene test keeps mock mode out of prod
manifests (the EMBED_MODE=hash scoping precedent)."""

from __future__ import annotations

from pathlib import Path

from mainframe_rag.mcp import bridge
from mainframe_rag.mcp.bridge import FTPConfig
from mainframe_rag.mcp.mock import MockFTPSession, mock_session_factory

REPO = Path(__file__).resolve().parents[1]


def _mock_root(tmp_path: Path, preset: str = "default") -> Path:
    from scripts.init_mock_zos import build_mock_tree

    root = tmp_path / "mock-zos"
    build_mock_tree(root, minimal=(preset == "minimal"))
    return root


def _session(root: Path) -> bridge.FTPSession:
    config = FTPConfig(host="mock", user="mock", password="mock")
    return bridge.FTPSession(MockFTPSession(root), config)


def test_generator_is_deterministic(tmp_path: Path) -> None:
    """Same bytes on every run: golden assertions pin substrings, so the
    generator must not drift between runs."""
    from scripts.init_mock_zos import build_mock_tree

    first = tmp_path / "a"
    second = tmp_path / "b"
    build_mock_tree(first)
    build_mock_tree(second)
    first_files = sorted(p.relative_to(first).as_posix() for p in first.rglob("*") if p.is_file())
    second_files = sorted(p.relative_to(second).as_posix() for p in second.rglob("*") if p.is_file())
    assert first_files == second_files
    for rel in first_files:
        assert (first / rel).read_bytes() == (second / rel).read_bytes()


def test_mock_dataset_and_member_reads(tmp_path: Path) -> None:
    root = _mock_root(tmp_path)
    out = bridge.dataset_read(_session(root), "USER.PARMLIB")
    assert out["isError"] is False
    assert "LFAREA=" in out["content"][0]["text"]
    out = bridge.dataset_read(_session(root), "USER.JCL", member="JOBCARD")
    assert "EXEC PGM=PAYCALC" in out["content"][0]["text"]
    out = bridge.dataset_read(_session(root), "USER.JCL")
    assert "JOBCARD" in out["content"][0]["text"]
    missing = bridge.dataset_read(_session(root), "NO.SUCH.DSN")
    assert missing["isError"] is True
    assert missing["content"][0]["text"].startswith("not_found")


def test_mock_uss_and_job_tools(tmp_path: Path) -> None:
    root = _mock_root(tmp_path)
    out = bridge.uss_read(_session(root), "/u/ops/report.txt")
    assert "PAYROLL RC=8" in out["content"][0]["text"]
    status = bridge.job_status(_session(root))
    text = status["content"][0]["text"]
    assert "PAYROLL JOB00023 OUTPUT RC=0008" in text
    assert "BACKUP JOB00024 ACTIVE" in text
    filtered = bridge.job_status(_session(root), job_id="JOB00024")
    assert "BACKUP" in filtered["content"][0]["text"]
    assert "PAYROLL" not in filtered["content"][0]["text"]
    spool = bridge.jes_spool_read(_session(root), "JOB00023", "2")
    assert "ABEND S0C4" in spool["content"][0]["text"]
    assert spool["content"][0]["text"].startswith("IEF142I")
    absent = bridge.jes_spool_read(_session(root), "JOB00023", "9")
    assert absent["isError"] is True


def test_mock_minimal_preset(tmp_path: Path) -> None:
    root = _mock_root(tmp_path, preset="minimal")
    out = bridge.job_status(_session(root))
    assert "PAYROLL" in out["content"][0]["text"]
    assert "BACKUP" not in out["content"][0]["text"]


def test_mock_factory_rejects_missing_root(tmp_path: Path) -> None:
    import ftplib

    factory = mock_session_factory(tmp_path / "absent")
    try:
        factory(FTPConfig(host="m", user="m", password="m"))
    except ftplib.error_perm as exc:
        assert "550" in str(exc)
    else:
        raise AssertionError("missing mock root must fail closed")


def test_mock_mode_never_reaches_prod_manifests() -> None:
    """Hygiene gate (EMBED_MODE=hash precedent): MCP_MOCK_DIR is test-only.
    A prod manifest carrying it would serve fixture data as live state."""
    hits = []
    for base in ("overlays", "deploy", "charts"):
        root = REPO / base
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".yaml", ".yml", ".sh", ".env"}:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "MCP_MOCK_DIR" in text:
                    hits.append(str(path.relative_to(REPO)))
    assert hits == []
