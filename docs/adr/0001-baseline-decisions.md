# ADR-0001: Baseline decisions adopted from architecture.md

- **Status:** accepted
- **Context:** First implementation; architecture.md §4 locks these.
- **Decision:** Qdrant OSS (vendored Helm, `*-unprivileged` image) on RWO block
  storage; PyMuPDF parser; `.pdx`/`.idx` ignored; FastEmbed BM25 for sparse;
  RRF fusion (local, weighted) until an eval set exists; reasoning model only
  for `/v1/answer`; corpus never leaves the enterprise; Splunk stays system of
  record (context in, not crawl).
- **Consequences:** superseding any of these requires a new ADR and an update
  to architecture.md in the same PR.
