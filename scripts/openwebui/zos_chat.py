"""One Docs-path turn for the zos-read phase (#1294, ADR-0012), run inside the lane's client pod.

    kubectl exec -i pod/zos-read-client -- python3 - ARGS_JSON < zos_chat.py

ARGS_JSON: {"question": ..., "tag": 64 hex (the docs Pipe's audit field), "markers": [...]}.
One streaming POST /v1/chat to the agent Service in the docs Pipe's request shape (messages and
stream: true, the x-zos-audit-tag header). Prints one ZOS_CHAT line: the status, whether the
stream completed, the agent-written first line only when it is a z/OS line (target, acquired_at,
outcome), counts, the finish chunk's zos_observation facts and which fixture markers the answer
carried. Never the answer text.
"""
import json
import sys
import urllib.error
import urllib.request

MARKER = '[z/OS observation]'
ZOS_LINE_PREFIXES = (MARKER, "z/OS read of ")

args = json.loads(sys.argv[1])
headers = {"content-type": "application/json", "accept": "text/event-stream"}
if args.get("tag"):
    headers["x-zos-audit-tag"] = args["tag"]
body = json.dumps({"messages": [{"role": "user", "content": args["question"]}], "stream": True}).encode()
request = urllib.request.Request(args.get("url", "http://rag-agent:8080") + "/v1/chat", data=body,
                                 headers=headers, method="POST")
out = {"status": None, "done": False}
parts: list[str] = []
final: dict = {}
try:
    with urllib.request.urlopen(request, timeout=240) as response:
        out["status"] = response.status
        for raw in response:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data == "[DONE]":
                out["done"] = True
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            choice = (chunk.get("choices") or [{}])[0]
            delta = (choice.get("delta") or {}).get("content")
            if delta:
                parts.append(delta)
            if choice.get("finish_reason"):
                final = choice
except urllib.error.HTTPError as exc:
    out["status"] = exc.code
except OSError as exc:
    out["error"] = type(exc).__name__
text = "".join(parts)
first = text.split("\n", 1)[0]
out.update(
    first_line=first[:400] if first.startswith(ZOS_LINE_PREFIXES) else "",
    marker_in_content=MARKER in text,
    fixture_seen=sorted(m for m in args.get("markers") or [] if m and m in text),
    content_chars=len(text),
    finish_reason=final.get("finish_reason"),
    citations=len(final.get("citations") or []),
    zos_observation=final.get("zos_observation"),
    feedback_suppressed=final.get("feedback_suppressed"),
)
print("ZOS_CHAT " + json.dumps(out, sort_keys=True))
