# Curated skills reference (repository-owned)

Read skills on demand. Project contracts, approved issues, pinned runtime
behavior and executed evidence govern their use. Use the local paths below.

## Engineering

These repository-owned entry points link unchanged upstream references and
state local compatibility rules; [provenance](../mattpocock-skills-provenance.md)
owns their source pin, license and allowlist.

- [diagnosing-bugs](diagnosing-bugs/SKILL.md): reproducible diagnosis and regression proof.
- [tdd](tdd/SKILL.md): one behavior at a time through approved testing interfaces.
- [codebase-design](codebase-design/SKILL.md): module interfaces, dependency seams and testability.

## Measurement, impact and lifecycle

These repository-owned entry points link unchanged upstream `pstack` references
and state local compatibility rules; [provenance](../pstack-skills-provenance.md)
owns their source pin, license and allowlist.

- [benchmark-checklist](benchmark-checklist/SKILL.md): vet a measured performance number before reporting it.
- [principle-explain-the-number](principle-explain-the-number/SKILL.md): name the limiter behind a perf or eval number.
- [blast-radius](blast-radius/SKILL.md): breakage beyond the diff, with the safety fact proven by running code.
- [principle-attack-the-premise](principle-attack-the-premise/SKILL.md): test the shared premise after repeated failed fixes.
- [principle-make-operations-idempotent](principle-make-operations-idempotent/SKILL.md): converge after repeats, crashes and retries.

## Qdrant operations

These directories are verbatim upstream content from `qdrant/skills`, pinned in
[the Qdrant provenance record](../qdrant-skills-provenance.md). The set is
deliberately incomplete: Cloud, Edge, multitenancy, multi-language SDK,
deployment-choice, model-migration and search-quality skills are not vendored,
and their absence is not an error. Read a Qdrant skill only for the server
operation it covers. There is no online fallback: ignore upstream links to
`skills.qdrant.tech`, `llms.txt`, snippet search, the Cloud console or `qcloud-cli`.

- [qdrant-version-upgrade](qdrant-version-upgrade/SKILL.md): upgrade paths, compatibility, rolling upgrades.
- [qdrant-sizing](qdrant-sizing/SKILL.md): RAM, disk, CPU and node count.
- [qdrant-scaling](qdrant-scaling/SKILL.md): data volume, QPS and latency scaling.
- [qdrant-monitoring](qdrant-monitoring/SKILL.md): metrics, health checks and production debugging.
- [qdrant-performance-optimization](qdrant-performance-optimization/SKILL.md): search speed, memory and indexing tuning.
