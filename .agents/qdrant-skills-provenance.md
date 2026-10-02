# Vendored Qdrant skills — pin record

- **Source:** https://github.com/qdrant/skills (Apache-2.0)
- **Pinned upstream SHA:** `bcca2c2da1c00992038e83038e01791d468f943d`
  (upstream commit 2026-08-27; vendored 2026-08-28 — the last release tag
  `v0.1.0` (2026-03-30) predates significant skill updates, so the SHA is the
  pin; keep SHA-only pins until upstream tags again)
- **License:** Apache-2.0, kept verbatim in `LICENSE.qdrant-skills`;
  attribution is recorded in `NOTICE.qdrant-skills`.

`.agents/skills/` is a curated, intentionally incomplete subset (#458), not the
upstream snapshot. Removed on purpose: `qdrant-clients-sdk`,
`qdrant-deployment-options`, `qdrant-edge`, `qdrant-model-migration`,
`qdrant-multitenancy`, `qdrant-search-quality` (project contracts own those
decisions; see [vendored skill routing](../docs/agent-workflow.md#qdrant-skills)).

## Vendored paths (allowlist)

Verbatim copies of upstream `skills/<name>/**` at the pinned SHA.
`scripts/check_agent_context.py` requires the top-level directories under
`.agents/skills/` to equal this list exactly.

<!-- skills-allowlist:start -->
- qdrant-monitoring
- qdrant-performance-optimization
- qdrant-scaling
- qdrant-sizing
- qdrant-version-upgrade
<!-- skills-allowlist:end -->

Repository-owned (not upstream): `.agents/skills/index.md` and this file.

## Air-gap note

Upstream files may reference `skills.qdrant.tech` (llms.txt, snippet search,
online skill URLs) or `qcloud-cli`; none of that is available or permitted here.
Do not patch the vendored tree to fix such references; repository policy owns
them.

## Updating

Bump the pinned SHA in a dedicated PR (not drive-by). A pin bump refreshes only
the allowlisted paths; it never adds a skill category, rewrites repository
policy or edits vendor files. Adding or removing a category needs an explicit
scope decision that changes the allowlist above in the same PR.

    git clone --quiet https://github.com/qdrant/skills.git "$TMPDIR/qdrant-skills"
    git -C "$TMPDIR/qdrant-skills" checkout <new-sha>
    for s in $(sed -n '/skills-allowlist:start/,/skills-allowlist:end/s/^- //p' \
        .agents/qdrant-skills-provenance.md); do
      rm -rf ".agents/skills/$s" && cp -r "$TMPDIR/qdrant-skills/skills/$s" .agents/skills/
    done
    cp "$TMPDIR/qdrant-skills/LICENSE" LICENSE.qdrant-skills
    # update the SHA/date lines here and in NOTICE.qdrant-skills, then
    # sh scripts/tools/run-task.sh qa:context; review upstream diffs for the
    # retained subtrees only. Do not copy upstream index.md or other directories.
