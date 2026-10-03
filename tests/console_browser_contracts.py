"""Executed real-browser contracts for the operator console (issue #372).

These tests run the SHIPPED `console.js` and `index.html` in a real Chrome
against the real FastAPI console routes, with a scripted gateway-shaped LLM
(tests/helpers_console_browser.py). Expected outcomes are asserted on the
rendered DOM, the browser's persisted storage and the accessibility tree,
never on source strings.

Runtime: an already-installed Chrome for Testing + matching chromedriver
(CONSOLE_BROWSER_CHROME / CONSOLE_BROWSER_CHROMEDRIVER, or the existing
selenium-manager cache). Nothing is downloaded. Without a runtime the suite
SKIPS - a skip is not a pass. Run: `pytest -m browser tests/console_browser_contracts.py`.
The filename deliberately does not match `test_*.py`: default collection (the pinned
CI selection policy in scripts/unit_evidence.py) never picks it up, so unit shards
carry no browser skips; it runs only when named explicitly.

Not covered here (see docs/console-contracts.md): other browsers, real
screen readers, OS high-contrast/forced-colors, native browser zoom.
"""

from __future__ import annotations

import json
import logging

import pytest

from tests.helpers_console_browser import CITE, Browser, LiveConsole, find_runtime

RUNTIME = find_runtime()
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        RUNTIME is None,
        reason="no offline Chrome for Testing + matching chromedriver "
        "(set CONSOLE_BROWSER_CHROME / CONSOLE_BROWSER_CHROMEDRIVER)",
    ),
]

STORE_KEY = "mainframe_rag_sessions"
FINAL_ANSWER = "command after initialization.\n\nCitations:\n- " + CITE + "\n"


@pytest.fixture(scope="module")
def console():
    live = LiveConsole()
    yield live
    live.close()


@pytest.fixture
def browser(console):
    console.llm.reset()
    assert RUNTIME is not None
    b = Browser(*RUNTIME)
    yield b
    console.llm.release_all()
    b.close()


@pytest.fixture
def page(console, browser):
    browser.get(console.url)
    browser.wait("return !!document.querySelector('#messages .empty-state')", what="console boot")
    return browser


# -- helpers -----------------------------------------------------------------


def store(b: Browser) -> dict:
    raw = b.eval("return window.localStorage.getItem(arguments[0])", STORE_KEY)
    return json.loads(raw) if raw else {"sessions": {}, "active": None}


def sessions_by_title(b: Browser) -> dict[str, dict]:
    return {s["title"]: s for s in store(b)["sessions"].values()}


def text(b: Browser, css: str) -> str:
    return b.eval("var e=document.querySelector(arguments[0]);return e?e.textContent:null", css)


def send(b: Browser, message: str) -> None:
    b.type("#message", message)
    b.click("#send-btn")


def wait_state(b: Browser, state: str, n: int = 1) -> None:
    b.wait(
        "return document.querySelectorAll('.turn-assistant[data-state=\"'+arguments[0]+'\"]').length>=arguments[1]",
        state,
        n,
        what=f"{n} assistant turn(s) in state {state}",
    )


def wait_status(b: Browser, fragment: str) -> None:
    b.wait(
        "return document.getElementById('console-status').textContent.indexOf(arguments[0])>=0",
        fragment,
        what=f"status announcement containing {fragment!r}",
    )


def capture_exports(b: Browser) -> None:
    b.init_script(
        "window.__exports=[];const o=URL.createObjectURL.bind(URL);"
        "URL.createObjectURL=(blob)=>{blob.text().then(t=>window.__exports.push(t));return o(blob);};"
    )


# -- send -> provisional -> final / error / EOF / cancel -----------------------


def test_send_shows_provisional_then_verified_final(console, page):
    console.llm.queue(("token", "Reissue the "), ("gate", "mid"), ("token", FINAL_ANSWER), ("done",))
    send(page, "What is IEA500I?")
    page.wait(
        "return !!document.querySelector('.turn-assistant[data-state=streaming] .md p')",
        what="first token rendered",
    )
    # While streaming: labelled provisional, no verification claim, Stop offered.
    assert "Provisional" in text(page, ".turn-assistant .state-provisional")
    assert "Verified" not in text(page, "#messages")
    assert page.eval("return document.getElementById('send-btn').getAttribute('aria-label')") == "Stop"
    assert page.eval("return document.getElementById('messages').getAttribute('aria-busy')") == "true"
    assert store(page)["sessions"] and all(
        len(s["turns"]) == 1 for s in store(page)["sessions"].values()
    ), "only the operator turn is persisted until the stream completes"

    console.llm.release("mid")
    wait_state(page, "complete")
    assert page.eval("return document.querySelectorAll('.state-provisional').length") == 0
    assert text(page, ".citations-title") == "Verified manual citations (1)"
    assert page.eval("return document.getElementById('send-btn').getAttribute('aria-label')") == "Send"
    assert page.eval("return document.getElementById('messages').hasAttribute('aria-busy')") is False
    wait_status(page, "Answer complete. Citations verified.")
    (session,) = store(page)["sessions"].values()
    assert [t["role"] for t in session["turns"]] == ["user", "assistant"]
    assert session["turns"][1]["verification_state"] == "accepted"


def test_midstream_failure_stays_incomplete_across_reload_export_and_reuse(console, page):
    capture_exports(page)
    console.llm.queue(("token", "Partial answer "), ("fail",))
    send(page, "What is IEA500I?")
    wait_state(page, "incomplete")
    body = text(page, "#messages")
    assert "Incomplete generation" in body
    assert "Stream failed" in body
    assert "internal detail" not in body and "scripted upstream" not in body
    (session,) = store(page)["sessions"].values()
    assert session["turns"][1]["verification_state"] == "generation_incomplete"
    assert "Partial answer" in session["turns"][1]["content"]

    page.reload()
    page.wait("return document.querySelectorAll('#messages .turn').length==2", what="restore")
    restored = text(page, "#messages")
    assert "Incomplete generation" in restored and "Partial answer" in restored
    assert "Verified" not in restored

    page.click("#export-btn")
    page.wait("return window.__exports && window.__exports.length==1", what="export blob")
    report = page.eval("return window.__exports[0]")
    assert "**Verification state:** generation_incomplete" in report
    assert "accepted" not in report and "**Verified citations" not in report

    # Reused as conversation context, the cut-off answer is still qualified.
    console.llm.queue(("token", "Retry answer."), ("done",))
    send(page, "Please retry")
    wait_state(page, "complete")
    sent = json.dumps(console.llm.requests[-1])
    assert "cut off before completion" in sent and "Partial answer" in sent


