"""Passage-level gold candidates (issue #1317, PR 1): hermetic steps over
original synthetic points and a fake model port. No corpus, network or GPU."""

from __future__ import annotations

import hashlib
import json
import logging
import stat
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from mainframe_rag.eval import gold_candidates as gc
from mainframe_rag.eval.datasets import GoldenEntry, parse_golden_text
from mainframe_rag.eval.retrieval import is_relevant_hit

ROOT = Path(__file__).resolve().parents[1]
RULES = "rules-synthetic-1"
# Sentinels: every synthetic passage and every fake question carries one, so
# a leak into logs, stdout or the manifest is detectable.
PASSAGE_MARK = "zorblatt"
QUESTION_MARK = "quibblefrost"


def _payload(doc: str, n: int, chunk_type: str = "narrative", *, heading: str | None = None,
             rev: str | None = None, message_ids: tuple[str, ...] = (), rules: str = RULES):
    return {
        "doc_id": doc,
        "source_rev": rev or f"{doc}@r1",
        "heading_path": heading or f"Part {doc} > Unit {n} of the gadget",
        "chunk_type": chunk_type,
        "page_start": n,
        "page_end": n,
        "page_label": f"{n + 1}",
        "message_ids": list(message_ids),
        "rules_v": rules,
        "title": f"Guide {doc}",
        "text": f"The {PASSAGE_MARK} lever {doc}-{n} must be turned twice before the valve opens.",
    }


def _point(doc: str, n: int, chunk_type: str = "narrative", **kw):
    return SimpleNamespace(id=f"{doc}-{chunk_type}-{n:02d}", payload=_payload(doc, n, chunk_type, **kw))


class ReaderDouble:
    """Local read-only Qdrant double. Kept here rather than in tests/fakes.py
    because it honours the ``with_payload`` field projection and logs every
    call: the tests prove the scan never asks for text and that a refusal
    makes no call at all."""

    def __init__(self, points, aliases=None):
        self.points = list(points)
        self.aliases = dict(aliases or {})
        self.calls: list[tuple] = []

    def get_aliases(self):
        self.calls.append(("get_aliases",))
        return SimpleNamespace(aliases=[
            SimpleNamespace(alias_name=a, collection_name=c) for a, c in self.aliases.items()])

    @staticmethod
    def _project(point, with_payload):
        return SimpleNamespace(
            id=point.id, payload={k: v for k, v in point.payload.items() if k in with_payload})

    def scroll(self, collection, *, scroll_filter=None, limit=10, with_payload,
               with_vectors=False, offset=None):
        self.calls.append(("scroll", collection, tuple(with_payload)))
        start = int(offset or 0)
        nxt = start + limit if start + limit < len(self.points) else None
        return [self._project(p, with_payload) for p in self.points[start:start + limit]], nxt

    def retrieve(self, collection, ids, *, with_payload, with_vectors=False):
        self.calls.append(("retrieve", collection, tuple(ids), tuple(with_payload)))
        return [self._project(p, with_payload) for p in self.points if p.id in set(ids)]


class ModelDouble:
    """Fake model port: one question per passage (numbered, so questions
    differ), YES to every check; per-call overrides by call index."""

    def __init__(self, questions=None, verdicts=None):
        self.questions = questions or {}
        self.verdicts = verdicts or {}
        self.calls: list[tuple[str, str]] = []
        self._asked = 0
        self._checked = 0

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if system == gc.ANSWERABILITY_PROMPT:
            reply = self.verdicts.get(self._checked, "YES")
            self._checked += 1
        else:
            reply = self.questions.get(
                self._asked, f"How often is {QUESTION_MARK} lever {self._asked} turned first?")
            self._asked += 1
        if isinstance(reply, Exception):
            raise reply
        return reply


def _identities(points):
    return gc.scan_identities(ReaderDouble(points), "c")


def _grid(docs=("A", "B", "C"), types=("narrative", "table"), per=4):
    return [_point(d, i, t) for d in docs for t in types for i in range(per)]


