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
 * fenced blocks, unordered/ordered lists (a plain line under an item
 * continues it), GFM pipe tables (issue #749) — built as DOM nodes via
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
  const STORE_LOCK = "mainframe-rag-store";
  const REVISION_MARK = "mainframe-rag-store-revision:";
  const STALE_WAIT_MS = 3000;
  const sharedLocks = navigator.locks && window.isSecureContext;
  const PROVISIONAL_LABEL = "Provisional — still generating, not verified";
  const ERROR_TEXT = "The reasoning agent could not complete this request. Check the agent logs and retry.";
  const EFFORT_ERROR_TEXT = "This deployment does not offer the selected reasoning effort. Reload the console and choose another level.";

  const memory = { sessions: {}, active: null };
  let storageOk = !!sharedLocks;
  // This tab's own current incident. `store.active` only records the last
  // incident used anywhere; two tabs must not fight over it.
  let currentId = null;
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

  /* The persisted store, or null when the key is absent or unreadable. */
  function readStored() {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object" || typeof parsed.sessions !== "object") return null;
      return parsed;
    } catch (err) {
      return null;
    }
  }

  function loadStore() {
    if (!storageOk) return memory;
    return readStored() || emptyStore();
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
      syncStorageNotice();
    }
  }

  function newSessionId() {
    return "inc-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
  }

  function evictOldest(store) {
    const ids = Object.keys(store.sessions);
    if (ids.length <= MAX_SAVED_SESSIONS) return;
    const inactive = ids
      .filter((id) => id !== store.active && id !== currentId)
      .sort((a, b) => (store.sessions[a].updated_at || 0) - (store.sessions[b].updated_at || 0));
    while (Object.keys(store.sessions).length > MAX_SAVED_SESSIONS && inactive.length) {
      delete store.sessions[inactive.shift()];
    }
  }

  function createSession(store) {
    const id = newSessionId();
    store.sessions[id] = { title: "New Incident", updated_at: Date.now(), turns: [] };
    store.active = id;
    currentId = id;
    evictOldest(store);
    return id;
  }

  /* Store revision. Each locked write bumps `store.revision` (absent = 0).
   * Chrome refreshes another tab's localStorage copy asynchronously, so the
   * stored revision can be as stale as the state. The writer therefore also
   * holds a lock named after its revision: the lock manager orders that name
   * before the store lock's release, so the next holder sees the newest
   * revision there and re-reads until its copy catches up. A copy still
   * behind after STALE_WAIT_MS fails closed: no write, a re-render and a
   * fixed notice. An absent store (site data cleared) proceeds. */
  let knownRevision = 0;
  let releaseMark = null;
  let staleNoticeShown = false;
  const STALE_TEXT = "Another tab changed your saved incidents; nothing was changed here. Try again.";
  const UNSAVED_TEXT =
    "This answer was not saved because another tab changed your saved incidents. " +
    "Copy it now if you need it; it disappears on your next change or when you leave this incident.";
  class StaleStoreError extends Error {}

  function revisionOf(store) {
    return Number.isSafeInteger(store.revision) && store.revision > 0 ? store.revision : 0;
  }

  function markRevision(revision) {
    return new Promise((marked) => {
      navigator.locks.request(REVISION_MARK + revision, { mode: "shared" }, () => new Promise((release) => {
        if (releaseMark) releaseMark();
        releaseMark = release;
        marked();
      }));
    });
  }

  async function freshStore() {
    let marks;
    try {
      marks = (await navigator.locks.query()).held
        .filter((lock) => lock.name.startsWith(REVISION_MARK))
        .map((lock) => Number(lock.name.slice(REVISION_MARK.length)) || 0);
    } catch (err) {
      throw new StaleStoreError();
    }
    const want = Math.max(knownRevision, ...marks);
    const deadline = Date.now() + STALE_WAIT_MS;
    let store = loadStore();
    while (storageOk && revisionOf(store) < want && Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 10));
      store = loadStore();
    }
    if (storageOk && revisionOf(store) < want && readStored()) throw new StaleStoreError();
    return { store, want };
  }

  /* Callers whose follow-up must not stop on a refused write. */
  function ignoreStale(err) {
    if (!(err instanceof StaleStoreError)) throw err;
  }

  /* The origin-wide Web Lock encloses both the fresh read and the write.
   * A tab without Web Locks uses its own memory store, so it cannot race a
   * shared read-modify-write. Every incident mutation uses this owner. */
  async function mutate(fn) {
    const apply = async () => {
      const { store, want } = sharedLocks ? await freshStore() : { store: loadStore(), want: 0 };
      const before = JSON.stringify(store);
      const result = fn(store);
      if (JSON.stringify(store) !== before) {
        store.revision = Math.max(revisionOf(store), want) + 1;
        saveStore(store);
        if (sharedLocks && storageOk) await markRevision(store.revision);
        unsaved = null;
        if (staleNoticeShown) {
          staleNoticeShown = false;
          composerNotice("");
        }
      }
      knownRevision = Math.max(knownRevision, revisionOf(store));
      return result;
    };
    try {
      return await (sharedLocks ? navigator.locks.request(STORE_LOCK, apply) : apply());
    } catch (err) {
      if (err instanceof StaleStoreError) {
        renderMessages();
        renderSessions();
        staleNoticeShown = true;
        composerNotice(STALE_TEXT);
      }
      throw err;
    }
  }

  /* Resolve this tab's current incident id inside `store`. `claim` also
   * records it as the persisted last-used incident; read-only renders do not
   * claim, so a cross-tab refresh never rewrites another tab's choice.
   * Creates an empty incident when none survive. */
  function resolveSession(store, claim) {
    let id = currentId && store.sessions[currentId] ? currentId : null;
    if (!id && store.active && store.sessions[store.active]) id = store.active;
    if (!id) {
      const ids = Object.keys(store.sessions);
      id = ids.length
        ? ids.sort((a, b) => (store.sessions[b].updated_at || 0) - (store.sessions[a].updated_at || 0))[0]
        : createSession(store);
    }
    currentId = id;
    if (claim) store.active = id;
    return id;
  }

  function currentSession() {
    const store = loadStore();
    const id = resolveSession(store);
    return { id: id, session: store.sessions[id] };
  }

  const INCOMPLETE_HISTORY_NOTE =
    "[This earlier answer was cut off before completion and was not verified; do not rely on it.]";

  function historyContent(turn) {
    let text = turn.content;
    if (turn.role === "assistant" && turn.citations && turn.citations.length) {
      text += "\n\nCitations:\n" + turn.citations.map((c) => "- " + c).join("\n");
    }
    // A cut-off answer reused as conversation context stays qualified for
    // the model exactly as it is for the operator (issue #372).
    if (turn.role === "assistant" && turn.verification_state === "generation_incomplete") {
      text += "\n\n" + INCOMPLETE_HISTORY_NOTE;
    }
    return text;
  }

  function historyMessages(session) {
    return session.turns.map((t) => ({ role: t.role, content: historyContent(t) }));
  }

  function markdownReport(session) {
    const lines = [
      "# Incident Analysis: " + session.title,
      "",
      "> Contains incident context and manual excerpts; handle per site data policy.",
      "",
      "---",
      "",
    ];
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
      if (turn.role === "assistant" && turn.verification_state) {
        lines.push("**Verification state:** " + turn.verification_state);
        lines.push("");
      }
      if (turn.script_review_required) {
        lines.push("**Human review required:** draft script below, not certified executable.");
        lines.push("");
      }
      if (turn.citations && turn.citations.length) {
        const citeLabel =
          turn.verification_state === "accepted" && !turn.citations_inferred
            ? "**Verified citations (source-matched to supplied excerpts; not a check that the excerpts support the answer):**"
            : "**Citations (" + (turn.verification_state || "unverified") + "):**";
        lines.push(citeLabel);
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

  /* Verification-state presentation (issue #365): one label table and one
   * citations-title rule shared by restored turns and streamed turns, so a
   * reload can never promote a draft to accepted. Titles mirror the server
   * fragment: only accepted, non-inferred cites read "Source-matched".
   * Accepted means each citation names a supplied excerpt, not that the
   * excerpt supports the claim (#838), so no label says "Verified". */
  const STATE_LABELS = {
    insufficient_evidence: "Insufficient evidence — not operational guidance",
    unverified_draft: "Unverified draft — no eligible citation",
    generation_incomplete: "Incomplete generation — output was cut off",
    accepted: "Accepted",
  };

  function citationsTitle(turn) {
    const n = turn.citations.length;
    if (turn.citations_inferred) {
      return "Inferred citations (" + n + ") — bracket provenance, not grounding";
    }
    if (turn.verification_state === "accepted") {
      return "Source-matched manual citations (" + n + ")";
    }
    const state = turn.verification_state ? turn.verification_state.replace(/_/g, " ") : "unverified";
    return "Citations (" + n + ") — " + state;
  }

  /* Warnings read before the answer they qualify: badges go directly above
   * the turn content, in the same order the server fragment renders them. */
  function stateBadges(article, turn) {
    const anchor = article.querySelector(".turn-content");
    const put = (node) => article.insertBefore(node, anchor);
    if (turn.role === "assistant" && turn.verification_state && turn.verification_state !== "accepted") {
      put(el("div", "state-badge state-" + turn.verification_state, STATE_LABELS[turn.verification_state] || turn.verification_state));
    }
    if (turn.script_review_required) {
      put(el("div", "state-badge state-review", "Human review required — draft script, not certified executable"));
    }
  }

  /* Header chip for an accepted, citation-backed answer (mirrors the server
   * fragment). Inferred or unverified citations never earn it. */
  function stateChip(article, turn) {
    if (turn.role !== "assistant" || turn.verification_state !== "accepted") return;
    if (!turn.citations || !turn.citations.length || turn.citations_inferred) return;
    const n = turn.citations.length;
    const chip = el("span", "state-chip state-chip-ok", "Source-matched \u00b7 " + n + (n === 1 ? " citation" : " citations"));
    const role = article.querySelector(".turn-head .turn-role");
    if (role) role.after(chip);
  }

  function citationsBox(turn) {
    if (!turn.citations || !turn.citations.length) return null;
    const box = el("div", "citations");
    box.appendChild(el("div", "citations-title", citationsTitle(turn)));
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
    if (!turn.citations_inferred) box.appendChild(el("div", "evidence-note citations-note", SOURCE_MATCH_NOTE));
    return box;
  }

  /* Supplied-excerpt view (issue #635). The excerpts the model actually
   * received for the CURRENT response only: held in memory, never written to
   * localStorage, never exported, gone on reload. Every string enters the DOM
   * through textContent, so excerpt markup renders as data. A citation is
   * source-matched (it names an excerpt that was supplied); that is not a
   * check that the excerpt semantically supports the claim. */
  const SOURCE_MATCH_NOTE =
    "Source-matched, not semantically checked: a citation here only means it matches an excerpt that was supplied for this answer. It does not confirm the excerpt supports the claim.";
  let liveEvidence = null;

  function evidenceLocation(item) {
    const parts = [];
    if (item.heading) parts.push(item.heading);
    if (item.page_label) parts.push("printed p. " + item.page_label);
    if (typeof item.pdf_page_start === "number") {
      const last = typeof item.pdf_page_end === "number" ? item.pdf_page_end : item.pdf_page_start;
      parts.push(last === item.pdf_page_start ? "PDF p. " + item.pdf_page_start : "PDF pp. " + item.pdf_page_start + "\u2013" + last);
    }
    return parts.length ? parts.join(" \u00b7 ") : "not recorded";
  }

  function evidenceCoverage(item) {
    let range = "Characters " + item.start_char + "\u2013" + item.end_char + " of " + item.source_chars + " in the stored chunk";
    if (!item.truncated_start && !item.truncated_end) return range + " (whole chunk supplied)";
    const cuts = [];
    if (item.truncated_start) cuts.push("earlier text left out");
    if (item.truncated_end) cuts.push("later text left out");
    return range + " (" + cuts.join("; ") + ")";
  }

  function evidenceItem(item) {
    const details = el("div", "evidence-item");
    details.setAttribute("data-index", String(item.index));
    details.appendChild(el("div", "evidence-head", "[" + item.index + "] " + item.citation));
    const unknown = "unknown";
    details.appendChild(
      el("div", "evidence-meta", "Source product: " + (item.product || unknown) + " \u00b7 version: " + (item.version || unknown))
    );
    details.appendChild(el("div", "evidence-loc", "Recorded location: " + evidenceLocation(item)));
    details.appendChild(el("div", "evidence-range", evidenceCoverage(item)));
    if (item.duplicate_citation) {
      details.appendChild(
        el("div", "evidence-warn", "Another supplied excerpt has this same citation text; entries are told apart by number and version, not by citation text.")
      );
    }
    if (item.text_clipped) {
      details.appendChild(el("div", "evidence-warn", "Displayed text is clipped to the response size limit."));
    }
    details.appendChild(el("pre", "evidence-text", item.text));
    return details;
  }

  function evidenceBox(turn) {
    if (turn.role !== "assistant" || turn.error || !turn.verification_state) return null;
    const box = el("div", "evidence");
    if (!liveEvidence || liveEvidence.ts !== turn.ts) {
      box.setAttribute("data-evidence", "unavailable");
      box.appendChild(
        el("div", "evidence-unavailable", "Supplied excerpts are not available for this response. They are shown only for the latest completed response in this tab, are never stored with earlier answers, and are not reconstructed by a new search.")
      );
      return box;
    }
    box.setAttribute("data-evidence", "current");
    const items = liveEvidence.items;
    const outer = el("details", "evidence-view");
    const incomplete = turn.verification_state === "generation_incomplete";
    outer.appendChild(
      el("summary", null, "Supplied excerpts (" + items.length + ")" + (incomplete ? " \u2014 incomplete generation" : ""))
    );
    const omitted = liveEvidence.omitted;
    outer.appendChild(
      el("div", "evidence-note", "Exactly what the model received for this response. " + (omitted > 0 ? omitted + " retrieved " + (omitted === 1 ? "excerpt was" : "excerpts were") + " omitted before the model call and " + (omitted === 1 ? "is" : "are") + " not shown. " : "") + SOURCE_MATCH_NOTE)
    );
    if (!items.length) outer.appendChild(el("div", "evidence-unavailable", "No excerpt was supplied."));
    items.forEach((item) => outer.appendChild(evidenceItem(item)));
    box.appendChild(outer);
    return box;
  }

  /* Safe markdown subset, mirroring routes.render_markdown_subset. Every
   * string enters the DOM through textContent — no innerHTML anywhere —
   * so the only elements that can exist are the ones built here. */
  const MD_HEADING = /^(#{1,4})\s+(.*?)\s*$/;
  const MD_FENCE = /^ {0,3}```([\w+-]*)\s*$/;
  const MD_UL = /^ {0,3}[-*]\s+(.*)$/;
  const MD_OL = /^ {0,3}\d+[.)]\s+(.*)$/;
  const MD_INLINE = /(`[^`\n]+?`)|(\*\*(.+?)\*\*)|((?<!\*)\*([^*\n]+?)\*(?!\*))/g;
  const MD_TABLE_DELIM_CELL = /^:?-{3,}:?$/;

  /* GFM pipe-table rows, mirroring routes._md_table_cells: optional outer
   * pipes, `\|` stays a literal pipe inside a cell. */
  function mdTableCells(line) {
    let row = line.trim();
    if (!row.includes("|")) return null;
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
    return row.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
  }

  function mdTableAlignments(line, width) {
    const cells = mdTableCells(line);
    if (!cells || cells.length !== width || !cells.every((c) => MD_TABLE_DELIM_CELL.test(c))) return null;
    return cells.map((c) => (c.startsWith(":") && c.endsWith(":") ? "center"
      : c.endsWith(":") ? "right" : c.startsWith(":") ? "left" : ""));
  }

  function mdTable(header, aligns, rows) {
    const wrap = el("div", "md-table");
    const table = el("table");
    const cell = (tag, text, align) => {
      const node = el(tag, align ? "align-" + align : null);
      mdInline(text, node);
      return node;
    };
    const head = el("thead");
    const headRow = el("tr");
    header.forEach((text, i) => headRow.appendChild(cell("th", text, aligns[i])));
    head.appendChild(headRow);
    const body = el("tbody");
    rows.forEach((row) => {
      const tr = el("tr");
      for (let i = 0; i < header.length; i += 1) tr.appendChild(cell("td", row[i] || "", aligns[i]));
      body.appendChild(tr);
    });
    table.appendChild(head);
    table.appendChild(body);
    wrap.appendChild(table);
    return wrap;
  }

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
    let item = null; // raw lines of the open list item

    function flushPara() {
      if (para.length) {
        const p = el("p");
        mdInline(para.join(" "), p);
        frag.appendChild(p);
        para = [];
      }
    }
    function flushItem() {
      if (item) {
        const li = el("li");
        mdInline(item.join(" "), li);
        list.appendChild(li);
        item = null;
      }
    }
    function closeList() {
      flushItem();
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
      const header = mdTableCells(line);
      const aligns = header && i + 1 < lines.length ? mdTableAlignments(lines[i + 1], header.length) : null;
      if (header && aligns) {
        flushPara();
        closeList();
        const rows = [];
        i += 2;
        while (i < lines.length && lines[i].trim()) {
          const row = mdTableCells(lines[i]);
          if (!row) break;
          rows.push(row);
          i += 1;
        }
        frag.appendChild(mdTable(header, aligns, rows));
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
        flushItem();
        item = [(ul || ol)[1]];
        i += 1;
        continue;
      }
      if (!line.trim()) {
        flushPara();
        closeList();
        i += 1;
        continue;
      }
      // A plain line under a list item continues it (lazy continuation),
      // never a paragraph emitted ahead of the still-open list.
      (item || para).push(line.trim());
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
    if (meta.error) {
      foot.textContent = "Stream failed — partial answer kept, not verified";
      return foot;
    }
    if (meta.stopped) {
      foot.textContent = "Stopped — partial answer kept";
      return foot;
    }
    const parts = [];
    if (typeof meta.ttft_ms === "number") parts.push("First token " + (meta.ttft_ms / 1000).toFixed(1) + " s");
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
    if (turn.role === "assistant") {
      // State hook shared by restored and streamed turns.
      article.setAttribute(
        "data-state",
        turn.verification_state === "generation_incomplete" ? "incomplete" : "complete"
      );
    }
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
    const cites = citationsBox(turn);
    if (cites) article.appendChild(cites);
    const evidence = evidenceBox(turn);
    if (evidence) article.appendChild(evidence);
    stateChip(article, turn);
    stateBadges(article, turn);
    const meta = renderMeta(turn);
    if (meta) article.appendChild(meta);
    const rating = renderFeedback(turn);
    if (rating) article.appendChild(rating);
    return article;
  }

  /* Answer feedback (issue #780): rate a recorded answer by its server
   * request id. Only the id, rating, reason codes and comment are sent; the
   * server attaches them to its own record, never to client-supplied text.
   * Rendered only when the deployment enables feedback. */
  const FEEDBACK_RATINGS = [["good", "Good"], ["neutral", "Neutral"], ["bad", "Bad"]];
  const FEEDBACK_REASONS = [
    ["wrong", "Wrong"], ["incomplete", "Incomplete"],
    ["missing_or_wrong_citation", "Missing or wrong citation"],
    ["should_have_declined", "Should have declined"], ["wrongly_declined", "Wrongly declined"],
    ["too_slow", "Too slow"], ["other", "Other"],
  ];
  const FEEDBACK_ERROR_TEXT = "The rating could not be sent.";
  // Unsent rating state by request id (choice, reasons, comment, in-flight
  // send, last error): renderMessages() rebuilds every article (another turn
  // completing, a cross-tab write), and the box is re-derived from this state.
  // Page memory only, never stored; an entry exists only once the operator
  // edits, and is dropped when the answer is rated or leaves every incident.
  const feedbackDrafts = new Map();
  const EMPTY_DRAFT = Object.freeze({ rating: null, reasons: [], comment: "", sending: false, status: "" });

  function editDraft(id) {
    if (!feedbackDrafts.has(id)) feedbackDrafts.set(id, { ...EMPTY_DRAFT, reasons: [] });
    return feedbackDrafts.get(id);
  }

  function pruneDrafts(store) {
    const known = new Set();
    for (const session of Object.values(store.sessions)) {
      for (const stored of session.turns || []) {
        if (typeof stored.request_id === "string" && !stored.feedback) known.add(stored.request_id);
      }
    }
    for (const id of Array.from(feedbackDrafts.keys())) if (!known.has(id)) feedbackDrafts.delete(id);
  }

  // Focus inside a rating box survives a rebuild: renderMessages() captures
  // the control before replacing the articles and restores it (and the caret)
  // after, so keystrokes are not lost to <body>.
  function captureFeedbackFocus() {
    const active = document.activeElement;
    const box = active && active.closest ? active.closest("[data-feedback-for]") : null;
    if (!box) return null;
    let selector = null;  // null: the box itself (a focusable "Rated" box)
    if (active.matches(".feedback-comment")) selector = ".feedback-comment";
    else if (active.matches(".feedback-submit")) selector = ".feedback-submit";
    else if (active.dataset.rating) selector = '.feedback-rating[data-rating="' + active.dataset.rating + '"]';
    else if (active.type === "checkbox") selector = '.feedback-reasons input[value="' + active.value + '"]';
    return {
      id: box.dataset.feedbackFor, selector,
      start: active.selectionStart, end: active.selectionEnd,
    };
  }

  function restoreFeedbackFocus(saved) {
    if (!saved) return;
    const box = messagesEl.querySelector('[data-feedback-for="' + saved.id + '"]');
    if (!box) return;
    // The saved control, else the box when it is focusable ("Rated"); never
    // a different control than the one that had focus.
    const target = saved.selector ? box.querySelector(saved.selector) : null;
    const fallback = box.hasAttribute("tabindex") ? box : null;
    if (!target) {
      if (fallback) fallback.focus({ preventScroll: true });
      return;
    }
    target.focus({ preventScroll: true });
    if (typeof saved.start === "number" && target.setSelectionRange) {
      target.setSelectionRange(saved.start, saved.end);
    }
  }

  function feedbackBox(id) {
    return messagesEl.querySelector('[data-feedback-for="' + id + '"]');
  }

  // The answer's turn as the store holds it now (another tab may have rated
  // it meanwhile), falling back to the caller's copy.
  function storedTurn(id, fallback) {
    for (const session of Object.values(loadStore().sessions)) {
      for (const stored of session.turns || []) if (stored.request_id === id) return stored;
    }
    return fallback;
  }

  // Replace one live box from the state. Focus inside it stays on the same
  // control; when that control no longer exists (the box became "Rated"),
  // focus moves to the box itself, never to <body>. Focus elsewhere is
  // never touched.
  function redrawFeedback(turn) {
    const live = feedbackBox(turn.request_id);
    if (!live) return;
    const active = document.activeElement;
    const saved = active && live.contains(active) ? captureFeedbackFocus() : null;
    const next = renderFeedback(turn);
    if (!next) {
      live.remove();
      return;
    }
    live.replaceWith(next);
    if (saved) restoreFeedbackFocus(saved);
  }

  function renderFeedback(turn) {
    if (document.body.dataset.feedback !== "on" || turn.role !== "assistant") return null;
    if (typeof turn.request_id !== "string" || !/^[0-9a-f]{12}$/.test(turn.request_id)) return null;
    const id = turn.request_id;
    const box = el("div", "turn-feedback");
    box.dataset.feedbackFor = id;
    if (turn.feedback) {
      feedbackDrafts.delete(id);
      box.tabIndex = -1;  // the focus target once its controls are gone
      box.textContent = "Rated: " + turn.feedback;
      return box;
    }
    const draft = feedbackDrafts.get(id) || EMPTY_DRAFT;
    // Locked while a send is in flight. Controls stay in place and focusable
    // (aria-disabled, read-only comment): disabling a focused node would drop
    // focus to <body>. Every handler refuses input while locked.
    const locked = () => (feedbackDrafts.get(id) || EMPTY_DRAFT).sending;
    const lockState = String(draft.sending);
    box.appendChild(el("span", "feedback-label", "Rate this answer:"));
    const details = el("div", "feedback-details");
    details.hidden = !draft.rating;
    for (const [value, label] of FEEDBACK_RATINGS) {
      const button = el("button", "seg feedback-rating", label);
      button.type = "button";
      button.setAttribute("data-rating", value);
      button.setAttribute("aria-pressed", String(draft.rating === value));
      button.setAttribute("aria-disabled", lockState);
      button.addEventListener("click", () => {
        if (locked()) return;
        editDraft(id).rating = value;
        box.querySelectorAll(".feedback-rating").forEach((other) => {
          other.setAttribute("aria-pressed", String(other === button));
        });
        details.hidden = false;
      });
      box.appendChild(button);
    }
    const reasons = el("fieldset", "feedback-reasons");
    reasons.appendChild(el("legend", null, "Reasons (optional)"));
    for (const [value, label] of FEEDBACK_REASONS) {
      const option = el("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = value;
      checkbox.checked = draft.reasons.includes(value);
      checkbox.setAttribute("aria-disabled", lockState);
      checkbox.addEventListener("click", (event) => {
        if (locked()) event.preventDefault();
      });
      checkbox.addEventListener("change", () => {
        editDraft(id).reasons = Array.from(reasons.querySelectorAll("input:checked")).map((input) => input.value);
      });
      option.appendChild(checkbox);
      option.appendChild(document.createTextNode(" " + label));
      reasons.appendChild(option);
    }
    details.appendChild(reasons);
    const comment = el("textarea", "feedback-comment");
    comment.maxLength = 2000;
    comment.placeholder = "Comment (optional)";
    comment.setAttribute("aria-label", "Comment (optional)");
    comment.value = draft.comment;
    comment.readOnly = draft.sending;
    comment.addEventListener("input", () => {
      editDraft(id).comment = comment.value;
    });
    details.appendChild(comment);
    const submit = el("button", "btn feedback-submit", "Send rating");
    submit.type = "button";
    submit.setAttribute("aria-disabled", lockState);
    const status = el("span", "feedback-status", draft.status);
    status.setAttribute("role", "status");
    submit.addEventListener("click", async () => {
      const sending = editDraft(id);
      if (!sending.rating || sending.sending) return;
      // Send a snapshot and lock the box through the state (the one source
      // the render derives every control from): what is shown and saved
      // afterwards is exactly what the service recorded.
      const sent = { rating: sending.rating, reasons: sending.reasons.slice(), comment: sending.comment };
      sending.sending = true;
      sending.status = "";
      redrawFeedback(turn);
      try {
        let ok = false;
        let message = FEEDBACK_ERROR_TEXT;
        try {
          const response = await fetch("/ui/feedback", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              request_id: id,
              rating: sent.rating,
              reasons: sent.reasons,
              comment: sent.comment,
            }),
          });
          ok = response.status === 201;
          if (!ok) {
            try {
              const envelope = await response.json();
              if (envelope && typeof envelope.message === "string") message = envelope.message;
            } catch (err) {
              /* fixed fallback text */
            }
          }
        } catch (err) {
          ok = false;
        }
        if (ok) {
          const rated = sent.rating;
          // The service recorded it: show "Rated" even if the local save
          // fails (the operator must not resend a recorded rating).
          turn.feedback = rated;
          try {
            await mutate((store) => {
              for (const session of Object.values(store.sessions)) {
                for (const stored of session.turns || []) {
                  if (stored.request_id === id) stored.feedback = rated;
                }
              }
              return true;
            });
          } catch (err) {
            /* recorded server-side; a rejected locked write keeps the label only until the next rebuild */
          }
          announce("Rating sent.");
        } else {
          // The draft may have been replaced or pruned meanwhile: write the
          // error into whatever state is current.
          const current = feedbackDrafts.get(id);
          if (current) current.status = message;
        }
      } finally {
        // Unlocked only once settled (after the store write), and always,
        // even if the store write fails.
        const current = feedbackDrafts.get(id);
        if (current) current.sending = false;
        // The article may have been rebuilt, or the answer rated in another
        // tab, while the request was in flight: redraw the live box from the
        // stored turn.
        const stored = storedTurn(id, turn);
        redrawFeedback(turn.feedback && !stored.feedback ? turn : stored);
      }
    });
    details.appendChild(submit);
    details.appendChild(status);
    box.appendChild(details);
    return box;
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
  const newBtn = document.getElementById("new-session-btn");

  /* Streaming UX state (P3): at most one in-flight turn. The Send button
   * doubles as Stop while streaming; aborts keep partial content. */
  let streamAbort = null;
  let submitting = false;
  let sessionFilter = "";

  function updateSendBtn() {
    if (!sendBtn) return;
    const isStreaming = streamAbort !== null;
    const hasText = promptEl ? promptEl.value.trim().length > 0 : false;
    if (isStreaming || hasText) {
      sendBtn.classList.remove("hidden");
    } else {
      sendBtn.classList.add("hidden");
    }
  }

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
    updateSendBtn();
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

  /* The in-flight turn's live article, so rendering the same incident again
   * (switching away and back, a cross-tab refresh) keeps the stream visible
   * instead of orphaning it. */
  let inflight = null;
  // A finished answer whose save was refused (stale copy): shown, never stored.
  let unsaved = null;
  let renaming = false;

  function orphanNote() {
    return el(
      "div",
      "orphan-note",
      "No answer recorded for this question (still generating in another tab, or interrupted)."
    );
  }

  function renderMessages() {
    const current = currentSession();
    const session = current.session;
    const feedbackFocus = captureFeedbackFocus();
    messagesEl.replaceChildren();
    if (!session.turns.length) {
      const empty = el("div", "empty-state");
      empty.appendChild(el("p", null, "Describe the abend, message ID, or procedure — answers cite the manual."));
      EMPTY_EXAMPLES.forEach((text) => {
        const ex = el("button", "example", text);
        ex.type = "button";
        ex.addEventListener("click", () => {
          promptEl.value = text;
          updateSendBtn();
          promptEl.focus();
        });
        empty.appendChild(ex);
      });
      messagesEl.appendChild(empty);
    }
    session.turns.forEach((turn) => messagesEl.appendChild(renderTurn(turn)));
    const streamingHere = inflight && inflight.sid === current.id;
    const unsavedHere = unsaved && unsaved.sid === current.id;
    const last = session.turns[session.turns.length - 1];
    if (last && last.role === "user" && !streamingHere && !unsavedHere) messagesEl.appendChild(orphanNote());
    if (streamingHere) messagesEl.appendChild(inflight.article);
    if (unsavedHere) messagesEl.appendChild(unsaved.article);
    stick = true;
    messagesEl.scrollIntoView({ block: "end" });
    pruneDrafts(loadStore());
    restoreFeedbackFocus(feedbackFocus);
  }

  function startRename(id, row, openBtn) {
    const session = loadStore().sessions[id];
    if (!session) return;
    const input = document.createElement("input");
    input.value = session.title || "";
    input.className = "rename";
    input.maxLength = 60;
    input.setAttribute("aria-label", "Rename incident");
    row.replaceChild(input, openBtn);
    renaming = true;
    input.focus();
    input.select();
    let done = false;
    const commit = async (save, refocus) => {
      if (done) return;
      done = true;
      renaming = false;
      // Finish the editor immediately while persistence waits for the
      // shared lock; keyboard users keep a visible focus target.
      if (save && input.value.trim()) openBtn.textContent = input.value.trim().slice(0, 60);
      if (input.parentNode === row) row.replaceChild(openBtn, input);
      if (refocus) openBtn.focus();
      if (save && input.value.trim()) {
        const title = input.value.trim().slice(0, 60);
        await mutate((store) => {
          const live = store.sessions[id];
          if (!live) return;
          live.title = title;
          live.updated_at = Date.now();
        });
      }
      renderSessions();
      if (refocus) focusSessionButton(id);
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") commit(true, true);
      else if (event.key === "Escape") commit(false, true);
    });
    input.addEventListener("blur", () => commit(true, false));
  }

  function focusSessionButton(id) {
    const row = sessionListEl.querySelector('[data-session-id="' + id + '"] .open');
    if (row) row.focus();
  }

  async function deleteSession(id) {
    if (inflight && inflight.sid === id && streamAbort) streamAbort.abort();
    if (currentId === id) currentId = null;
    await mutate((store) => {
      delete store.sessions[id];
      if (store.active === id) store.active = null;
      // Record the surviving (or fresh) incident as active; resolving alone
      // used to pick one without ever assigning it (issue #372).
      resolveSession(store, true);
    });
    renderMessages();
    renderSessions();
    const active = sessionListEl.querySelector(".row.active .open");
    if (active) active.focus();
    else if (newBtn) newBtn.focus();
    announce("Incident deleted.");
  }

  async function openSession(id) {
    if (id !== currentId) unsaved = null;
    currentId = id;
    await mutate((store) => {
      if (store.sessions[id]) store.active = id;
    });
    renderMessages();
    renderSessions();
    focusSessionButton(id);
  }

  function renderSessions() {
    const store = loadStore();
    sessionListEl.replaceChildren();
    const needle = sessionFilter.trim().toLowerCase();
    Object.keys(store.sessions)
      .filter((id) => !needle || (store.sessions[id].title || "").toLowerCase().includes(needle))
      .sort((a, b) => (store.sessions[b].updated_at || 0) - (store.sessions[a].updated_at || 0))
      .forEach((id) => {
        const session = store.sessions[id];
        const title = session.title || "New Incident";
        const isCurrent = id === currentId;
        const row = el("li", "row" + (isCurrent ? " active" : ""));
        row.setAttribute("data-session-id", id);
        const open = el("button", "open", title);
        open.type = "button";
        open.title = "Open incident (double-click to rename)";
        if (isCurrent) open.setAttribute("aria-current", "true");
        open.addEventListener("click", () => openSession(id));
        open.addEventListener("dblclick", () => startRename(id, row, open));
        // Rename and delete are real buttons: double-click alone is not
        // keyboard-operable.
        const ren = el("button", "ren", "\u270E");
        ren.type = "button";
        ren.title = "Rename incident";
        ren.setAttribute("aria-label", "Rename incident: " + title);
        ren.addEventListener("click", () => startRename(id, row, open));
        const del = el("button", "del", "\u2715");
        del.type = "button";
        del.title = "Delete incident";
        del.setAttribute("aria-label", "Delete incident: " + title);
        del.addEventListener("click", () => deleteSession(id));
        row.appendChild(open);
        row.appendChild(ren);
        row.appendChild(del);
        sessionListEl.appendChild(row);
      });
  }

  /* Polite state announcements for assistive tech. The message list itself
   * is not a live region: re-rendering the whole answer on every token would
   * flood a screen reader. */
  const statusEl = document.getElementById("console-status");
  let announceTimer = null;
  function announce(text) {
    if (!statusEl) return;
    statusEl.textContent = "";
    window.clearTimeout(announceTimer);
    announceTimer = window.setTimeout(() => {
      statusEl.textContent = text;
    }, 30);
  }

  function syncStorageNotice() {
    const notice = document.getElementById("storage-notice");
    if (notice) notice.hidden = storageOk;
  }

  function applyTheme(theme) {
    const next = theme === "theme-3270" ? "theme-3270" : "theme-dark";
    document.body.classList.remove("theme-dark", "theme-3270");
    document.body.classList.add(next);
    if (themeSelect) themeSelect.value = next;
  }

  /* Reasoning effort: one pressed toggle in the .segmented group; the
   * hidden form field carries the value for both the JS and no-JS paths.
   * The server renders only the efforts the configured model accepts (#748). */
  function effortLevels() {
    return Array.from(document.querySelectorAll(".segmented .seg")).map((seg) => seg.getAttribute("data-effort"));
  }

  function getReasoningEffort() {
    const levels = effortLevels();
    const hidden = document.getElementById("reasoning-effort-input");
    if (hidden && levels.includes(hidden.value)) return hidden.value;
    const pressed = document.querySelector('.segmented .seg[aria-pressed="true"]');
    return pressed ? pressed.getAttribute("data-effort") : levels[0] || null;
  }

  function setReasoningEffort(effort) {
    // An effort this deployment does not offer (e.g. a stale saved choice)
    // keeps the current selection; it is never remapped to another level.
    if (!effortLevels().includes(effort)) return;
    const target = effort;
    const hidden = document.getElementById("reasoning-effort-input");
    if (hidden) hidden.value = target;
    document.querySelectorAll(".segmented .seg").forEach((seg) => {
      seg.setAttribute("aria-pressed", seg.getAttribute("data-effort") === target ? "true" : "false");
    });
  }

  /* Incident-context summary chips: what the next question will carry,
   * visible while the drawer is closed. Text only (textContent). */
  function updateContextChips() {
    const chips = document.getElementById("context-chips");
    if (!chips) return;
    chips.replaceChildren();
    const product = productEl ? productEl.value.trim() : "";
    const version = versionEl ? versionEl.value.trim() : "";
    const context = splunkEl ? splunkEl.value.trim() : "";
    const label = [product, version].filter(Boolean).join(" ");
    if (label) chips.appendChild(el("span", "chip", label.slice(0, 40)));
    if (context) {
      const lines = context.split("\n").length;
      chips.appendChild(el("span", "chip", "Context \u00b7 " + lines + (lines === 1 ? " line" : " lines")));
    }
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

  async function streamTurn(sid, history, userTurn) {
    const assistantTurn = { role: "assistant", content: "", citations: [], ts: Date.now() };
    const article = renderTurn(assistantTurn);
    // Provisional until the final frame: streamed text must never read as a
    // verified answer (issue #372). Cleared only by the completion path.
    article.classList.add("turn-provisional");
    article.setAttribute("data-state", "streaming");
    const provisional = el("div", "state-badge state-provisional", PROVISIONAL_LABEL);
    article.insertBefore(provisional, article.querySelector(".turn-content"));
    inflight = { sid: sid, article: article };
    messagesEl.appendChild(article);
    messagesEl.setAttribute("aria-busy", "true");
    announce("Generating answer. Provisional text, not yet verified.");
    const contentEl = article.querySelector(".turn-content");
    contentEl.appendChild(el("span", "thinking", "Thinking…"));
    stickScroll();

    const controller = new AbortController();
    streamAbort = controller;
    setStreaming(true);
    let failed = false;
    let stopped = false;
    let effortRefused = false;
    let started = false;
    let finalPayload = null;
    let frameBuffer = "";

    try {
      const payload = {
        messages: history,
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
      if (response.status === 422) {
        let envelope = null;
        try {
          envelope = await response.json();
        } catch (err) {
          envelope = null;
        }
        if (envelope && envelope.code === "unsupported_reasoning_effort") effortRefused = true;
      }
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
      inflight = null;
      messagesEl.removeAttribute("aria-busy");
      provisional.remove();
      article.classList.remove("turn-provisional");
      setStreaming(false);
    }

    if (stopped && !assistantTurn.content) {
      // Stopped before the first token: leave no husk behind.
      article.remove();
      announce("Stopped before any answer text arrived.");
      return;
    }
    const streamFailed = failed || (!stopped && !finalPayload);
    if (streamFailed && !assistantTurn.content) {
      // Failed before the first token: only the banner would persist, nothing
      // was displayed to retain — leave no husk behind.
      assistantTurn.error = true;
      assistantTurn.content = effortRefused ? EFFORT_ERROR_TEXT : ERROR_TEXT;
      assistantTurn.verification_state = "generation_incomplete";
      contentEl.replaceChildren(renderMarkdown(assistantTurn.content));
      article.classList.add("turn-error");
      article.setAttribute("data-state", "failed");
      announce("The request failed. No answer was produced.");
      return;
    }
    if (streamFailed) {
      // Partial tokens were displayed, so the turn is retained in history and
      // exports with a non-accepted state (issue #365): never promoted to
      // accepted guidance, never silently dropped from the record.
      assistantTurn.error = true;
      assistantTurn.verification_state = "generation_incomplete";
      assistantTurn.meta = { stopped: false, error: true };
      contentEl.replaceChildren(renderMarkdown(assistantTurn.content));
      article.classList.add("turn-error");
    } else if (finalPayload) {
      assistantTurn.content = finalPayload.answer || assistantTurn.content;
      if (finalPayload.script) {
        // Tagged script fences leave the answer body during citation parsing;
        // render them with their threaded language tag (issue #337), falling
        // back to unlabeled when None, mirroring the server fragment.
        const tag = finalPayload.script_lang || "";
        assistantTurn.content += "\n\n```" + tag + "\n" + finalPayload.script + "\n```";
      }
      assistantTurn.citations = finalPayload.citations || [];
      assistantTurn.citations_inferred = !!finalPayload.citations_inferred;
      // The server request id (12 hex, no content): the key a rating attaches to
      // (#780). Kept only when feedback is enabled: disabled means no change.
      if (document.body.dataset.feedback === "on" && typeof finalPayload.request_id === "string") {
        assistantTurn.request_id = finalPayload.request_id;
      }
      assistantTurn.verification_state = finalPayload.verification_state || "unverified_draft";
      assistantTurn.script_review_required = !!finalPayload.script_review_required;
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
      // Operator-stopped with partial content: output was cut off before
      // finalization, so it stays explicitly incomplete (issue #365).
      assistantTurn.verification_state = "generation_incomplete";
      assistantTurn.meta = { stopped: true };
    }
    assistantTurn.ts = Date.now();
    // Current-response excerpts live in memory only (issue #635): never on
    // the persisted turn, so localStorage and exports cannot carry them.
    liveEvidence =
      finalPayload && Array.isArray(finalPayload.supplied_evidence)
        ? {
            ts: assistantTurn.ts,
            items: finalPayload.supplied_evidence,
            omitted: typeof finalPayload.evidence_omitted === "number" ? finalPayload.evidence_omitted : 0,
          }
        : null;
    contentEl.replaceChildren(renderMarkdown(assistantTurn.content));
    const cites = citationsBox(assistantTurn);
    if (cites) article.appendChild(cites);
    const evidence = evidenceBox(assistantTurn);
    if (evidence) article.appendChild(evidence);
    stateChip(article, assistantTurn);
    stateBadges(article, assistantTurn);
    const meta = renderMeta(assistantTurn);
    if (meta) article.appendChild(meta);
    stickScroll();
    // Commit against the freshly persisted store by session id: the operator
    // may have created, renamed, deleted or switched incidents (here or in
    // another tab) while this turn streamed. A deleted incident stays
    // deleted: the finished turn is dropped, never resurrected.
    let kept;
    try {
      kept = await mutate((store) => {
        const live = store.sessions[sid];
        if (!live) return false;
        live.turns.push(assistantTurn);
        live.updated_at = Date.now();
        return true;
      });
    } catch (err) {
      ignoreStale(err);
      unsaved = { sid: sid, article: article };
      article.setAttribute("data-state", "unsaved");
      renderMessages();
      composerNotice(UNSAVED_TEXT);
      return;
    }
    if (!kept) {
      article.remove();
      announce("The incident was deleted while the answer was generating; the answer was discarded.");
      return;
    }
    article.setAttribute(
      "data-state",
      assistantTurn.verification_state === "generation_incomplete" ? "incomplete" : "complete"
    );
    // An asynchronous open/rename/storage refresh may have re-rendered the
    // messages while completion waited for persistence. Render the committed
    // store so the final is visible even if its provisional node was detached.
    renderMessages();
    renderSessions();
    announce(completionAnnouncement(assistantTurn));
  }

  function completionAnnouncement(turn) {
    if (turn.verification_state === "generation_incomplete") {
      return turn.meta && turn.meta.stopped
        ? "Stopped. Partial answer kept; incomplete and not verified."
        : "Answer incomplete: the stream failed. Partial text kept; not verified.";
    }
    if (turn.verification_state === "accepted") return "Answer complete. Citations source-matched.";
    return "Answer complete. State: " + String(turn.verification_state).replace(/_/g, " ") + ".";
  }

  function composerNotice(text) {
    const notice = document.getElementById("composer-notice");
    if (notice) {
      notice.textContent = text;
      notice.hidden = !text;
    }
    if (text) announce(text);
  }

  async function onSubmit(event) {
    event.preventDefault();
    event.stopImmediatePropagation();
    // The Send button doubles as Stop while a turn streams.
    if (streamAbort) {
      streamAbort.abort();
      return;
    }
    if (submitting) return;
    const text = promptEl.value.trim();
    if (!text) return;
    composerNotice("");
    submitting = true;
    const userTurn = {
      role: "user",
      content: text,
      splunk_context: splunkEl.value.trim() || null,
      ts: Date.now(),
    };
    try {
      const selected = await mutate((store) => resolveSession(store, true));
      const submit = async (lock) => {
        if (sharedLocks && !lock) {
          composerNotice("This incident is already generating an answer in another tab. Try again when it finishes.");
          return;
        }
        const saved = await mutate((store) => {
          const session = store.sessions[selected];
          if (!session) return null;
          session.turns.push(userTurn);
          if (session.title === "New Incident") session.title = text.split("\n")[0].slice(0, 45);
          session.updated_at = Date.now();
          return { sid: selected, history: historyMessages(session) };
        });
        if (!saved) {
          composerNotice("The incident was deleted before the question was sent.");
          return;
        }
        renderMessages();
        promptEl.value = "";
        updateSendBtn();
        stick = true;
        stickScroll();
        renderSessions();
        await streamTurn(saved.sid, saved.history, userTurn);
      };
      // Hold the incident lock through completion persistence, so a second
      // tab cannot insert a user turn before this turn's assistant response.
      if (sharedLocks) await navigator.locks.request(
        "mainframe-rag-turn:" + selected, { ifAvailable: true }, submit
      );
      else await submit(null);
    } finally {
      submitting = false;
    }
  }

  /* Copy buttons read from the adjacent rendered node — no payload ever
   * travels in attributes, so there is nothing to escape. One delegated
   * listener covers streamed, restored, and server-fragment turns alike. */
  /* The async Clipboard API exists only in secure contexts (HTTPS or
   * localhost); a console reached over plain http://host:8080 has no
   * navigator.clipboard at all. Fall back to the legacy copy command on an
   * off-screen textarea, still inside the click's user activation, so the
   * button works on every origin and a failure is always reported. */
  function writeClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise((resolve, reject) => {
      const area = el("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.setAttribute("aria-hidden", "true");
      area.style.position = "fixed";
      area.style.top = "-1000px";
      area.style.opacity = "0";
      const focused = document.activeElement;
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (err) {
        ok = false;
      }
      area.remove();
      if (focused && typeof focused.focus === "function") focused.focus();
      if (ok) resolve();
      else reject(new Error("copy command refused"));
    });
  }

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
    if (!text) return;
    writeClipboard(text).then(
      () => {
        button.textContent = "Copied";
        window.setTimeout(() => {
          button.textContent = "Copy";
        }, 1500);
      },
      () => {
        button.textContent = "Copy failed";
        announce("Copy failed. Select the text and copy it manually.");
        window.setTimeout(() => {
          button.textContent = "Copy";
        }, 3000);
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

  function download(text, filename) {
    try {
      const blob = new Blob([text], { type: "text/markdown" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      announce("Incident exported. The file leaves this browser's control; handle it per site data policy.");
    } catch (err) {
      announce("Export failed. The incident is still saved in this browser.");
    }
  }

  /* Explicit operator-initiated erase of every saved incident in this
   * browser (issue #372 retention policy). Two-step so a stray click or
   * keypress cannot destroy a shift's record; no native dialog, so it is
   * equally operable by keyboard and by assistive tech. */
  async function clearAllSaved() {
    if (streamAbort) streamAbort.abort();
    await mutate((store) => {
      // A quota failure may have selected memory while an older persisted
      // record still exists. Explicit erase must reach that record too.
      try {
        window.localStorage.removeItem(STORAGE_KEY);
      } catch (err) {
        /* unavailable storage: still erase this tab's memory below */
      }
      store.sessions = {};
      store.active = null;
    });
    currentId = null;
    renderMessages();
    renderSessions();
    announce("All saved incidents were erased from this browser.");
  }

  function wireClearButton() {
    const btn = document.getElementById("clear-history-btn");
    if (!btn) return;
    const idle = btn.textContent;
    let armed = false;
    let timer = null;
    const disarm = () => {
      armed = false;
      btn.textContent = idle;
      btn.classList.remove("armed");
      window.clearTimeout(timer);
    };
    btn.addEventListener("click", () => {
      if (!armed) {
        armed = true;
        btn.textContent = "Confirm: erase ALL saved incidents";
        btn.classList.add("armed");
        announce("Press again to erase all saved incidents in this browser. This cannot be undone.");
        timer = window.setTimeout(disarm, 6000);
        return;
      }
      disarm();
      clearAllSaved();
    });
    btn.addEventListener("blur", disarm);
  }

  async function boot() {
    // Disable the HTML fallback before the first asynchronous lock wait.
    // HTMX must not bind its form submit while browser boot is suspended.
    if (formEl && window.fetch && window.ReadableStream) {
      formEl.removeAttribute("hx-post");
      formEl.removeAttribute("hx-target");
      formEl.removeAttribute("hx-swap");
      formEl.addEventListener("submit", onSubmit);
    }
    await mutate((store) => resolveSession(store, true)).catch(ignoreStale);
    syncStorageNotice();
    renderMessages();
    renderSessions();
    localizeTimes(document);
    wireClearButton();

    // Another tab wrote the store: re-read it rather than keep showing (and
    // later overwriting) a stale copy. The streaming tab keeps its live
    // article; an open rename input is never torn down.
    window.addEventListener("storage", async (event) => {
      if (event.key !== null && event.key !== STORAGE_KEY) return;
      if (renaming) return;
      await mutate((store) => resolveSession(store)).catch(ignoreStale);
      renderMessages();
      renderSessions();
    });

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
        return window.localStorage.getItem(THEME_KEY) || "theme-dark";
      } catch (err) {
        return "theme-dark";
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

    document.querySelectorAll(".segmented .seg").forEach((seg) => {
      seg.addEventListener("click", () => {
        const effort = seg.getAttribute("data-effort");
        setReasoningEffort(effort);
        try {
          window.localStorage.setItem(REASONING_KEY, effort);
        } catch (err) {
          /* storage loss is not an error */
        }
      });
    });

    [productEl, versionEl, splunkEl].forEach((field) => {
      if (field) field.addEventListener("input", updateContextChips);
    });
    updateContextChips();

    try {
      const savedEffort = window.localStorage.getItem(REASONING_KEY);
      if (savedEffort) setReasoningEffort(savedEffort);
    } catch (err) {
      /* storage loss is not an error */
    }

    const exportBtn = document.getElementById("export-btn");
    if (exportBtn) {
      exportBtn.addEventListener("click", () => {
        const session = currentSession().session;
        download(markdownReport(session), "incident-" + (session.title || "session").replace(/[^\w.-]+/g, "_") + ".md");
      });
    }

    if (newBtn) {
      newBtn.addEventListener("click", async () => {
        await mutate(createSession);
        renderMessages();
        renderSessions();
        promptEl.focus();
        announce("New incident started.");
      });
    }

    if (promptEl) {
      promptEl.addEventListener("input", updateSendBtn);
      updateSendBtn();
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
        renderSessions();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