def test_stream_eof_without_final_is_never_accepted(console, page):
    console.llm.queue(("token", "Half an ans"))  # no finish frame: EOF
    send(page, "What is IEA500I?")
    page.wait(
        "return document.querySelectorAll('.turn-assistant:not([data-state=streaming])').length>=1",
        what="stream settled",
    )
    (session,) = store(page)["sessions"].values()
    assistant = [t for t in session["turns"] if t["role"] == "assistant"]
    assert assistant, "partial text that was displayed must be retained"
    assert assistant[0]["verification_state"] != "accepted"
    assert "Verified" not in text(page, "#messages")


def test_stop_keeps_partial_incomplete_and_stop_before_first_token_leaves_no_husk(console, page):
    console.llm.queue(("token", "Stopped mid "), ("gate", "never"))
    send(page, "What is IEA500I?")
    page.wait("return !!document.querySelector('.turn-assistant[data-state=streaming] .md p')")
    page.click("#send-btn")  # Stop
    wait_state(page, "incomplete")
    assert "Stopped — partial answer kept" in text(page, "#messages")
    wait_status(page, "Stopped. Partial answer kept; incomplete and not verified.")
    (session,) = store(page)["sessions"].values()
    assert session["turns"][1]["verification_state"] == "generation_incomplete"
    assert session["turns"][1]["meta"]["stopped"] is True

    console.llm.queue(("gate", "first"))
    send(page, "Second question")
    page.wait("return document.querySelectorAll('.turn-user').length==2")
    page.click("#send-btn")
    page.wait("return !document.querySelector('[data-state=streaming]')", what="stopped")
    assert page.eval("return document.querySelectorAll('.turn-assistant').length") == 1
    (session,) = store(page)["sessions"].values()
    assert [t["role"] for t in session["turns"]] == ["user", "assistant", "user"]
    # The unanswered question is visible as such after a reload, not silently lost.
    page.reload()
    page.wait("return document.querySelectorAll('#messages .turn').length==3")
    assert "No answer recorded" in text(page, "#messages")


def test_transport_splits_utf8_and_frame_boundaries(console, page):
    unicode_answer = "Réémettre 🙂 世界 — ok.\n\nCitations:\n- " + CITE + "\n"
    console.llm.queue(("token", "Réémettre 🙂 "), ("token", unicode_answer[len("Réémettre 🙂 ") :]), ("done",))
    page.eval(
        """
        const real = window.fetch.bind(window);
        window.fetch = async (url, opts) => {
          const resp = await real(url, opts);
          const bytes = new Uint8Array(await resp.arrayBuffer());
          let i = 0;
          const stream = new ReadableStream({
            pull(controller) {
              if (i >= bytes.length) { controller.close(); return; }
              controller.enqueue(bytes.slice(i, i + 1));  // every byte its own chunk
              i += 1;
            }
          });
          return new Response(stream, { status: resp.status, headers: resp.headers });
        };
        """
    )
    send(page, "What is IEA500I?")
    wait_state(page, "complete")
    assert "Réémettre 🙂 世界 — ok." in text(page, ".turn-assistant .turn-content")
    assert "�" not in text(page, "#messages")
    assert text(page, ".citations-title") == "Verified manual citations (1)"


# -- sessions: delete / new / rename / cross-tab races ---------------------------


def _two_sessions(console, page) -> tuple[str, str]:
    send(page, "First incident question")
    wait_state(page, "complete")
    page.click("#new-session-btn")
    page.wait("return !!document.querySelector('#messages .empty-state')")
    send(page, "Second incident question")
    wait_state(page, "complete")
    sess = sessions_by_title(page)
    return sess["First incident question"], sess["Second incident question"]


def test_delete_active_then_send_uses_survivor_and_records_it_active(console, page):
    _two_sessions(console, page)
    assert page.eval("return document.querySelectorAll('.row.active').length") == 1
    page.eval("document.querySelector('.row.active .del').click()")
    page.wait("return document.querySelectorAll('.row').length==1", what="one incident left")
    assert page.eval("return document.querySelectorAll('.row.active').length") == 1
    assert page.eval("return document.querySelector('.row.active .open').getAttribute('aria-current')") == "true"
    data = store(page)
    (survivor_id,) = data["sessions"].keys()
    assert data["active"] == survivor_id, "the surviving incident must be recorded as active"
    assert "First incident question" in text(page, "#messages")

    send(page, "After delete")
    wait_state(page, "complete", 2)
    (session,) = store(page)["sessions"].values()
    assert [t["content"] for t in session["turns"] if t["role"] == "user"] == [
        "First incident question",
        "After delete",
    ]


def test_delete_last_incident_then_next_send_works(console, page):
    send(page, "Only incident")
    wait_state(page, "complete")
    page.eval("document.querySelector('.row .del').click()")
    page.wait("return !!document.querySelector('#messages .empty-state')")
    assert "Only incident" not in page.eval("return localStorage.getItem(arguments[0])", STORE_KEY)
    send(page, "Fresh start")
    wait_state(page, "complete")
    (session,) = store(page)["sessions"].values()
    assert [t["content"] for t in session["turns"] if t["role"] == "user"] == ["Fresh start"]