# ---------------------------------------------------------------- sampling
@pytest.mark.parametrize(
    ("count", "per_doc", "per_stratum"),
    [(3, 1, None), (6, 2, 1), (12, 4, 2), (24, 8, 4)],
)
def test_sample_spreads_over_documents_then_chunk_types(count, per_doc, per_stratum):
    samples = gc.stratified_sample(
        _identities(_grid()), count=count, classes=["diagnostic"], seed=7)
    assert len({s.point.chunk_id for s in samples}) == count
    assert set(Counter(s.point.doc_id for s in samples).values()) == {per_doc}
    if per_stratum is not None:
        strata = Counter((s.point.doc_id, s.point.chunk_type) for s in samples)
        assert set(strata.values()) == {per_stratum} and len(strata) == 6


def test_sample_takes_what_a_small_document_has_and_fills_from_the_rest():
    points = [_point("A", 0), *(_point("B", i) for i in range(5))]
    samples = gc.stratified_sample(_identities(points), count=4, classes=["diagnostic"], seed=1)
    assert Counter(s.point.doc_id for s in samples) == {"A": 1, "B": 3}


def test_sample_is_deterministic_for_a_seed_and_input_order_independent():
    points = _grid()
    one = gc.stratified_sample(_identities(points), count=6, classes=["diagnostic"], seed=3)
    two = gc.stratified_sample(_identities(points[::-1]), count=6, classes=["diagnostic"], seed=3)
    other = gc.stratified_sample(_identities(points), count=6, classes=["diagnostic"], seed=4)
    assert one == two
    assert [s.point.chunk_id for s in one] != [s.point.chunk_id for s in other]


@pytest.mark.parametrize(
    ("query_class", "chunk_type", "message_ids", "eligible"),
    [
        ("diagnostic", "narrative", (), True),
        ("table", "table", (), True),
        ("table", "narrative", (), False),
        ("syntax", "syntax", (), True),
        ("syntax", "message", ("ABC123I",), False),
        ("message_id", "message", ("ABC123I",), True),
        ("message_id", "message", (), False),
    ],
)
def test_a_target_class_is_drawn_only_for_points_that_can_carry_it(
        query_class, chunk_type, message_ids, eligible):
    points = [_point("A", 0, chunk_type, message_ids=message_ids)]
    samples = gc.stratified_sample(_identities(points), count=1, classes=[query_class], seed=0)
    assert [s.query_class for s in samples] == ([query_class] if eligible else [])


def test_target_classes_are_balanced_over_eligible_points():
    samples = gc.stratified_sample(
        _identities(_grid(types=("table",), per=4)), count=12,
        classes=["diagnostic", "table"], seed=5)
    assert Counter(s.query_class for s in samples) == {"diagnostic": 6, "table": 6}


def test_section_gold_is_the_chunk_then_its_siblings_within_one_source_revision():
    points = [
        _point("A", 2, heading="Part A > Valves"),
        _point("A", 0, heading="Part A > Valves"),
        _point("A", 1, heading="Part A > Valves"),
        _point("A", 3, heading="Part A > Levers"),
        _point("A", 4, heading="Part A > Valves", rev="A@r2"),
    ]
    gold = gc.section_gold(_identities(points))
    assert gold["A-narrative-02"] == ["A-narrative-02", "A-narrative-00", "A-narrative-01"]
    assert gold["A-narrative-03"] == ["A-narrative-03"]
    assert gold["A-narrative-04"] == ["A-narrative-04"]


def test_the_scan_never_asks_for_text_and_text_is_fetched_for_the_sample_only():
    reader = ReaderDouble(_grid())
    model = ModelDouble()
    rows, manifest = gc.generate(reader, model, collection="c", count=3,
                                 classes=["diagnostic"], seed=2, model_label="fake", page_size=5)
    scrolls = [c for c in reader.calls if c[0] == "scroll"]
    retrieves = [c for c in reader.calls if c[0] == "retrieve"]
    assert len(scrolls) == 5 and all("text" not in c[2] for c in scrolls)
    assert [c[3] for c in retrieves] == [("text",)]
    assert sorted(retrieves[0][2]) == sorted(r["gold_chunk_ids"][0] for r in rows)
    assert manifest["counts"]["scanned"] == 24 and len(rows) == 3


