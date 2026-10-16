---
name: principle-attack-the-premise
description: When two or more fixes that share one premise have failed the same gate, write the premise down and test it directly before writing another fix that assumes it.
---

Read the [pinned principle](../../vendor/pstack-skills/principle-attack-the-premise/SKILL.md).
Apply these repository-specific interpretations where it differs:

- Its links to other upstream principles are not installed. Make the premise check
  rerunnable as a committed test or script per
  [test design](../../../docs/testing.md#evidence-design); trace causes with
  [diagnosing-bugs](../diagnosing-bugs/SKILL.md); reshape a design with
  [codebase-design](../codebase-design/SKILL.md); keep the fix to one concern per
  [AGENTS.md](../../../AGENTS.md).
- Record the written premise, the observation that tested it and its result in the
  issue or PR record with the evidence location.
- When the gate is a paid or long run, test the premise offline first whenever it
  can be tested offline. Issue #762 is the local example: two sessions failed on
  the premise that the Qdrant forward only needed supervision, and an offline
  idle-reuse sweep refuted it.