def test_new_incident_during_stream_does_not_receive_or_lose_the_answer(console, page):
    console.llm.queue(("token", "Answer for A. "), ("gate", "a"), ("token", "more"), ("done",))
    send(page, "Question for A")
    page.wait("return !!document.querySelector('.turn-assistant[data-state=streaming] .md p')")
    page.click("#new-session-btn")
    page.wait("return !!document.querySelector('#messages .empty-state')")
    console.llm.release("a")
    page.wait("return document.getElementById('send-btn').getAttribute('aria-label')=='Send'", what="stream over")
    page.wait("return document.querySelectorAll('#messages .turn').length==0")
    assert "Answer for A" not in text(page, "#messages"), "finalized text raced into the wrong incident"

    data = store(page)
    assert len(data["sessions"]) == 2, "creating an incident mid-stream must survive the stream's save"
    by_title = {s["title"]: s for s in data["sessions"].values()}
    assert [t["role"] for t in by_title["Question for A"]["turns"]] == ["user", "assistant"]
    assert by_title["New Incident"]["turns"] == []
    assert data["sessions"][data["active"]]["title"] == "New Incident"
    page.click(".row:not(.active) .open")
    page.wait("return document.querySelectorAll('#messages .turn').length==2")
    assert "Answer for A. more" in text(page, "#messages")


def test_switching_away_and_back_keeps_the_live_stream_visible(console, page):
    console.llm.queue(("token", "Live text. "), ("gate", "x"), ("token", "end"), ("done",))
    send(page, "Question for A")
    page.wait("return !!document.querySelector('.turn-assistant[data-state=streaming] .md p')")
    page.click("#new-session-btn")
    page.wait("return !!document.querySelector('#messages .empty-state')")
    page.click(".row:not(.active) .open")
    page.wait("return !!document.querySelector('.turn-assistant[data-state=streaming]')", what="stream re-attached")
    assert "Provisional" in text(page, ".turn-assistant")
    console.llm.release("x")
    wait_state(page, "complete")
    assert "Live text. end" in text(page, ".turn-assistant .turn-content")


def test_rename_during_stream_survives_the_final_save(console, page):
    console.llm.queue(("token", "Renaming. "), ("gate", "r"), ("done",))
    send(page, "Question to rename")
    page.wait("return !!document.querySelector('.turn-assistant[data-state=streaming] .md p')")
    page.eval("document.querySelector('.row .ren').click()")
    page.wait("return !!document.querySelector('input.rename')")
    page.eval("var i=document.querySelector('input.rename');i.value='JOB12345 abend';")
    page.keys("ENTER")
    page.wait("return document.querySelector('.row .open').textContent=='JOB12345 abend'")
    console.llm.release("r")
    page.wait("return !document.querySelector('[data-state=streaming]') && document.querySelectorAll('.turn-assistant').length==1")
    (session,) = store(page)["sessions"].values()
    assert session["title"] == "JOB12345 abend"
    assert [t["role"] for t in session["turns"]] == ["user", "assistant"]


def test_delete_during_stream_discards_the_answer_and_does_not_resurrect(console, page):
    console.llm.queue(("token", "Doomed text. "), ("gate", "d"), ("done",))
    send(page, "Secret question")
    page.wait("return !!document.querySelector('.turn-assistant[data-state=streaming] .md p')")
    page.eval("document.querySelector('.row .del').click()")
    console.llm.release("d")
    page.wait("return document.getElementById('send-btn').getAttribute('aria-label')=='Send'")
    page.wait("return !!document.querySelector('#messages .empty-state')")
    raw = page.eval("return localStorage.getItem(arguments[0])", STORE_KEY)
    assert "Doomed text" not in raw and "Secret question" not in raw
    assert page.eval("return document.querySelectorAll('#messages .turn').length") == 0


def test_cross_tab_writes_are_reconciled_not_overwritten(console, page):
    tab_a = page.handle()
    send(page, "Question from A")
    wait_state(page, "complete")
    tab_b = page.new_tab()
    page.switch(tab_b)
    page.get(console.url)
    page.wait("return document.querySelectorAll('#messages .turn').length==2", what="tab B restores")
    send(page, "Question from B")
    wait_state(page, "complete", 2)

    page.switch(tab_a)  # idle tab A learns about B's write without a reload
    page.wait("return document.querySelectorAll('#messages .turn').length==4", what="A picks up B's turns")
    console.llm.queue(("token", "A again. "), ("gate", "ab"), ("done",))
    send(page, "Second from A")
    page.wait("return !!document.querySelector('.turn-assistant[data-state=streaming] .md p')")

    page.switch(tab_b)  # B writes a brand-new incident while A's stream is open
    page.click("#new-session-btn")
    page.wait("return !!document.querySelector('#messages .empty-state')")
    send(page, "Incident only B knows")
    wait_state(page, "complete")

    page.switch(tab_a)
    console.llm.release("ab")
    wait_state(page, "complete", 3)
    data = store(page)
    users = {
        s["title"]: [t["content"] for t in s["turns"] if t["role"] == "user"]
        for s in data["sessions"].values()
    }
    assert users["Question from A"] == ["Question from A", "Question from B", "Second from A"]
    assert users["Incident only B knows"] == ["Incident only B knows"], "A's final save clobbered B's incident"
    assert len(data["sessions"]) == 2


# -- inert markup, assets ----------------------------------------------------------


def test_list_item_continuation_lines_render_inside_their_item(console, page):
    """Escaped defect: a plain line under a list item was emitted as a
    paragraph ahead of the still-open list, so items showed as empty headings
    below their own bodies. Streamed and reloaded renders keep source order."""
    answer = (
        "Depends on the reason.\n\n"
        "1. **If revoked:**\nRemove the suspension.\n"
        "2. **If inactive:**\nReplace the password.\n\n"
        "Then retry the logon.\n"
    )
    console.llm.queue(("token", answer), ("done",))
    send(page, "How do I reset it?")
    wait_state(page, "complete")
    shape = (
        "return Array.from(document.querySelectorAll('.turn-assistant .md > *'))"
        ".map(e => e.tagName + ':' + e.textContent.replace(/\\s+/g, ' ').trim())"
    )
    expected = [
        "P:Depends on the reason.",
        "OL:If revoked: Remove the suspension.If inactive: Replace the password.",
        "P:Then retry the logon.",
    ]
    assert page.eval(shape) == expected
    assert page.eval("return Array.from(document.querySelectorAll('.turn-assistant .md li')).map(e => e.textContent)") == [
        "If revoked: Remove the suspension.",
        "If inactive: Replace the password.",
    ]
    page.reload()
    wait_state(page, "complete")
    assert page.eval(shape) == expected