# ----------------------------------------------------------------- filters
HEADING = "Part A > Resetting the pressure valve"


@pytest.mark.parametrize(
    ("question", "query_class", "seen", "code"),
    [
        ("How many turns open the outlet?", "diagnostic", (), None),
        ("Why turn it?", "diagnostic", (), "too_short"),
        (" ".join(["word"] * 41) + "?", "diagnostic", (), "too_long"),
        ("What does resetting the pressure valve involve here?", "diagnostic", (), "heading_copy"),
        ("What is RESETTING  THE PRESSURE VALVE for the unit?", "diagnostic", (), "heading_copy"),
        ("What does message ABC123I mean?", "diagnostic", (), "identifier_only"),
        ("What does message ABC123I mean?", "message_id", (), None),
        ("Which operator action clears ABC123I after a restart?", "diagnostic", (), None),
        ("How many turns open the outlet?", "diagnostic",
         ("how many turns open the outlet",), "duplicate"),
        ("how MANY turns   open the outlet", "diagnostic",
         ("how many turns open the outlet",), "duplicate"),
    ],
)
def test_question_filter_returns_the_first_drop_code(question, query_class, seen, code):
    assert gc.question_filter(
        question, heading_path=HEADING, query_class=query_class, seen=set(seen)) == code


@pytest.mark.parametrize(
    ("reply", "question"),
    [
        ("  How many   turns?  ", "How many turns?"),
        ("\nHow many turns?\n\n", "How many turns?"),
        ("Here is a question:\nHow many turns?", None),
        ("", None),
    ],
)
def test_a_reply_is_a_question_only_as_exactly_one_line(reply, question):
    assert gc.normalize_question(reply) == question


@pytest.mark.parametrize(
    ("question", "passage", "overlap"),
    [
        ("How many turns open the valve?", "The valve opens after two turns.", 0.5),
        ("Which lever resets pressure today?", "Lever, pressure; RESETS.", 0.75),
        ("What is it?", "Anything at all.", 0.0),
        ("Gasket torque sequence?", "Nothing shared here.", 0.0),
        ("valve valve valve", "The valve.", 1.0),
    ],
)
def test_lexical_overlap_is_the_share_of_question_content_tokens_in_the_passage(
        question, passage, overlap):
    assert gc.lexical_overlap(question, passage) == overlap


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [("YES", True), ("yes.", True), ("No", False), ("NO - it needs the table", False),
     ("maybe", None), ("", None), ("The answer is yes", None)],
)
def test_answerability_reads_only_a_leading_yes_or_no(reply, verdict):
    model = ModelDouble(verdicts={0: reply})
    assert gc.check_answerable(model, "q?", "p") is verdict
    system, user = model.calls[0]
    assert system == gc.ANSWERABILITY_PROMPT
    assert user == "<passage>\np\n</passage>\n<question>\nq?\n</question>"


@pytest.mark.parametrize("query_class", gc.TARGET_CLASSES)
def test_the_question_prompt_carries_the_class_hint_and_the_passage_as_data(query_class):
    model = ModelDouble()
    gc.write_question(model, "p", query_class)
    system, user = model.calls[0]
    assert system == gc.QUESTION_PROMPT.replace("{hint}", gc.CLASS_HINTS[query_class])
    assert user == "<passage>\np\n</passage>"


class _ChatDouble:
    def __init__(self, reply):
        self.reply = reply
        self.calls: list[tuple] = []

    def chat(self, messages, reasoning_effort=None, temperature=None):
        self.calls.append(([(m.role, m.content) for m in messages], reasoning_effort, temperature))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_the_reasoning_client_adapter_sends_system_then_user_at_temperature_zero():
    from mainframe_rag.ports import ChatResult

    chat = _ChatDouble(ChatResult(content="A question?"))
    assert gc._LLMModel(chat).complete("sys", "usr") == "A question?"
    assert chat.calls == [([("system", "sys"), ("user", "usr")], None, 0.0)]


