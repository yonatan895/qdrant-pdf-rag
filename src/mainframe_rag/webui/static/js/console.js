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
  const REASONING_KEY = "mainframe_rag_reasoning_effort";
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

  const JCL_CARD_RE = /^\/\//;
  const JCL_DD_DATA_RE = /^\/\/\S+\s+DD\s+(\*|DATA)(?=[\s,]|$)/i;
  const REXX_HEADER_RE = /\/\*\s*rexx/i;
  const REXX_KEYWORD_RE = /^\s*(say|do|end|parse|pull|push|queue|exit|return|address|trace|signal|call|select|when|otherwise|nop|drop|interpret)\b/i;
  const REXX_ASSIGN_RE = /^\s*[A-Za-z_][\w.]*\s*=[^=]/;

  function detectCodeRegion(text) {
    const lines = text.split("\n").filter((l) => l.trim().length > 0);
    if (!lines.length) return null;
    const stripped = lines.map((l) => l.trimStart());
    if (stripped.filter((l) => JCL_CARD_RE.test(l)).length / lines.length >= 0.6) {
      return "jcl";
    }
    if (stripped.some((l) => JCL_DD_DATA_RE.test(l))) {
      return "jcl";
    }
    if (REXX_HEADER_RE.test(text) || lines.some((l) => (l.match(/\/\*/g) || []).length > (l.match(/\*\//g) || []).length)) {
      return "rexx";
    }
    const kwLines = lines.filter((l) => REXX_KEYWORD_RE.test(l)).length;
    if (kwLines >= 2 && (lines.some((l) => REXX_ASSIGN_RE.test(l)) || text.includes(";"))) {
      return "rexx";
    }
    if (lines.filter((l) => /^\s/.test(l)).length / lines.length >= 0.6) {
      return "console";
    }
    return null;
  }

  const JCL_KEYWORDS = [
    "command", "cntl", "dd", "else", "endcntl", "endif", "exec", "if",
    "include", "jcllib", "job", "output", "pend", "proc", "set", "then", "xmit",
    "avgrec", "blksize", "catlg", "class", "cond", "contig", "copies", "cyl",
    "dataclas", "dcb", "delete", "dest", "disp", "dsn", "dsname", "dummy",
    "expdt", "free", "hold", "keep", "label", "like", "lrecl", "mgmtclas",
    "mod", "msgclass", "msglevel", "new", "notify", "old", "parm", "pass",
    "password", "pgm", "recfm", "refdd", "region", "restart", "retpd", "rlse",
    "shr", "space", "storclas", "subsys", "sysout", "term", "time", "trk",
    "typrun", "uncatlg", "unit", "user", "vol", "volume"
  ].sort((a, b) => b.length - a.length);

  const REXX_KEYWORDS = [
    "address", "arg", "by", "call", "digits", "do", "drop", "else", "end",
    "exit", "expose", "for", "forever", "form", "fuzz", "if", "interpret",
    "iterate", "leave", "nop", "numeric", "options", "otherwise", "parse",
    "procedure", "pull", "push", "queue", "return", "say", "select", "signal",
    "then", "to", "trace", "until", "upper", "value", "var", "when", "while", "with",
    "abbrev", "center", "centre", "copies", "c2d", "c2x", "datatype", "date",
    "delstr", "delword", "d2c", "d2x", "errortext", "format", "insert",
    "lastpos", "left", "length", "linein", "lineout", "lines", "overlay",
    "pos", "queued", "random", "reverse", "right", "sourceline", "space",
    "strip", "substr", "subword", "symbol", "time", "translate", "trunc",
    "verify", "word", "wordindex", "wordlength", "wordpos", "words", "x2c", "x2d"
  ].sort((a, b) => b.length - a.length);

  const JCL_TOKEN = new RegExp(
    "(^[ \\t]*\\/\\/\\*.*$)" +
    "|('(?:''|[^'\\n])*')" +
    "|(\\b(?:" + JCL_KEYWORDS.join("|") + ")\\b)" +
    "|(\\b\\d+\\b)",
    "gmi"
  );

  const REXX_TOKEN = new RegExp(
    "(\\/\\*[\\s\\S]*?(?:\\*\\/|$))" +
    "|('(?:''|[^'\\n])*'|\"(?:\"\"|[^\"\\n])*\")" +
    "|(\\b(?:" + REXX_KEYWORDS.join("|") + ")\\b)" +
    "|(\\b\\d+(?:\\.\\d+)?\\b)",
    "gi"
  );

  function tokenizeCode(code, lang, parent) {
    const re = lang === "jcl" ? JCL_TOKEN : (lang === "rexx" ? REXX_TOKEN : null);
    if (!re) {
      parent.appendChild(document.createTextNode(code));
      return;
    }
    re.lastIndex = 0;
    let last = 0;
    let m = re.exec(code);
    while (m !== null) {
      if (m.index > last) {
        parent.appendChild(document.createTextNode(code.slice(last, m.index)));
      }
      let kind = null;
      if (m[1] !== undefined) kind = "tok-comment";
      else if (m[2] !== undefined) kind = "tok-string";
      else if (m[3] !== undefined) kind = "tok-keyword";
      else if (m[4] !== undefined) kind = "tok-number";

      if (!kind) {
        parent.appendChild(document.createTextNode(m[0]));
      } else {
        parent.appendChild(el("span", kind, m[0]));
      }
      last = m.index + m[0].length;
      m = re.exec(code);
    }
    if (last < code.length) {
      parent.appendChild(document.createTextNode(code.slice(last)));
    }
  }

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
        let lang = fence[1] ? fence[1].toLowerCase() : null;
        const body = [];
        i += 1;
        while (i < lines.length && !MD_FENCE.test(lines[i])) {
          body.push(lines[i]);
          i += 1;
        }
        i += 1; // consume the closing fence, or run off the end (unclosed)
        const rawCode = body.join("\n");
        if (!lang) {
          lang = detectCodeRegion(rawCode);
        }
        const pre = el("pre");
        pre.appendChild(el("button", "copy-btn", "Copy"));
        pre.lastChild.type = "button";
        const code = el("code", lang ? "language-" + lang : null);
        if (lang === "jcl" || lang === "rexx") {
          tokenizeCode(rawCode, lang, code);
        } else {
          code.textContent = rawCode;
        }
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

  function renderMeta(turn) {
    /* Per-turn receipt from the final payload (TTFT, cite count, tokens);
     * persisted on the turn so restored sessions keep it. Absent on old
     * turns and user turns — no footer then. */
    const meta = turn.meta;
    if (!meta) return null;
    const foot = el("div", "turn-meta");
    if (meta.stopped) {
      foot.textContent = "Stopped — partial answer kept";
      return foot;
    }
    const parts = [];
    if (typeof meta.ttft_ms === "number") parts.push("TTFT " + (meta.ttft_ms / 1000).toFixed(1) + "s");
    if (typeof meta.citations === "number") {
      parts.push(meta.citations + (meta.citations === 1 ? " citation" : " citations"));
    }
    if (typeof meta.tokens === "number") parts.push(meta.tokens + " tokens");
    foot.textContent = parts.join(" · ");
    return foot;
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
    const copyTurn = el("button", "copy-btn copy-turn", "Copy");
    copyTurn.type = "button";
    head.appendChild(copyTurn);
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
    const meta = renderMeta(turn);
    if (meta) article.appendChild(meta);
    return article;
  }

  const EMPTY_EXAMPLES = [
    "What does message IEA500I mean?",
    "Explain abend S0C4",
    "Show JCL to run IEFBR14",
  ];

  const messagesEl = document.getElementById("messages");
  const sessionListEl = document.getElementById("session-list");
  const formEl = document.getElementById("chat-form");
  const promptEl = document.getElementById("message");
  const splunkEl = document.getElementById("splunk-context");
  const productEl = document.getElementById("product");
  const versionEl = document.getElementById("version");
  const themeSelect = document.getElementById("theme-select");
  const sendBtn = document.getElementById("send-btn");
  const filterEl = document.getElementById("session-filter");

  /* Streaming UX state (P3): at most one in-flight turn. The Send button
   * doubles as Stop while streaming; aborts keep partial content. */
  let streamAbort = null;
  let sessionFilter = "";

  function setStreaming(active) {
    if (promptEl) {
      // The Stop glyph sits on a submit button, but after send the field
      // is empty + required — native validation would eat the click
      // ("Please fill in this field") before our handler runs and the
      // abort would never fire. Drop it while streaming; the no-JS form
      // keeps native validation always.
      if (active) promptEl.removeAttribute("required");
      else promptEl.setAttribute("required", "");
    }
    if (sendBtn) {
      // Glyphs carry the state (▲ send / ■ stop); the accessible name
      // carries the meaning for assistive tech.
      sendBtn.textContent = active ? "■" : "▲";
      sendBtn.setAttribute("aria-label", active ? "Stop" : "Send");
      sendBtn.classList.toggle("stop", active);
    }
  }

  /* Sticky autoscroll: follow the stream only while the operator is already
   * near the bottom; a manual scroll-up parks the view until they return. */
  let stick = true;
  function nearBottom() {
    const root = document.documentElement;
    return root.scrollHeight - window.scrollY - window.innerHeight < 96;
  }
  window.addEventListener("scroll", () => {
    stick = nearBottom();
  }, { passive: true });

  function stickScroll() {
    if (stick) window.scrollTo(0, document.documentElement.scrollHeight);
  }

  function renderMessages(store) {
    const session = activeSession(store);
    messagesEl.replaceChildren();
    if (!session.turns.length) {
      const empty = el("div", "empty-state");
      empty.appendChild(el("p", null, "Describe the abend, message ID, or procedure — answers cite the manual."));
      EMPTY_EXAMPLES.forEach((text) => {
        const ex = el("button", "example", text);
        ex.type = "button";
        ex.addEventListener("click", () => {
          promptEl.value = text;
          promptEl.focus();
        });
        empty.appendChild(ex);
      });
      messagesEl.appendChild(empty);
    }
    session.turns.forEach((turn) => messagesEl.appendChild(renderTurn(turn)));
    stick = true;
    messagesEl.scrollIntoView({ block: "end" });
  }

  function startRename(store, id, row, openBtn) {
    const session = store.sessions[id];
    if (!session) return;
    const input = document.createElement("input");
    input.value = session.title || "";
    input.className = "rename";
    input.maxLength = 60;
    input.setAttribute("aria-label", "Rename incident");
    row.replaceChild(input, openBtn);
    input.focus();
    input.select();
    let done = false;
    const commit = (save) => {
      if (done) return;
      done = true;
      if (save && input.value.trim()) {
        session.title = input.value.trim().slice(0, 60);
        session.updated_at = Date.now();
        saveStore(store);
      }
      renderSessions(store);
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") commit(true);
      else if (event.key === "Escape") commit(false);
    });
    input.addEventListener("blur", () => commit(true));
  }

  function renderSessions(store) {
    sessionListEl.replaceChildren();
    const needle = sessionFilter.trim().toLowerCase();
    Object.keys(store.sessions)
      .filter((id) => !needle || (store.sessions[id].title || "").toLowerCase().includes(needle))
      .sort((a, b) => (store.sessions[b].updated_at || 0) - (store.sessions[a].updated_at || 0))
      .forEach((id) => {
        const session = store.sessions[id];
        const row = el("li", "row" + (id === store.active ? " active" : ""));
        const open = el("button", "open", session.title || "New Incident");
        open.type = "button";
        open.title = "Open incident (double-click to rename)";
        open.addEventListener("click", () => {
          store.active = id;
          saveStore(store);
          renderMessages(store);
          renderSessions(store);
        });
        open.addEventListener("dblclick", () => {
          startRename(store, id, row, open);
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

  function getReasoningEffort() {
    const checked = document.querySelector('input[name="reasoning_effort"]:checked');
    return checked ? checked.value : "low";
  }

  function setReasoningEffort(effort) {
    const valid = ["low", "medium", "high"];
    const target = valid.includes(effort) ? effort : "low";
    const radio = document.querySelector('input[name="reasoning_effort"][value="' + target + '"]');
    if (radio) radio.checked = true;
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
    const assistantTurn = { role: "assistant", content: "", citations: [], ts: Date.now() };
    const article = renderTurn(assistantTurn);
    messagesEl.appendChild(article);
    const contentEl = article.querySelector(".turn-content");
    contentEl.appendChild(el("span", "thinking", "Thinking…"));
    stickScroll();

    const controller = new AbortController();
    streamAbort = controller;
    setStreaming(true);
    let failed = false;
    let stopped = false;
    let started = false;
    let finalPayload = null;
    let frameBuffer = "";

    try {
      const payload = {
        messages: historyMessages(session),
        splunk_context: userTurn.splunk_context || null,
        product: productEl.value.trim() || null,
        version: versionEl.value.trim() || null,
        reasoning_effort: getReasoningEffort(),
      };
      const response = await fetch("/ui/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal,
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
              if (!started) {
                started = true;
                contentEl.replaceChildren();
              }
              assistantTurn.content += eventPayload.delta || "";
              contentEl.replaceChildren(renderMarkdown(assistantTurn.content));
              stickScroll();
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
      if (err && err.name === "AbortError") stopped = true;
      else failed = true;
    } finally {
      streamAbort = null;
      setStreaming(false);
    }

    if (stopped && !assistantTurn.content) {
      // Stopped before the first token: leave no husk behind.
      article.remove();
      return;
    }
    if (failed || (!stopped && !finalPayload)) {
      assistantTurn.error = true;
      assistantTurn.content = assistantTurn.content || ERROR_TEXT;
      contentEl.replaceChildren(renderMarkdown(assistantTurn.content));
      article.classList.add("turn-error");
      return;
    }

    if (finalPayload) {
      assistantTurn.content = finalPayload.answer || assistantTurn.content;
      if (finalPayload.script) {
        // Tagged script fences leave the answer body during citation parsing;
        // render them with their threaded language tag (issue #337), falling
        // back to unlabeled when None, mirroring the server fragment.
        const tag = finalPayload.script_lang || "";
        assistantTurn.content += "\n\n```" + tag + "\n" + finalPayload.script + "\n```";
      }
      assistantTurn.citations = finalPayload.citations || [];
      assistantTurn.meta = {
        ttft_ms: typeof finalPayload.ttft_ms === "number" ? finalPayload.ttft_ms : null,
        citations: assistantTurn.citations.length,
        tokens:
          finalPayload.usage && typeof finalPayload.usage.total_tokens === "number"
            ? finalPayload.usage.total_tokens
            : null,
        stopped: false,
      };
    } else {
      assistantTurn.meta = { stopped: true };
    }
    assistantTurn.ts = Date.now();
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
    const meta = renderMeta(assistantTurn);
    if (meta) article.appendChild(meta);
    stickScroll();
    session.turns.push(assistantTurn);
    session.updated_at = Date.now();
    saveStore(store);
    renderSessions(store);
  }

  async function onSubmit(event) {
    event.preventDefault();
    event.stopImmediatePropagation();
    // The Send button doubles as Stop while a turn streams.
    if (streamAbort) {
      streamAbort.abort();
      return;
    }
    const text = promptEl.value.trim();
    if (!text) return;
    const store = loadStore();
    const session = activeSession(store);
    const userTurn = {
      role: "user",
      content: text,
      splunk_context: splunkEl.value.trim() || null,
      ts: Date.now(),
    };
    session.turns.push(userTurn);
    if (session.title === "New Incident") {
      session.title = text.split("\n")[0].slice(0, 45);
    }
    session.updated_at = Date.now();
    saveStore(store);
    // A fresh question re-engages the follow; renderMessages resets stick.
    const empty = messagesEl.querySelector(".empty-state");
    if (empty) empty.remove();
    messagesEl.appendChild(renderTurn(userTurn));
    promptEl.value = "";
    stick = true;
    stickScroll();
    await streamTurn(store, session, userTurn);
  }

  /* Copy buttons read from the adjacent rendered node — no payload ever
   * travels in attributes, so there is nothing to escape. One delegated
   * listener covers streamed, restored, and server-fragment turns alike. */
  function copyFromButton(button) {
    let text = "";
    if (button.classList.contains("copy-turn")) {
      // Whole-turn copy: the rendered plain text of the message card.
      const turn = button.closest(".turn");
      const content = turn ? turn.querySelector(".turn-content") : null;
      text = content ? content.textContent : "";
    } else {
      const pre = button.closest("pre");
      if (pre) {
        const code = pre.querySelector("code");
        text = code ? code.textContent : pre.textContent;
      } else {
        const li = button.closest("li");
        const label = li ? li.querySelector("span") : null;
        text = label ? label.textContent : "";
      }
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

    try {
      const savedEffort = window.localStorage.getItem(REASONING_KEY);
      if (savedEffort) setReasoningEffort(savedEffort);
    } catch (err) {
      /* storage loss is not an error */
    }

    document.querySelectorAll('input[name="reasoning_effort"]').forEach((radio) => {
      radio.addEventListener("change", () => {
        try {
          window.localStorage.setItem(REASONING_KEY, radio.value);
        } catch (err) {
          /* storage loss is not an error */
        }
      });
    });

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

    if (promptEl) {
      promptEl.addEventListener("keydown", (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
          event.preventDefault();
          formEl.requestSubmit();
        }
      });
    }

    if (filterEl) {
      filterEl.addEventListener("input", () => {
        sessionFilter = filterEl.value;
        renderSessions(loadStore());
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
