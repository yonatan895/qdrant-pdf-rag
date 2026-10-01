# OpenCode instruction-loading audit (record)

Moved from `docs/agent-workflow.md` on 2026-10-01 (#582 process budget): a dated
investigation record, not a working rule. Issue #397 is closed; links below are
relative to `docs/`.

The [OpenCode rules guide](https://opencode.ai/docs/rules/) describes project
`AGENTS.md`, global `~/.config/opencode/AGENTS.md`, Claude fallbacks and additional
`instructions` paths/globs/URLs. Ordinary Markdown links do not automatically
load their targets. Keep this repository's technical docs on demand.

For the workflow's **1.18.25** pin, [instruction.ts](https://github.com/anomalyco/opencode/blob/v1.18.25/packages/opencode/src/session/instruction.ts)
tries project names `AGENTS.md`, `CLAUDE.md` (unless disabled), then deprecated
`CONTEXT.md`; it takes upward matches for the first matching name. Its
[findUp helper](https://github.com/anomalyco/opencode/blob/v1.18.25/packages/core/src/fs-util.ts)
collects matches from the invocation directory through the worktree root.
Global rules prefer the OpenCode config directory over the Claude fallback.
Additional configured instructions are combined; local read failures become
empty content. The read/assembly code contains no instruction-byte truncation.
This is not a claim of unlimited model input or proof that a runner loaded a file.
`AGENTS.override.md` is a Codex convention, not an OpenCode override in this pin.
The offline checker inventories the union of known first-party names for audit;
it does not emulate each client's precedence or load global configuration.

[OpenCode's GitHub integration](https://opencode.ai/docs/github/) runs in Actions.
[GitHub documents fresh hosted runners](https://docs.github.com/en/actions/concepts/runners/github-hosted-runners);
our [workflow](../../.github/workflows/opencode.yml) uses `ubuntu-latest`, restores
only the pinned binary directory and writes no instruction configuration.
Together with the current file inventory, this supports the **inference** that
root AGENTS is the project instruction from both audited directories.
[OpenCode configuration](https://opencode.ai/docs/config/) can also merge remote,
global and environment inputs; workflow text alone does not observe those inputs
or the final model request. Keep the fresh runner audit open. Public documentation
also cannot reveal this hosted Codex session's effective loader configuration;
the verified local CLI evidence and documented Codex default remain separate.

For each used entry point, record fresh root and representative-subdirectory
invocations, tool version, exact tested SHA, loader diagnostics (or the specific
missing capability), byte limit and discovered paths. Record redacted metadata
only; never dump a client config, tokens, transcript or private paths. Do not
change global config, `CODEX_HOME`, sandbox or approvals for an audit. Existing
owner overrides remain in place. Unknown loading acceptance stays open under
#397 while independent repository work proceeds.
