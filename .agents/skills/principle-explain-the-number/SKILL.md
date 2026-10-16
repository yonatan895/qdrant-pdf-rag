---
name: principle-explain-the-number
description: Before trusting, reporting or acting on a measured speedup, regression, throughput, latency or eval result, name what limits it and rule out that it measured something other than the intended work.
---

Read the [pinned principle](../../vendor/pstack-skills/principle-explain-the-number/SKILL.md).
Apply these repository-specific interpretations where it differs:

- For a performance number, use the local
  [benchmark-checklist](../benchmark-checklist/SKILL.md) entry point.
- For an eval result, keep the denominator complete (every error, refusal and
  truncation per case), compare per case only on matching dataset, model and
  configuration identities, and treat sampled rates as trend data per the
  [answer tier](../../../docs/testing.md#answer-tier). Never rewrite a baseline to
  explain a number.
- The upstream Prove It Works principle is not installed; the verification
  section of [AGENTS.md](../../../AGENTS.md) owns proof of real outcomes.
