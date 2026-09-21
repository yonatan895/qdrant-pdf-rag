"""Legacy-residue recovery against real Qdrant client semantics and stored content."""
from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient, models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.chunk import Chunk
from mainframe_rag.ingest.completion import (
    acquire_run_lock,
    completion_collection_for,
    expected_digests,
    is_doc_complete,
    release_run_lock,
    write_completion,
)
from mainframe_rag.ingest.inventory import InventoryRecord, append_record, load_inventory
from mainframe_rag.ingest.publish import verify_searchable_coverage, write_publish_state
from mainframe_rag.ingest.repair import apply_repair, plan_repair
from mainframe_rag.ingest.representation import write_manifest
from mainframe_rag.ingest.rules_version import extraction_rules_version


@pytest.fixture
def repair_case(tmp_path):
    # Optional explicit disposable-server run exercises the same cases over HTTP.
    url = os.environ.get('REPAIR_TEST_QDRANT_URL')
    client = QdrantClient(url=url) if url else QdrantClient(location=':memory:')
    prefix = 'repair_' + uuid4().hex
    live, staging, alias = prefix + '_old', prefix + '_new', prefix + '_alias'
    controls = completion_collection_for(staging)
    for name in (live, staging, controls):
        client.create_collection(name, vectors_config={
            'dense': models.VectorParams(size=2, distance=models.Distance.DOT)},
            sparse_vectors_config={'bm25': models.SparseVectorParams()})
    settings = Settings(_env_file=None, embed_mode='vllm', dense_dim=2, embed_model='synthetic',
                        embed_model_revision='fixture-revision',
                        qdrant_collection=alias)
    selected = settings.model_copy(update={'qdrant_collection': staging})
    rules = extraction_rules_version()
    sha, rev, doc = 'a' * 64, 'unknown|||' + 'a' * 64, 'filename-stem'
    ids = [str(uuid4()) for _ in range(3)]
    vector = {'dense': [1.0, 0.0], 'bm25': models.SparseVector(indices=[2], values=[1.0])}
    legacy = [models.PointStruct(id=i, vector=vector, payload={
        'doc_id': doc, 'sha256': sha, 'text': 'original synthetic legacy text'}) for i in ids[:2]]
    replacement = models.PointStruct(id=ids[2], vector=vector, payload={
        'doc_id': doc, 'sha256': sha, 'source_rev': rev, 'rules_v': rules,
        'text': 'original synthetic replacement text'})
    client.upsert(live, points=legacy, wait=True)
    client.upsert(staging, points=[*legacy, replacement], wait=True)
    client.update_collection_aliases([models.CreateAliasOperation(create_alias=models.CreateAlias(
        alias_name=alias, collection_name=live))])
    write_manifest(client, controls, selected, rules, state='pending')
    chunk = Chunk(chunk_id=ids[2], doc_id=doc, heading_path='', page_start=1,
                  page_label='1', chunk_type='narrative', text=replacement.payload['text'],
                  message_ids=[], members=[], ordinal=0)
    count, id_digest, content_digest = expected_digests([chunk])
    write_completion(client, selected, doc_id=doc, sha256=sha, rules_v=rules,
                     source_labels='||', source_rev=rev, expected_chunks=count,
                     chunk_ids_digest=id_digest, content_digest=content_digest)
    progress = tmp_path / 'progress.jsonl'
    append_record(progress, InventoryRecord(path='/private/filename-stem.pdf', sha256=sha,
                  doc_id=doc, status='upserted', rules_version=rules, source_rev=rev))
    write_publish_state(progress, alias, staging, 'build', 'corpus')
    directory = tmp_path / 'backup'
    yield client, settings, progress, directory, live, staging, ids, selected, rev
    for a in client.get_aliases().aliases:
        if a.alias_name.startswith(prefix):
            client.update_collection_aliases([models.DeleteAliasOperation(
                delete_alias=models.DeleteAlias(alias_name=a.alias_name))])
    for name in (live, staging, controls):
        client.delete_collection(name)
    client.close()


