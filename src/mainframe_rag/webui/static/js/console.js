/* Operator console client state + streaming (ADR-0004).
 *
 * The browser owns the conversation: sessions and turns live in localStorage
 * under `mainframe_rag_sessions`, capped at MAX_SAVED_SESSIONS with oldest
 * inactive eviction, and degrade to an in-memory store when storage is
 * unavailable (privacy mode / quota). Answers stream from
 * POST /ui/chat/stream over the repo SSE contract (`event: token` deltas then
 * exactly one `event: final`; `event: error` and no final means failure).
 *
 * Assistant turns render the same safe markdown subset as the server
 * (routes.render_markdown_subset): headings, bold/italic, code spans,
 * fenced blocks, unordered/ordered lists — built as DOM nodes via
 * textContent, never innerHTML, so hostile markup stays inert.
 *
 * No inline handlers or eval: the strict CSP (`script-src 'self'`) applies.
 */
"use strict";

(function () {
  const STORAGE_KEY = "mainframe_rag_sessions";
  const THEME_KEY = "mainframe_rag_theme";
  const MAX_SAVED_SESSIONS = 30;
  const ERROR_TEXT = "The reasoning agent could not complete this request. Check the agent logs and retry.";

  const memory = { sessions: {}, active: null };
  let storageOk = true;
  try {
    const probe = "__mainframe_rag_probe__";
    window.localStorage.setItem(probe, "1");
    window.localStorage.removeItem(probe);
  } catch (err) {
    storageOk = false;
  }

  function emptyStore() {
    return { sessions: {}, active: null };
  }

  function loadStore() {
    if (!storageOk) return memory;
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return emptyStore();
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object" || typeof parsed.sessions !== "object") {
        return emptyStore();
      }
      return parsed;
    } catch (err) {
      return emptyStore();
    }
  }

  function saveStore(store) {
    if (!storageOk) {
      memory.sessions = store.sessions;
      memory.active = store.active;
      return;
    }
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
    } catch (err) {
      storageOk = false;
      memory.sessions = store.sessions;
      memory.active = store.active;
    }
  }

  function newSessionId() {
    return "inc-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
  }

  function evictOldest(store) {
    const ids = Object.keys(store.sessions);
    if (ids.length <= MAX_SAVED_SESSIONS) return;
    const inactive = ids
      .filter((id) => id !== store.active)
      .sort((a, b) => (store.sessions[a].updated_at || 0) - (store.sessions[b].updated_at || 0));
    while (Object.keys(store.sessions).length > MAX_SAVED_SESSIONS && inactive.length) {
      delete store.sessions[inactive.shift()];
    }
  }

  function createSession(store) {
    const id = newSessionId();
    store.sessions[id] = { title: "New Incident", updated_at: Date.now(), turns: [] };
    store.active = id;
    evictOldest(store);
    saveStore(store);
    return id;
  }

  function activeSession(store) {
    let id = store.active;
    if (!id || !store.sessions[id]) {
      const ids = Object.keys(store.sessions);
      id = ids.length ? ids.sort((a, b) => store.sessions[b].updated_at - store.sessions[a].updated_at)[0] : createSession(store);
    }
    return store.sessions[id];
  }

  function historyContent(turn) {
    if (turn.role === "assistant" && turn.citations && turn.citations.length) {
      return turn.content + "\n\nCitations:\n" + turn.citations.map((c) => "- " + c).join("\n");
    }
    return turn.content;
  }

  function historyMessages(session) {
    return session.turns.map((t) => ({ role: t.role, content: historyContent(t) }));
  }

  function markdownReport(session) {
    const lines = ["# Incident Analysis: " + session.title, "", "---", ""];
    session.turns.forEach((turn) => {
      lines.push("### " + (turn.role === "user" ? "Operator" : "Mainframe Copilot"));
      if (turn.splunk_context) {
        lines.push("```text");
        lines.push("--- Attached Incident Context ---");
        lines.push(turn.splunk_context);
        lines.push("```");
        lines.push("");
      }
      lines.push(turn.content, "");
      if (turn.citations && turn.citations.length) {
        lines.push("**Verified citations:**");
        turn.citations.forEach((c) => lines.push("- " + c));
        lines.push("");
      }
      lines.push("---", "");
    });
    return lines.join("\n");
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* Safe markdown subset, mirroring routes.render_markdown_subset. Every
   * string enters the DOM through textContent — no innerHTML anywhere —
   * so the only elements that can exist are the ones built here. */
  const MD_HEADING = /^(#{1,4})\s+(.*?)\s*$/;
  const MD_FENCE = /^ {0,3}```([\w+-]*)\s*$/;
  const MD_UL = /^ {0,3}[-*]\s+(.*)$/;
  const MD_OL = /^ {0,3}\d+[.)]\s+(.*)$/;
  const MD_INLINE = /(`[^`\n]+?`)|(\*\*(.+?)\*\*)|((?<!\*)\*([^*\n]+?)\*(?!\*))/g;

  function mdInline(text, parent) {
    let last = 0;
    MD_INLINE.lastIndex = 0;
    let match = MD_INLINE.exec(text);
    while (match) {
      if (match.index > last) parent.appendChild(document.createTextNode(text.slice(last, match.index)));
      if (match[1] !== undefined) {
        parent.appendChild(el("code", null, match[1].slice(1, -1)));
      } else if (match[2] !== undefined) {
        const strong = el("strong");
        strong.appendChild(document.createTextNode(match[3]));
        parent.appendChild(strong);
      } else {
        const em = el("em");
        em.appendChild(document.createTextNode(match[5]));
        parent.appendChild(em);
      }
      last = match.index + match[0].length;
      match = MD_INLINE.exec(text);
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function renderMarkdown(text) {
    const frag = document.createDocumentFragment();
    let para = [];
    let list = null;

    function flushPara() {
      if (para.length) {
        const p = el("p");
        mdInline(para.join(" "), p);
        frag.appendChild(p);
        para = [];
      }
    }
    function closeList() {
      if (list) {
        frag.appendChild(list);
        list = null;
      }
    }

    const lines = text.split("\n");
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const fence = MD_FENCE.exec(line);
      if (fence) {
        flushPara();
        closeList();
        const body = [];
        i += 1;
        while (i < lines.length && !MD_FENCE.test(lines[i])) {
          body.push(lines[i]);
          i += 1;
        }
        i += 1; // consume the closing fence, or run off the end (unclosed)
        const pre = el("pre");
        pre.appendChild(el("button", "copy-btn", "Copy"));
        pre.lastChild.type = "button";
        const code = el("code", fence[1] ? "language-" + fence[1] : null, body.join("\n"));
        pre.appendChild(code);
        frag.appendChild(pre);
        continue;
      }
      const heading = MD_HEADING.exec(line);
      if (heading) {
        flushPara();
        closeList();
        const h = el("h" + heading[1].length);
        mdInline(heading[2], h);
        frag.appendChild(h);
        i += 1;
        continue;
      }
      const ul = MD_UL.exec(line);
      const ol = ul ? null : MD_OL.exec(line);
      if (ul || ol) {
        flushPara();
        const kind = ul ? "ul" : "ol";
        if (!list || list.tagName.toLowerCase() !== kind) {
          closeList();
          list = el(kind);
        }
        const li = el("li");
        mdInline((ul || ol)[1], li);
        list.appendChild(li);
        i += 1;
        continue;
      }
      if (!line.trim()) {
        flushPara();
        closeList();
        i += 1;
        continue;
      }
      para.push(line.trim());
      i += 1;
    }
    flushPara();
    closeList();
    return frag;
  }

  function fmtTime(ts) {
    try {
      const d = new Date(typeof ts === "number" ? ts : Date.parse(ts));
      if (Number.isNaN(d.getTime())) return null;
      // Short zone label keeps local vs server-UTC unambiguous after
      // localizeTimes rewrites the server-stamped UTC text.
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
    } catch (err) {
      return null;
    }
  }

  function renderTurn(turn) {
    const article = el("article", "turn turn-" + turn.role);
    if (turn.error) article.classList.add("turn-error");
    if (turn.splunk_context) {
      const details = el("details", "turn-context");
      details.appendChild(el("summary", null, "Attached incident context"));
      details.appendChild(el("pre", null, turn.splunk_context));
      article.appendChild(details);
    }
    const head = el("div", "turn-head");
    const isUser = turn.role === "user";
    head.appendChild(el("span", "avatar", isUser ? "O" : "C"));
    head.firstChild.setAttribute("aria-hidden", "true");
    head.appendChild(el("div", "turn-role", isUser ? "Operator" : "Copilot"));
    // Stored turns (and server fragments) carry data-ts; fresh turns stamp now.
    const ts = turn.ts || Date.now();
    turn.ts = ts;
    const local = fmtTime(ts);
    if (local) {
      const time = el("time", "turn-time", local);
      time.setAttribute("data-ts", new Date(ts).toISOString());
      head.appendChild(time);
    }
    article.appendChild(head);
    if (turn.role === "assistant") {
      const content = el("div", "turn-content md");
      content.appendChild(renderMarkdown(turn.content));
      article.appendChild(content);
    } else {
      article.appendChild(el("pre", "turn-content", turn.content));
    }
    if (turn.citations && turn.citations.length) {
      const box = el("div", "citations");
      box.appendChild(el("div", "citations-title", "Verified manual citations (" + turn.citations.length + ")"));
      const list = el("ul");
      turn.citations.forEach((cite) => {
        const li = el("li");
        li.appendChild(el("span", null, cite));
        const copy = el("button", "copy-btn copy-cite", "Copy");
        copy.type = "button";
        li.appendChild(copy);
        list.appendChild(li);
      });
      box.appendChild(list);
      article.appendChild(box);
    }
    return article;
  }

  const messagesEl = document.getElementById("messages");
  const sessionListEl = document.getElementById("session-list");
  const formEl = document.getElementById("chat-form");
  const promptEl = document.getElementById("message");
  const splunkEl = document.getElementById("splunk-context");
  const productEl = document.getElementById("product");
  const versionEl = document.getElementById("version");
  const themeSelect = document.getElementById("theme-select");

  function renderMessages(store) {
    const session = activeSession(store);
    messagesEl.replaceChildren();
    session.turns.forEach((turn) => messagesEl.appendChild(renderTurn(turn)));
    messagesEl.scrollIntoView({ block: "end" });
  }

  function renderSessions(store) {
    sessionListEl.replaceChildren();
    Object.keys(store.sessions)
      .sort((a, b) => (store.sessions[b].updated_at || 0) - (store.sessions[a].updated_at || 0))
      .forEach((id) => {
        const session = store.sessions[id];
        const row = el("li", "row" + (id === store.active ? " active" : ""));
        const open = el("button", "open", session.title || "New Incident");
        open.type = "button";
        open.addEventListener("click", () => {
          store.active = id;
          saveStore(store);
          renderMessages(store);
          renderSessions(store);
        });
        const del = el("button", "del", "\u2715");
        del.type = "button";
        del.title = "Delete session";
        del.addEventListener("click", () => {
          delete store.sessions[id];
          if (store.active === id) store.active = null;
          activeSession(store);
          saveStore(store);
          renderMessages(store);
          renderSessions(store);
        });
        row.appendChild(open);
        row.appendChild(del);
        sessionListEl.appendChild(row);
      });
  }

  function applyTheme(theme) {
    document.body.className = theme === "theme-dark" ? "theme-dark" : "theme-3270";
    if (themeSelect) themeSelect.value = document.body.className;
  }

  function parseFrames(frame) {
    let name = "message";
    let data = "";
    frame.split("\n").forEach((line) => {
      if (line.startsWith("event: ")) name = line.slice(7).trim();
      else if (line.startsWith("data: ")) data += line.slice(6);
    });
    return { name: name, data: data };
  }

  async function streamTurn(store, session, userTurn) {
    const assistantTurn = { role: "assistant", content: "", citations: [], updated_at: Date.now() };
    const article = renderTurn(assistantTurn);
    messagesEl.appendChild(article);
    const contentEl = article.querySelector(".turn-content");

    let failed = false;
    let finalPayload = null;
    let frameBuffer = "";

    try {
      const payload = {
        messages: historyMessages(session),
        splunk_context: userTurn.splunk_context || null,
        product: productEl.value.trim() || null,
        version: versionEl.value.trim() || null,
      };
      const response = await fetch("/ui/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok || !response.body) throw new Error("HTTP " + response.status);

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        frameBuffer += decoder.decode(chunk.value, { stream: true });
        let boundary = frameBuffer.indexOf("\n\n");
        while (boundary !== -1) {
          const frame = frameBuffer.slice(0, boundary);
          frameBuffer = frameBuffer.slice(boundary + 2);
          const parsed = parseFrames(frame);
          if (parsed.data) {
            let eventPayload = null;
            try {
              eventPayload = JSON.parse(parsed.data);
            } catch (err) {
              eventPayload = null;
            }
            if (eventPayload && parsed.name === "token") {
              assistantTurn.content += eventPayload.delta || "";
              contentEl.replaceChildren(renderMarkdown(assistantTurn.content));
            } else if (eventPayload && parsed.name === "final") {
              finalPayload = eventPayload;
            } else if (eventPayload && parsed.name === "error") {
              failed = true;
            }
          }
          boundary = frameBuffer.indexOf("\n\n");
        }
      }
    } catch (err) {
      failed = true;
    }

    if (failed || !finalPayload) {
      assistantTurn.error = true;
      assistantTurn.content = assistantTurn.content || ERROR_TEXT;
      contentEl.replaceChildren(renderMarkdown(assistantTurn.content));
      article.classList.add("turn-error");
      return;
    }

    assistantTurn.content = finalPayload.answer || assistantTurn.content;
    if (finalPayload.script) {
      // Tagged script fences leave the answer body during citation parsing;
      // show them as one unlabeled fence, mirroring the server fragment.
      assistantTurn.content += "\n\n```\n" + finalPayload.script + "\n```";
    }
    assistantTurn.citations = finalPayload.citations || [];
    contentEl.replaceChildren(renderMarkdown(assistantTurn.content));
    if (assistantTurn.citations.length) {
      const box = el("div", "citations");
      box.appendChild(el("div", "citations-title", "Verified manual citations (" + assistantTurn.citations.length + ")"));
      const list = el("ul");
      assistantTurn.citations.forEach((cite) => {
        const li = el("li");
        li.appendChild(el("span", null, cite));
        const copy = el("button", "copy-btn copy-cite", "Copy");
        copy.type = "button";
        li.appendChild(copy);
        list.appendChild(li);
      });
      box.appendChild(list);
      article.appendChild(box);
    }
    session.turns.push(assistantTurn);
    session.updated_at = Date.now();
    saveStore(store);
    renderSessions(store);
  }

  async function onSubmit(event) {
    event.preventDefault();
    event.stopImmediatePropagation();
    const text = promptEl.value.trim();
    if (!text) return;
    const store = loadStore();
    const session = activeSession(store);
    const userTurn = {
      role: "user",
      content: text,
      splunk_context: splunkEl.value.trim() || null,
    };
    session.turns.push(userTurn);
    if (session.title === "New Incident") {
      session.title = text.split("\n")[0].slice(0, 45);
    }
    session.updated_at = Date.now();
    saveStore(store);
    messagesEl.appendChild(renderTurn(userTurn));
    promptEl.value = "";
    await streamTurn(store, session, userTurn);
  }

  /* Copy buttons read from the adjacent rendered node — no payload ever
   * travels in attributes, so there is nothing to escape. One delegated
   * listener covers streamed, restored, and server-fragment turns alike. */
  function copyFromButton(button) {
    let text = "";
    const pre = button.closest("pre");
    if (pre) {
      const code = pre.querySelector("code");
      text = code ? code.textContent : pre.textContent;
    } else {
      const li = button.closest("li");
      const label = li ? li.querySelector("span") : null;
      text = label ? label.textContent : "";
    }
    if (!text || !navigator.clipboard) return;
    navigator.clipboard.writeText(text).then(
      () => {
        button.textContent = "Copied";
        window.setTimeout(() => {
          button.textContent = "Copy";
        }, 1500);
      },
      () => {
        button.textContent = "Copy failed";
      }
    );
  }

  /* Server fragments stamp UTC; upgrade to the operator's local time.
   * Runs on boot (no-JS first paint) and after HTMX fragment swaps. */
  function localizeTimes(root) {
    (root || document).querySelectorAll("time[data-ts]").forEach((node) => {
      const local = fmtTime(node.getAttribute("data-ts"));
      if (local) node.textContent = local;
    });
  }

  function download(text, filename) {    const blob = new Blob([text], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  }

  function boot() {
    const store = loadStore();    activeSession(store);
    saveStore(store);
    renderMessages(store);
    renderSessions(store);
    localizeTimes(document);

    if (messagesEl) {
      messagesEl.addEventListener("click", (event) => {
        const button = event.target.closest ? event.target.closest(".copy-btn") : null;
        if (button && messagesEl.contains(button)) copyFromButton(button);
      });
    }
    // No-fetch fallback path renders server fragments via HTMX.
    document.body.addEventListener("htmx:afterSwap", (event) => {
      if (event.target) localizeTimes(event.target);
    });

    const theme = (function () {
      try {
        return window.localStorage.getItem(THEME_KEY) || "theme-3270";
      } catch (err) {
        return "theme-3270";
      }
    })();
    applyTheme(theme);
    if (themeSelect) {
      themeSelect.addEventListener("change", () => {
        applyTheme(themeSelect.value);
        try {
          window.localStorage.setItem(THEME_KEY, themeSelect.value);
        } catch (err) {
          /* theme is cosmetic; storage loss is not an error */
        }
      });
    }

    const exportBtn = document.getElementById("export-btn");
    if (exportBtn) {
      exportBtn.addEventListener("click", () => {
        const current = loadStore();
        const session = activeSession(current);
        download(markdownReport(session), "incident-" + (session.title || "session").replace(/[^\w.-]+/g, "_") + ".md");
      });
    }

    const newBtn = document.getElementById("new-session-btn");
    if (newBtn) {
      newBtn.addEventListener("click", () => {
        const current = loadStore();
        createSession(current);
        renderMessages(current);
        renderSessions(current);
      });
    }

    if (formEl) {
      if (window.fetch && window.ReadableStream) {
        formEl.removeAttribute("hx-post");
        formEl.removeAttribute("hx-target");
        formEl.removeAttribute("hx-swap");
        formEl.addEventListener("submit", onSubmit);
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
