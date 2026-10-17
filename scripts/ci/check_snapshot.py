"""CI-only snapshot administration in a short-lived job with ingest credentials."""
import argparse
import copy
import json
import re
import subprocess
import uuid

# Executed inside the published ingest image; no credential values leave the pod.
SNAPSHOT_CHECK = """
from mainframe_rag.config import load_settings
import httpx2
import uuid

SAMPLE = 256


def need(ok, message):
    # Fixed messages only: stored payloads hold manual text and must never be printed.
    if not ok:
        raise AssertionError(message)


def read_points(client, url, ids=None, limit=None):
    # id -> (payload, vectors) for the whole collection, or only the given ids.
    found = {}
    if ids is not None:
        r = client.post(url + '/points', json={'ids': ids, 'with_payload': True, 'with_vector': True})
        r.raise_for_status()
        return {p['id']: (p['payload'], p['vector']) for p in r.json()['result']}
    offset = None
    while True:
        body = {'limit': 256, 'with_payload': True, 'with_vector': True}
        if offset is not None:
            body['offset'] = offset
        r = client.post(url + '/points/scroll', json=body)
        r.raise_for_status()
        result = r.json()['result']
        for p in result['points']:
            found[p['id']] = (p['payload'], p['vector'])
        offset = result.get('next_page_offset')
        if offset is None or (limit is not None and len(found) >= limit):
            return found


def kinds(control):
    # Control records by role: the representation manifest, the publication
    # record and one completion record per published document generation.
    payloads = [payload for payload, _ in control.values()]
    return (
        [p for p in payloads if p.get('record_type') == 'representation-manifest'],
        [p for p in payloads if p.get('record_type') == 'publication-metadata'],
        [p for p in payloads if 'record_type' not in p and 'chunk_ids_digest' in p],
    )


def source_state(client, data_url, control_url):
    # Taken before any restore: what the restored copy must reproduce exactly.
    r = client.get(data_url)
    r.raise_for_status()
    count = r.json()['result']['points_count']
    need(count > 0, 'source data collection is empty')
    sample = read_points(client, data_url, limit=SAMPLE)
    control = read_points(client, control_url)
    manifests, publications, completions = kinds(control)
    need(len(manifests) == 1 and manifests[0].get('state') == 'committed',
         'source representation manifest is not committed')
    need(len(publications) == 1, 'source publication record is missing')
    need(completions, 'source has no completion records')
    return {'count': count, 'sample': sample, 'control': control}


def verify_restore(client, source, data_url, control_url):
    # The restored pair must carry the same stored content as the source.
    r = client.get(control_url)
    need(r.status_code == 200, 'restored completions collection missing')
    control = read_points(client, control_url)
    need(set(control) == set(source['control']), 'restored completions or representation points differ')
    need(all(control[i][0] == source['control'][i][0] for i in control),
         'restored completion or representation payload differs')
    need(all(control[i][1] == source['control'][i][1] for i in control), 'restored control vectors differ')
    r = client.get(data_url)
    need(r.status_code == 200, 'restored data collection missing')
    need(r.json()['result']['points_count'] == source['count'], 'restored point count differs')
    sample = read_points(client, data_url, ids=list(source['sample']))
    need(set(sample) == set(source['sample']), 'restored sampled points differ')
    need(all(sample[i][0] == source['sample'][i][0] for i in sample), 'restored sampled payload differs')
    need(all(sample[i][1] == source['sample'][i][1] for i in sample), 'restored sampled vectors differ')


def refused(check, message):
    try:
        check()
    except AssertionError as error:
        return str(error) == message
    return False


s = load_settings()
headers = {'api-key': s.qdrant_api_key} if s.qdrant_api_key else {}
with httpx2.Client(headers=headers, timeout=30) as client:
    base = s.qdrant_url + '/collections/'

    def aliases():
        r = client.get(s.qdrant_url + '/aliases')
        r.raise_for_status()
        return {a['alias_name']: a['collection_name'] for a in r.json()['result']['aliases']}

    aliases_before = aliases()
    # The configured name is the live alias; recovery works on its physical pair.
    physical = aliases_before.get(s.qdrant_collection, s.qdrant_collection)
    source_url = base + physical
    source_control_url = base + physical + '__completions'
    source = source_state(client, source_url, source_control_url)
    vector = next(iter(source['sample'].values()))[1]['dense']
    query = {'query': vector, 'using': 'dense', 'limit': 5, 'params': {'exact': True}}
    r = client.post(source_url + '/points/query', json=query)
    r.raise_for_status()
    expected = [p['id'] for p in r.json()['result']['points']]

    def snapshot(url):
        r = client.post(url + '/snapshots')
        r.raise_for_status()
        name = r.json()['result']['name']
        r = client.get(url + '/snapshots/' + name)
        r.raise_for_status()
        return r.content

    data_bytes = snapshot(source_url)
    control_bytes = snapshot(source_control_url)
    restore_url = base + 'ci-restore-' + uuid.uuid4().hex
    restore_control_url = restore_url + '__completions'
    # A name that is never created: the data-only restore counterexample.
    omitted_control_url = restore_url + '__completions-omitted'

    def upload(url, content):
        r = client.post(url + '/snapshots/upload?priority=snapshot',
                        files={'snapshot': ('synthetic.snapshot', content, 'application/octet-stream')})
        r.raise_for_status()

    try:
        upload(restore_url, data_bytes)
        upload(restore_control_url, control_bytes)
        r = client.post(restore_url + '/points/query', json=query)
        r.raise_for_status()
        need([p['id'] for p in r.json()['result']['points']] == expected, 'restored exact query differs')
        verify_restore(client, source, restore_url, restore_control_url)
        # Counterexample on the real service: data restored without its
        # completions snapshot must be refused, not served.
        need(refused(lambda: verify_restore(client, source, restore_url, omitted_control_url),
                     'restored completions collection missing'),
             'data-only restore was not refused')
        print('Synthetic snapshot restore: data, completions and representation match; data-only restore refused')
    finally:
        for url in (restore_url, restore_control_url):
            r = client.delete(url)
            assert r.status_code in (200, 404)

    # After cleanup the source and the next ordinary operations are intact.
    for url in (restore_url, restore_control_url):
        need(client.get(url).status_code == 404, 'scratch collection was not removed')
    need(aliases() == aliases_before, 'aliases changed')
    after = source_state(client, source_url, source_control_url)
    need(after['count'] == source['count'] and after['sample'] == source['sample'], 'source data changed')
    need(after['control'] == source['control'], 'source completions or representation changed')
    r = client.post(source_url + '/points/query', json=query)
    r.raise_for_status()
    need([p['id'] for p in r.json()['result']['points']] == expected, 'source query changed')
    r = client.post(source_url + '/snapshots')
    r.raise_for_status()
    print('Source unchanged after cleanup; next query and snapshot succeeded')
"""


