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
  with 180-day retention. Telemetry (metrics, traces, logs) never holds content;
  `request_id` is the only join key. Ratings surface as bounded-label metrics
  plus a periodic report from a read-only export of the store.
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
