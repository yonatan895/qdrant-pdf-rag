"""Unit tests for scripts/capture_pool.py pure record helpers (hermetic).

Live ``capture_query`` runs against real Qdrant/vLLM (RC/gap only), but its
leg plumbing is exercised here with the shared fakes — no network, no GPU.
The capture→replay seam is pinned structurally: rows emitted by
``record_to_rows`` carry the exact shapes ``replay_pool`` requires
(ranked id lists, chunk table, optional finite CE).
"""

from datetime import UTC

import pytest
from scripts.capture_pool import (
    CE_DEPTH_MAX,
    MAX_CAPTURE_DEPTH,
    capture_query,
    legs_to_record,
    main,
    record_to_rows,
    replay_pool,
)

from mainframe_rag.config import Settings
from tests.conftest import FakeEmbedder, FakeQdrant, MockReranker, _point


def _cpoint(pid, doc, page, ctype):
    """Shared point with a per-chunk payload (recorded-schema shapes)."""
    base = _point(pid)
    payload = dict(base.payload or {})
    payload.update({"doc_id": doc, "page_label": page, "chunk_type": ctype})
    return base.model_copy(update={"payload": payload})


def _legs():
    dense = [_cpoint("p1", "D1", "1", "narrative"), _cpoint("p2", "D2", "2", "table")]
    sparse = [_cpoint("p2", "D2", "2", "table"), _cpoint("p3", "D3", "3", "syntax")]
    return [{"effective_text": "sizing lookaside", "filter_fallback": False, "dense": dense, "sparse": sparse}]


def _settings(**overrides):
    kw = {
        "qdrant_url": "http://localhost:6333",
        "qdrant_collection": "mainframe_manuals",
        "dense_dim": 768,
        "embed_base_url": "http://localhost:8000/v1",
        "embed_model": "test-embed",
        "bm25_model": "Qdrant/bm25",
    }
    kw.update(overrides)
    return Settings(**kw)


def test_legs_to_record_shape():
    record = legs_to_record("sizing lookaside", "nl", _legs(), {"p1": 0.9}, {"collection": "c"})
    assert record["query"] == "sizing lookaside"
    assert record["query_kind"] == "nl"
    assert record["legs"] == [
        {
            "effective_text": "sizing lookaside",
            "filter_fallback": False,
            "dense": ["p1", "p2"],
            "sparse": ["p2", "p3"],
        }
    ]
    assert record["chunks"]["p2"] == {"doc_id": "D2", "page": "2", "chunk_type": "table", "page_start": 5}
    assert record["ce"] == {"p1": 0.9}
    assert record["_meta"] == {"collection": "c"}


@pytest.mark.parametrize("bad_ce", [float("nan"), "high"])
def test_legs_to_record_rejects_bad_ce(bad_ce):
    with pytest.raises((ValueError, TypeError)):
        legs_to_record("q", "nl", _legs(), {"p1": bad_ce}, {})


@pytest.mark.parametrize(
    ("legs", "exc"),
    [
        ([], ValueError),
        ([{"dense": "nope", "sparse": []}], TypeError),
    ],
)
def test_legs_to_record_rejects_bad_legs(legs, exc):
    with pytest.raises(exc):
        legs_to_record("q", "nl", legs, {}, {})


def test_legs_to_record_empty_query_rejected():
    with pytest.raises(ValueError):
        legs_to_record("", "nl", _legs(), {}, {})


def test_record_to_rows_round_trip():
    record = legs_to_record("sizing lookaside", "nl", _legs(), {"p1": 0.9, "p2": 0.1}, {})
    rows = record_to_rows(record)
    by_id = {row["id"]: row for row in rows}
    assert [row["id"] for row in rows] == ["p1", "p2", "p3"]
    assert by_id["p1"]["dense_rank"] == 0
    assert by_id["p1"]["sparse_rank"] is None
    assert by_id["p2"] == {
        "id": "p2",
        "doc_id": "D2",
        "page": "2",
        "chunk_type": "table",
        "page_start": 5,
        "dense_rank": 1,
        "sparse_rank": 0,
        "ce": 0.1,
    }
    assert by_id["p3"]["sparse_rank"] == 1
    assert by_id["p3"]["ce"] is None
    # Replay-contract shape: unique dense ranks 0..n-1 in leg order.
    dense_ranks = sorted(row["dense_rank"] for row in rows if row["dense_rank"] is not None)
    assert dense_ranks == [0, 1]


