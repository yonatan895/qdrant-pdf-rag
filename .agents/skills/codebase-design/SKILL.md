---
name: codebase-design
description: Design or improve module interfaces, dependency seams, and testability while preserving the project's ownership, authority, and lifecycle boundaries.
---

Read the [pinned module-design reference](../../vendor/mattpocock-skills/codebase-design/SKILL.md).
Use its deep-module principles with these repository-specific interpretations:

- Start from the [module and state map](../../../docs/architecture.md#boundary-map)
  and existing owner contracts. Use CodeGraph first when indexed, and trace the
  affected unchanged producers, persistence, callers and consumers.
- Preserve established domain terms and precise technical names such as API,
  service and security boundary; upstream vocabulary is design guidance, not a
  requirement to rename repository concepts or create a glossary.
- Read/write capabilities, transport adapters and client ownership can justify
  separate interfaces. Assess their authority and lifetime rather than deleting
  a separation solely because only one production adapter exists.
- The upstream deepening guide's test deletion advice is conditional here:
  preserve behavior coverage and map old cases to retained tests, or justify
  retirement of an implementation-only pin, under [test design](../../../docs/testing.md).
- Use the alternative-interface exercise only when the task calls for comparing
  designs. Existing delegation permissions and concurrency limits still apply.
  Record decisions with their existing owner and finish through the local
  [review protocol](../../../docs/agent-workflow.md#review-handoff).