@pytest.mark.parametrize(
    "reply", [RuntimeError("upstream said: secret"), OSError("down"), "a bare string", None])
def test_the_adapter_turns_any_failed_or_unstructured_completion_into_a_model_error(reply):
    with pytest.raises(gc.ModelCallError) as failure:
        gc._LLMModel(_ChatDouble(reply)).complete("sys", "usr")
    assert str(failure.value) == "model call failed" and failure.value.__cause__ is None


# ---------------------------------------------------------------- pipeline
def test_every_sampled_point_is_kept_or_dropped_under_exactly_one_code():
    points = [_point("A", i, heading="Part A > Resetting the pressure valve") for i in range(10)]
    points[9].payload["text"] = "   "
    order = [s.point.chunk_id for s in gc.stratified_sample(
        _identities(points), count=10, classes=["diagnostic"], seed=11)]
    # Question overrides are keyed by the model's call order; the point with
    # no text never reaches the model.
    asked = [i for i in order if i != "A-narrative-09"]
    model = ModelDouble(
        questions={
            0: gc.ModelCallError("down"),
            1: "Two lines\nof reply",
            2: "Too short?",
            3: "What does resetting the pressure valve need first?",
            4: "What does message ABC123I mean?",
            5: f"How often is {QUESTION_MARK} lever 9 turned first?",
            6: f"How often is {QUESTION_MARK} lever 9 turned first?",
        },
        # Checks run only for questions 5, 7, 8 (6 is a duplicate of 5).
        verdicts={1: "perhaps", 2: "NO"},
    )
    rows, manifest = gc.generate(ReaderDouble(points), model, collection="c", count=10,
                                 classes=["diagnostic"], seed=11, model_label="fake")
    assert manifest["counts"] == {
        "scanned": 10, "sampled": 10, "kept": 1,
        "dropped": {
            "text_unavailable": 1, "model_error": 1, "malformed_question": 1, "too_short": 1,
            "too_long": 0, "heading_copy": 1, "identifier_only": 1, "duplicate": 1,
            "answerability_unparseable": 1, "not_answerable": 1,
        },
    }
    assert [r["gold_chunk_ids"][0] for r in rows] == [asked[5]]
    assert tuple(manifest["counts"]["dropped"]) == gc.DROP_CODES


def test_a_model_failure_in_the_answerability_pass_drops_the_row():
    model = ModelDouble(verdicts={0: gc.ModelCallError("down")})
    rows, manifest = gc.generate(ReaderDouble([_point("A", 0)]), model, collection="c", count=1,
                                 classes=["diagnostic"], seed=0, model_label="fake")
    assert rows == [] and manifest["counts"]["dropped"]["model_error"] == 1


def _one_row(point, query_class="diagnostic", seed=9):
    rows, manifest = gc.generate(
        ReaderDouble([point], aliases={"current": "physical_1"}), ModelDouble(),
        collection="current", count=1, classes=[query_class], seed=seed, model_label="fake-model")
    return rows[0], manifest