def test_hostile_markup_is_inert_everywhere(console, page):
    hostile = (
        '<img src=x onerror="window.__pwn=1"> <script>window.__pwn=2</script> '
        "[go](javascript:window.__pwn=3) <b>bold?</b> **real bold**\n\n"
        '```\n<iframe src="javascript:window.__pwn=4"></iframe>\n```\n'
    )
    console.llm.queue(("token", hostile), ("done",))
    page.eval("document.getElementById('splunk-context').value = arguments[0]", '<svg onload="window.__pwn=5">')
    page.eval("document.querySelector('details.drawer').open = true")
    send(page, '<img src=x onerror="window.__pwn=6"> title')
    wait_state(page, "complete")
    page.reload()
    page.wait("return document.querySelectorAll('#messages .turn').length==2")
    for sel in ("img", "script", "iframe", "svg", "a[href]", "b"):
        assert page.eval(f"return document.querySelectorAll('#messages {sel}, #session-list {sel}').length") == 0, sel
    assert page.eval("return window.__pwn === undefined")
    shown = text(page, "#messages")
    assert "<img src=x" in shown and "<script>" in shown and "javascript:window.__pwn=3" in shown
    assert page.eval("return document.querySelectorAll('#messages strong').length") == 1


def test_no_external_requests_and_no_csp_violations(console, page):
    page.init_script(
        "window.__csp=[];document.addEventListener('securitypolicyviolation',e=>window.__csp.push(e.violatedDirective));"
    )
    page.reload()
    page.wait("return !!document.querySelector('#messages .empty-state')")
    send(page, "What is IEA500I?")
    wait_state(page, "complete")
    page.wait("return !!document.querySelector('#health-badge .badge')", what="health badge via htmx")
    origins = page.eval(
        "return Array.from(new Set(performance.getEntriesByType('resource').map(e=>new URL(e.name).origin)))"
    )
    assert origins == [f"http://127.0.0.1:{console.port}"], origins
    assert page.eval("return window.__csp") == []


# -- storage / clipboard / export failures -----------------------------------------


def test_vendored_fonts_load_under_the_strict_csp(console, page):
    """Every vendored face decodes and is usable from /ui/static under the
    shipped CSP (fonts fall under default-src 'self'); a broken file, a wrong
    path or a CSP block leaves the face unloaded."""
    faces = page.eval_async(
        "const done = arguments[arguments.length - 1];"
        "const specs = ['400 16px \"IBM Plex Sans\"', 'italic 400 16px \"IBM Plex Sans\"',"
        " '500 16px \"IBM Plex Sans\"', '600 16px \"IBM Plex Sans\"',"
        " '400 16px \"IBM Plex Mono\"', '500 16px \"IBM Plex Mono\"'];"
        "Promise.all(specs.map((s) => document.fonts.load(s, 'Abc')))"
        ".then((loaded) => done(loaded.map((list) => list.length)), (err) => done(String(err)));"
    )
    assert faces == [1, 1, 1, 1, 1, 1]
    assert page.eval("return Array.from(document.fonts).filter((f) => f.status === 'error').length") == 0


def test_unavailable_localstorage_degrades_visibly(console, browser):
    browser.init_script(
        "Object.defineProperty(window,'localStorage',{get(){throw new DOMException('denied','SecurityError');}});"
    )
    browser.get(console.url)
    browser.wait("return !!document.querySelector('#messages .empty-state')")
    notice = browser.eval("var n=document.getElementById('storage-notice');return n.hidden?'':n.innerText")
    assert "Browser storage is unavailable" in notice and "lost on reload" in notice
    send(browser, "What is IEA500I?")
    wait_state(browser, "complete")
    assert browser.eval("return document.querySelectorAll('#messages .turn').length") == 2
    browser.click("#new-session-btn")
    browser.wait("return !!document.querySelector('#messages .empty-state')")
    browser.eval("document.querySelector('.row:not(.active) .open').click()")
    browser.wait("return document.querySelectorAll('#messages .turn').length==2", what="in-memory restore")


def test_quota_failure_flips_to_memory_with_notice_and_keeps_the_turn(console, page):
    page.eval(
        "const o=Storage.prototype.setItem;Storage.prototype.setItem=function(k,v){"
        "if(k==='mainframe_rag_sessions'&&window.__full)throw new DOMException('full','QuotaExceededError');"
        "return o.call(this,k,v);};window.__full=true;"
    )
    send(page, "What is IEA500I?")
    wait_state(page, "complete")
    assert page.eval("return document.getElementById('storage-notice').hidden") is False
    assert page.eval("return document.querySelectorAll('#messages .turn').length") == 2


def test_clipboard_and_export_failures_are_reported_not_swallowed(console, page):
    page.eval(
        "Object.defineProperty(navigator,'clipboard',{value:{writeText:()=>Promise.reject(new Error('no'))}});"
        "URL.createObjectURL=()=>{throw new Error('blocked');};"
    )
    send(page, "What is IEA500I?")
    wait_state(page, "complete")
    page.eval("document.querySelector('.turn-assistant .copy-turn').click()")
    page.wait("return document.querySelector('.turn-assistant .copy-turn').textContent=='Copy failed'")
    wait_status(page, "Copy failed.")
    page.wait("return document.querySelector('.turn-assistant .copy-turn').textContent=='Copy'", timeout=8)
    page.click("#export-btn")
    wait_status(page, "Export failed.")
    assert len(store(page)["sessions"]) == 1


INSECURE_HOST = "console.test"


