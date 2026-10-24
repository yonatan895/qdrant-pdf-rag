# ADR-0005: Answer feedback data boundary (content only in the feedback store)

- **Status:** accepted (maintainer, 2026-10-17; #780)
- **Context:** console answer feedback (#780) needs question and answer text,
  ratings and a telemetry snapshot for quality analysis. Telemetry backends
  were the obvious place to put it, and a ClickHouse-backed single pane (#529)
  made that tempting. But OBS-1A deliberately removed content from telemetry,
  and the platform team offers Crunchy Postgres while our serving credentials
  are read-only.
- **Decision:** question, answer and comment text live **only** in a dedicated
  feedback store, SQLite owned by one service process on its own RWO block PVC
  with a 180-day retention window (`FEEDBACK_RETENTION_DAYS`) enforced by an
  explicit purge (scheduled in-process by the service, #804; nothing deletes implicitly). Telemetry (metrics, traces, logs) never holds content;
  `request_id` is the only join key. Ratings surface as bounded-label metrics
  plus a periodic report from a read-only export of the store.
  *(Amendment, ADR-0007, proposed: while Open WebUI is enabled, its chat
  history may also hold question, answer and excerpt text as user session
  state, not quality records, under ADR-0007's storage and retention rules.)*
- **Consequences / rules:**
  - No backend choice under #529 or #782 may ingest question, answer, excerpt or
    comment text. Using content for analytics means exporting from the feedback
    store under its own access control.
  - Records carry their own telemetry snapshot, so quality analysis survives
    trace expiry (14 days, ADR-0002) without raising trace retention.
  - **Single writer, best effort:** agents never write SQLite directly. A
    feedback-service outage drops records (counted) and makes ratings
    unavailable, but never affects answers.
  - **One rating per answer;** re-rating replaces it (idempotent submission).
  - The store keeps a narrow interface and portable SQL. Moving to the
    platform's Postgres needs a new ADR and is triggered by an availability
    requirement for feedback, multiple writers, live concurrent analytics, or a
    platform mandate.
  - Not adopted: content in SigNoz or ClickHouse, content in Qdrant (it would
    need write credentials on the serving path), and append-only files.
- **Amends (maintainer decision, 2026-10-17, #780):** with `FEEDBACK_ENABLED`
  (default off) the agent originates exactly two kinds of stored-data write,
  both to the feedback service and nowhere else (never the corpus or Qdrant,
  never a source system). Its other outbound calls never carry feedback
  ratings or comments: the model gateway call is unchanged (it carries the
  question and returns the answer, as before), and OTLP telemetry gains only the
  bounded-label feedback counters (`rag.feedback.ratings`,
  `rag.feedback.records`; no text, no ids):
  1. **Answer records, outbound from the answer, chat and console routes**
     (never `/v1/search`). On every
     finalized answer of `/v1/answer`, `/v1/chat` (with `/v1/chat/completions`),
     `/ui/chat` and their streams, the agent sends one record carrying the
     question, answer, script, citations, supplied-evidence citations and a
     scalar telemetry snapshot. No rating is needed. This is the content flow
     this ADR exists for, best effort and off the answer's critical path.
  2. **Ratings, inbound on two new console routes.** `/ui/feedback` and
     `/ui/feedback/form` forward a bounded rating (vocabulary codes plus a
     comment of at most 2000 characters) for an already recorded answer.

  The amendments are:
  - **ADR-0003, agent-surface clause.** The agent's POST set adds the two
    rating routes. No PUT/PATCH/DELETE and no `fetch_live` reference still
    hold, and `tests/test_mcp_agent.py` pins the exact POST set. The answer,
    chat and console routes gain the outbound answer-record write above, but
    no new inbound operation; `/v1/search` is unchanged.
  - **ADR-0004, non-goal 2, scoped.** The feedback service is a separate
    single-replica deployment (PR B) that the agent calls over ClusterIP. It
    is not a Backend-for-Frontend, serves no UI, and runs no process alongside
    the agent.
  - **ADR-0004, non-goal 3, scoped.** Console question and answer text is
    stored server-side, as quality records in the feedback store, for every
    finalized console answer while feedback is enabled. That is not session state: the
    console never reads it back, and dialogue history, incidents and
    preferences stay in browser `localStorage`. The agent pod keeps no
    persistent volume and no database; its existing non-persistent mounts
    (the `/tmp` emptyDir, CA, TLS and OAuth volumes) are unchanged.
- **Amends (maintainer decision, 2026-10-18, #804):** the feedback service is
  deployed by the application chart as the single-replica StatefulSet
  `rag-feedback` on a kept standalone RWO claim, default off. Its purge is
  scheduled in-process by the service (first attempt 30 s after start, then every
  24 h; `wal_truncated: false` and errors are retried, never success); the
  authenticated `POST /v1/purge` remains the manual action. Its network boundary
  is its own ingress NetworkPolicy admitting agent pods only (monitoring only
  with metrics, still bearer-protected) plus the operator-created bearer token,
  so enabling it without the agent NetworkPolicy is refused. Disabling it keeps
  the claim and its content, unpurged until the operator deletes the claim.
  Rater attribution stays deferred (D5).