def maintenance_job(ingest, namespace):
    """Reuse the deployed ingest image and writer reference, never a Secret value."""
    source = ingest['spec']['template']['spec']
    container = copy.deepcopy(next(c for c in source['containers'] if c['name'] == 'ingest'))
    env = {entry['name']: entry for entry in container['env']}
    writer = env['QDRANT_API_KEY'].get('valueFrom', {}).get('secretKeyRef', {})
    if not writer.get('name') or writer.get('key') != 'api-key' or 'value' in env['QDRANT_API_KEY']:
        raise ValueError('Ingest must reference the full-access Qdrant Secret key')
    container['env'] = [env[name] for name in ('QDRANT_URL', 'QDRANT_COLLECTION', 'QDRANT_API_KEY')]
    container['command'] = ['python3', '-c', SNAPSHOT_CHECK]
    # Recovery needs only ephemeral temporary storage, never ingest's RWO PVCs.
    for field in ('args', 'volumeMounts', 'volumeDevices', 'envFrom'):
        container.pop(field, None)
    container['volumeMounts'] = [{'name': 'tmp', 'mountPath': '/tmp'}]
    spec = {key: source[key] for key in ('imagePullSecrets', 'serviceAccountName', 'securityContext')
            if key in source}
    spec.update(containers=[container], volumes=[{'name': 'tmp', 'emptyDir': {}}],
                restartPolicy='Never', automountServiceAccountToken=False)
    return {
        'apiVersion': 'batch/v1', 'kind': 'Job',
        'metadata': {'name': 'ci-qdrant-recovery-' + uuid.uuid4().hex, 'namespace': namespace},
        'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 180,
                 'template': {'metadata': {'labels': {'app': 'ingest'}}, 'spec': spec}},
    }


def valid_namespace(value):
    if len(value) > 63 or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', value):
        raise argparse.ArgumentTypeError('invalid namespace')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('namespace', type=valid_namespace)
    args = parser.parse_args()
    kubectl = ['kubectl', '-n', args.namespace]
    result = subprocess.run(kubectl + ['get', 'job', 'ingest', '-o', 'json'],
                            check=True, text=True, capture_output=True)
    job = maintenance_job(json.loads(result.stdout), args.namespace)
    # Create a unique job; never adopt/delete a pre-existing maintenance workload.
    subprocess.run(kubectl + ['create', '-f', '-'], input=json.dumps(job), check=True, text=True)
    name = 'job/' + job['metadata']['name']
    try:
        result = subprocess.run(kubectl + ['wait', '--for=condition=complete', name, '--timeout=180s'],
                                check=False)
        subprocess.run(kubectl + ['logs', name], check=True)
        result.check_returncode()
    finally:
        subprocess.run(kubectl + ['delete', name, '--wait=true', '--timeout=60s'], check=True)


if __name__ == '__main__':
    main()