@pytest.fixture
def insecure_page(console):
    """The console served over plain http from a non-localhost name: not a
    secure context, so the async Clipboard API does not exist (the shape of
    a console reached at http://<host>:8080 instead of the OAuth Route)."""
    console.llm.reset()
    assert RUNTIME is not None
    b = Browser(*RUNTIME, extra_args=(f"--host-resolver-rules=MAP {INSECURE_HOST} 127.0.0.1",))
    b.get(f"http://{INSECURE_HOST}:{console.port}/ui")
    b.wait("return !!document.querySelector('#messages .empty-state')", what="console boot")
    yield b
    console.llm.release_all()
    b.close()


def read_clipboard(b: Browser) -> str:
    """What a real paste yields: Ctrl+V into the emptied composer."""
    b.eval("document.getElementById('message').value = ''")
    b.type("#message", "\ue009v\ue000")  # WebDriver CONTROL down, v, release all
    return b.eval("return document.getElementById('message').value")


def test_copy_works_on_a_plain_http_origin(console, insecure_page):
    assert insecure_page.eval("return [window.isSecureContext, typeof navigator.clipboard]") == [False, "undefined"]
    console.llm.queue(("token", "Reissue the command after initialization."), ("done",))
    send(insecure_page, "What is IEA500I?")
    wait_state(insecure_page, "complete")
    insecure_page.click(".turn-assistant .copy-turn")
    insecure_page.wait("return document.querySelector('.turn-assistant .copy-turn').textContent=='Copied'")
    assert read_clipboard(insecure_page).strip() == "Reissue the command after initialization."


def test_copy_refused_on_a_plain_http_origin_is_reported(console, insecure_page):
    console.llm.queue(("token", "Answer."), ("done",))
    send(insecure_page, "What is IEA500I?")
    wait_state(insecure_page, "complete")
    insecure_page.eval("document.execCommand = () => false;")
    insecure_page.click(".turn-assistant .copy-turn")
    insecure_page.wait("return document.querySelector('.turn-assistant .copy-turn').textContent=='Copy failed'")
    wait_status(insecure_page, "Copy failed.")


# -- retention -----------------------------------------------------------------------


def test_history_lives_only_in_the_one_storage_key_and_never_in_server_logs(console, page, caplog):
    marker = "ZXQ-SENSITIVE-9731"
    with caplog.at_level(logging.DEBUG):
        page.eval("document.querySelector('details.drawer').open = true")
        page.eval("document.getElementById('splunk-context').value = arguments[0]", "JOB " + marker)
        send(page, "What is IEA500I? " + marker)
        wait_state(page, "complete")
    where = page.eval(
        """
        const hits = [];
        for (let i = 0; i < localStorage.length; i++) {
          const k = localStorage.key(i);
          if (localStorage.getItem(k).includes(arguments[0])) hits.push('local:' + k);
        }
        for (let i = 0; i < sessionStorage.length; i++) {
          if (sessionStorage.getItem(sessionStorage.key(i)).includes(arguments[0])) hits.push('session');
        }
        if (document.cookie.includes(arguments[0])) hits.push('cookie');
        return hits;
        """,
        marker,
    )
    assert where == ["local:" + STORE_KEY]
    assert page.eval_async("const done=arguments[arguments.length-1];indexedDB.databases().then(d=>done(d.length))") == 0
    assert marker not in caplog.text, "operator text reached server logs"
    assert "Reissue" not in caplog.text, "answer text reached server logs"


def test_clear_all_is_two_step_erases_everywhere_and_next_send_works(console, page):
    tab_a = page.handle()
    send(page, "Erase me please")
    wait_state(page, "complete")
    tab_b = page.new_tab()
    page.switch(tab_b)
    page.get(console.url)
    page.wait("return document.querySelectorAll('#messages .turn').length==2")
    page.switch(tab_a)

    page.click("#clear-history-btn")  # first press only arms
    assert "Confirm" in text(page, "#clear-history-btn")
    assert "Erase me please" in page.eval("return localStorage.getItem(arguments[0])", STORE_KEY)
    page.click("#clear-history-btn")
    page.wait("return !!document.querySelector('#messages .empty-state')", what="cleared view")
    assert "Erase me please" not in (page.eval("return localStorage.getItem(arguments[0])", STORE_KEY) or "")
    assert "Erase me" not in page.eval("return document.body.innerText")
    wait_status(page, "All saved incidents were erased")

    page.switch(tab_b)  # the other tab follows the erase instead of resurrecting it
    page.wait("return !!document.querySelector('#messages .empty-state')", what="tab B cleared")
    page.switch(tab_a)
    send(page, "After the erase")
    wait_state(page, "complete")
    assert [s["title"] for s in store(page)["sessions"].values()] == ["After the erase"]


def test_armed_clear_disarms_without_erasing(console, page):
    send(page, "Keep me")
    wait_state(page, "complete")
    page.click("#clear-history-btn")
    page.eval("document.getElementById('clear-history-btn').blur()")
    assert "Clear all saved" in text(page, "#clear-history-btn")
    assert "Keep me" in page.eval("return localStorage.getItem(arguments[0])", STORE_KEY)


def test_eviction_keeps_the_open_incident_at_the_cap(console, page):
    page.eval(
        """
        const s = JSON.parse(localStorage.getItem(arguments[0]) || '{"sessions":{},"active":null}');
        for (let i = 0; i < 30; i++) s.sessions['old-' + i] = {title: 'old ' + i, updated_at: i + 1, turns: []};
        localStorage.setItem(arguments[0], JSON.stringify(s));
        """,
        STORE_KEY,
    )
    page.reload()
    page.wait("return document.querySelectorAll('.row').length>=30")
    send(page, "The incident I am working in")
    wait_state(page, "complete")
    page.click("#new-session-btn")  # 32nd incident forces eviction of the oldest idle one
    page.wait("return !!document.querySelector('#messages .empty-state')")
    data = store(page)
    assert len(data["sessions"]) == 30
    assert "old-0" not in data["sessions"]
    assert any(s["title"] == "The incident I am working in" for s in data["sessions"].values())


# -- keyboard, focus, accessibility tree, viewport, contrast --------------------------