def test_a_row_is_a_candidate_with_chunk_gold_overlap_and_provenance():
    point = _point("A", 4, heading="Part A > Valves")
    row, manifest = _one_row(point)
    question = f"How often is {QUESTION_MARK} lever 0 turned first?"
    assert row == {
        "id": "GEN-" + hashlib.sha256(b"A-narrative-04|diagnostic|9").hexdigest()[:12],
        "query": question,
        "expected_doc_ids": ["A"],
        "expected_heading": "Part A > Valves",
        "expected_page": "5",
        "expected_behavior": "answer",
        "query_class": "diagnostic",
        "source": "generated",
        "status": "candidate",
        "reviewers": [],
        "reviewed_by": None,
        "reviewed_at": None,
        "gold_chunk_ids": ["A-narrative-04"],
        "chunk_type": "narrative",
        "physical_page": 5,
        # Content tokens (no stopwords): often quibblefrost lever 0 turned first;
        # the passage holds two of the six: lever, turned.
        "lexical_overlap": round(2 / 6, 4),
        "provenance": {
            "text_sha256": hashlib.sha256(point.payload["text"].encode()).hexdigest(),
            "question_prompt_sha256": hashlib.sha256(
                gc.QUESTION_PROMPT.replace("{hint}", gc.CLASS_HINTS["diagnostic"]).encode()
            ).hexdigest(),
            "answerability_prompt_sha256": hashlib.sha256(
                gc.ANSWERABILITY_PROMPT.encode()).hexdigest(),
            "model": "fake-model",
            "seed": 9,
        },
    }
    assert manifest["collection_alias"] == "current" and manifest["collection"] == "physical_1"
    assert manifest["rules_version"] == RULES and manifest["seed"] == 9
    assert manifest["model"] == "fake-model" and manifest["reviewers"] == []
    assert manifest["prompts"] == {
        "question_template_sha256": hashlib.sha256(gc.QUESTION_PROMPT.encode()).hexdigest(),
        "question_sha256": {"diagnostic": row["provenance"]["question_prompt_sha256"]},
        "answerability_sha256": row["provenance"]["answerability_prompt_sha256"],
    }


def test_a_physical_collection_records_no_alias():
    _, manifest = gc.generate(ReaderDouble([_point("A", 0)]), ModelDouble(), collection="c",
                              count=1, classes=["diagnostic"], seed=0, model_label="fake")
    assert (manifest["collection_alias"], manifest["collection"]) == (None, "c")


@pytest.mark.parametrize(
    ("heading", "page_label"),
    [
        ("Part A > Valves", "5"),
        ("Part  A >\tValves\nand levers", " iv "),
        ("", ""),
    ],
)
def test_the_source_chunk_as_a_search_hit_is_relevant_to_its_row(heading, page_label):
    """Producer to consumer: the heading and page a row expects are the
    values the retrieval eval reads off a hit of the same stored point."""
    from mainframe_rag.retrieve.query import _to_hit

    point = _point("A", 4)
    point.payload.update(heading_path=heading, page_label=page_label)
    row, _ = _one_row(point)
    entry = GoldenEntry.model_validate(row)
    hit = _to_hit(SimpleNamespace(id=point.id, payload=point.payload), 1.0)
    assert is_relevant_hit(hit.doc_id, hit.heading, entry)
    assert entry.expected_page == (hit.page_label or None)
    assert hit.chunk_id == row["gold_chunk_ids"][0]


def test_mixed_rules_versions_and_an_empty_collection_are_refused_before_any_model_call():
    model = ModelDouble()
    with pytest.raises(gc.Refused) as mixed:
        gc.generate(ReaderDouble([_point("A", 0), _point("A", 1, rules="other")]), model,
                    collection="c", count=1, classes=["diagnostic"], seed=0, model_label="fake")
    with pytest.raises(gc.Refused) as empty:
        gc.generate(ReaderDouble([]), model, collection="c", count=1, classes=["diagnostic"],
                    seed=0, model_label="fake")
    assert (mixed.value.code, empty.value.code) == ("mixed_rules_versions", "no_points")
    assert model.calls == []


# --------------------------------------------------------------------- CLI
def _cli(out: Path, *extra: str, reader=None, model=None):
    reader = reader if reader is not None else ReaderDouble(_grid())
    model = model if model is not None else ModelDouble()
    argv = ["--out", str(out), "--collection", "c", "--count", "6", "--seed", "3",
            "--model-label", "fake-model", *extra]
    return gc.main(argv, client=reader, model=model), reader, model


