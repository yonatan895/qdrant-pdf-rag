# pstack skills — pin record

- Source: https://github.com/cursor/plugins/tree/main/pstack (MIT).
- Pinned upstream SHA: `f713f14203cd6420cec13cd7c55c32d598dbab68`
  (upstream 2026-10-16, installed 2026-10-16; plugin version 0.15.25).
- License: verbatim upstream `pstack/LICENSE` in `LICENSE.pstack-skills`;
  attribution and pin in `NOTICE.pstack-skills`.
- Authority: the maintainer's 2026-10-16 request to review `pstack/skills` and
  integrate the fitting ones; selection and exclusions recorded in
  [issue #765](https://github.com/yonatan895/qdrant-pdf-rag/issues/765).

## Selected paths

Verbatim copies of upstream `pstack/skills/<name>/**` live under
`.agents/vendor/pstack-skills/<name>/`, checked against the upstream git blob
hashes at the pin. Their byte digests are recorded in `pstack-skills.sha256`
beside this file. Repository-owned entry points with the same names live under
`.agents/skills/`; they state compatibility rules before linking the originals.
The context checker validates the union of this allowlist and the independent
[Qdrant](qdrant-skills-provenance.md) and
[engineering](mattpocock-skills-provenance.md) allowlists.

<!-- skills-allowlist:start -->
- benchmark-checklist
- blast-radius
- principle-explain-the-number
- principle-attack-the-premise
- principle-make-operations-idempotent
<!-- skills-allowlist:end -->

Read skills on demand. Upstream links and names for pstack skills, playbooks,
models or subagent rules that are not vendored here (`how`, `why`, `arena`,
`unslop`, `poteto-mode` playbooks, other `principle-*` skills) do not resolve
to installed content; each entry point names the local owner that replaces them.
The upstream `disable-model-invocation` field is Cursor-specific and has no
effect here. Root instructions and runtime/deployment behavior are unchanged.

## Updating

Change the pin in a dedicated maintenance PR. Download the pinned upstream tree
into an isolated directory, compare each selected file with the upstream git blob
hash at the new pin, review the full diff, then replace only these verbatim
subtrees. Refresh the license, notice, byte-digest manifest and license inventory
bindings together. Keep compatibility edits in the repository entry points.
Adding another skill requires a separate scope decision.

Verify the manifest with `sha256sum -c .agents/pstack-skills.sha256` and run
`sh scripts/tools/run-task.sh qa:context` plus the focused context and license
suites. Review entry-point and upstream-reference behavior separately from
structural checks. All referenced material is local; no runtime fetch is needed.