def test_record_to_rows_rejects_unknown_chunk():
    record = legs_to_record("q", "nl", _legs(), {}, {})
    record["legs"][0]["dense"].append("ghost")
    with pytest.raises(ValueError):
        record_to_rows(record)


@pytest.mark.parametrize(
    "record",
    [
        "not-a-dict",
        {"legs": []},
        {"legs": [{"dense": [], "sparse": []}]},
    ],
)
def test_record_to_rows_rejects_bad_record(record):
    with pytest.raises((ValueError, TypeError)):
        record_to_rows(record)


@pytest.mark.parametrize("kwargs", [{"leg": 2}, {"max_rank": 0}, {"max_rank": True}])
def test_record_to_rows_refuses_out_of_range_leg_and_max_rank(kwargs):
    record = legs_to_record("q", "nl", _legs(), {}, {})
    with pytest.raises(ValueError):
        record_to_rows(record, **kwargs)
    assert [row["id"] for row in record_to_rows(record, max_rank=1)] == ["p1", "p2"]


def test_capture_query_nl_kind_with_fake_legs():
    dense = [_cpoint("p1", "D1", "1", "narrative")]
    sparse = [_cpoint("p1", "D1", "1", "narrative")]
    fake = FakeQdrant(dense=dense, sparse=sparse)
    record = capture_query(fake, FakeEmbedder(), "mainframe_manuals", "sizing lookaside", _settings())
    assert record["query"] == "sizing lookaside"
    assert record["query_kind"] == "nl"
    assert len(record["legs"]) == 1
    assert record["legs"][0]["dense"] == ["p1"]
    assert record["legs"][0]["sparse"] == ["p1"]
    assert record["legs"][0]["filter_fallback"] is False
    assert record["chunks"]["p1"]["doc_id"] == "D1"
    assert record["ce"] == {}
    assert record["_meta"]["collection"] == "mainframe_manuals"
    assert record["_meta"]["ce_scored"] is False


def test_capture_query_identifier_kind_bypasses_ce():
    dense = [_cpoint("p1", "D1", "1", "message")]
    fake = FakeQdrant(dense=dense, sparse=[])
    reranker = MockReranker()
    record = capture_query(
        fake,
        FakeEmbedder(),
        "mainframe_manuals",
        "what does IEA500I mean",
        _settings(),
        score_ce=True,
        reranker=reranker,
    )
    assert record["query_kind"] == "identifier"
    assert record["ce"] == {}
    assert record["_meta"]["ce_scored"] is False
    assert reranker.call_count == 0


def test_capture_query_explicit_reranker_scores_nl_pool():
    dense = [_cpoint("p1", "D1", "1", "narrative")]
    fake = FakeQdrant(dense=dense, sparse=[])
    reranker = MockReranker()
    record = capture_query(
        fake,
        FakeEmbedder(),
        "mainframe_manuals",
        "sizing lookaside",
        _settings(),
        score_ce=True,
        reranker=reranker,
    )
    assert reranker.call_count == 1
    assert record["ce"] == {"p1": 0.5}
    assert record["_meta"]["ce_scored"] is True


def test_capture_query_depth_records_deeper_never_shallower():
    point = _cpoint("p1", "D1", "1", "narrative")
    deep = FakeQdrant(dense=[point], sparse=[point])
    record = capture_query(deep, FakeEmbedder(), "mainframe_manuals", "sizing lookaside", _settings(),
                           depth=MAX_CAPTURE_DEPTH)
    assert {req.limit for req in deep.batch_requests} == {MAX_CAPTURE_DEPTH}
    assert record["_meta"]["depth"] == MAX_CAPTURE_DEPTH
    shallow = FakeQdrant(dense=[point], sparse=[point])
    record = capture_query(shallow, FakeEmbedder(), "mainframe_manuals", "sizing lookaside", _settings(), depth=1)
    # Production prefetch for the non-rerank path (query.PREFETCH_LIMIT) is the floor.
    assert {req.limit for req in shallow.batch_requests} == {40}
    assert record["_meta"]["depth"] == 40
    assert record["_meta"]["ce_depth"] is None


def test_capture_query_ce_scores_stop_at_ce_depth_per_leg():
    dense = [_cpoint(f"d{i}", "D1", str(i), "narrative") for i in range(CE_DEPTH_MAX + 20)]
    sparse = [_cpoint(f"s{i}", "D2", str(i), "narrative") for i in range(CE_DEPTH_MAX + 20)]
    reranker = MockReranker()
    record = capture_query(FakeQdrant(dense=dense, sparse=sparse), FakeEmbedder(), "mainframe_manuals",
                           "sizing lookaside", _settings(), reranker=reranker, depth=MAX_CAPTURE_DEPTH)
    expected = {f"d{i}" for i in range(CE_DEPTH_MAX)} | {f"s{i}" for i in range(CE_DEPTH_MAX)}
    assert set(record["ce"]) == expected
    assert len(record["legs"][0]["dense"]) == CE_DEPTH_MAX + 20  # ranks are still recorded deeper
    assert record["_meta"]["ce_depth"] == CE_DEPTH_MAX


