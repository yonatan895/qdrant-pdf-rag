/* Operator console client state + streaming (ADR-0004).
 *
 * The browser owns the conversation: sessions and turns live in localStorage
 * under `mainframe_rag_sessions`, capped at MAX_SAVED_SESSIONS with oldest
 * inactive eviction, and degrade to an in-memory store when storage is
 * unavailable (privacy mode / quota). Answers stream from
 * POST /ui/chat/stream over the repo SSE contract (`event: token` deltas then
 * exactly one `event: final`; `event: error` and no final means failure).
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

  function renderTurn(turn) {
    const article = el("article", "turn turn-" + turn.role);
    if (turn.error) article.classList.add("turn-error");
    if (turn.splunk_context) {
      const details = el("details", "turn-context");
      details.appendChild(el("summary", null, "Attached incident context"));
      details.appendChild(el("pre", null, turn.splunk_context));
      article.appendChild(details);
    }
    article.appendChild(el("div", "turn-role", turn.role === "user" ? "Operator" : "Copilot"));
    article.appendChild(el("pre", "turn-content", turn.content));
    if (turn.citations && turn.citations.length) {
      const box = el("div", "citations");
      box.appendChild(el("div", "citations-title", "Verified manual citations"));
      const list = el("ul");
      turn.citations.forEach((cite) => list.appendChild(el("li", null, cite)));
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
              contentEl.textContent = assistantTurn.content;
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
      contentEl.textContent = assistantTurn.content;
      article.classList.add("turn-error");
      return;
    }

    assistantTurn.content = finalPayload.answer || assistantTurn.content;
    assistantTurn.citations = finalPayload.citations || [];
    contentEl.textContent = assistantTurn.content;
    if (assistantTurn.citations.length) {
      const box = el("div", "citations");
      box.appendChild(el("div", "citations-title", "Verified manual citations"));
      const list = el("ul");
      assistantTurn.citations.forEach((cite) => list.appendChild(el("li", null, cite)));
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

  function download(text, filename) {
    const blob = new Blob([text], { type: "text/markdown" });
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
    const store = loadStore();
    activeSession(store);
    saveStore(store);
    renderMessages(store);
    renderSessions(store);

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
