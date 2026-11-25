"""Run inside the Open WebUI container by acceptance.py team-tools (#1077); stdlib and the image's own code.

Open WebUI's own MCP client (open_webui.utils.mcp.client.MCPClient, the client a chat's native tool call
uses) connects to the tool server the chart registered, with the url and the bearer key from this
container's TOOL_SERVER_CONNECTIONS (the kubelet expanded $(MFR_SPLUNK_GATEWAY_KEY) from the Secret), lists
the tools and calls one template. The rows travel Open WebUI -> gateway -> fake Splunk through both
NetworkPolicies. Prints one line `TEAM_TOOL_CALL {json}`: tool names, the row count and flags; never the
key, the search or the rows.

    python3 - TOOL < team_tool_call.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/app/backend")
TOOL = sys.argv[1] if len(sys.argv) > 1 else "errors_by_host"
FRAME = f'<tool-output gateway="splunk" tool="{TOOL}">'


def rows_in(text):
    for line in text.splitlines():
        if line.startswith('{"rows"'):
            return len(json.loads(line)["rows"])
    return None


def describe(exc, depth=0):
    """The exception's class and where it was raised, then its causes and group members (never str(exc))."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    where = f"{os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}" if tb is not None else "?"
    parts = [f"{type(exc).__name__}@{where}"]
    if depth < 4:
        for inner in list(getattr(exc, "exceptions", ()))[:3] + [exc.__cause__ or exc.__context__]:
            if inner is not None:
                parts.append("<" + describe(inner, depth + 1) + ">")
    return " ".join(parts)


async def main():
    out = {}
    try:
        from open_webui.utils.mcp.client import MCPClient

        (conn,) = [c for c in json.loads(os.environ["TOOL_SERVER_CONNECTIONS"]) if c.get("type") == "mcp"]
        key = conn.get("key") or ""
        out.update(auth_type=conn.get("auth_type"), key_expanded=bool(key) and not key.startswith("$("))
        client = MCPClient(server_name="splunk")
        await client.connect(conn["url"], headers={"Authorization": f"Bearer {key}"})
        try:
            out["tools"] = sorted(spec["name"] for spec in await client.list_tool_specs())
            result = await client.call_tool(TOOL, {"host": "web-01"})
        finally:
            await client.disconnect()
        items = result if isinstance(result, list) else getattr(result, "context", [])
        text = "\n".join(str(item.get("text", "")) for item in items if isinstance(item, dict))
        out.update(rows=rows_in(text), framed=FRAME in text, redacted="[REDACTED]" in text,
                   raw_secret_seen="hunter2" in text)
    except Exception as exc:  # noqa: BLE001 - classes and places only, never the text
        out["error"] = describe(exc)
    print("TEAM_TOOL_CALL " + json.dumps(out, sort_keys=True), flush=True)


asyncio.run(main())