def test_record_to_rows_headings_replace_placeholder_and_refuse_gaps():
    record = legs_to_record("q", "nl", _legs(), {}, {})
    headings = {"p1": "Book > Section A", "p2": "Book > Table B", "p3": ""}
    rows = record_to_rows(record, headings=headings)
    assert [r["heading"] for r in rows] == ["Book > Section A", "Book > Table B", ""]
    dense, _sparse, _ce = replay_pool(rows)
    assert [p.payload["heading_path"] for p in dense] == ["Book > Section A", "Book > Table B"]
    with pytest.raises(ValueError, match="missing from the heading join"):
        record_to_rows(record, headings={"p1": "x"})
    placeholder, _, _ = replay_pool(record_to_rows(record))
    assert placeholder[0].payload["heading_path"] == "Replay > p1"


def test_legs_to_record_keeps_physical_page_for_diversification():
    labelled = _cpoint("p1", "D1", "", "narrative")
    labelled = labelled.model_copy(update={"payload": {**labelled.payload, "page_start": 41}})
    legacy = _cpoint("p2", "D1", "", "narrative")
    legacy = legacy.model_copy(update={"payload": {k: v for k, v in legacy.payload.items() if k != "page_start"}})
    record = legs_to_record("q", "nl", [{"effective_text": "q", "dense": [labelled, legacy], "sparse": []}], {}, {})
    assert record["chunks"]["p1"]["page_start"] == 41
    assert "page_start" not in record["chunks"]["p2"]
    rows = record_to_rows(record)
    assert [r["page_start"] for r in rows] == [41, None]
    dense, _, _ = replay_pool(rows)
    assert dense[0].payload["page_start"] == 41 and "page_start" not in dense[1].payload
    with pytest.raises(ValueError, match="page_start"):
        replay_pool([{**rows[0], "page_start": -1}])


@pytest.mark.parametrize("depth", [str(MAX_CAPTURE_DEPTH + 1)])
def test_capture_cli_refuses_out_of_range_depth(tmp_path, depth):
    with pytest.raises(SystemExit) as exc:
        main(["--golden", "g.jsonl", "--out", str(tmp_path / "o.jsonl"), "--depth", depth])
    assert exc.value.code == 2
    assert not (tmp_path / "o.jsonl").exists()


@pytest.fixture
def in_process_capture(tmp_path, monkeypatch):
    """Run the real ``main`` with the shared Qdrant/embedder fakes, no Task or venv."""
    import json

    import qdrant_client

    from mainframe_rag import config
    from mainframe_rag.ingest import embed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VENUE", "dev")
    monkeypatch.setattr(config, "load_settings", lambda: _settings(_env_file=None, rerank_enabled=False))
    monkeypatch.setattr(
        qdrant_client, "QdrantClient",
        lambda **kwargs: FakeQdrant(dense=[_cpoint("p1", "D1", "1", "prose")], sparse=[]))
    monkeypatch.setattr(embed, "build_embedder", lambda settings: FakeEmbedder())
    golden = tmp_path / "golden.jsonl"
    golden.write_text("\n".join([
        json.dumps({"query": "first question"}), "  ",
        json.dumps({"query": "second question"}), json.dumps({"query": "third question"}),
    ]) + "\n")
    return tmp_path, golden


def test_capture_bundle_dir_defaults_golden_and_output_name_and_honors_max_queries(in_process_capture, monkeypatch, capsys):
    import json

    from scripts import capture_pool

    root, golden = in_process_capture
    monkeypatch.setattr(capture_pool.time, "strftime", lambda fmt: "20260102")
    monkeypatch.setattr(capture_pool, "DEV_GOLDEN_PATH", golden)
    assert main(["--bundle-dir", "new-bundle", "--max-queries", "2"]) == 0
    output = root / "new-bundle" / "pools-20260102.jsonl"
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert [r["query"] for r in records] == ["first question", "second question"]
    summary = json.loads(capsys.readouterr().out)
    assert summary["queries"] == 2 and summary["failures"] == 0


