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
s=load_settings()
headers={'api-key':s.qdrant_api_key} if s.qdrant_api_key else {}
with httpx2.Client(headers=headers,timeout=30) as client:
    url=s.qdrant_url+'/collections/'+s.qdrant_collection
    r=client.get(url);r.raise_for_status()
    count=r.json()['result']['points_count']
    assert count>0
    r=client.post(url+'/points/scroll',json={'limit':1,'with_vector':True});r.raise_for_status()
    vector=r.json()['result']['points'][0]['vector']['dense']
    query={'query':vector,'using':'dense','limit':5,'params':{'exact':True}}
    r=client.post(url+'/points/query',json=query);r.raise_for_status()
    expected=[p['id'] for p in r.json()['result']['points']]
    r=client.post(url+'/snapshots');r.raise_for_status()
    snapshot=r.json()['result']['name']
    r=client.get(url+'/snapshots/'+snapshot);r.raise_for_status()
    snapshot_bytes=r.content
    restore_url=s.qdrant_url+'/collections/ci-restore-'+uuid.uuid4().hex
    try:
        r=client.post(restore_url+'/snapshots/upload?priority=snapshot',
                      files={'snapshot':('synthetic.snapshot',snapshot_bytes,'application/octet-stream')})
        r.raise_for_status()
        r=client.get(restore_url);r.raise_for_status()
        assert r.json()['result']['points_count']==count
        r=client.post(restore_url+'/points/query',json=query);r.raise_for_status()
        assert [p['id'] for p in r.json()['result']['points']]==expected
        print('Synthetic snapshot restore and exact-query equivalence passed')
    finally:
        r=client.delete(restore_url)
        assert r.status_code in (200,404)

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
    # Recovery needs no corpus/scratch PVC; avoid attaching ingest's RWO volumes.
    for field in ('args', 'volumeMounts', 'volumeDevices', 'envFrom'):
        container.pop(field, None)
    spec = {key: source[key] for key in ('imagePullSecrets', 'serviceAccountName', 'securityContext')
            if key in source}
    spec.update(containers=[container], restartPolicy='Never', automountServiceAccountToken=False)
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