def test_repair_retry_then_ordinary_completion(repair_case):
    client, settings, progress, directory, live, staging, ids, selected, rev = repair_case
    walked = [('/private/filename-stem.pdf', 'a' * 64)]
    inventory = load_inventory(progress)
    assert verify_searchable_coverage(client, selected, walked, inventory,
                                     extraction_rules_version(), '||')
    digest = plan_repair(client, settings, progress, directory, max_points=2)
    backup = json.loads((directory / 'points.json').read_text())
    assert {p['id'] for p in backup} == set(ids[:2])
    assert all(p['payload']['text'] == 'original synthetic legacy text' for p in backup)
    assert all(p['vector']['dense'] == [1.0, 0.0] for p in backup)
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert ((directory / 'points.json').stat().st_mode & 0o777) == 0o600
    assert apply_repair(client, settings, progress, directory, digest) == 2
    assert apply_repair(client, settings, progress, directory, digest) == 0
    assert client.count(live, exact=True).count == 2
    assert client.retrieve(staging, ids, with_payload=True)[0].payload['text'] == (
        'original synthetic replacement text')
    # The next ordinary skip verifies the surviving stored content, not a receipt.
    assert is_doc_complete(client, selected, 'filename-stem', sha256='a' * 64,
                           rules_v=extraction_rules_version(), source_labels='||', source_rev=rev)
    assert verify_searchable_coverage(client, selected, walked, inventory,
                                     extraction_rules_version(), '||') == []
    assert client.get_aliases().aliases[0].collection_name == live


@pytest.mark.parametrize('mutation', ['backup', 'plan', 'inventory', 'live_vector',
                                      'replacement', 'served_staging', 'new_residue'])
def test_changed_inputs_refuse_before_deletion(repair_case, mutation):
    client, settings, progress, directory, live, staging, ids, _, _ = repair_case
    digest = plan_repair(client, settings, progress, directory, max_points=2)
    if mutation in ('backup', 'plan'):
        path = directory / ('points.json' if mutation == 'backup' else 'plan.json')
        path.write_bytes(path.read_bytes() + b' ')
    elif mutation == 'inventory':
        with progress.open('a') as file:
            file.write('\n')
    elif mutation == 'live_vector':
        client.update_vectors(live, points=[models.PointVectors(id=ids[0], vector={
            'dense': [0.0, 1.0]})], wait=True)
    elif mutation == 'replacement':
        client.set_payload(staging, payload={'text': 'corrupted replacement'}, points=[ids[2]], wait=True)
    elif mutation == 'served_staging':
        client.update_collection_aliases([models.CreateAliasOperation(create_alias=models.CreateAlias(
            alias_name=live + '_reader', collection_name=staging))])
    else:
        point = client.retrieve(staging, [ids[0]], with_vectors=True)[0]
        client.upsert(staging, points=[models.PointStruct(id=str(uuid4()),
                      payload=point.payload, vector=point.vector)], wait=True)
    with pytest.raises((ValueError, RuntimeError)):
        apply_repair(client, settings, progress, directory, digest)
    assert len(client.retrieve(staging, ids[:2])) == 2


def test_partial_delete_ack_lost_retry(repair_case):
    client, settings, progress, directory, _, staging, ids, _, _ = repair_case
    digest = plan_repair(client, settings, progress, directory, max_points=2)
    client.delete(staging, points_selector=models.PointIdsList(points=[ids[0]]), wait=True)
    assert apply_repair(client, settings, progress, directory, digest) == 1
    assert client.count(staging, exact=True).count == 1


def test_writer_lock_and_bound_refuse(repair_case):
    client, settings, progress, directory, _, staging, _, _, _ = repair_case
    lock = acquire_run_lock(progress)
    try:
        with pytest.raises(RuntimeError):
            plan_repair(client, settings, progress, directory, max_points=2)
    finally:
        release_run_lock(lock)
    with pytest.raises(ValueError, match='bound'):
        plan_repair(client, settings, progress, directory, max_points=1)
    assert client.count(staging, exact=True).count == 3
    assert not directory.exists()