def test_capture_records_failed_query_as_error_row_exits_nonzero_and_next_run_succeeds(
        in_process_capture, monkeypatch, capsys):
    import json

    from scripts import capture_pool

    root, golden = in_process_capture
    real = capture_pool.capture_query

    def flaky(client, embedder, collection, query, *args, **kwargs):
        if query == "second question":
            raise RuntimeError("upstream unavailable " + "x" * 400)
        return real(client, embedder, collection, query, *args, **kwargs)

    out = root / "pools.jsonl"
    monkeypatch.setattr(capture_pool, "capture_query", flaky)
    assert main(["--golden", str(golden), "--out", str(out)]) == 1
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["query"] for r in rows] == ["first question", "second question", "third question"]
    assert "legs" in rows[0] and "legs" in rows[2]
    assert rows[1].keys() == {"query", "error"} and len(rows[1]["error"]) == 200
    assert json.loads(capsys.readouterr().out)["failures"] == 1
    monkeypatch.setattr(capture_pool, "capture_query", real)
    assert main(["--golden", str(golden), "--out", str(out)]) == 0
    assert all("legs" in json.loads(line) for line in out.read_text().splitlines())


@pytest.fixture
def capture_workspace(tmp_path):
    """Actual Task/CLI/capture serializer, with shared runtime client fakes."""
    import json
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

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
    shutil.copy2(repo / "scripts/capture_pool.py", tmp_path / "scripts/capture_pool.py")
    (tmp_path / ".venv/bin").mkdir(parents=True)
    launcher = tmp_path / ".venv/bin/python"
    launcher.write_text(f"#!{sys.executable}\n" + f"repo = {str(repo)!r}\n" + '''
import importlib.util, json, sys
from pathlib import Path
sys.path[:0] = [str(Path(repo) / 'src'), repo]
from mainframe_rag import config
from mainframe_rag.ingest import embed
from tests.fakes import QdrantFake, EmbedderFake, make_point
import qdrant_client

def settings():
    return config.Settings(_env_file=None, qdrant_collection='synthetic', dense_dim=3, rerank_enabled=False)
config.load_settings = settings
point = make_point('synthetic-id').model_copy(update={'payload': {
    'doc_id': 'original-doc', 'page_label': '7', 'chunk_type': 'prose',
    'text': 'PRIVATE-TEXT-SENTINEL', 'heading_path': 'Example', 'message_ids': []}})
def client(**kwargs):
    Path('client-started').write_text('yes')
    return QdrantFake(dense=[point], sparse=[point])
qdrant_client.QdrantClient = client
embed.build_embedder = lambda settings: EmbedderFake()
spec = importlib.util.spec_from_file_location('actual_capture_cli', sys.argv[1])
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
raise SystemExit(cli.main(sys.argv[2:]))
''')
    launcher.chmod(0o755)
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals/golden.jsonl").write_text(json.dumps({"query": "Original default question"}) + "\n")
    (tmp_path / "custom אב;$(touch SENTINEL).jsonl").write_text(json.dumps({"query": "Original override question אב;$(touch SENTINEL)"}) + "\n")
    def run(*args, direct=False, ambient=None):
        command = ([str(launcher), "scripts/capture_pool.py"] if direct else
                   [task, "--taskfile", str(tmp_path / "Taskfile.yml"), "eval:capture-pool"])
        return subprocess.run([*command, *args], cwd=tmp_path,
            env={"PATH": os.defpath, "HOME": str(tmp_path), "TZ": "UTC", **(ambient or {})},
            capture_output=True, text=True, timeout=30, check=False)
    return tmp_path, run


