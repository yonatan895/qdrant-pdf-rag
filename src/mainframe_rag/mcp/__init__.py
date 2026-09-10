"""Read-only Zowe MCP bridge over FTP (ADR-0002, phase 1).

MCP interface, FTP transport: stdio + Streamable HTTP framing around the
four allowlisted read tools in `bridge.py`. The dispatch table IS the
capability lock — exactly four tools register, and `tools/call` rejects
anything else before touching the network.
"""

from __future__ import annotations
