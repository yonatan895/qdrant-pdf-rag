---
name: writing-for-agents
description: Write or edit documents agents consume (skills and their entry points, AGENTS.md, instruction pointers, owner docs reached by pointers) with sharp context pointers, progressive disclosure and pruning.
---

Read the [pinned writing reference](../../vendor/mattpocock-skills/writing-for-agents/SKILL.md),
and its [skill mechanics](../../vendor/mattpocock-skills/writing-for-agents/SKILL-MECHANICS.md)
when the document is a skill. Apply these repository-specific interpretations:

- Repository rules govern instruction files: root rules only for repository-wide
  policy, behavior detail in its owner contract, and the
  [process budget](../../../docs/agent-workflow.md#process-budget) (link the value;
  stay word-neutral unless an incident requires more).
- Instruction chains have hard byte budgets checked by
  `sh scripts/tools/run-task.sh qa:context`; see the
  [context check format](../../../docs/agent-maintenance.md#context-check-format).
  Run it after every instruction or skill edit.
- Vendored upstream skills stay verbatim under `.agents/vendor/`. Put local
  adaptations in the repository-owned entry point and keep one source of truth:
  an entry point links owner contracts instead of restating them.
- The upstream `disable-model-invocation` and invocation choices describe
  Claude Code and Codex packaging; this repository registers each entry point
  through a name-and-description wrapper in `.claude/skills` (no permissions,
  checked by `qa:context`), routes them through the [skill index](../index.md),
  and adds no automatic instruction chain.
