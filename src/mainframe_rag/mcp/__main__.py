"""Serve the read-only FTP MCP bridge (sidecar entrypoint: python -m mainframe_rag.mcp).

Config is environment-only; in particular there is NO password flag —
MCP_FTP_PASSWORD must arrive via a mounted secret or the environment.
--mock DIR (or MCP_MOCK_DIR) serves a deterministic fixture tree instead
of the wire: test-only, never in prod manifests (hygiene-gated).
"""

from __future__ import annotations

import argparse
import os
import sys

from mainframe_rag.agent import tracing as tracing_mod
from mainframe_rag.mcp.bridge import FTPConfig, connect
from mainframe_rag.mcp.server import create_app, sample_ratio_from_env, serve_stdio


def _config_from_env() -> FTPConfig:
    def _float(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except ValueError:
            return default

    def _int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, default))
        except ValueError:
            return default

    return FTPConfig(
        host=os.environ.get("MCP_FTP_HOST", ""),
        user=os.environ.get("MCP_FTP_USER", ""),
        password=os.environ.get("MCP_FTP_PASSWORD", ""),
        port=_int("MCP_FTP_PORT", 21),
        timeout_s=_float("MCP_FTP_TIMEOUT_S", 15.0),
        max_bytes=_int("MCP_FTP_MAX_BYTES", 262144),
        use_tls=os.environ.get("MCP_FTP_TLS", "").strip().lower() in ("1", "true", "yes"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--mock", default=None, help="fixture dir: serve mock z/OS instead of FTP (test-only)")
    args = parser.parse_args(argv)

    mock_dir = args.mock or os.environ.get("MCP_MOCK_DIR", "").strip()
    config = _config_from_env()
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
    import uvicorn

    uvicorn.run(create_app(config, connect_fn), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
