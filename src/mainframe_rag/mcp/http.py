"""HTTP transport launch shared by the MCP entry points (#588, #989 review).

Only the HTTP branch configures logging. The stdio transports own stdout for
JSON-RPC framing, so their stdout must stay free of log lines.
"""

from __future__ import annotations

from typing import Any

from mainframe_rag.logs import configure_logging

_HTTP_LOG_LEVEL = "warning"


def serve_http(app: Any, host: str, port: int) -> None:
    """Run one MCP ASGI app under uvicorn with the repository JSON log.

    uvicorn's default log config installs plain-text handlers on `uvicorn`,
    `uvicorn.error` and `uvicorn.access`, so a crash or lifespan failure
    printed the rendered traceback (issue #989, fixed for the agent and the
    feedback service through their app factories). These entry points call
    `uvicorn.run` directly, so configure the JSON handler first and pass
    `log_config=None`: uvicorn still applies the requested warning level and
    its access-log switch, but it never replaces the handler.
    """
    import uvicorn

    configure_logging(_HTTP_LOG_LEVEL)
    uvicorn.run(app, host=host, port=port, log_level=_HTTP_LOG_LEVEL, log_config=None)
