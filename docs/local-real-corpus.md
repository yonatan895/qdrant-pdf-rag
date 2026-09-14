# Use the local console with a preserved real corpus

The release rehearsal uses synthetic PDFs. To use your manuals interactively,
restore their verified **real-embedding** snapshot into a separate local Kind
deployment. Keep CRC stopped, both real models behind the authenticated HTTPS
gateway, and the original cluster and backups preserved. A hash-mode collection
cannot be queried with the real embedder, even if its documents have the same IDs.

Follow [the Kind fallback setup](local-release-fallback.md#3-disposable-kind-with-both-real-models)
for the fresh trusted bundle, isolated clients, registry/node trust, gateway DNS,
Secrets and CA bundle. Use a new cluster name, namespace, private state directory
and kubeconfig for this operational deployment. Keep the release rehearsal's
configuration files and evidence unchanged. Run commands from the verified bundle
workspace; this procedure does not rebuild or modify application images.

## 1. Mount the backup before creating the cluster

Select a backup with a recorded checksum, point count, dense dimension, embedding
model and revision. Verify those records against the running model's immutable
files. Keep PDFs, snapshots, payloads and browser evidence outside Git.

```sh
export REAL_SNAPSHOT=/protected/backups/real_manuals.snapshot
export SNAPSHOT_SHA256=REPLACE_WITH_RECORDED_SHA256
export EXPECTED_POINTS=REPLACE_WITH_RECORDED_POINT_COUNT
export EXPECTED_EMBED_MODEL=REPLACE_WITH_RECORDED_SERVED_MODEL_ID
test -f "$REAL_SNAPSHOT"
printf '%s  %s\n' "$SNAPSHOT_SHA256" "$REAL_SNAPSHOT" | sha256sum -c -
```

Before the `kind create cluster` command in fallback section 3.3, add this mount
to its newly generated configuration. Never delete the preserved original cluster
to add a mount. The backup is mounted read-only through both node and pod.

```sh
python3 - <<'PY'
import json, os
from pathlib import Path
path = Path(os.environ['KIND_STATE']) / 'kind-config.json'
config = json.loads(path.read_text())
config['nodes'][0]['extraMounts'].append({
    'hostPath': str(Path(os.environ['REAL_SNAPSHOT']).resolve(strict=True)),
    'containerPath': '/mnt/restore/real_manuals.snapshot', 'readOnly': True,
})
path.write_text(json.dumps(config))
PY
```

Complete cluster creation, DNS and Secret setup. Use the following local
`QDRANT_EXTRA_VALUES` file in place of the small synthetic sizing file:

```yaml
replicaCount: 1
podSecurityContext: {fsGroup: 3000}
resources:
  requests: {cpu: 200m, memory: 1Gi}
  limits: {cpu: "2", memory: 2Gi}
additionalVolumes:
- name: real-corpus-backup
  hostPath:
    path: /mnt/restore/real_manuals.snapshot
    type: File
additionalVolumeMounts:
- name: real-corpus-backup
  mountPath: /qdrant/snapshots/restore/real_manuals.snapshot
  readOnly: true
```

These are Kind-only overrides. Production keeps its existing SCC, storage and
resource configuration. In the protected operational `AIRGAP_ENV`, set
`QDRANT_STORAGE_SIZE=20Gi`, the new override's absolute path, and
`AGENT_ROUTE=false`. Keep the two agent replicas and existing Jaeger sizing.
Remove `CORPUS_PVC`; do not create the synthetic generator or run synthetic ingest
against this deployment. Check Windows disk headroom for extracted data as well
as host/WSL memory; a PVC capacity is not a reservation of physical disk space.

```sh
make airgap-validate
make airgap-load
make airgap-deploy
kubectl -n "$KIND_NAMESPACE" exec qdrant-0 -- \
  test -r /qdrant/snapshots/restore/real_manuals.snapshot
kubectl -n "$KIND_NAMESPACE" exec deploy/rag-agent -- \
  python3 /app/scripts/probe_gateway.py --require-reasoning --stream
```

Qdrant 1.19 restricts local snapshot recovery to its configured snapshots
directory, including canonical-path checks. Mounting at `/qdrant/restore` fails;
a symlink outside the allowed directory also fails. Use the nested mount above
and the synchronous API response: `wait=false` acknowledges scheduling without
establishing recovery success. See the [pinned path validation](https://github.com/qdrant/qdrant/blob/v1.19.0/lib/storage/src/content_manager/snapshots/download.rs).

## 2. Recover, verify vectors, then enable application routing

This maintenance command refuses a nonempty target. Its 1800-second HTTP timeout
is for snapshot recovery only; application and gateway request deadlines stay
unchanged. Credentials come from the actual agent pod's Secret references.

```sh
kubectl -n "$KIND_NAMESPACE" exec -i deploy/rag-agent -- \
  python3 - "$EXPECTED_POINTS" "$SNAPSHOT_SHA256" "$EXPECTED_EMBED_MODEL" <<'PY'
import json, math, sys, time
import httpx2
from mainframe_rag.config import load_settings
from mainframe_rag.ingest.embed import VllmEmbedder, build_embed_text

expected, checksum, model = int(sys.argv[1]), sys.argv[2], sys.argv[3]
s = load_settings()
assert expected >= 3 and s.embed_model == model
with httpx2.Client(base_url=s.qdrant_url,
                  headers={'api-key': s.qdrant_api_key}, timeout=1800) as q:
    def request(method, path, **kwargs):
        response = q.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()['result']

    assert request('GET', '/collections')['collections'] == []
    assert request('PUT', '/collections/real_manuals/snapshots/recover', json={
        'location': 'file:///qdrant/snapshots/restore/real_manuals.snapshot',
        'priority': 'snapshot', 'checksum': checksum,
    }) is True
    for _ in range(120):
        info = request('GET', '/collections/real_manuals')
        if info['status'] == 'green':
            break
        time.sleep(2)
    assert info['status'] == 'green' and info['points_count'] == expected
    assert info['config']['params']['vectors']['dense']['size'] == s.dense_dim
    assert request('POST', '/collections/real_manuals/points/count',
                   json={'exact': True})['count'] == expected
    points = request('POST', '/collections/real_manuals/points/scroll', json={
        'limit': 3, 'with_payload': True, 'with_vector': ['dense'],
    })['points']
    assert len(points) == 3
    with httpx2.Client(timeout=s.embed_timeout_s) as http:
        embedder = VllmEmbedder(s, client=http)
        for point in points:
            p = point['payload']
            text = build_embed_text(p.get('product'), p.get('version'),
                                   p['doc_id'], p['title'], p['heading_path'],
                                   p['text'], p.get('context'))
            fresh, stored = embedder.dense([text])[0], point['vector']['dense']
            assert len(fresh) == len(stored) == s.dense_dim
            cosine = sum(a*b for a,b in zip(fresh, stored)) / math.sqrt(
                sum(a*a for a in fresh) * sum(b*b for b in stored))
            print(json.dumps({'point_id': point['id'], 'cosine': cosine}), flush=True)
            assert cosine >= 0.995, 'Investigate embedding compatibility before routing'
    assert request('GET', '/aliases')['aliases'] == []
    request('POST', '/collections/aliases', json={'actions': [{'create_alias': {
        'collection_name': 'real_manuals', 'alias_name': 'mainframe_manuals',
    }}]})
    assert request('GET', '/collections/mainframe_manuals')['points_count'] == expected
    print(json.dumps({'points': expected, 'alias': 'mainframe_manuals', 'passed': True}))
PY
```

The alias lets the unchanged application use its default collection name.
Three vector comparisons are a compatibility spot-check, not a quality benchmark;
dimension equality alone is insufficient. Stop if the model/revision, restored
count or similarity differs from expectations. Retain the backup and diagnose
before changing routing. Never print payload text or raw vectors to shared logs.

## 3. Open the console and retain this deployment

```sh
make airgap-smoke
kubectl -n "$KIND_NAMESPACE" port-forward --address 127.0.0.1 svc/rag-agent 8080:8080
```

Keep that terminal running and open **http://localhost:8080/ui** from Windows.
Kind uses private loopback access without an OAuth Route; OAuth coverage belongs
to CRC. Ask a question supported by your manuals, inspect the returned citations,
and verify the stream finishes and Send becomes available again. Test a follow-up
and inspect agent/Qdrant restarts, memory events and Windows/WSL/GPU headroom.
Keep ordinary browser/manual content private. Run one interactive request at a
time with this serving profile; finish gateway diagnostics before submitting it.

For Jaeger, leave a second terminal running:

```sh
kubectl -n "$KIND_NAMESPACE" port-forward --address 127.0.0.1 svc/jaeger 16686:16686
```

Open **http://localhost:16686**, select service `mainframe-rag-agent`, and choose
**Find Traces**. Console requests appear under `ui.chat`; retrieval, prompt and
reasoning spans show where the time was spent. Jaeger uses the retained Badger
PVC. Re-establish this port-forward after a pod replacement or cluster restart.

This deployment is for ongoing use: do not run the disposable-rehearsal cleanup
while it holds the active console. Keep its PVCs, kubeconfig, configuration files
and backup. After an intentional stop or pod replacement, restart the recorded
models/gateway and cluster as appropriate, then re-establish the port-forward and
check the collection, alias and health. Keep CRC and the original Kind nodes
stopped while this local deployment is running.

The recorded restore used 435,057 real-manual points with 1,024-dimensional Qwen
embeddings and the same candidate `89e10d3926b2e7f6a2e6ad0c1450c68c60e1b5f8`.
Three fresh document embeddings had cosine similarity 0.99988 to stored vectors.
An actual `/v1/answer` stream returned eight hits, two verified citations and
`finish_reason=stop` in 39.6 seconds for a concise manual question. Two earlier,
longer console answers completed without verified citations; retain those failed
citation checks in the private record. A fresh Windows browser conversation with
a concise question and follow-up subsequently returned one verified citation per
turn and cleared its busy state after both streams. This procedure does not
establish reliable grounding for every question or a quality benchmark on the
real corpus.
This operational restore adds full-corpus access; it does not replace the
complementary release checks or establish that CRC and both models fit together.