def test_every_control_is_reachable_by_tab_with_a_visible_focus_indicator(console, page):
    send(page, "What is IEA500I?")
    wait_state(page, "complete")
    page.eval("document.querySelector('details.drawer').open = true")
    page.eval("document.activeElement.blur(); document.body.focus();")
    ident = (
        "function ident(e){return e.tagName+'#'+e.id+'.'+e.className+'@'+"
        "Array.from(e.parentNode.children).indexOf(e)+'/'+(e.textContent||'').slice(0,12);}"
    )
    expected = set(
        page.eval(
            ident
            + """
            const sel = 'a[href],button:not([disabled]),input:not([type=hidden]):not([disabled]),select,textarea,summary,[tabindex]:not([tabindex="-1"])';
            return Array.from(document.querySelectorAll(sel)).filter(e => e.getClientRects().length).map(ident);
            """
        )
    )
    reached, no_ring = set(), []
    for step in range(len(expected) + 10):
        page.keys("TAB")
        info = page.eval(
            ident
            + """
            const e = document.activeElement;
            if (!e || e === document.body) return null;
            const cs = getComputedStyle(e);
            const ring = cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0;
            return {tag: ident(e), ring: ring, id: e.id};
            """
        )
        if info is None:
            continue
        reached.add(info["tag"])
        if not info["ring"]:
            no_ring.append(info["tag"])
    assert expected <= reached, f"unreachable by Tab: {sorted(expected - reached)}"
    assert "message" in {t.split("#")[1].split(".")[0] for t in reached}
    assert not no_ring, f"controls with no visible focus ring: {no_ring}"


def test_keyboard_only_send_stop_rename_and_focus_return(console, page):
    page.eval("document.getElementById('message').focus()")
    page.keys("W", "h", "a", "t", "?")
    page.keys("CONTROL+ENTER")
    wait_state(page, "complete")
    assert page.eval("return document.activeElement.id") == "message", "focus must stay in the composer"

    console.llm.queue(("token", "Streaming. "), ("gate", "k"))
    page.keys("N", "e", "x", "t")
    page.keys("CONTROL+ENTER")
    page.wait("return !!document.querySelector('.turn-assistant[data-state=streaming] .md p')")
    page.eval("document.getElementById('send-btn').focus()")
    page.keys("ENTER")  # Stop by keyboard
    wait_state(page, "incomplete")

    page.eval("document.querySelector('.row .ren').focus()")
    page.keys("ENTER")
    page.wait("return document.activeElement.classList.contains('rename')", what="rename input focused")
    page.keys("ESCAPE")
    page.wait("return document.activeElement.classList.contains('open')", what="focus back on the incident")
    page.eval("document.querySelector('.row .ren').focus()")
    page.keys("ENTER")
    page.wait("return document.activeElement.classList.contains('rename')")
    page.keys("X", "ENTER")
    page.wait("return document.activeElement.classList.contains('open')")
    assert text(page, ".row .open").endswith("X")


def test_accessibility_tree_names_roles_and_announcements(console, page):
    tree = page.cdp("Accessibility.getFullAXTree")["nodes"]
    unnamed = []
    for node in tree:
        role = (node.get("role") or {}).get("value")
        name = ((node.get("name") or {}).get("value") or "").strip()
        ignored = node.get("ignored")
        if role in {"button", "textbox", "searchbox", "combobox", "slider", "link"} and not ignored and not name:
            unnamed.append(role)
    assert not unnamed, f"controls without an accessible name: {unnamed}"
    roles = {(n.get("role") or {}).get("value") for n in tree if not n.get("ignored")}
    assert {"main", "complementary", "banner", "status"} <= roles
    assert page.eval("return document.documentElement.lang") == "en"
    assert page.eval("return document.getElementById('messages').hasAttribute('aria-live')") is False
    assert page.eval("return document.getElementById('console-status').getAttribute('role')") == "status"
    pressed = "return Array.from(document.querySelectorAll('.segmented .seg')).map(b => b.getAttribute('aria-pressed'))"
    assert page.eval(pressed) == ["true", "false", "false"]
    assert page.eval("return document.querySelector('.reasoning-control').getAttribute('role')") == "group"
    page.click('.segmented .seg[data-effort="high"]')
    assert page.eval(pressed) == ["false", "false", "true"]

    console.llm.queue(("token", "Partial "), ("fail",))
    send(page, "What is IEA500I?")
    wait_state(page, "incomplete")
    wait_status(page, "Answer incomplete: the stream failed.")


def test_reasoning_effort_choice_reaches_the_model_and_survives_reload(console, page):
    page.click('.segmented .seg[data-effort="high"]')
    send(page, "What is IEA500I?")
    wait_state(page, "complete")
    assert console.llm.efforts[-1] == "high"
    page.reload()
    page.wait("return document.querySelectorAll('#messages .turn').length==2")
    assert page.eval("return document.querySelector('.segmented .seg[aria-pressed=\"true\"]').dataset.effort") == "high"
    page.click('.segmented .seg[data-effort="low"]')
    send(page, "Again")
    wait_state(page, "complete", 2)
    assert console.llm.efforts[-1] == "low"


def test_answer_status_reads_before_the_answer_it_qualifies(console, page):
    """Verified answers carry a header chip; an unverified answer's warning
    sits above its text, both when streamed and after reload."""
    send(page, "What is IEA500I?")
    wait_state(page, "complete")
    console.llm.queue(("token", "Reissue the command after initialization completes."), ("done",))
    send(page, "And then?")
    wait_state(page, "complete", 2)
    order = (
        "return Array.from(document.querySelectorAll('.turn-assistant')).map(a =>"
        " Array.from(a.children).map(c => c.className.split(' ')[0]))"
    )
    chips = "return Array.from(document.querySelectorAll('.turn-assistant .turn-head .state-chip')).map(c => c.textContent)"
    for _ in range(2):
        verified, draft = page.eval(order)
        assert verified[:2] == ["turn-head", "turn-content"] and "citations" in verified
        assert draft[:3] == ["turn-head", "state-badge", "turn-content"]
        assert page.eval(chips) == ["Verified \u00b7 1 citation"]
        page.reload()
        page.wait("return document.querySelectorAll('#messages .turn-assistant').length==2")


