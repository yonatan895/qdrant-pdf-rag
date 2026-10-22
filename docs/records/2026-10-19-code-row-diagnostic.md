# Code-row packing diagnostic on a current-rules corpus (record)

Status: dated record for issue #826, run 2026-10-19 and recorded 2026-10-22. Tool:
[`scripts/packing_diagnostic.py`](../../scripts/packing_diagnostic.py), unchanged and read-only, default classes (CMP, TBL, SYS). It
follows up the [2026-10-17 packing diagnostic](2026-10-17-packing-diagnostic.md), which could not attribute the code rows because
its corpus had no code-entry chunks. It holds counts, ranks and identifiers only, with no manual or chunk text.

## Corpus and embedding identity

| Item | Value |
|---|---|
| Collection | alias `colab_manuals_qwen3_8b_4096_c47352b2` -> `colab_manuals_qwen3_8b_4096_c47352b2__gen6741c1c8fd83e5e22d10f9f0f592`, 234,723 points, 452 documents (#829) |
| Extraction rules | `c47352b258f9f94f` (current at the time of the run), so code-entry chunks with `system_codes` are present |
| Embedding | `Qwen/Qwen3-Embedding-8B`, dim 4096, revision attested; serving gate `compatible` |
| Run | G4 sandbox session at `912a23f`, reader key only, rerank off; private run dir `colab-runs/2026-10-19-912a23f/eval-5/` |

## Attribution per SYS row

| Row | Code (normalized) | Outcome | Attribution |
|---|---|---|---|
| SYS-02 | `0C7` | entry heading retrieved and packed | ok |
| SYS-03 | `806` | entry heading retrieved and packed | ok: the earlier refusal came from the stale corpus, which had no code-entry chunks |
| SYS-04 | `B37` | entry heading retrieved and packed | ok |
| SYS-07 | `AFB` | entry heading retrieved and packed | ok: same as SYS-03 |
| SYS-08 | `W064` (wait state 064) | all 2 chunks labelled `W064` in `SA38-0665-03` retrieved and packed | ok |
| SYS-09 | `W0A2` (wait state 0A2) | all 3 chunks labelled `W0A2` retrieved and packed | ok |

Packing: 0 drops in every SYS row.

**Correction to the first reading.** The diagnostic first reported SYS-08 and SYS-09 as `entry-none` (not retrieved). A read-only check
showed both queries parse to the normalized ids (`system_codes: [W064]` / `[W0A2]`, with a must-filter) and retrieve exactly the chunks
carrying those codes. The manual prints the codes as `064` and `0A2`. The `W` prefix exists only in the normalized label
(`regexes.find_system_codes`), and `packing_diagnostic.py` matched the requested ids against chunk text, so it found no `W064`. That
was a diagnostic artifact, not a retrieval miss.

## Conclusion

On the current-rules corpus, all six code rows (0C7, 806, B37, AFB, W064, W0A2) are retrieved and fully packed. The #591 question
(did 806 and AFB lose their entry unit to packing?) is answered: no. Any remaining code-row answer failure is downstream of retrieval
and packing, in model behaviour or verification, and belongs to the answer baseline (#830). No retrieval task for wait-state queries
is needed.

The same prefix mismatch reaches `agent/answer.py::_requested_identifiers`, whose entry-heading anchor uses the normalized ids and so
never matches a wait-state heading. It has no current effect, because all 77 wait-state chunks fit under the 4,000-character excerpt
cap and range selection returns the whole chunk. It is recorded on #588.
