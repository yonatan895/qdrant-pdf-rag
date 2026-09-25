# Literal v1 evidence design witnesses

These original synthetic examples qualify the proposal in [the owning contract](evidence-contract.md#complete-chunk-granularity-and-location). They are golden format inputs, not evidence that exact reads or complete page provenance are implemented. `source_sha256` and `representation_sha256` are fixed fixture digests; no vendor source is included.

Each chunk UUID uses the unchanged UUID5 URL-namespace key `source_revision|Witness N|zero-based start page|0`. Build identity is fixed below. Each JSON line is the exact canonical envelope (UTF-8, **no trailing newline**); its `text` value encodes the exact bytes returned by a successful read. Text is returned whole, never a selected atomic span.

## 1. Two independent atomic items

```json
{"atomic_spans":[{"end":16,"start":0},{"end":34,"start":18}],"build_id":"00000000-0000-4000-8000-000000000001","chunk_id":"ca6ff27f-a5b6-5cfa-9768-a0e570bc5928","chunk_type":"code","locations":[{"end":16,"origin":"source","page":1,"printed_label":"i","start":0},{"end":18,"origin":"separator","page":null,"printed_label":null,"start":16},{"end":34,"origin":"source","page":1,"printed_label":"i","start":18}],"representation_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","schema":1,"source_revision":"synthetic|guide|1|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","text":"//A EXEC PGM=ONE\n\n//B EXEC PGM=TWO"}
```

Envelope SHA-256: `7071c5f88e7c2bd01df79a43f228e4888952a84db2ee0939a18d34d2cd3670b6`.

Exact reference: `e1.AAAAAAAAQACAAAAAAAAAAcpv8n-ltlz6l2ig5XC8WShwccX4jnwr0B33mkPyKOSIiVKoTbLuCTmhjTTSzTZwtg`.

Expected returned UTF-8 text hex: `2f2f4120455845432050474d3d4f4e450a0a2f2f4220455845432050474d3d54574f` (34 bytes).

## 2. Two physical pages, one unknown printed label

```json
{"atomic_spans":[],"build_id":"00000000-0000-4000-8000-000000000001","chunk_id":"5755f0db-8cf2-5fb0-9396-38c04dff673d","chunk_type":"prose","locations":[{"end":14,"origin":"source","page":3,"printed_label":"iii","start":0},{"end":16,"origin":"separator","page":null,"printed_label":null,"start":14},{"end":28,"origin":"source","page":4,"printed_label":null,"start":16}],"representation_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","schema":1,"source_revision":"synthetic|guide|1|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","text":"First µ page.\n\nSecond page."}
```

Envelope SHA-256: `b15402c5428ba101afb7cee746eb15578f74b7c03b1134d55a7b57398241e56a`.

Exact reference: `e1.AAAAAAAAQACAAAAAAAAAAVdV8NuM8l-wk5Y4wE3_Zz2xVALFQouhAa-3zudG6xVXj3S3wDsRNNVae1c5gkHlag`.

Expected returned UTF-8 text hex: `466972737420c2b520706167652e0a0a5365636f6e6420706167652e` (28 bytes).

## 3. One atomic item across a page boundary

```json
{"atomic_spans":[{"end":32,"start":0}],"build_id":"00000000-0000-4000-8000-000000000001","chunk_id":"0da50ae1-f944-5f89-9001-5e8fc27c609c","chunk_type":"code","locations":[{"end":20,"origin":"source","page":8,"printed_label":"8","start":0},{"end":21,"origin":"separator","page":null,"printed_label":null,"start":20},{"end":32,"origin":"source","page":9,"printed_label":null,"start":21}],"representation_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","schema":1,"source_revision":"synthetic|guide|1|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","text":"//STEP EXEC PGM=APP,\n// PARM=YES"}
```

Envelope SHA-256: `4086c9a1085004af2f1627837cbfdc594cb4654a0756e753135604de5e8abba0`.

Exact reference: `e1.AAAAAAAAQACAAAAAAAAAAQ2lCuH5RF-JkAFej8J8YJxAhsmhCFAEry8WJ4N8v9xZTLRlSgdW51MTVgTeXoq7oA`.

Expected returned UTF-8 text hex: `2f2f5354455020455845432050474d3d4150502c0a2f2f205041524d3d594553` (32 bytes).

## Discriminating outcomes

1. Witness 1 returns both independent atomic items with the join separator. Neither atomic span has its own reference or UUID. A budget below the full 34 bytes refuses; it cannot return only the first item.

2. Witness 2 preserves both physical pages and leaves page 4’s printed label null. The two-byte `µ` makes character offsets differ from byte offsets. The separator has no source page; no range compression may invent a second printed label.

3. Witness 3 contains one atomic interval crossing two source pages and a separator. A budget below 32 bytes refuses the whole read. An ingest result with only page-start metadata, two disconnected partial atomic items, or no cross-page completeness proof cannot mint this reference. The witness is an E1 producer obligation, not a claim about today’s page-based chunker.

For all three, changing text, ranges, location or labels changes the digest/reference while the existing chunk UUID stays fixed. Changing the parent chunk/build fields without recomputing a matching token fails lookup verification. An E1 implementation must assert these literal bytes and refusal outcomes independently; serializing its own output and comparing only with itself is insufficient.