def test_cli_writes_private_rows_and_a_manifest_and_prints_counts_only(tmp_path, capsys, caplog):
    out = tmp_path / "candidates.jsonl"
    with caplog.at_level(logging.DEBUG):
        code, _, _ = _cli(out, model=ModelDouble(verdicts={0: "NO"}))
    assert code == 0
    data = out.read_bytes()
    rows = [json.loads(line) for line in data.decode().splitlines()]
    manifest_file = tmp_path / "candidates.jsonl.manifest.json"
    manifest = json.loads(manifest_file.read_text())
    assert len(rows) == 5 and all(r["status"] == "candidate" and r["reviewers"] == [] for r in rows)
    assert manifest["counts"]["kept"] == 5 and manifest["counts"]["dropped"]["not_answerable"] == 1
    assert manifest["rows_sha256"] == hashlib.sha256(data).hexdigest()
    assert [stat.S_IMODE(p.stat().st_mode) for p in (out, manifest_file)] == [0o600, 0o600]
    printed = capsys.readouterr()
    assert json.loads(printed.out) == manifest["counts"]
    assert QUESTION_MARK in data.decode()
    exposed = printed.out + printed.err + caplog.text + manifest_file.read_text()
    assert QUESTION_MARK not in exposed and PASSAGE_MARK not in exposed
    assert "code=not_answerable" in caplog.text


def test_cli_exits_1_and_keeps_the_manifest_when_no_row_is_kept(tmp_path, capsys):
    out = tmp_path / "candidates.jsonl"
    failing = ModelDouble(questions={i: gc.ModelCallError("down") for i in range(6)})
    code, _, _ = _cli(out, model=failing)
    counts = json.loads(gc.manifest_path(out).read_text())["counts"]
    assert code == 1 and out.read_bytes() == b""
    assert (counts["sampled"], counts["kept"], counts["dropped"]["model_error"]) == (6, 0, 6)
    assert json.loads(capsys.readouterr().out) == counts


def test_cli_output_is_byte_identical_for_the_same_seed(tmp_path):
    first, second = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    assert (_cli(first)[0], _cli(second)[0]) == (0, 0)
    assert first.read_bytes() == second.read_bytes()
    assert (gc.manifest_path(first).read_bytes() == gc.manifest_path(second).read_bytes())


def _in_worktree(tmp_path: Path) -> Path:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "data").mkdir()
    return tmp_path / "repo" / "data" / "candidates.jsonl"


def _in_linked_worktree(tmp_path: Path) -> Path:
    (tmp_path / "linked").mkdir()
    (tmp_path / "linked" / ".git").write_text("gitdir: elsewhere\n")
    return tmp_path / "linked" / "candidates.jsonl"


def _through_symlinked_directory(tmp_path: Path) -> Path:
    target = _in_worktree(tmp_path).parent
    (tmp_path / "alias").symlink_to(target, target_is_directory=True)
    return tmp_path / "alias" / "candidates.jsonl"


def _existing(tmp_path: Path) -> Path:
    path = tmp_path / "candidates.jsonl"
    path.write_text("kept\n")
    return path


def _existing_manifest(tmp_path: Path) -> Path:
    (tmp_path / "candidates.jsonl.manifest.json").write_text("kept\n")
    return tmp_path / "candidates.jsonl"


def _dangling_symlink(tmp_path: Path) -> Path:
    path = tmp_path / "candidates.jsonl"
    path.symlink_to(tmp_path / "elsewhere.jsonl")
    return path