@pytest.mark.parametrize("width", [1280, 640, 375, 320])
def test_layout_has_no_horizontal_scroll_and_composer_stays_reachable(console, page, width):
    # 640 and 320 CSS px at a 1280 px window stand in for 200% and 400% zoom.
    page.cdp("Emulation.setDeviceMetricsOverride", width=width, height=800, deviceScaleFactor=1, mobile=False)
    send(page, "What is IEA500I? " + "x" * 120)
    wait_state(page, "complete")
    page.eval("document.querySelector('details.drawer').open = true")
    metrics = page.eval(
        """
        const r = (id) => document.getElementById(id).getBoundingClientRect();
        return {
          scroll: document.documentElement.scrollWidth, inner: window.innerWidth,
          ta: [r('message').left, r('message').right], send: [r('send-btn').left, r('send-btn').right],
          nav: [r('new-session-btn').left, r('new-session-btn').right],
          sendDisplay: getComputedStyle(document.getElementById('send-btn')).display,
        };
        """
    )
    assert metrics["scroll"] <= metrics["inner"], metrics
    for span in ("ta", "nav"):
        assert metrics[span][0] >= 0 and metrics[span][1] <= metrics["inner"], (span, metrics)
    page.type("#message", "more")
    send_box = page.eval("var b=document.getElementById('send-btn').getBoundingClientRect();return [b.left,b.right]")
    assert 0 <= send_box[0] and send_box[1] <= width


@pytest.mark.parametrize("theme", ["theme-3270", "theme-dark"])
def test_text_contrast_meets_wcag_aa_in_both_themes(console, page, theme):
    console.llm.queue(("token", "Partial "), ("fail",))
    send(page, "What is IEA500I?")
    wait_state(page, "incomplete")
    console.llm.queue(("token", "Reissue the "), ("token", FINAL_ANSWER), ("done",))
    send(page, "Again")
    wait_state(page, "complete")
    page.eval("document.getElementById('storage-notice').hidden = false")
    page.eval("var t=document.getElementById('theme-select');t.value=arguments[0];t.dispatchEvent(new Event('change'))", theme)
    page.wait("return document.body.classList.contains(arguments[0])", theme)
    page.eval("document.querySelector('details.drawer').open = true")
    failures = page.eval(
        """
        function parse(c) { const m = c.match(/[\\d.]+/g).map(Number); return {r:m[0],g:m[1],b:m[2],a:m.length>3?m[3]:1}; }
        function lin(v) { v/=255; return v<=0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055,2.4); }
        function lum(c) { return 0.2126*lin(c.r)+0.7152*lin(c.g)+0.0722*lin(c.b); }
        function over(fg, bg) { return {r:fg.r*fg.a+bg.r*(1-fg.a), g:fg.g*fg.a+bg.g*(1-fg.a), b:fg.b*fg.a+bg.b*(1-fg.a), a:1}; }
        function bgOf(e) {
          const stack = [];
          for (let n = e; n; n = n.parentElement) stack.push(parse(getComputedStyle(n).backgroundColor));
          let acc = {r:0,g:0,b:0,a:1};
          for (let i = stack.length - 1; i >= 0; i--) acc = over(stack[i], acc);
          return acc;
        }
        const sels = ['body', '.turn-assistant .turn-content', '.turn-user .turn-content', '.turn-role', '.turn-time',
          '.turn-meta', '.state-badge', '.citations-title', '.citations li span', '.sidebar-note', '.storage-notice',
          '.session-list .open', '.row.active .open', '#new-session-btn', '#export-btn', '#clear-history-btn',
          '.drawer summary', '.drawer label', '.sidebar-tools label', '.copy-btn', '.control-label',
          '.segmented .seg', '.segmented .seg[aria-pressed="true"]', '.topbar h1', '.brand-sub', '.avatar',
          '.turn-content code', '.example', '.state-chip', '.chip', '.sidebar-heading', '.composer-hint', '#health-badge .badge'];
        const out = [];
        for (const sel of sels) {
          document.querySelectorAll(sel).forEach((e, i) => {
            if (i > 0 || !e.getClientRects().length) return;
            const cs = getComputedStyle(e);
            const bg = bgOf(e);
            const fg = over(parse(cs.color), bg);
            const l1 = lum(fg), l2 = lum(bg);
            const ratio = (Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05);
            const size = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight) >= 700;
            const need = (size >= 24 || (size >= 18.66 && bold)) ? 3 : 4.5;
            if (ratio < need) out.push(sel + ' ' + ratio.toFixed(2) + ' < ' + need);
          });
        }
        for (const sel of ['#message', '#session-filter']) {
          const e = document.querySelector(sel); const ph = getComputedStyle(e, '::placeholder');
          const bg = bgOf(e); const fg = over(parse(ph.color), bg);
          const ratio = (Math.max(lum(fg),lum(bg))+0.05)/(Math.min(lum(fg),lum(bg))+0.05);
          if (ratio < 4.5) out.push(sel + '::placeholder ' + ratio.toFixed(2));
        }
        return out;
        """
    )
    assert failures == [], failures


_STORE_BARRIER = """
window.__storeAttempts = [];
window.__holdStore = arguments[0];
const original = navigator.locks.request.bind(navigator.locks);
navigator.locks.request = (name, ...args) => {
  if (name !== 'mainframe-rag-store') return original(name, ...args);
  // Both contenders observe the same version before either mutation saves.
  const attempt = {before: localStorage.getItem('mainframe_rag_sessions'), acquired: false};
  window.__storeAttempts.push(attempt);
  const callback = args.pop();
  return original(name, ...args, async lock => {
    attempt.acquired = true;
    if (window.__holdStore) {
      window.__holdStore = false;
      await new Promise(resolve => { window.__releaseStore = resolve; });
    }
    return callback(lock);
  });
};
"""


