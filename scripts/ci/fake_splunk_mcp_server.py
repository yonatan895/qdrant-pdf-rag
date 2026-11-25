"""CI-only: serve the fake Splunk MCP Server (tests/fake_splunk_mcp.py) over HTTPS.

The Kind `openwebui` lane (scripts/openwebui/acceptance.sh team-tools, #1077) runs
this in a pod on the agent image, which has python3, httpx2 and uvicorn, with
the fake module and this file mounted from a ConfigMap, as the Splunk behind
the deployed Splunk gateway. Never shipped, never part of an operator bundle.

    python3 fake_splunk_mcp_server.py --port 8089 --cert CERT --key KEY [--module-dir DIR]

The MCP token the fake expects is read from the file named by
FAKE_SPLUNK_TOKEN_FILE (a Secret mount), never from argv. One JSON line per
request on stdout: HTTP method, JSON-RPC method and status; never the token or
the search text.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from typing import Any


def build_app(fake: Any, httpx2: Any) -> Any:
    """An ASGI app that hands every request to fake.handler, as MockTransport does."""

    async def app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]]
        query = scope.get("query_string", b"").decode("latin-1")
        url = "https://fake-splunk" + scope["path"] + (f"?{query}" if query else "")
        seen = len(fake.messages)
        response = await fake.handler(httpx2.Request(scope["method"], url, headers=headers, content=body))
        content = await response.aread()
        out = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in response.headers.items()
               if k.lower() not in ("content-length", "transfer-encoding")]
        out.append((b"content-length", str(len(content)).encode()))
        await send({"type": "http.response.start", "status": response.status_code, "headers": out})
        await send({"type": "http.response.body", "body": content})
        rpc = fake.messages[-1].get("method") if len(fake.messages) > seen else None
        print(json.dumps({"fake_splunk": scope["method"], "rpc": rpc, "status": response.status_code}), flush=True)

    return app


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--module-dir", default=os.path.dirname(os.path.abspath(__file__)))
    args = parser.parse_args(argv)
    sys.path.insert(0, args.module_dir)
    fake_module = importlib.import_module("fake_splunk_mcp")
    import httpx2
    import uvicorn

    fake = fake_module.FakeSplunkMcp()
    with open(os.environ["FAKE_SPLUNK_TOKEN_FILE"], encoding="ascii") as fh:
        fake.token = fh.read().strip()
    uvicorn.run(build_app(fake, httpx2), host="0.0.0.0", port=args.port, ssl_certfile=args.cert,
                ssl_keyfile=args.key, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
