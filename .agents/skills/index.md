# Curated Qdrant operations reference (repository-owned)

This index is first-party; every other file under `.agents/skills/` is verbatim
upstream content from `qdrant/skills`, pinned in
[../qdrant-skills-provenance.md](../qdrant-skills-provenance.md). The set is
deliberately incomplete: Cloud, Edge, multitenancy, multi-language SDK,
deployment-choice, model-migration and search-quality skills are not vendored,
and their absence is not an error. Read a skill only for the Qdrant server
operation it covers; project contracts, approved issues, pinned runtime
behavior and executed evidence override it. Use only the local paths below.
There is no online fallback: ignore upstream links to `skills.qdrant.tech`,
`llms.txt`, snippet search, the Cloud console or `qcloud-cli`.

- [qdrant-version-upgrade](qdrant-version-upgrade/SKILL.md): upgrade paths, compatibility, rolling upgrades.
- [qdrant-sizing](qdrant-sizing/SKILL.md): RAM, disk, CPU and node count.
- [qdrant-scaling](qdrant-scaling/SKILL.md): data volume, QPS and latency scaling.
- [qdrant-monitoring](qdrant-monitoring/SKILL.md): metrics, health checks and production debugging.
- [qdrant-performance-optimization](qdrant-performance-optimization/SKILL.md): search speed, memory and indexing tuning.
