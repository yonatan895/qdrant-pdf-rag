"""Task -> actual evaluator sampling/repeat loops; synthetic runtime boundaries.

The dependency-free runner suite proves transport. These prepared-owner tests
prove the measured rows, repeats and verdict after parsing that transport.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def sampling_workspace(tmp_path):
    from tests.test_taskfile_contracts import REQUIRE_RUNNER, find_task

    task = find_task()
    if task is None:
        if REQUIRE_RUNNER:
            pytest.fail("pinned Task unavailable in required lane")
        pytest.skip("pinned Task unavailable")
    repo = Path(__file__).resolve().parents[1]
    shutil.copy2(repo / "Taskfile.yml", tmp_path / "Taskfile.yml")
    shutil.copytree(repo / "taskfiles", tmp_path / "taskfiles")
    (tmp_path / "scripts").mkdir()
    for name in ("eval_answers.py", "eval_chat.py", "harness_l2.py", "harness_l4.py"):
        shutil.copy2(repo / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    launcher = tmp_path / ".venv/bin/python"
    launcher.write_text(f"#!{sys.executable}\n" + f"source = {str(repo / 'src')!r}\n" + '''
import importlib.util, json, os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, source)
from mainframe_rag import config, manifest
from mainframe_rag.eval import answers, answer_tier, chat
from mainframe_rag.agent import answer
from mainframe_rag.ingest import embed
from mainframe_rag.retrieve import rerank
from mainframe_rag.retrieve.query import SearchHit
import fastapi.testclient
import qdrant_client

def event(kind, value):
    with Path('calls.jsonl').open('a') as out:
        out.write(json.dumps([kind, value]) + '\\n')
settings = lambda: config.Settings(_env_file=None, qdrant_collection='synthetic', llm_model_reasoning='fixture-model')
config.load_settings = answers.load_settings = chat.load_settings = settings
manifest.write_run_manifest = chat.write_run_manifest = lambda *a, **k: {'git_sha': 'synthetic'}
hit = {'cite': 'Original p. 1', 'doc_id': 'A', 'text': 'Original evidence'}
class Client:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def close(self): pass
    def post(self, path, json):
        assert path == '/v1/search'
        event('search', json['query'])
        return SimpleNamespace(status_code=200, json=lambda: {'hits': [hit]})
    def chat(self, messages, **kwargs):
        label = 'entailed' if 'entailed' in messages[0].content else 'relevant'
        event('judge', label)
        return SimpleNamespace(content=json.dumps({'label': label}))
fastapi.testclient.TestClient = Client
answer.HttpxLLMClient = chat.HttpxLLMClient = Client
qdrant_client.QdrantClient = Client
embed.build_embedder = lambda settings: object()
rerank.build_reranker = lambda settings: None

def measured_answer(client, entry, *args, **kwargs):
    event('answer', entry['id'])
    return dict(entry, path='llm', verdict='pass', answer='Original evidence [1]',
                citations=['[1] Original p. 1'], request_id=entry['id'], failures=[], warns=[])
answers.run_query = answer_tier.run_query = measured_answer
async def condense(client, messages, settings):
    event('condense', messages[0].content)
    return 'Original standalone question'
chat.condense_query = condense
def retrieve(*args, **kwargs):
    event('chat-search', args[3])
    return [SearchHit(chunk_id='row', doc_id='A', text='Original evidence', score=1,
        cite='Original p. 1', heading='Example', title='Original', page_label='1',
        chunk_type='prose', message_ids=())], 'nl', {}
chat.retrieve_search = retrieve
spec = importlib.util.spec_from_file_location('actual_cli', sys.argv[1])
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
raise SystemExit(cli.main(sys.argv[2:]))
''')
    launcher.chmod(0o755)
    evals = tmp_path / "evals"
    evals.mkdir()
    # Reverse input order distinguishes sampling from simply taking the file head.
    entries = [{"id": f"{prefix}{i:02d}", "query": f"Original {prefix}{i:02d} אב;$(touch SENTINEL)",
                "query_class": cls, "expected_behavior": "answer", "expected_doc_ids": ["A"],
                "syntax_pattern": "Original"}
               for prefix, cls in (("s", "syntax"), ("t", "table")) for i in range(15)]
    (evals / "golden.jsonl").write_text("".join(json.dumps(e) + "\n" for e in reversed(entries)))
    metrics = {"grounded_rate": 1, "citation_precision": 1, "citation_recall": 1,
               "truncation_rate": 0, "syntax_compliance": 1, "faithfulness.entailed": 1,
               "faithfulness.contradiction": 0, "relevance.relevant": 1, "relevance.irrelevant": 0}
    (evals / "harness-l4-thresholds.json").write_text(json.dumps({"_meta": {
        "venue": "synthetic", "embed_mode": "hash", "llm_model_reasoning": "fixture-model", "tolerance": 0.15},
        "metrics": metrics}))
    def run(operation, *args, ambient=None):
        log = tmp_path / "calls.jsonl"
        log.unlink(missing_ok=True)
        return subprocess.run([task, "--taskfile", str(tmp_path / "Taskfile.yml"), f"eval:{operation}", *args],
            cwd=tmp_path, env={"PATH": os.defpath, "HOME": str(tmp_path), **(ambient or {})},
            text=True, capture_output=True, timeout=30, check=False)
    return tmp_path, run


@pytest.mark.parametrize("operation", ["answers", "chat", "harness:l2", "harness:l4", "harness:l4-record"])
@pytest.mark.parametrize("args,ambient,count,repeats", [
    ([], {}, None, 3),
    (["N=", "REPEATS="], {"N": "5", "REPEATS": "2"}, None, 3),
    (["N=5", "REPEATS=2"], {"N": "1", "REPEATS": "1"}, 5, 2),
    ([], {"N": "3", "REPEATS": "1"}, 3, 1),
])
def test_task_sampling_and_repeat_counts(sampling_workspace, operation, args, ambient, count, repeats):
    root, run = sampling_workspace
    reference = root / "evals/harness-l4-thresholds.json"
    before = reference.read_bytes()
    output = "reports אב;$(touch SENTINEL)"
    proc = run(operation, *args, f"BUNDLE_DIR={output}", ambient=ambient)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    count = count if count is not None else (12 if operation == "chat" else 24)
    selected = ([f"s{i:02d}" for i in range(15)] + [f"t{i:02d}" for i in range(15)] if operation == "chat"
                else [f"{prefix}{i:02d}" for i in range(15) for prefix in ("s", "t")])[:count]
    calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
    name = {"answers": "eval-answers", "chat": "eval-chat", "harness:l2": "harness-l2",
            "harness:l4": "harness-l4", "harness:l4-record": "harness-l4"}[operation]
    report = json.loads((root / output / f"{name}-report.json").read_text())
    if operation == "chat":
        assert [row["session"] for row in report["rows"]] == [identity for identity in selected for _ in range(2)]
        assert len([c for c in calls if c[0] == "condense"]) == count
        assert len([c for c in calls if c[0] == "chat-search"]) == count * 2
    else:
        iterations = repeats if operation.startswith("harness:l4") else 1
        assert [c[1] for c in calls if c[0] == "answer"] == selected * iterations
        if operation.startswith("harness:l4"):
            assert [row["id"] for row in report["runs"][0]["rows"]] == selected
            assert report["summary"]["queries_per_run"] == count
            assert report["summary"]["repeats"] == repeats
            assert report["verdict"] == ("baseline" if operation.endswith("record") else "pass")
            assert len([c for c in calls if c[0] == "judge"]) == count * repeats * 2
        else:
            assert [row["id"] for row in report["results"]] == selected
            assert report["metrics"]["queries"] == count
            assert len([c for c in calls if c[0] == "judge"]) == (count if operation == "harness:l2" else 0)
    if operation.endswith("record"):
        recorded = json.loads(reference.read_text())
        assert recorded["_meta"]["repeats"] == repeats
        assert recorded["_meta"]["queries_per_run"] == count
        recorded_bytes = reference.read_bytes()
        proc = run("harness:l4", *args, ambient=ambient)
        assert proc.returncode == 0, proc.stderr
        assert reference.read_bytes() == recorded_bytes
    else:
        assert reference.read_bytes() == before
    assert not (root / "SENTINEL").exists()


@pytest.mark.parametrize("operation", ["answers", "chat", "harness:l2", "harness:l4", "harness:l4-record"])
def test_zero_sample_is_not_replaced_with_default(sampling_workspace, operation):
    root, run = sampling_workspace
    reference = root / "evals/harness-l4-thresholds.json"
    before = reference.read_bytes()
    proc = run(operation, "N=0", ambient={"N": "5"})
    # Preserve distinct existing exit contracts, including empty diagnostic
    # answer/L2 results. This is not a claim of semantic acceptance for N=0.
    assert (proc.returncode == 0) == (operation in ("answers", "harness:l2")), proc.stderr
    assert not (root / "calls.jsonl").exists()
    assert reference.read_bytes() == before


@pytest.mark.parametrize("operation", ["answers", "chat", "harness:l2", "harness:l4", "harness:l4-record"])
def test_invalid_sample_refuses_before_measurement(sampling_workspace, operation):
    root, run = sampling_workspace
    proc = run(operation, "N=bad אב;$(touch SENTINEL)")
    assert proc.returncode != 0
    assert "invalid int value" in proc.stderr
    assert not (root / "calls.jsonl").exists()
    assert not (root / "SENTINEL").exists()


@pytest.mark.parametrize("operation", ["harness:l4", "harness:l4-record"])
@pytest.mark.parametrize("value", ["0", "-1", "bad אב;$(touch SENTINEL)"])
def test_invalid_repeat_refuses_before_measurement(sampling_workspace, operation, value):
    root, run = sampling_workspace
    proc = run(operation, "N=1", f"REPEATS={value}")
    assert proc.returncode != 0
    assert not (root / "calls.jsonl").exists()
    assert not (root / "SENTINEL").exists()
