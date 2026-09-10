"""Serve the read-only FTP MCP bridge (sidecar entrypoint: python -m mainframe_rag.mcp).

Config is environment-only; in particular there is NO password flag —
MCP_FTP_PASSWORD must arrive via a mounted secret or the environment.
"""

from __future__ import annotations

import argparse
import os
import sys

from mainframe_rag.mcp.bridge import FTPConfig, connect
from mainframe_rag.mcp.server import create_app, serve_stdio


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
    args = parser.parse_args(argv)

    config = _config_from_env()
    if not config.host or not config.user or not config.password:
        print("mcp bridge: MCP_FTP_HOST, MCP_FTP_USER, and MCP_FTP_PASSWORD must all be set", file=sys.stderr)
        return 2
    if args.transport == "stdio":
        serve_stdio(config, connect)
        return 0
    import uvicorn

    uvicorn.run(create_app(config, connect), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
