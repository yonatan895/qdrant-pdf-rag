# Agent instruction maintenance

Owns the instruction-chain budgets, the loader audit, fresh-context exercises and the
offline context-check/prerequisite format. Read it when editing `AGENTS.md`, an instruction
file, a skill or a workflow doc, or when diagnosing `qa:context`/`dev:doctor`; it is
reference, not entry-path reading.

<a id="instruction-loading"></a>
## Instruction loading audit

Repository maintenance budgets: root **8,192 UTF-8 bytes**; each supported
first-party automatic directory chain **24,576 bytes including two newline
separator bytes between files**; the entry path (`AGENTS.md` + `docs/agent-workflow.md`)
**32,768 bytes**, and all below any smaller effective client limit.
These are project budgets, not universal model limits. Keep ordinary docs on
demand. A new nested instruction file needs a directory-specific reason and a
fresh loader audit; arbitrary siblings are not automatically loaded.

The [official Codex guide](https://developers.openai.com/codex/guides/agents-md/)
describes a default 32 KiB project limit, root-to-working-directory discovery,
`AGENTS.override.md` precedence and configurable fallback names. These are
Codex-specific defaults; verify the actual version/configuration. Files read
later through tools are not proof of automatic discovery. Raising a personal
limit is a temporary diagnostic option, not this repository's solution.

<!-- instruction-chains:start -->
| Invocation directory | First-party automatic chain |
|---|---|
| `.` | [root](../AGENTS.md) |
| `src/mainframe_rag` | [root](../AGENTS.md) |
| `tests` | [root](../AGENTS.md) |
<!-- instruction-chains:end -->

Audit inventory on 15 September 2026, baseline `9fece72df92ca5da414bc8f5b05cb2f89fcd18c8`:
only root `AGENTS.md` is present as a first-party instruction file; no nested
AGENTS/overrides, CLAUDE/GEMINI/CONTEXT, Cursor or Copilot instructions were found.
The GitHub opencode workflow contains an inline review prompt and a comment-agent
entry point. Vendored skills are on-demand third-party guidance, not a replacement
workflow. The original root was 45,644 bytes; that creates a loading risk, not
proof this particular conversation was truncated.

| Agent/tool and version | Invocation / directory | Discovered instruction paths | Effective relevant byte limit | Overrides/fallbacks | Explicit follow-up reads | Evidence / unknowns |
|---|---|---|---|---|---|---|
| Current Codex hosted session | repository root | User-supplied AGENTS excerpt and skill catalog visible; automatic loader paths unverified | Unavailable in hosted diagnostics | User-supplied excerpt is not a loader log | Entire baseline AGENTS and contract owners | Session owner must verify hosted loader; no truncation claim |
| Local Codex CLI 0.154.0 | fresh `codex debug prompt-input` from root and `src/mainframe_rag` | Complete root AGENTS, including its tail, present byte-for-byte in model-visible input | Config limit unset; documented default 32 KiB; complete 7,246-byte entry observed in both diagnostics | No global AGENTS/override or project config found; fallback key unset | Fresh semantic exercises recorded in the implementation PR | Diagnostic exit 0 in both directories; redacted metadata only retained. This proves local CLI loading, not hosted or CI loading |
| GitHub OpenCode 1.18.25 (workflow pin) | `github run` in workflow checkout; no working-directory override | Root AGENTS expected from root/subdirectory under the pinned discovery code; actual runner input not captured | No byte cap in pinned instruction read/assembly; effective model/request limit unverified | No tracked OpenCode config; workflow sets no instruction override and caches only the binary; runtime global/remote inputs unverified | Review prompt names this guide; ordinary Markdown links are not automatically followed | Official docs and pinned source reviewed below; CI maintainer owns remaining fresh root/subdirectory runtime audit |

The OpenCode pinned-source loading audit is a dated record:
[OpenCode instruction-loading audit](records/2026-10-01-opencode-instruction-loading-audit.md).

### Fresh-context exercises

Run bounded read-only planning/review tasks through the actual setup, supplying
the task packet and using the context map. Record sources read, omissions and
semantic decisions, not exact wording:

- Representation/publication: find completion, publish, cache and configuration;
  identify unmarked-data and active-reader cases; names/markers are not proof.
- Required configuration: trace example, override, preflight, both renders and
  runtime; use gateway handoff; no invented production value/attestation bypass.
- Test consolidation: preserve independent outcomes/faults and fake
  projection/capability semantics; no universal fake or weakened product tests.

Keep the dated evidence in the implementation PR. These exercises cannot prove
all future tasks succeed. For a small later sample, use existing PR records to
note review iterations, setup/context failures and escaped-defect categories.
No fixed ten-PR study or invented percentage improvement blocks this issue.

<a id="context-check-format"></a>
## Offline check format and prerequisite diagnosis

`sh scripts/tools/run-task.sh qa:context` checks deterministic structure only. The marked
[context map](agent-workflow.md#context-map) and the instruction-chains table below are the
curated input. Each contract row
has one canonical owner link; other cells name boundary/evidence links. Supported
links are ordinary `[label]` followed by `(relative/path)`, with an optional `#explicit-id` whose
target declares `<a id="explicit-id"></a>`. Paths are relative to the containing
file, with no spaces, URLs, titles or reference-style indirection in checked
local links. Documentation-to-documentation links stay relative. GitHub-facing
templates under `.github/` are copied into issue/PR bodies, so they must use
explicit canonical repository-file URLs
(`https://github.com/yonatan895/qdrant-pdf-rag/blob/main/...` with an explicit
`#anchor`); the checker maps that prefix offline to the current checkout and
validates scheme, host, repository and ref, then applies the shared containment
and explicit-anchor checks, including decoded paths and symlinks, without network
access. Relative local links inside `.github/` templates are rejected.
Unrelated external links are not crawled. Inspect a rendered body preview when
changing template links; before merge, use a head-pinned equivalent for
click-through, since a new file/anchor may not exist on `main` yet.
The checker does not establish semantic consistency or agent understanding.

`sh scripts/tools/run-task.sh dev:doctor` defaults to `unit`; `PROFILE=sim` and `PROFILE=deploy` add
CLI/pin prerequisites. Run `python3 scripts/agent_doctor.py --profile sim
--probe-docker` for the optional local-socket probe (one command). Exit 0 means
verified prerequisites, 2 means missing or unable-to-verify prerequisites with
distinct labels, and 1 means internal checker failure. It is read-only,
standard-library-only and performs no
service probe by default. An explicitly requested bounded Docker probe may
report unavailable access. Results distinguish `ready`, `missing prerequisite`
and `unable to verify`; readiness is not test/application/production acceptance.
It never sources `.env`, reads secret values, installs, launches or repairs.
Use the tier's existing runbook after diagnosing prerequisites.