@pytest.mark.parametrize("action", ["rename", "delete"])
def test_barrier_serializes_cross_tab_mutation_against_stream_completion(console, page, action):
    tab_a = page.handle()
    console.llm.queue(("token", "Concurrent answer. "), ("gate", "store-race"), ("done",))
    send(page, "Concurrent incident")
    page.wait("return !!document.querySelector('[data-state=streaming] .md p')")
    tab_b = page.new_tab()
    page.switch(tab_b)
    page.get(console.url)
    page.wait("return document.querySelectorAll('.turn-user').length==1")
    snapshot = page.eval("return localStorage.getItem(arguments[0])", STORE_KEY)
    page.eval(_STORE_BARRIER, True)
    if action == "rename":
        page.eval("document.querySelector('.row .ren').click()")
        page.wait("return !!document.querySelector('input.rename')")
        page.eval("document.querySelector('input.rename').value='Renamed under barrier'")
        page.keys("ENTER")
    else:
        page.eval("document.querySelector('.row .del').click()")
    page.wait("return window.__storeAttempts.some(a=>a.acquired) && !!window.__releaseStore")
    assert page.eval("return localStorage.getItem(arguments[0])", STORE_KEY) == snapshot

    page.switch(tab_a)
    page.eval(_STORE_BARRIER, False)
    console.llm.release("store-race")
    page.wait("return window.__storeAttempts.length>0")
    attempts = page.eval("return window.__storeAttempts")
    assert attempts[0]["before"] == snapshot and attempts[0]["acquired"] is False
    assert page.eval("return localStorage.getItem(arguments[0])", STORE_KEY) == snapshot
    page.switch(tab_b)
    page.eval("window.__releaseStore()")
    page.switch(tab_a)
    if action == "rename":
        wait_state(page, "complete")
        (session,) = store(page)["sessions"].values()
        assert session["title"] == "Renamed under barrier"
        assert [turn["role"] for turn in session["turns"]] == ["user", "assistant"]
        assert "Concurrent answer" in session["turns"][1]["content"]
    else:
        page.wait("return document.getElementById('console-status').textContent.includes('discarded')")
        assert "Concurrent incident" not in json.dumps(store(page))
        assert "Concurrent answer" not in json.dumps(store(page))
    send(page, "Next ordinary question")
    wait_state(page, "complete", 2 if action == "rename" else 1)


def test_overlapping_cross_tab_sends_are_refused_until_first_turn_is_persisted(console, page):
    tab_a = page.handle()
    send(page, "Initial turn")
    wait_state(page, "complete")
    tab_b = page.new_tab()
    page.switch(tab_b)
    page.get(console.url)
    page.wait("return document.querySelectorAll('.turn').length==2")
    page.switch(tab_a)
    console.llm.queue(("token", "First overlapping answer. "), ("gate", "turn-race"), ("done",))
    send(page, "First overlapping question")
    page.wait("return !!document.querySelector('[data-state=streaming] .md p')")
    before = len(console.llm.requests)
    page.switch(tab_b)
    send(page, "Second overlapping question")
    wait_status(page, "already generating an answer in another tab")
    assert page.eval("var n=document.getElementById('composer-notice');return !n.hidden && n.getBoundingClientRect().height>0 && n.innerText.includes('already generating an answer in another tab')")
    assert len(console.llm.requests) == before
    (session,) = store(page)["sessions"].values()
    assert [turn["content"] for turn in session["turns"] if turn["role"] == "user"] == [
        "Initial turn", "First overlapping question",
    ]
    assert page.eval("return document.getElementById('message').value") == "Second overlapping question"
    console.llm.release("turn-race")
    page.wait("return document.querySelectorAll('.turn-assistant[data-state=complete]').length==2")
    page.click("#send-btn")
    wait_state(page, "complete", 3)
    assert page.eval("return document.getElementById('composer-notice').hidden")
    (session,) = store(page)["sessions"].values()
    assert [turn["role"] for turn in session["turns"]] == ["user", "assistant"] * 3
    assert [turn["content"] for turn in session["turns"] if turn["role"] == "user"] == [
        "Initial turn", "First overlapping question", "Second overlapping question",
    ]
    assert "First overlapping answer" in session["turns"][3]["content"]
    assert len(console.llm.requests) == before + 1


def test_missing_web_locks_uses_visible_tab_memory_fallback(console, browser):
    browser.init_script("Object.defineProperty(navigator, 'locks', {value: undefined});")
    browser.get(console.url)
    browser.wait("return !!document.querySelector('#messages .empty-state')")
    assert browser.eval("return !document.getElementById('storage-notice').hidden")
    send(browser, "Only this tab owns it")
    wait_state(browser, "complete")
    assert "Only this tab owns it" not in json.dumps(store(browser))
    browser.get(console.url)
    browser.wait("return !!document.querySelector('#messages .empty-state')")


@pytest.mark.parametrize("fallback", ["quota", "missing_locks"])
def test_clear_all_erases_previous_persisted_data_after_memory_fallback(console, page, fallback):
    send(page, "Persisted before fallback")
    wait_state(page, "complete")
    assert "Persisted before fallback" in json.dumps(store(page))
    if fallback == "quota":
        page.eval("""
        const write = Storage.prototype.setItem;
        Storage.prototype.setItem = function(key, value) {
          if (key === 'mainframe_rag_sessions') throw new DOMException('Quota', 'QuotaExceededError');
          return write.call(this, key, value);
        };
        """)
        send(page, "Memory after quota failure")
        wait_state(page, "complete", 2)
    else:
        page.init_script("Object.defineProperty(navigator, 'locks', {value: undefined});")
        page.get(console.url)
        page.wait("return !!document.querySelector('#messages .empty-state')")
    assert page.eval("return !document.getElementById('storage-notice').hidden")
    page.click("#clear-history-btn")
    page.click("#clear-history-btn")
    wait_status(page, "All saved incidents were erased")
    persisted = page.eval("return localStorage.getItem(arguments[0])", STORE_KEY)
    assert "Persisted before fallback" not in (persisted or "")
    assert "Memory after quota failure" not in (persisted or "")
    page.get(console.url)
    page.wait("return !!document.querySelector('#messages .empty-state')")
    assert "Persisted before fallback" not in page.eval("return document.body.innerText")
    send(page, "Next after erase")
    wait_state(page, "complete")