def _assert_capture(path, query):
    import json

    from scripts.capture_pool import replay_pool

    content = path.read_text()
    records = [json.loads(line) for line in content.splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["query"] == query
    assert record["legs"][0]["dense"] == record["legs"][0]["sparse"] == ["synthetic-id"]
    assert record["chunks"] == {"synthetic-id": {"doc_id": "original-doc", "page": "7", "chunk_type": "prose"}}
    assert record["ce"] == {}
    assert "PRIVATE-TEXT-SENTINEL" not in content
    dense, sparse, ce = replay_pool(record_to_rows(record))
    assert [p.id for p in dense] == [p.id for p in sparse] == ["synthetic-id"]
    assert ce == {"synthetic-id": None}


@pytest.mark.parametrize("args,ambient,override", [
    ([], {}, False),
    (["GOLDEN=custom אב;$(touch SENTINEL).jsonl", "OUT=chosen אב;$(touch SENTINEL).jsonl"],
     {"GOLDEN": "missing.jsonl", "OUT": "wrong.jsonl"}, True),
])
def test_task_capture_defaults_and_overrides(capture_workspace, args, ambient, override):
    from datetime import datetime

    root, run = capture_workspace
    before = datetime.now(UTC).strftime("%Y%m%d")
    bundle = "bundle אב;$(touch SENTINEL)"
    proc = run(*args, f"BUNDLE_DIR={bundle}", ambient=ambient)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    if override:
        output = root / "chosen אב;$(touch SENTINEL).jsonl"
    else:
        outputs = list((root / bundle).glob("pools-*.jsonl"))
        assert len(outputs) == 1
        output = outputs[0]
        after = datetime.now(UTC).strftime("%Y%m%d")
        assert output.name in {f"pools-{day}.jsonl" for day in (before, after)}
    _assert_capture(output, "Original override question אב;$(touch SENTINEL)" if override else "Original default question")
    assert not (root / "wrong.jsonl").exists()
    assert not (root / "SENTINEL").exists()


def test_task_capture_holdout_refusal_and_recovery(capture_workspace):
    import hashlib
    import json

    root, run = capture_workspace
    holdout = root / "evals/holdout.jsonl"
    original = (json.dumps({"query": "Original protected question"}) + "\n").encode()
    holdout.write_bytes(original)
    pin = holdout.with_suffix(".jsonl.sha256")
    pin.write_text(hashlib.sha256(original).hexdigest() + "  holdout.jsonl\n")
    args = ["GOLDEN=evals/holdout.jsonl", "OUT=capture.jsonl"]
    good = run(*args, "VENUE=rc")
    assert good.returncode == 0, good.stderr
    output = root / "capture.jsonl"
    _assert_capture(output, "Original protected question")
    good_bytes = output.read_bytes()
    for venue, content in (("dev", original), ("rc", b"broken, not JSON")):
        (root / "client-started").unlink()
        holdout.write_bytes(content)
        bad = run(*args, f"VENUE={venue}")
        assert bad.returncode != 0
        assert not (root / "client-started").exists()
        assert output.read_bytes() == good_bytes
        holdout.write_bytes(original)
        recovered = run(*args, "VENUE=rc")
        assert recovered.returncode == 0, recovered.stderr
        _assert_capture(output, "Original protected question")


def test_capture_explicit_cli_and_empty_inputs(capture_workspace):
    root, run = capture_workspace
    proc = run("--golden", "evals/golden.jsonl", "--out", "explicit.jsonl", direct=True)
    assert proc.returncode == 0, proc.stderr
    _assert_capture(root / "explicit.jsonl", "Original default question")
    for args in ([], ["--golden", "evals/golden.jsonl"], ["--out", "explicit.jsonl"],
                 ["--golden", "", "--out", "explicit.jsonl"],
                 ["--golden", "evals/golden.jsonl", "--out", ""]):
        assert run(*args, direct=True).returncode != 0
    assert run("BUNDLE_DIR=").returncode != 0


def test_capture_retry_records_the_pool_search_prefetches():
    """An empty exact code lookup retries under the filter that keeps scope
    and still excludes a known-wrong sibling code. The capture runs search's
    own prefetch, so it records that pool, never an unfiltered one."""
    from types import SimpleNamespace

    from qdrant_client.local.payload_filters import check_filter

    from mainframe_rag.retrieve.query import search
    from tests.fakes import batch_via_query_points

    points = []
    for pid, codes in (("missing", None), ("wrong", ["IEC070I"])):
        point = _cpoint(pid, f"DOC-{pid}", "1", "message")
        point.payload.pop("message_ids", None)
        if codes is not None:
            point.payload["message_ids"] = codes
        points.append(point)

    class FilterClient:
        def query_points(self, collection, query, using, limit, query_filter, **_):
            eligible = [p for p in points if query_filter is None
                        or check_filter(query_filter, p.payload, p.id, {})]
            return SimpleNamespace(points=eligible[:limit])

        query_batch_points = batch_via_query_points

    query = "What does IEC072I report?"
    record = capture_query(FilterClient(), FakeEmbedder(), "mainframe_manuals", query, _settings())
    recorded = {pid for leg in record["legs"] for pid in leg["dense"] + leg["sparse"]}
    assert record["legs"][0]["filter_fallback"] is True
    assert recorded == {"missing"}, "the known-wrong sibling never enters the recorded pool"
    hits, _, _ = search(FilterClient(), FakeEmbedder(), "mainframe_manuals", query, limit=8)
    assert {h.chunk_id for h in hits} == recorded
