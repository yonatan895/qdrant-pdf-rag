# Test justification ledger (record)

Status: dated snapshot for issue #769 (PR #770), 2026-10-16. Owner:
[testing contract](../testing.md). The live admission rule is
[testing.md §D](../testing.md#test-admission); this record is not a rule and
not evidence about the current suite.

## What it measured

PR #770 simplified the suite from 6,466 to 3,587 collected items and removed
the only unit coverage of 874 lines in `src/` and `scripts/`. The follow-up
restored 73 collected items, each shown to fail when the line it protects is
mutated. At that restoration head the 874 lines accounted as:

| Status | Lines | Basis |
|---|---:|---|
| Unit-covered again | 299 | full unit suite under coverage |
| Executed by a product-path lane | 277 | sim, load and HA lanes under coverage, with subprocess and SIGTERM capture |
| Not executed by any measured run | 298 | traced to the 43 base (`e272663`) tests that ran them; each line either outside any product guarantee or with a current owner shown to catch the failure |

Every kept test carried one class and its owning contract: `core` (product
behavior or data integrity), `infra` (deployment, CI-gate or tooling
contract), `noise-kept` (timing- or wording-sensitive, kept for a stated
contract) and `gate-control` (negative control or gate-integrity test).

## Where the detail lives

The full per-test table (guarantee and owner for 2,422 functions) and the
298-line trace remain in Git history, bound to the 2026-10-16 tree:

```bash
git show 7c4bb00:docs/records/2026-10-16-test-justification-ledger.md
```

Tests named there have since been removed or renamed (Jaeger-era and legacy
migration names among them), two test files were deleted
(`tests/test_local_jaeger_sh.py`, `tests/test_ingest_repair.py`), and the
collected-item counts above are superseded. Read it as the state at that
commit, never as the current coverage map.
