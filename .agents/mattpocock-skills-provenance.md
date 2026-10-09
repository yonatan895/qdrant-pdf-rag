# Matt Pocock engineering skills — pin record

- Source: https://github.com/mattpocock/skills (MIT).
- Pinned upstream SHA: `49dd158d1076134a641b33efb035946536778336`
  (upstream and installed 2026-10-09).
- License: verbatim upstream `LICENSE` in `LICENSE.mattpocock-skills`;
  attribution and pin in `NOTICE.mattpocock-skills`.
- Authority: the maintainer's 2026-10-09 request to add the three highest-ranked
  skills reviewed in this conversation; baseline was no installed engineering skills.

## Selected paths

Verbatim copies of upstream `skills/engineering/<name>/**` live under
`.agents/vendor/mattpocock-skills/<name>/`. Their byte digests are recorded in
`mattpocock-skills.sha256` beside this file. Repository-owned entry points with
the same names live under `.agents/skills/`; they state compatibility rules
before linking the originals. The context checker validates the union of this
allowlist and the independent [Qdrant allowlist](qdrant-skills-provenance.md).

<!-- skills-allowlist:start -->
- codebase-design
- diagnosing-bugs
- tdd
<!-- skills-allowlist:end -->

Read skills on demand. Existing owner contracts establish testing interfaces,
stored-content and lifecycle proof, coverage preservation, delegation limits,
and review. Upstream references to `code-review` resolve to the repository's
review protocol; no additional skill is implicitly installed. Root instructions
and runtime/deployment behavior are unchanged.

## Updating

Change the pin in a dedicated maintenance PR. Stage the same three upstream
directories with the skill-installer helper's `--ref <sha>` and `--dest <temp>`
options, review their full diff and supporting files, then replace only these
verbatim subtrees. Refresh the license, notice, byte-digest manifest and license
inventory bindings together. Keep compatibility edits in the repository entry
points. Adding another skill requires a separate scope decision.

Verify the manifest with `sha256sum -c .agents/mattpocock-skills.sha256` and run
`sh scripts/tools/run-task.sh qa:context` plus the focused context and license
suites. Review entry-point and upstream-reference behavior separately from
structural checks. All referenced material is local; no runtime fetch is needed.
