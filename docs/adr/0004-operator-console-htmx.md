# ADR-0004: Operator Console & Multi-Turn Chat Architecture (FastAPI + HTMX + OpenShift OAuth-Proxy)

- **Status:** accepted
- **Context:** Mainframe systems operators and systems programmers require an
  interactive conversational console to diagnose abends (e.g. S0C4, S0C7, IEC141I),
  analyze JES spool and syslog dumps, validate JCL/REXX syntax, execute multi-turn
  troubleshooting workflows, and inspect verified IBM and ISV manual citations.
  ADR-0001 established the core retrieval and reasoning pipeline for single-turn
  `/v1/answer` in an air-gapped OpenShift environment.
  
  An early prototype in `src/mainframe_rag/ui/` was implemented using Streamlit
  with an on-disk SQLite database (`copilot_sessions.db` in `src/mainframe_rag/ui/db.py`).
  Forensic inspection of this prototype revealed critical architectural,
  operational, and security violations:
  1. **Multi-process and port sprawl:** Streamlit requires a separate Python
     runtime process, a dedicated container, a distinct port (8501 vs 8080), and
     duplicated Kubernetes health monitoring and lifecycle management.
  2. **Multi-replica concurrency and distributed storage failure modes:** In production,
     `rag-agent` runs 2 replicas behind a Service and Route
     (`deploy/kustomize/overlays/openshift/agent-prod-patch.yaml:9`). An on-disk
     SQLite database induces catastrophic failure modes:
     - *Local ephemeral storage:* Operator requests alternating between Pod 1 and
       Pod 2 suffer split-brain dialogue state and lost sessions.
     - *Shared network storage (RWX/NFS):* Concurrent POSIX file lock contention
       (`fcntl`/`flock`) over NFS triggers `sqlite3.OperationalError: database is locked`,
       risks database corruption, and violates `docs/architecture.md` §3.1 (which
       strictly bans NFS for writable state).
     - *RWO block storage:* Kubernetes ReadWriteOnce volumes cannot be attached
       to multiple cluster nodes concurrently, triggering multi-attach deadlocks
       that break horizontal pod scaling.
  3. **Air-gap compliance breach:** Prototype CSS in `src/mainframe_rag/ui/styles.py:56`
     imported Google Fonts over the public internet (`@import url('https://fonts.googleapis.com/...')`),
     which hangs and fails in air-gapped OpenShift clusters with zero internet egress.
  4. **Frontend build tooling and supply-chain bloat:** Single-page application (SPA)
     frameworks (React, Vue, Angular) demand Node.js, npm/yarn dependencies, and
     complex bundlers that violate the minimal CPython 3.14 GIL UBI container standard.

- **Decision:** Consolidate the operator console directly into the existing FastAPI
  agent application (`rag-agent` in `src/mainframe_rag/agent/app.py`), served at
  `/ui` (gated by `Settings.ui_enabled`) using server-rendered Jinja2 templates,
  vendored HTMX 1.9.12, and Server-Sent Events (SSE) streaming over the shared
  `answer_core` engine (`src/mainframe_rag/agent/answer.py`).
  
  Decommission the prototype SQLite database and Streamlit container; persist all
  dialogue history, incident attachments, and session state strictly in the
  operator's browser `localStorage`.
  
  Terminate external OpenShift ingress at an official Red Hat `oauth-proxy` sidecar
  container co-located in the `rag-agent` pod on port 8443, proxying authenticated
  traffic over loopback to FastAPI on `http://127.0.0.1:8080`.
  
  Enforce strict air-gap compliance: zero external CDN, font, or script calls;
  pure web-safe system monospace font stacks for authentic IBM 3270 Green Phosphor
  and Modern Dark CSS themes; vendored HTMX 1.9.12 with SHA256 checksum verification
  and 0BSD license; and strict Content-Security-Policy headers restricted to `'self'`.

