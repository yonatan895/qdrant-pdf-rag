# Vendored Qdrant skills — pin record

- **Source:** https://github.com/qdrant/skills (Apache-2.0)
- **Vendored at:** commit `bcca2c2da1c00992038e83038e01791d468f943d`
  (upstream commit 2026-08-27; vendored on 2026-08-28 — the last release tag
  `v0.1.0` (2026-03-30) predates significant skill updates, so the SHA is the
  pin; keep SHA-only pins until upstream tags again)
- **Copied into:** `.agents/skills/` (upstream `skills/` only — `index.md`
  plus the skill directories; no `evals/`, `webapp/`, blog assets, or CI)
- **License:** Apache-2.0, kept verbatim in `LICENSE.qdrant-skills`;
  attribution is recorded in `NOTICE.qdrant-skills`.

This is a repository-owned provenance record. The full pinned snapshot remains
in place while #458 owns the proposed operational-skill curation. Relocation
under #523 does not change the approved copied paths or upstream pin.

## Air-gap note

`.agents/skills/` is the complete skill set for this repository. Upstream
files may reference `skills.qdrant.tech` (llms.txt, snippet search, online
skill URLs) or `qcloud-cli`; none of that is available or permitted here —
see [vendored skill routing](../docs/agent-workflow.md#qdrant-skills). Do not
patch the vendored tree to fix such references; repository policy owns them.

## Updating

Bump the pinned SHA in a dedicated PR (not drive-by); pin-bump PRs refresh
the snapshot, they do not rewrite vendor files:

    git clone --quiet https://github.com/qdrant/skills.git /tmp/qdrant-skills
    git -C /tmp/qdrant-skills checkout <new-sha>
    rm -rf .agents/skills && mkdir -p .agents/skills
    cp -r /tmp/qdrant-skills/skills/* .agents/skills/
    cp /tmp/qdrant-skills/LICENSE LICENSE.qdrant-skills
    # update .agents/qdrant-skills-provenance.md SHA/date lines, then one PR

Commands used for this vendoring (connected host):

    git clone --quiet https://github.com/qdrant/skills.git /tmp/qdrant-skills
    cp -r /tmp/qdrant-skills/skills/* .agents/skills/
    cp /tmp/qdrant-skills/LICENSE LICENSE.qdrant-skills
    git -C /tmp/qdrant-skills rev-parse HEAD   # -> recorded SHA above
