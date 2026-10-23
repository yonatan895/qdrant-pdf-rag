#!/usr/bin/env python3
"""Scripted ~30 s console recording on the Synthetic Corp corpus (issue #890).

Dev tooling only: never a product path, never CI, never the air gap. It drives
the shipped operator console (`/ui`) in the pinned Chrome for Testing over the
DevTools protocol (a pipe, no websocket library), captures the DevTools
screencast frames, and encodes them with `ffmpeg` into the project-site media.
Standard library only; `ffmpeg` and `ffprobe` must be on PATH for `encode`.

Prerequisites (see docs/live-stack.md section 5 and docs/live-stack.md section 9):
  * the local stack is up (`local:stack` with CORPUS_DIR pointing at the
    generated Synthetic Corp PDFs) on a scratch collection, real embeddings;
  * CONSOLE_BROWSER_CHROME names the pinned Chrome for Testing binary.

  python3 scripts/make_synthetic_pdf.py --synthetic-corp --out "$CORPUS_DIR"
  python3 scripts/record_demo.py run --base-url http://127.0.0.1:8080 \\
      --frames "$SCRATCH_DIR/demo-frames" --media-dir site/media

The scenes are three real requests to the running agent: a keyword lookup with
its retrieved excerpts opened, a streamed cited answer, and a decline on an
out-of-corpus question. Nothing is mocked and no text is injected into the
page. Waiting time is compressed at encode time (idle gaps are capped and the
whole recording is sped up to fit the target length); the script prints the
factor so the PR can state it.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

VIEWPORT = (1280, 720)
TARGET_SECONDS = 32.0
GAP_CAP_SECONDS = 1.0
SCENE_HOLD_SECONDS = 2.5
FPS = 15
MEDIA_LIMIT_BYTES = 3 * 1024 * 1024
ANSWER_TIMEOUT_S = 240.0

# Scenes: (name, question, open_excerpts). The decline question is outside the
# invented manuals on purpose.
SCENES = [
    ("search", "queue lease timeout", True),
    (
        "answer",
        "Message MRD2041E appeared. What should the operator do, and which "
        "parameter controls how long the dispatcher waits?",
        False,
    ),
    ("decline", "How do I configure autoscaling for a Kubernetes cluster?", False),
]

# The recording must never show these. A hit aborts the run before encoding;
# the frame-by-frame review in the PR is still the authority.
FORBIDDEN_TEXT = re.compile(
    r"(/home/|/Users/|/mnt/|\.config|kubeconfig|Bearer |sk-[A-Za-z0-9]|api[_-]?key|password|secret)",
    re.IGNORECASE,
)


class Cdp:
    """Minimal DevTools protocol client over --remote-debugging-pipe."""

    def __init__(self, chrome: str, profile: Path, frames: Path) -> None:
        to_chrome_r, to_chrome_w = os.pipe()
        from_chrome_r, from_chrome_w = os.pipe()
        # Chrome reads commands on fd 3 and writes replies on fd 4. Park the
        # child's ends on high descriptors first so dup2 can never clobber one.
        child_in = fcntl.fcntl(to_chrome_r, fcntl.F_DUPFD, 5)
        child_out = fcntl.fcntl(from_chrome_w, fcntl.F_DUPFD, 5)
        args = [
            "--headless=new",
            "--remote-debugging-pipe",
            f"--user-data-dir={profile}",
            f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}",
            "--hide-scrollbars",
            "--mute-audio",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--force-color-profile=srgb",
            "--font-render-hinting=none",
        ]
        if os.environ.get("DEMO_CHROME_NO_SANDBOX") == "1":
            args.append("--no-sandbox")
        self.proc = subprocess.Popen(
            ["sh", "-c", f'exec "$0" "$@" 3<&{child_in} 4>&{child_out}', chrome, *args],
            pass_fds=(child_in, child_out),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=open(os.environ["DEMO_CHROME_LOG"], "wb") if os.environ.get("DEMO_CHROME_LOG") else subprocess.DEVNULL,
        )
        if max(child_in, child_out) > 9:
            raise RuntimeError("descriptor numbers too high for sh redirection")
        for fd in (to_chrome_r, from_chrome_w, child_in, child_out):
            os.close(fd)
        self._wfd, self._rfd = to_chrome_w, from_chrome_r
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, dict] = {}
        self._cond = threading.Condition()
        self.session = ""
        self.frames_dir = frames
        self.frames: list[tuple[float, str]] = []
        self.closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        buf = b""
        while True:
            try:
                chunk = os.read(self._rfd, 1 << 20)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                if raw:
                    self._dispatch(json.loads(raw))
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg:
            with self._cond:
                self._pending[msg["id"]] = msg
                self._cond.notify_all()
            return
        if msg.get("method") == "Page.screencastFrame":
            params = msg["params"]
            index = len(self.frames)
            name = f"frame_{index:05d}.jpg"
            (self.frames_dir / name).write_bytes(base64.b64decode(params["data"]))
            self.frames.append((float(params["metadata"]["timestamp"]), name))
            self._send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}, self.session, wait=False)

    def _send(self, method: str, params: dict | None, session: str, wait: bool = True) -> dict:
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            message = {"id": mid, "method": method, "params": params or {}}
            if session:
                message["sessionId"] = session
            os.write(self._wfd, json.dumps(message).encode() + b"\0")
        if not wait:
            return {}
        deadline = time.monotonic() + 60
        with self._cond:
            while mid not in self._pending:
                if self.closed or time.monotonic() > deadline:
                    raise RuntimeError(f"no reply to {method}")
                self._cond.wait(0.5)
            reply = self._pending.pop(mid)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message', 'error')}")
        return reply.get("result", {})

    def browser(self, method: str, params: dict | None = None) -> dict:
        return self._send(method, params, "")

    def call(self, method: str, params: dict | None = None) -> dict:
        return self._send(method, params, self.session)

    def evaluate(self, expression: str):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in result:
            raise RuntimeError("page script failed")
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.browser("Browser.close")
        except RuntimeError:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        for fd in (self._wfd, self._rfd):
            try:
                os.close(fd)
            except OSError:
                pass


def _type(cdp: Cdp, text: str, delay: float = 0.04) -> None:
    for char in text:
        cdp.call("Input.dispatchKeyEvent", {"type": "char", "text": char})
        time.sleep(delay)


def _wait(cdp: Cdp, expression: str, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cdp.evaluate(expression):
            return
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {what}")


def _assert_clean(cdp: Cdp, scene: str) -> None:
    text = cdp.evaluate("document.body.innerText") or ""
    hit = FORBIDDEN_TEXT.search(text)
    if hit:
        raise RuntimeError(f"scene {scene}: forbidden text on screen ({hit.group(0)!r})")


def record(base_url: str, chrome: str, frames: Path) -> dict:
    """Run the scenes and write screencast frames plus scenes.json into frames."""
    if frames.exists():
        shutil.rmtree(frames)
    frames.mkdir(parents=True)
    profile = Path(tempfile.mkdtemp(prefix="demo-profile-", dir=os.environ.get("DEMO_TMPDIR") or None))
    cdp = Cdp(chrome, profile, frames)
    marks: list[dict] = []
    try:
        target = cdp.browser("Target.createTarget", {"url": "about:blank"})["targetId"]
        cdp.session = cdp.browser("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": VIEWPORT[0], "height": VIEWPORT[1], "deviceScaleFactor": 1, "mobile": False},
        )
        cdp.call("Page.navigate", {"url": base_url.rstrip("/") + "/ui"})
        _wait(cdp, "document.readyState === 'complete' && !!document.getElementById('message')", 30, "console")
        _wait(cdp, "!!document.querySelector('#health-badge .badge-ok, #health-badge .badge')", 30, "health badge")
        cdp.call(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 85, "maxWidth": VIEWPORT[0], "maxHeight": VIEWPORT[1], "everyNthFrame": 1},
        )
        time.sleep(1.5)
        for position, (name, question, open_excerpts) in enumerate(SCENES):
            if position:
                cdp.evaluate("document.getElementById('new-session-btn').click(); true")
                time.sleep(0.8)
            cdp.evaluate("document.getElementById('message').focus(); true")
            _type(cdp, question)
            time.sleep(0.6)
            before = cdp.evaluate("document.querySelectorAll('#messages article.turn-assistant').length")
            cdp.evaluate("document.getElementById('send-btn').click(); true")
            _wait(
                cdp,
                "(function(){var m=document.getElementById('messages');"
                f"var a=m.querySelectorAll('article.turn-assistant');return a.length>{before}"
                "&&!m.hasAttribute('aria-busy')&&a[a.length-1].getAttribute('data-state')!=='streaming';})()",
                ANSWER_TIMEOUT_S,
                f"scene {name} to finish",
            )
            time.sleep(0.8)
            if open_excerpts:
                cdp.evaluate(
                    "var d=document.querySelector('#messages article.turn-assistant:last-of-type details.evidence-view');"
                    "if(d){d.open=true;d.scrollIntoView({block:'start'});} true"
                )
                time.sleep(0.8)
            _assert_clean(cdp, name)
            state = cdp.evaluate(
                "(function(){var a=document.querySelectorAll('#messages article.turn-assistant');"
                "var b=a[a.length-1].querySelector('.state-badge');"
                "var c=a[a.length-1].querySelectorAll('.citations li').length;"
                "return JSON.stringify({badge:b?b.textContent.trim():'',citations:c});})()"
            )
            marks.append({"scene": name, "frame": len(cdp.frames) - 1, **json.loads(state)})
            time.sleep(0.4)
        cdp.call("Page.stopScreencast")
        time.sleep(0.5)
    finally:
        cdp.close()
        shutil.rmtree(profile, ignore_errors=True)
    scenes = {"viewport": list(VIEWPORT), "frames": cdp.frames, "scenes": marks}
    (frames / "scenes.json").write_text(json.dumps(scenes))
    return scenes


def retime(scenes: dict) -> tuple[list[float], float]:
    """Display duration per frame: real gaps capped, sped up to the target, with
    a fixed hold on the last frame of each scene. Returns (durations, speedup)."""
    stamps = [t for t, _ in scenes["frames"]]
    raw = [min(stamps[i + 1] - stamps[i], GAP_CAP_SECONDS) for i in range(len(stamps) - 1)] + [0.0]
    holds = {m["frame"]: SCENE_HOLD_SECONDS for m in scenes["scenes"]}
    budget = TARGET_SECONDS - sum(holds.values())
    speed = max(1.0, sum(raw) / budget) if budget > 0 else 1.0
    return [d / speed + holds.get(i, 0.0) for i, d in enumerate(raw)], speed


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def encode(frames: Path, media_dir: Path) -> dict:
    """Encode demo.mp4, demo.webm and poster.jpg; raise the CRF until the three
    files together fit MEDIA_LIMIT_BYTES."""
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not found on PATH (required by the encode step)")
    scenes = json.loads((frames / "scenes.json").read_text())
    durations, speed = retime(scenes)
    media_dir.mkdir(parents=True, exist_ok=True)
    listing = frames / "concat.txt"
    names = [name for _, name in scenes["frames"]]
    lines = []
    for name, duration in zip(names, durations, strict=True):
        lines += [f"file '{name}'", f"duration {max(duration, 0.001):.4f}"]
    lines.append(f"file '{names[-1]}'")
    listing.write_text("\n".join(lines) + "\n")
    answer = next(m for m in scenes["scenes"] if m["scene"] == "answer")
    shutil.copyfile(frames / names[answer["frame"]], media_dir / "poster.jpg")
    vf = f"fps={FPS},scale={VIEWPORT[0]}:{VIEWPORT[1]}:flags=lanczos,format=yuv420p"
    sizes: dict[str, int] = {}
    for crf_mp4, crf_webm in ((30, 36), (34, 40), (38, 44), (42, 48)):
        _ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-vf", vf, "-an",
                "-c:v", "libx264", "-preset", "slow", "-crf", str(crf_mp4),
                "-movflags", "+faststart", str(media_dir / "demo.mp4"))
        _ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-vf", vf, "-an",
                "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(crf_webm),
                "-row-mt", "1", str(media_dir / "demo.webm"))
        sizes = {n: (media_dir / n).stat().st_size for n in ("demo.mp4", "demo.webm", "poster.jpg")}
        if sum(sizes.values()) <= MEDIA_LIMIT_BYTES:
            break
    else:
        raise SystemExit(f"media exceeds {MEDIA_LIMIT_BYTES} bytes at the highest CRF: {sizes}")
    length = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(media_dir / "demo.mp4")], check=True, capture_output=True, text=True).stdout)
    return {"sizes": sizes, "total": sum(sizes.values()), "seconds": round(length, 1),
            "speedup": round(speed, 2), "scenes": scenes["scenes"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("record", "encode", "run"):
        p = sub.add_parser(name)
        p.add_argument("--frames", type=Path, required=True, help="frame directory (outside the repo)")
        if name != "encode":
            p.add_argument("--base-url", default="http://127.0.0.1:8080")
            p.add_argument("--chrome", default=os.environ.get("CONSOLE_BROWSER_CHROME", ""))
        if name != "record":
            p.add_argument("--media-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.cmd in ("record", "run"):
        if not args.chrome or not Path(args.chrome).is_file():
            raise SystemExit("set --chrome or CONSOLE_BROWSER_CHROME to the pinned Chrome for Testing binary")
        scenes = record(args.base_url, args.chrome, args.frames)
        print(f"recorded {len(scenes['frames'])} frames; scenes: {json.dumps(scenes['scenes'])}")
    if args.cmd in ("encode", "run"):
        print(json.dumps(encode(args.frames, args.media_dir), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