- **Explicit Non-Goals:**
  1. **No Node.js in production:** No Node.js runtime, npm/yarn packages, or
     JavaScript bundlers in production container images or packaging pipelines.
  2. **No secondary backend service:** No Streamlit server, no Backend-for-Frontend
     (BFF), and no auxiliary container processes alongside the agent.
  3. **No server-side user/session DB:** No SQLite, PostgreSQL, MySQL, Redis, or
     stateful PVCs for UI data; zero server database tables.
  4. **No application-level authentication:** FastAPI implements no custom login forms,
     credential hashing, or session cookie generation; authentication and RBAC are
     delegated entirely to the OpenShift `oauth-proxy` sidecar.
  5. **No remote asset retrieval:** Zero calls to external CDNs, Google Fonts, or
     third-party script/icon registries.
  6. **No unauthenticated external console access:** Unauthenticated requests to `/ui`
     or `/` redirect (HTTP 302) to OpenShift OAuth login.

- **Consequences / rules:**

  ### 1. One-Service Architecture (FastAPI + Jinja2 + HTMX + SSE)
  - **Mounting discipline:** The console is mounted under FastAPI in
    `src/mainframe_rag/agent/app.py` at path `/ui`, with static assets mounted at
    `/ui/static`. The feature is gated by `ui_enabled: bool = False` in `Settings`
    (`src/mainframe_rag/config.py`). When `ui_enabled=False` (fail-closed default),
    `/ui` and its sub-paths return the standard HTTP 404 JSON envelope
    (`{"code": "not_found", "message": "not found"}`) without disclosing route existence.
  - **Shared application core (`answer_core`):** WebUI routes do not maintain separate
    retrieval or prompt assembly logic. Single-turn `/v1/answer`, multi-turn `POST /v1/chat`,
    and `/ui` thin routes invoke the shared `answer_core` engine in
    `src/mainframe_rag/agent/answer.py`. This unifies input validation (`query_max_chars = 2000`),
    complexity modulation, prompt budgeting, hybrid retrieval (dense + BM25 with RRF),
    and reasoning model execution.
  - **Reasoning-model-only contract:** In strict adherence to ADR-0001, all conversational
    and console generation dispatches exclusively to `settings.llm_model_reasoning`.
    No secondary "fast" or conversational LLM tier is permitted. Query condensation
    across turns is deferred behind `chat_condense_enabled: bool = False` in `Settings`.
  - **No-JavaScript fallback:** `POST /ui/chat` inspects incoming headers: HTMX requests
    (`HX-Request: true`) receive an HTML fragment (`_message_pair.html`), while standard
    form POST submissions render the full `index.html` page with the updated turn,
    guaranteeing operator console availability in locked-down or script-disabled browsers.

  ### 2. Browser-Only `localStorage` Session State
  - **Stateless pod contract:** The `rag-agent` service is 100% stateless. Pods mount
    no persistent storage volumes for UI state. Horizontal scaling across 2+ replicas
    requires no sticky sessions, session replication, or distributed lock management.
  - **Client storage schema:** Dialogue history, named incident sessions, and active
    preferences are maintained by `src/mainframe_rag/webui/static/js/console.js`
    under key `mainframe_rag_sessions` in browser `localStorage`.
  - **Storage quota & memory protection:** `console.js` caps saved incident sessions
    at 30 (`MAX_SAVED_SESSIONS = 30`) and automatically evicts the oldest inactive
    sessions by `updated_at`.
  - **Privacy mode degradation:** If browser privacy settings disable `localStorage`
    or throw `QuotaExceededError` / `SecurityError`, `console.js` traps the exception
    and transparently falls back to an in-memory session object for the tab lifecycle.
  - **Client-side Markdown export:** Incident handover reports are assembled into
    Markdown in the browser and downloaded directly via `Blob` (`URL.createObjectURL`),
    requiring zero server roundtrips or server-side file generation.
  - **Prototype decommissioning:** The prototype package `src/mainframe_rag/ui/`
    (`app.py`, `db.py`, `styles.py`) and its SQLite unit tests (`tests/test_ui_db.py`)
    are deprecated and decommissioned. `streamlit` is removed from `pyproject.toml`.

  ### 3. OpenShift `oauth-proxy` Sidecar Route Termination
  - **Sidecar topology:** In `deploy/kustomize/overlays/openshift/agent-prod-patch.yaml`,
    the `rag-agent` pod is patched with an official `oauth-proxy` container listening
    on port 8443 (HTTPS), forwarding authenticated requests to FastAPI on
    `http://127.0.0.1:8080`.
  - **Route termination & encryption in transit:** When `AGENT_ROUTE=true`, the OpenShift
    Route terminates with `tls.termination: reencrypt`. TLS certificates for port 8443
    are provisioned and automatically rotated by the OpenShift Service CA controller via:
    `service.beta.openshift.io/serving-cert-secret-name: rag-agent-tls`.
  - **Cookie encryption:** Session cookies are encrypted via AES-256 GCM using a 32-byte
    random secret mounted from Secret `rag-agent-oauth-cookie`.
  - **Probe bypass discipline:** Kubelet liveness/readiness probes bypass OAuth
    authentication via strictly anchored argument:
    `-skip-auth-regex=^/healthz.*$`
    External monitoring reaching `/healthz` passes through to port 8080 without
    redirection, while all requests to `/ui` or `/` require authentication.
  - **Route timeout configuration:** To accommodate deep reasoning deliberation by the
    underlying model without connection drops, the OpenShift Route manifest must include:
    `haproxy.router.openshift.io/timeout: 300s`
    matching `settings.llm_timeout_s` (300 seconds).
  - **Air-gap supply chain:** The `oauth-proxy` container image is pinned by full SHA256
    digest in `images.txt` (`registry.redhat.io/openshift4/ose-oauth-proxy:v4.14`),
    packaged into the sneakernet bundle by `scripts/airgap/pack.sh`, and loaded into
    the internal disconnected registry by `scripts/airgap/load.sh`.

  ### 4. Zero External CDN/Font Assets & Strict Air-Gap Isolation
  - **Air-gap isolation invariant:** Disconnected enterprise clusters have no access
    to the public internet. All styling, scripts, and fonts resolve locally from
    `/ui/static/`.
  - **System monospace font stacks:**
    - *IBM 3270 Green Phosphor Theme:*
      `'Courier New', Courier, 'Lucida Console', Monaco, 'Liberation Mono', monospace`
      styled with pure CSS procedural scanlines, glow, and CRT curvature.
    - *Modern Dark Theme:*
      `ui-monospace, 'Cascadia Code', 'Source Code Pro', Consolas, 'Liberation Mono', monospace`
      and `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`.
  - **Vendored frontend assets:**
    - `src/mainframe_rag/webui/static/vendor/htmx.min.js`: HTMX 1.9.12 vendored locally,
      pinned to SHA256 `449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452`
      with upstream 0BSD `LICENSE`.
    - `src/mainframe_rag/webui/static/vendor/sse.js`: HTMX SSE extension vendored locally,
      pinned to SHA256 `be05b2e2265279f035271adbea0b72a356f20ce4dfa5870481bfe9c51b822fc1`.
    - Unit tests in `tests/test_webui.py` verify file presence and SHA256 digests.
  - **Content-Security-Policy (CSP) & security headers:** All HTML responses from `/ui`
    enforce strict security headers:
    ```http
    Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self';
    X-Content-Type-Options: nosniff
    X-Frame-Options: DENY
    Referrer-Policy: strict-origin-when-cross-origin
    ```
    This ensures defense-in-depth: even if an untrusted spool dump contains malicious
    scripts, `script-src 'self'` prevents inline execution and `connect-src 'self'`
    blocks outbound data exfiltration.

  ### 5. Superseding Conditions
  Superseding any of these rules—introducing a secondary UI service, server-side
  session storage, Node.js tooling, remote CDN/font links, or altering OAuth Route
  termination—requires an amending or superseding ADR and a synchronized update to
  `docs/architecture.md` in the same PR.
