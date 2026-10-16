---
name: retro
description: Run a retrospective on a coding session and propose environment improvements (navigation, automated checks, instruction pruning, tool economy), most severe first.
---

Read the [pinned retrospective](../../vendor/mattpocock-skills/retro/SKILL.md) and
apply the local [writing-for-agents](../writing-for-agents/SKILL.md) entry point
where it calls that skill. Apply these repository-specific interpretations:

- Present proposals; do not apply them in the retrospective. Each proposal links
  its value (an escaped defect, a measured cost or a maintainer request) under the
  [process budget](../../../docs/agent-workflow.md#process-budget).
- There is no `CODING_STANDARDS.md`: standards live in owner contracts reached
  through the [context map](../../../docs/agent-workflow.md#context-map), and the
  reviewer applies them through the [code-review](../code-review/SKILL.md) entry
  point. Prefer an automated check (lint, test, `qa:context`) over a new rule.
- Read only the named session's own logs and repository records. Proposals and
  any written summary contain no secrets, private configuration or manual text.
