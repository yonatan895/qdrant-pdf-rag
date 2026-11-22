"""Serve the read-only FTP MCP bridge (entrypoint: python -m mainframe_rag.mcp).

Config is environment-only; in particular there is NO password flag —
MCP_FTP_PASSWORD must arrive via a mounted secret or the environment.
`--probe` runs a one-time logon-free connectivity probe (ADR-0012 D5 point 5)
without credentials to verify whether the target supports verified FTPS.
`--site-probe` runs the read-only D8 site probe (one logon; `site_probe.py`)
and prints a text-free JSON-lines receipt (also to `--receipt FILE`, mode
0600); it exits 1 when a check failed, 2 on a configuration or target error,
3 when it stopped on an unexpected error (the receipt is still whole).
`--mock DIR` (or `MCP_MOCK_DIR`) serves a deterministic fixture tree instead
of the wire: test-only, never in prod manifests (hygiene-gated).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Literal, cast

from mainframe_rag import tracing as tracing_mod
from mainframe_rag.mcp.bridge import (
    DEFAULT_ENABLED_TOOLS,
    FTPConfig,
    connect,
    parse_dataset_allowlist,
    parse_enabled_tools,
    parse_jes_prefixes,
    probe_transport,
)
from mainframe_rag.mcp.http import serve_http
from mainframe_rag.mcp.server import create_app, sample_ratio_from_env, serve_stdio
from mainframe_rag.mcp.site_probe import run_zos_probe, validate_targets
from mainframe_rag.probe_receipt import write_receipt


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _config_from_env() -> FTPConfig:
    def _float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be a number") from exc

    def _int(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc

    raw_mode = os.environ.get("MCP_FTP_TRANSPORT_MODE", "ftps").strip().lower()
    if raw_mode not in ("ftps", "plain"):
        raise ValueError("MCP_FTP_TRANSPORT_MODE must be 'ftps' or 'plain'")
    transport_mode = cast(Literal["ftps", "plain"], raw_mode)

    raw_tools = os.environ.get("MCP_ENABLED_TOOLS", "").strip()
    enabled_tools = (
        parse_enabled_tools(raw_tools) if raw_tools else DEFAULT_ENABLED_TOOLS
    )
    ca_file = os.environ.get("MCP_FTP_CA_FILE", "").strip() or None
    encoding = os.environ.get("MCP_FTP_ENCODING", "latin-1").strip() or "latin-1"

    return FTPConfig(
        host=os.environ.get("MCP_FTP_HOST", "").strip(),
        user=os.environ.get("MCP_FTP_USER", ""),
        password=os.environ.get("MCP_FTP_PASSWORD", ""),
        port=_int("MCP_FTP_PORT", 21),
        timeout_s=_float("MCP_FTP_TIMEOUT_S", 15.0),
        max_bytes=_int("MCP_FTP_MAX_BYTES", 262144),
        transport_mode=transport_mode,
        plain_ftp_accepted=_env_bool("MCP_FTP_PLAIN_ACCEPTED"),
        ca_file=ca_file,
        encoding=encoding,
        dataset_allowlist=parse_dataset_allowlist(os.environ.get("MCP_DATASET_ALLOWLIST", "")),
        enabled_tools=enabled_tools,
        jes_jobname_prefixes=parse_jes_prefixes(os.environ.get("MCP_JES_JOBNAME_PREFIXES", "")),
        jes_owner_prefixes=parse_jes_prefixes(os.environ.get("MCP_JES_OWNER_PREFIXES", "")),
        logon_breaker_cooldown_s=_float("MCP_FTP_LOGON_COOLDOWN_S", 900.0),
        spool_stream_max_bytes=_int("MCP_FTP_SPOOL_STREAM_MAX_BYTES", 16 * 1024 * 1024),
        spool_stream_max_s=_float("MCP_FTP_SPOOL_STREAM_MAX_S", 8.0),
    )


def _site_probe(args: argparse.Namespace) -> int:
    """The D8 site probe on the live wire (never mock mode); fixed messages."""
    if args.mock or args.probe or os.environ.get("MCP_MOCK_DIR", "").strip():
        print("mcp bridge site probe: runs on the live wire only, alone", file=sys.stderr)
        return 2
    try:
        config = _config_from_env()
        targets = validate_targets(config, dataset=args.dataset, job_id=args.job_id)
    except ValueError as exc:
        print(f"mcp bridge site probe: invalid configuration ({exc})", file=sys.stderr)
        return 2
    if not config.host or not config.user or not config.password:
        print(
            "mcp bridge site probe: MCP_FTP_HOST, MCP_FTP_USER, and MCP_FTP_PASSWORD must all be set",
            file=sys.stderr,
        )
        return 2
    if any(ch in value for value in (config.user, config.password) for ch in "\r\n"):
        # ftplib would send USER and then fail (exit 3); refuse before any
        # socket instead, and never echo the value (#1349 review F1).
        print(
            "mcp bridge site probe: MCP_FTP_USER and MCP_FTP_PASSWORD must not contain CR or LF",
            file=sys.stderr,
        )
        return 2
    receipt = run_zos_probe(config, targets)
    try:
        write_receipt(receipt, sys.stdout, args.receipt)
    except OSError:
        print("mcp bridge site probe: the receipt file could not be written", file=sys.stderr)
        return 2
    return receipt.exit_code()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument(
        "--mock", default=None, help="fixture dir: serve mock z/OS instead of FTP (test-only)"
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="run a one-time logon-free FTPS/FTP connectivity probe and exit",
    )
    parser.add_argument(
        "--site-probe",
        action="store_true",
        help="run the read-only ADR-0012 D8 site probe (one logon) and print its receipt",
    )
    parser.add_argument(
        "--dataset", default=None, help="site probe: an exact MCP_DATASET_ALLOWLIST entry to read"
    )
    parser.add_argument(
        "--job-id", default=None, help="site probe: an allowlisted job whose JESMSGLG to read"
    )
    parser.add_argument(
        "--receipt", type=Path, default=None, help="site probe: also write the receipt here (0600)"
    )
    args = parser.parse_args(argv)

    if args.site_probe:
        return _site_probe(args)
    if args.dataset or args.job_id or args.receipt:
        print("mcp bridge: --dataset, --job-id and --receipt need --site-probe", file=sys.stderr)
        return 2

    if args.probe:
        host = os.environ.get("MCP_FTP_HOST", "").strip()
        if not host:
            print("mcp bridge probe: MCP_FTP_HOST must be set", file=sys.stderr)
            return 2
        try:
            port = int(os.environ.get("MCP_FTP_PORT", "21") or "21")
            timeout_s = float(os.environ.get("MCP_FTP_TIMEOUT_S", "15.0") or "15.0")
        except ValueError:
            print("mcp bridge probe: MCP_FTP_PORT and MCP_FTP_TIMEOUT_S must be numeric", file=sys.stderr)
            return 2
        ca_file = os.environ.get("MCP_FTP_CA_FILE", "").strip() or None
        result = probe_transport(host, port, timeout_s=timeout_s, ca_file=ca_file)
        print(
            json.dumps(
                {
                    "host": result.host,
                    "port": result.port,
                    "outcome": result.outcome,
                    "recommended_mode": result.recommended_mode,
                }
            )
        )
        return 0 if result.outcome in ("ftps_verified", "plain_only") else 1

    mock_dir = args.mock or os.environ.get("MCP_MOCK_DIR", "").strip()
    try:
        config = _config_from_env()
    except ValueError as exc:
        print(f"mcp bridge: invalid configuration ({exc})", file=sys.stderr)
        return 2

    raw_auth_token = os.environ.get("MCP_BRIDGE_AUTH_TOKEN", "").strip()
    auth_token: str | None = raw_auth_token or None

    if mock_dir:
        # Mock mode serves fixtures, never the wire: credentials are
        # unused, so the credential gate is skipped (not weakened — the
        # live path below still refuses without them).
        from mainframe_rag.mcp.mock import mock_session_factory

        print(f"mcp bridge: MOCK MODE rooted at {mock_dir} (never production)", file=sys.stderr)
        connect_fn = mock_session_factory(mock_dir)
    else:
        if not config.host or not config.user or not config.password:
            print(
                "mcp bridge: MCP_FTP_HOST, MCP_FTP_USER, and MCP_FTP_PASSWORD must all be set",
                file=sys.stderr,
            )
            return 2
        if args.transport == "http" and auth_token is None:
            print(
                "mcp bridge: MCP_BRIDGE_AUTH_TOKEN must be set for HTTP transport",
                file=sys.stderr,
            )
            return 2
        connect_fn = connect

    if args.transport == "stdio":
        tracing_mod.setup_tracing(
            os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
            sample_ratio=sample_ratio_from_env(),
        )
        try:
            serve_stdio(config, connect_fn)
        finally:
            tracing_mod.shutdown_tracing()
        return 0
    serve_http(create_app(config, connect_fn, auth_token=auth_token), args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
