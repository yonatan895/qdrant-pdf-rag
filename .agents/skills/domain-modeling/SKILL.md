---
name: domain-modeling
description: Sharpen the project's domain language and record hard-to-reverse decisions; use when challenging terminology against code, defining a project term, or proposing or editing an ADR.
---

Read the [pinned domain-modeling reference](../../vendor/mattpocock-skills/domain-modeling/SKILL.md)
with its [ADR](../../vendor/mattpocock-skills/domain-modeling/ADR-FORMAT.md) and
[glossary](../../vendor/mattpocock-skills/domain-modeling/GLOSSARY-FORMAT.md) formats.
Apply these repository-specific interpretations where they differ:

- ADRs use this repository's [template](../../../docs/adr/0000-template.md) and
  numbering in `docs/adr/`, with the upstream three-part test for when to offer
  one. Superseding a baseline decision follows
  [ADR-0001](../../../docs/adr/0001-baseline-decisions.md). Historical outcomes
  stay in issues, PRs and dated records, not ADRs.
- Owner contracts define protected vocabularies and states (the four `chunk_type`
  values, verification states, identity and publication terms). A glossary entry
  links its owner and never redefines or renames them; changing such a term is a
  dedicated approved concern.
- The glossary is `docs/glossary.md`, created only when the first term is
  resolved with the maintainer; no root `GLOSSARY.md` or multi-context map.
- Cross-reference code with CodeGraph when `.codegraph/` exists.
