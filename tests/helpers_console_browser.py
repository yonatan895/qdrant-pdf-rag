"""Test-only real-browser harness for the operator console (issue #372).

Drives an already-installed Chrome for Testing through its matching
chromedriver over the W3C WebDriver HTTP protocol, using only `httpx`
(already a dependency). Nothing is downloaded or installed: the runtime is
located through explicit environment variables or the pre-existing
selenium-manager cache, and the suite skips (never passes) when none is
present. Nothing here ships in the production image (ADR-0004: no Node, no
browser service, no frontend framework in production).

The page under test is the shipped `console.js` served by the real FastAPI
app on a loopback uvicorn thread; only the LLM and the retrieval function
are scripted doubles (gateway-shaped streams).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx

ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"
_CACHE = Path(os.environ.get("SE_CACHE_PATH", Path.home() / ".cache" / "selenium"))


def _major(path: Path) -> int:
    match = re.search(r"(\d+)\.", path.name)
    return int(match.group(1)) if match else -1


def find_runtime() -> tuple[str, str] | None:
    """Return (chrome, chromedriver) or None.

    CONSOLE_BROWSER_CHROME / CONSOLE_BROWSER_CHROMEDRIVER win. Otherwise the
    newest chrome in the selenium-manager cache that has a chromedriver of
    the same major version. Mismatched pairs are never used."""
    chrome = os.environ.get("CONSOLE_BROWSER_CHROME")
    driver = os.environ.get("CONSOLE_BROWSER_CHROMEDRIVER")
    if chrome or driver:
        if chrome and driver and Path(chrome).is_file() and Path(driver).is_file():
            return chrome, driver
        return None
    chrome_root = _CACHE / "chrome" / "linux64"
    driver_root = _CACHE / "chromedriver" / "linux64"
    if not chrome_root.is_dir() or not driver_root.is_dir():
        return None
    for candidate in sorted(chrome_root.iterdir(), key=_major, reverse=True):
        binary = candidate / "chrome"
        match = driver_root / candidate.name / "chromedriver"
        if binary.is_file() and match.is_file():
            return str(binary), str(match)
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Scripted gateway-shaped LLM + live server
# ---------------------------------------------------------------------------

CITE = "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"
DEFAULT_SCRIPT: list[tuple[Any, ...]] = [
    ("token", "Reissue the "),
    ("token", "command after initialization.\n\nCitations:\n- " + CITE + "\n"),
    ("done",),
]


class ScriptedLLM:
    """Gateway-shaped streaming double with test-controlled timing.

    Each request consumes one script of steps: ("token", text),
    ("gate", name) blocks until the test calls `release(name)`,
    ("fail",) raises mid-stream, ("done",) ends with the finish frame.
    A script that simply ends yields EOF without a finish frame."""

    def __init__(self) -> None:
        self.scripts: deque[list[tuple[Any, ...]]] = deque()
        self.requests: list[list[dict]] = []
        self._gates: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def gate(self, name: str) -> threading.Event:
        with self._lock:
            return self._gates.setdefault(name, threading.Event())

    def release(self, name: str) -> None:
        self.gate(name).set()

    def release_all(self) -> None:
        with self._lock:
            for event in self._gates.values():
                event.set()

    def reset(self) -> None:
        self.release_all()
        with self._lock:
            self._gates = {}
        self.scripts.clear()
        self.requests.clear()

    def queue(self, *steps: tuple[Any, ...]) -> None:
        self.scripts.append(list(steps))

    async def chat_stream(self, messages, reasoning_effort=None, temperature=None):
        from mainframe_rag.ports import TokenUsage

        self.requests.append([dict(m) for m in messages])
        script = self.scripts.popleft() if self.scripts else DEFAULT_SCRIPT
        first = True
        for step in script:
            kind = step[0]
            if kind == "token":
                event: dict[str, Any] = {"type": "token", "delta": step[1]}
                if first:
                    event["ttft_ms"] = 5
                    first = False
                yield event
            elif kind == "gate":
                await asyncio.to_thread(self.gate(step[1]).wait, 30)
            elif kind == "fail":
                raise RuntimeError("scripted upstream failure: internal detail")
            elif kind == "done":
                yield {
                    "type": "done",
                    "finish_reason": step[1] if len(step) > 1 else "stop",
                    "usage": TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                    "ttft_ms": 5,
                }


class LiveConsole:
    """The real agent app (console enabled) on a loopback uvicorn thread."""

    def __init__(self) -> None:
        import uvicorn

        from mainframe_rag.agent import app as app_mod
        from mainframe_rag.agent.tokenizer import FallbackTokenizer
        from tests.fakes import ServingGateFake
        from tests.test_webui import MockSearch

        self._patch = __import__("pytest").MonkeyPatch()
        env = {
            "QDRANT_URL": "http://localhost:6333",
            "EMBED_MODE": "hash",
            "ALLOW_HASH_MODE": "true",
            "LLM_BASE_URL": "http://llm.internal/v1",
            "LLM_MODEL_REASONING": "test-reasoning-model",
            "UI_ENABLED": "true",
        }
        for key, value in env.items():
            self._patch.setenv(key, value)
        self._patch.setattr("qdrant_client.AsyncQdrantClient", _HermeticQdrant)
        self._patch.setattr(app_mod, "serving_gate", ServingGateFake())
        self._patch.setattr(app_mod, "retrieve_search", MockSearch().search)
        self.llm = ScriptedLLM()
        self.port = _free_port()
        config = uvicorn.Config(
            app_mod.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"
        )
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self.server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("console test server did not start")
            time.sleep(0.05)
        # lifespan replaced app_mod.llm/tokenizer: install doubles afterwards
        self._patch.setattr(app_mod, "llm", self.llm)
        self._patch.setattr(app_mod, "tokenizer", FallbackTokenizer())

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/ui"

    def close(self) -> None:
        self.llm.release_all()
        self.server.should_exit = True
        self._thread.join(timeout=15)
        self._patch.undo()


class _HermeticQdrant:
    def __init__(self, *a, **k):
        pass

    async def retrieve(self, *a, **k):
        return []

    async def scroll(self, *a, **k):
        return ([], None)

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Minimal W3C WebDriver client
# ---------------------------------------------------------------------------


class Browser:
    def __init__(self, chrome: str, chromedriver: str) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="console-browser-"))
        port = _free_port()
        self._proc = subprocess.Popen(
            [chromedriver, f"--port={port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._http = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60)
        deadline = time.monotonic() + 20
        while True:
            try:
                self._http.get("/status")
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        args = [
            "--headless=new",
            "--no-sandbox",  # unprivileged test container/WSL host; test-only
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--disable-extensions",
            "--disable-background-networking",
            "--window-size=1280,900",
            f"--user-data-dir={self._tmp / 'profile'}",
        ]
        reply = self._http.post(
            "/session",
            json={
                "capabilities": {
                    "alwaysMatch": {
                        "browserName": "chrome",
                        "goog:chromeOptions": {"binary": chrome, "args": args},
                    }
                }
            },
        )
        self._check(reply)
        self.sid = reply.json()["value"]["sessionId"]

    # -- protocol plumbing -------------------------------------------------
    @staticmethod
    def _check(reply: httpx.Response) -> Any:
        body = reply.json()
        value = body.get("value")
        if reply.status_code >= 400:
            raise RuntimeError(f"webdriver error: {value}")
        return value

    def _cmd(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = f"/session/{self.sid}{path}"
        reply = (
            self._http.get(url) if method == "GET" else self._http.request(method, url, json=payload or {})
        )
        return self._check(reply)

    def cdp(self, cmd: str, **params: Any) -> Any:
        return self._cmd("POST", "/goog/cdp/execute", {"cmd": cmd, "params": params})

    # -- navigation / scripting --------------------------------------------
    def get(self, url: str) -> None:
        self._cmd("POST", "/url", {"url": url})

    def reload(self) -> None:
        self._cmd("POST", "/refresh")

    def eval(self, script: str, *args: Any) -> Any:
        return self._cmd("POST", "/execute/sync", {"script": script, "args": list(args)})

    def eval_async(self, script: str, *args: Any) -> Any:
        """Async script: the last argument is the completion callback."""
        return self._cmd("POST", "/execute/async", {"script": script, "args": list(args)})

    def wait(self, condition_js: str, *args: Any, timeout: float = 15.0, what: str = "") -> Any:
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            last = self.eval("return (function(){" + condition_js + "}).apply(null, arguments);", *args)
            if last:
                return last
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for: {what or condition_js} (last={last!r})")

    def init_script(self, source: str) -> None:
        """Run `source` in every new document before page scripts (CDP)."""
        self.cdp("Page.addScriptToEvaluateOnNewDocument", source=source)

    # -- elements ------------------------------------------------------------
    def find(self, css: str) -> str:
        value = self._cmd("POST", "/element", {"using": "css selector", "value": css})
        return value[ELEMENT_KEY]

    def click(self, css: str) -> None:
        self._cmd("POST", f"/element/{self.find(css)}/click")

    def type(self, css: str, text: str) -> None:
        self._cmd("POST", f"/element/{self.find(css)}/value", {"text": text})

    def keys(self, *chords: str) -> None:
        """Send raw key presses to the focused element. Each chord is a
        '+'-joined list: 'TAB', 'SHIFT+TAB', 'CONTROL+ENTER', 'ESCAPE'."""
        names = {
            "TAB": "",
            "ENTER": "",
            "ESCAPE": "",
            "SHIFT": "",
            "CONTROL": "",
            "SPACE": "",
            "ARROW_RIGHT": "",
            "ARROW_LEFT": "",
        }
        actions: list[dict] = []
        for chord in chords:
            parts = [names.get(p, p) for p in chord.split("+")]
            for part in parts:
                actions.append({"type": "keyDown", "value": part})
            for part in reversed(parts):
                actions.append({"type": "keyUp", "value": part})
        self._cmd(
            "POST",
            "/actions",
            {"actions": [{"type": "key", "id": "kbd", "actions": actions}]},
        )
        self._cmd("DELETE", "/actions")

    # -- windows -------------------------------------------------------------
    def new_tab(self) -> str:
        return self._cmd("POST", "/window/new", {"type": "tab"})["handle"]

    def switch(self, handle: str) -> None:
        self._cmd("POST", "/window", {"handle": handle})

    def handle(self) -> str:
        return self._cmd("GET", "/window")

    def resize(self, width: int, height: int) -> None:
        self._cmd("POST", "/window/rect", {"width": width, "height": height})

    def close_tab(self) -> None:
        self._cmd("DELETE", "/window")

    def close(self) -> None:
        try:
            self._http.request("DELETE", f"/session/{self.sid}")
        except httpx.HTTPError:
            pass
        self._http.close()
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        shutil.rmtree(self._tmp, ignore_errors=True)
