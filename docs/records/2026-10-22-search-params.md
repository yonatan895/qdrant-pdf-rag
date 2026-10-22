# Dense search-time parameters (record)

Status: dated record for issue #842, 2026-10-22. Base: `main` `a936b66`. Owner of the pinned values:
[retrieval contracts](../retrieval.md#2-prefetch-and-filters) and [agent settings](../agent.md). Read-only: no collection,
index or data change; the frozen holdout was not read. It holds counts and metrics only (no query or manual text).

## Question

No dense query sent `SearchParams`, so HNSW `ef` and the INT8 quantization rescore/oversampling were server defaults on
a quantized collection. What do those defaults do against exact search, and which pinned values are worth sending?

## Setup

- Collection: alias `colab_manuals_qwen3_8b_4096_c47352b2` (current rules `c47352b2`, local Kind Qdrant 1.19.0,
  `qdrant-client` 1.19.0): 234,723 points, 9 segments, status green, all vectors indexed. Dense: 4096-d cosine,
  `on_disk`, HNSW `m=16 ef_construct=128`, INT8 scalar quantization (`quantile=0.99`, `always_ram`).
- Read-only key; supervised port-forward (`scripts/kube_forward.py`); client on the same host as the cluster.
- Run under the heavy lock, one process, sequential requests.

## Method

- **Queries are stored vectors**: no query-embedding model (8B on a GPU) was available. A fixed random sample
  (`random.Random(842)` over the sorted point ids) of corpus points supplied their own stored `dense` vectors as queries:
  200 points unfiltered, and the first 100 of them that carry a `product` payload with the filter
  `product == <that point's product>` (the production scope-filter shape; payload-indexed).
- **Ground truth**: `exact=True` with `quantization.ignore=True` (brute force over the stored float32 vectors), top 41.
- **Overlap**: for each query, the query point itself is dropped from both the approximate and the exact result
  (limit k+1), then `|approx ∩ exact| / k` at k 8 and k 40. `perfect@k` is the share of queries with overlap 1.0.
- **Candidates**: `hnsw_ef` in {server default, 64, 128, 256, 512} x quantization in {server default, `rescore=false`,
  `rescore=true`, `rescore=true` + oversampling 1.0 / 2.0 / 4.0} = 30 settings, each at k 8 and k 40.
- **Latency**: client wall time of one `query_points` per call (request build, HTTP, JSON), sequential, after a
  30-query warm-up; settings were interleaved per query in a random order to spread drift. p50/p95 over 200 (100)
  calls per cell. `same_as_default` is the share of queries whose ordered id list equals the server-default result.
- Counts: 200 + 100 queries x 30 settings x 2 k = 18,000 timed calls, plus 300 exact ground-truth calls (median 1.9 s
  unfiltered, 3.0 s filtered).

## Results

Unfiltered, 200 queries (overlap with exact; latency in ms, p50 / p95 at k 8 and k 40):

| ef | quantization | overlap@8 | perfect@8 | overlap@40 | perfect@40 | lat@8 | lat@40 |
|---|---|---|---|---|---|---|---|
| default | default | 0.9844 | 0.885 | 0.9834 | 0.475 | 11.4 / 13.3 | 11.4 / 13.8 |
| default | rescore=false | 0.9844 | 0.885 | 0.9834 | 0.475 | 11.4 / 13.4 | 11.4 / 13.6 |
| 128 | default | 0.9844 | 0.885 | 0.9834 | 0.475 | 11.4 / 13.5 | 11.3 / 13.5 |
| default | rescore=true | 0.9931 | 0.945 | 0.9946 | 0.850 | 11.4 / 15.0 | 11.5 / 19.6 |
| default | rescore=true, os 1.0 | 0.9931 | 0.945 | 0.9946 | 0.850 | 11.5 / 16.7 | 11.7 / 26.4 |
| 128 | rescore=true, os 2.0 | 1.0000 | 1.000 | 0.9997 | 0.990 | 11.5 / 19.0 | 12.0 / 44.5 |
| 128 | rescore=true, os 4.0 | 1.0000 | 1.000 | 0.9999 | 0.995 | 11.6 / 29.9 | 14.6 / 115.0 |
| 64 | default | 0.9844 | 0.885 | 0.9828 | 0.475 | 8.2 / 10.4 | 8.2 / 10.1 |
| 64 | rescore=true, os 2.0 | 1.0000 | 1.000 | 0.9997 | 0.990 | 8.5 / 19.8 | 9.9 / 53.4 |
| 256 | rescore=true, os 2.0 | 1.0000 | 1.000 | 0.9997 | 0.995 | 16.7 / 21.0 | 17.4 / 52.6 |
| 512 | rescore=true, os 2.0 | 1.0000 | 1.000 | 0.9997 | 0.995 | 24.4 / 34.6 | 25.7 / 66.0 |

Product-filtered, 100 queries: the same pattern. Server default 0.9825 / 0.9830 (overlap@8 / @40, perfect@40 0.430);
`rescore=true` 0.9975 / 0.9980 (0.920); `rescore=true`, os 2.0 (ef 128) 1.0000 / 0.9997 (0.990); p50 10.8 ms at k 8 and
11.2 ms at k 40 (p95 17.4 ms at both) for ef 128 / os 2.0, against 10.7 ms and 12.4 / 12.7 ms for the default.

Findings:

1. **The server default is `rescore=false` on this collection.** Default and explicit `rescore=false` returned
   identical ordered ids for 200/200 (unfiltered) and 100/100 (filtered) queries at both k, so the INT8 scores
   decide the order of the dense leg today.
2. **`hnsw_ef` default equals 128** (the collection's `ef_construct`): ef 128 returned identical ids to the default in
   every query at both k and both conditions. Recall does not improve above it (ef 256 and 512: overlap within 0.0001
   of ef 128) while p50 rises about 1.45x (256) and 2.1x (512). ef 64 loses 0.0006 overlap@40 (unfiltered) and is the
   fastest (p50 8.2 ms), but changes ids for 6.5% of queries at k 40.
3. **Rescoring is the effective lever**: overlap@8 0.984 -> 0.993 (os 1.0) -> 1.000 (os 2.0); overlap@40 0.983 ->
   0.995 -> 0.9997; queries with the exact top 8 in 88.5% -> 94.5% -> 100%. Oversampling 4.0 adds at most 0.0002 and
   a p95 of 115 ms at k 40; it is rejected. p50 cost of os 2.0 is about +0.1 ms at k 8 and +0.6 ms at k 40; the p95
   tail grows (13 -> 19 ms at k 8, 14 -> 45-51 ms at k 40 on this host).

## Decision

Pinned as `Settings` defaults and sent on every dense query: `dense_hnsw_ef = 128`, `dense_quant_rescore = true`,
`dense_quant_oversampling = 2.0`.

- `dense_hnsw_ef = 128` changes nothing today (it is the effective default) and protects against a server default
  change.
- Rescore on with oversampling 2.0 **is a behavior change**: the dense candidate lists move from the quantized order
  to the near-exact order (43.5% of unfiltered and 45% of filtered queries change their top-8 id order versus the
  server default). It follows the measured ranking against exact search and does not change identity, payload,
  collection or index configuration. To restore today's behavior set the defaults back to
  `dense_quant_rescore = false` and `dense_quant_oversampling = None` (the measured `rescore=false` setting; rescore
  off with oversampling 2.0 was not measured).

## Limitations

- **Stored-vector queries** are in-distribution document embeddings: each has a near-duplicate neighbourhood (its own
  chunk and its page and section siblings) and a different geometry from natural-language query embeddings, which sit
  farther from the documents. ANN recall and the benefit of rescoring on real queries can differ in either
  direction. The overlap numbers prove the mechanism and the cost; they are not the production recall.
- **Latency is dev-host** (WSL, local Kind, loopback, client wall time including request serialization of a 4096-d
  vector). Use it for ratios between settings, not as a production figure. p95 values of 100 to 200 calls are noisy.
- The measurement says nothing about answer quality: overlap with exact search is not relevance, and no golden or
  holdout row was run.
- **Confirmation with real query vectors belongs to the next G4 session**: capture real query embeddings (or run
  `eval_retrieval --no-check` on dev only) with rescore on against `dense_quant_rescore = false`, and compare dense
  overlap with `exact=True` and dev retrieval metrics. Revert the default if dev metrics regress. The frozen holdout
  stays untouched.