@pytest.mark.parametrize(
    "make_path",
    [
        _in_worktree,
        _in_linked_worktree,
        _through_symlinked_directory,
        _existing,
        _existing_manifest,
        _dangling_symlink,
        lambda tmp: tmp / "holdout.jsonl",
        lambda tmp: tmp / "release_set.jsonl",
        lambda tmp: tmp / "missing-directory" / "candidates.jsonl",
        lambda tmp: ROOT / "evals" / "generated-candidates.jsonl",
        lambda tmp: ROOT / "docs" / "generated-candidates.jsonl",
    ],
    ids=["worktree", "linked-worktree", "symlinked-directory", "existing", "existing-manifest",
         "dangling-symlink", "holdout-name", "release-set-name", "missing-directory",
         "repository-evals", "repository-docs"],
)
def test_cli_refuses_an_unsafe_output_before_any_qdrant_or_model_call(
        make_path, tmp_path, capsys):
    out = make_path(tmp_path)
    before = sorted(p for p in tmp_path.rglob("*"))
    code, reader, model = _cli(out)
    assert code == 2
    assert capsys.readouterr().err == gc.OUTPUT_REFUSAL + "\n"
    assert reader.calls == [] and model.calls == []
    assert sorted(p for p in tmp_path.rglob("*")) == before
    assert not (ROOT / "evals" / "generated-candidates.jsonl").exists()
    assert not (ROOT / "docs" / "generated-candidates.jsonl").exists()


def test_cli_refuses_the_real_corpus_collection_outside_the_rc_venue(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("VENUE", raising=False)
    code, reader, model = _cli(tmp_path / "candidates.jsonl", "--collection", "real_manuals")
    assert code == 2 and capsys.readouterr().err == gc.REFUSALS["venue"] + "\n"
    assert reader.calls == [] and model.calls == [] and list(tmp_path.iterdir()) == []


def test_cli_refuses_an_alias_of_the_real_corpus_collection_before_any_scan(
        tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("VENUE", raising=False)
    reader = ReaderDouble(_grid(), aliases={"current": "real_manuals"})
    code, _, model = _cli(tmp_path / "candidates.jsonl", "--collection", "current", reader=reader)
    assert code == 2 and capsys.readouterr().err == gc.REFUSALS["venue"] + "\n"
    assert reader.calls == [("get_aliases",)] and model.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("classes", ["negative", "comparative", "diagnostic,diagnostic", ",", "x"])
def test_cli_refuses_classes_a_single_passage_cannot_carry(classes, tmp_path, capsys):
    code, reader, model = _cli(tmp_path / "candidates.jsonl", "--classes", classes)
    assert code == 2 and capsys.readouterr().err == gc.REFUSALS["classes"] + "\n"
    assert reader.calls == [] and model.calls == [] and list(tmp_path.iterdir()) == []


# ------------------------------------------------- candidates are not golden
def test_candidate_rows_are_recognised_by_content(tmp_path):
    out = tmp_path / "candidates.jsonl"
    assert _cli(out)[0] == 0
    lines = out.read_text().splitlines()
    assert lines and all(gc.is_candidate_record(line) for line in lines)


@pytest.mark.parametrize(
    "line",
    [
        '{"query": "q", "expected_doc_ids": ["A"], "note": "a candidate"}',
        '{"query": "q", "expected_doc_ids": ["A"], "status": "candidate"}',
        '{"query": "q", "expected_doc_ids": ["A"], "status": "accepted", "gold_chunk_ids": []}',
        '"candidate"',
        '{"status": "candidate"',
    ],
)
def test_other_records_are_not_candidates(line):
    assert not gc.is_candidate_record(line)


def test_the_golden_parser_still_accepts_a_candidate_row(tmp_path):
    """Pin of today's behaviour, not a guarantee (PR body, maintainer
    decision): GoldenEntry ignores unknown fields, so only the path guards
    and ``is_candidate_record`` stand between a candidate file and an
    explicit ``--golden``. PR 2 owns the reader-side refusal; this test
    must then flip."""
    out = tmp_path / "candidates.jsonl"
    assert _cli(out)[0] == 0
    assert len(parse_golden_text(out.read_text())) == 6


@pytest.mark.parametrize(
    "name", ["golden.jsonl", "paraphrase.jsonl", "sections.jsonl", "expert_golden_seed.jsonl"])
def test_no_tracked_eval_set_holds_a_candidate_row(name):
    lines = (ROOT / "evals" / name).read_text(encoding="utf-8").splitlines()
    assert lines and not any(gc.is_candidate_record(line) for line in lines)
