"""scripts/airgap/retire_legacy.sh + legacy_layout.py (issue #1070, R1).

The site runs release d53595e with a plain `mainframe_manuals` collection and
its `__completions` pair, no alias. `--stage copy` keeps a verified points-API
copy (the rollback data) and deletes nothing; `--stage retire` takes this
run's snapshots on every peer, binds and records them, and only then deletes.
Both converge when re-run after a crash at any point. kubectl is a stub that records every call; its `exec -i deploy/rag-agent`
arm runs the real in-pod program locally against a fake Qdrant HTTP server.
The fixture tree is a plain copy, so live runs take the connected opt-out
(#1118 F1); the verified-release guard itself is tested at the end.
"""

from __future__ import annotations

import base64
import json
import subprocess

import pytest

from tests.helpers_airgap import FakeQdrant, connected_checkout, make_bin_tree, write_stub

L, LC = "mainframe_manuals", "mainframe_manuals__completions"
K, KC = "mainframe_manuals_d53595e_keep", "mainframe_manuals_d53595e_keep__completions"
R = "mainframe_manuals_d53595e_retire_record"

KUBECTL = r"""#!/bin/sh
printf '%s\n' "$*" >> "$KC_LOG"
case "$*" in
  *'get statefulset/'*)
    [ "${STS_FAIL:-}" != 1 ] || { echo 'dial tcp: i/o timeout' >&2; exit 1; }
    case "$*" in *jsonpath*) printf '%s' "${STS_REPLICAS:-1}" ;; *) [ "${STS_ABSENT:-}" = 1 ] || echo statefulset.apps/qdrant ;; esac ;;
  *'get secret '*'-apikey '*)
    printf '%s' "${WRITER_KEY_B64:-}" ;;
  *' -- df -Pk '*)
    [ "${DF_FAIL:-}" != 1 ] || exit 1
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
    printf '/dev/a 100000 %s %s 10%% /qdrant/storage\n' "${DF_USED:-1000}" "${DF_FREE:-90000}"
    printf '/dev/b 100000 0 %s 0%% /qdrant/snapshots\n' "${DF_SNAP_FREE:-90000}" ;;
  *'exec -i deploy/rag-agent -- python3 -c '*)
    [ "${EXEC_FAIL:-}" != 1 ] || { echo 'error: unable to upgrade connection: container not found' >&2; exit 1; }
    while [ "$1" != "--" ]; do shift; done; shift
    exec env QDRANT_URL="$FAKE_QDRANT_URL" QDRANT_API_KEY="$FAKE_READ_KEY" "$@" ;;
esac
exit 0
"""


def _points(n, *, size=4, prefix=""):
    return [{"id": i, "vector": {"dense": [0.25 * (i % 4)] * size,
                                 "bm25": {"indices": [i % 7], "values": [0.5]}},
             "payload": {"doc_id": f"{prefix}doc-{i % 3}"}} for i in range(1, n + 1)]


def _legacy_site(qdrant: FakeQdrant, n: int = 600) -> None:
    """The d53595e layout: L, LC with its manifest and completion markers."""
    from mainframe_rag.ingest.representation import manifest_point_id

    qdrant.add_collection(L, _points(n), schema={"doc_id": {"data_type": "keyword"}})
    markers = [{"id": 900 + i, "vector": {"dense": [0.0] * 4}, "payload": {"doc_id": f"doc-{i}"}}
               for i in range(3)]
    manifest = {"id": manifest_point_id(LC), "vector": {"dense": [1.0, 0.0, 0.0, 0.0]},
                "payload": {"record_type": "representation-manifest", "state": "committed",
                            "target_collection": LC, "manifest_digest": "m1"}}
    qdrant.add_collection(LC, [*markers, manifest])


@pytest.fixture
def qdrant():
    fake = FakeQdrant()
    yield fake
    fake.close()


@pytest.fixture
def tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "retire_legacy.sh", "legacy_layout.py"])
    for name in ("kubectl", "oc", "helm"):
        write_stub(tmp_path / "bin" / name, KUBECTL)
    return tmp_path


def _run(tree, qdrant, *args, **env):
    base = {
        "PATH": f"{tree / 'bin'}:/usr/bin:/bin",
        "KC_LOG": str(tree / "kc.log"),
        "AIRGAP_ENV": str(tree / "empty.env"),
        "NAMESPACE": "ns",
        "FAKE_QDRANT_URL": qdrant.url,
        "FAKE_READ_KEY": FakeQdrant.READ_KEY,
        "WRITER_KEY_B64": base64.b64encode(FakeQdrant.WRITE_KEY.encode()).decode(),
        "QDRANT_PEER_URLS": qdrant.url,
    }
    (tree / "empty.env").write_text("")
    base.update(env)
    connected_checkout(base, explicit=tuple(env))
    base = {key: value for key, value in base.items() if value is not None}
    script = tree / "scripts" / "airgap" / "retire_legacy.sh"
    return subprocess.run(["sh", str(script), *args], capture_output=True, text=True,
                          env=base, cwd=tree, check=False)


def _log(tree):
    path = tree / "kc.log"
    return path.read_text() if path.exists() else ""


def _assert_no_secret(tree, result):
    for text in (result.stdout, result.stderr, _log(tree)):
        assert FakeQdrant.WRITE_KEY not in text
        assert FakeQdrant.READ_KEY not in text
        assert base64.b64encode(FakeQdrant.WRITE_KEY.encode()).decode() not in text


def _writes(qdrant):
    return [op for op in qdrant.ops if op.split(":")[0] in FakeQdrant.WRITE_OPS]


def _assert_retired(qdrant, n=600):
    assert L not in qdrant.collections and LC not in qdrant.collections
    assert qdrant.aliases == {}
    assert len(qdrant.collections[K]["points"]) == n


# --- arguments ----------------------------------------------------------------


@pytest.mark.parametrize("args", [
    (), ("--confirm", L), ("--stage", "copy"), ("--stage", "copy", "--confirm"),
    ("--stage", "copy", "--confirm", "other"), ("--stage", "all", "--confirm", L),
    ("--stage", "copy", "--rollback", "--confirm", L),
    ("--stage", "retire", "--confirm", L, "--qdrant-url", "http://127.0.0.1:1"),
    ("--rollback", "--confirm", L, "--qdrant-url", "http://qdrant:6333"),
])
def test_without_an_explicit_stage_and_confirmation_nothing_is_called(tree, qdrant, args):
    _legacy_site(qdrant)
    result = _run(tree, qdrant, *args)
    assert result.returncode != 0
    assert "E_RETIRE_" in result.stderr
    assert _log(tree) == ""
    assert qdrant.ops == []


def test_unknown_argument_refuses(tree, qdrant):
    result = _run(tree, qdrant, "--stage", "copy", "--confirm", L, "--force")
    assert result.returncode != 0
    assert "unknown argument" in result.stderr
    assert qdrant.ops == []


def test_a_retired_operator_key_refuses_before_any_call(tree, qdrant):
    """#1111: a d53595e-era key (NAMESPACE's old alias) is refused, not ignored."""
    _legacy_site(qdrant)
    result = _run(tree, qdrant, "--stage", "copy", "--confirm", L, OPENSHIFT_NAMESPACE="site-ns")
    assert result.returncode != 0
    assert "OPENSHIFT_NAMESPACE" in result.stderr
    assert "site-ns" not in result.stdout + result.stderr
    assert _log(tree) == "" and qdrant.ops == []


# --- stage copy: safe while d53595e serves -------------------------------------


def _copy(tree, qdrant, *extra, **env):
    return _run(tree, qdrant, "--stage", "copy", "--confirm", L, *extra, **env)


def _retire(tree, qdrant, *extra, **env):
    return _run(tree, qdrant, "--stage", "retire", "--confirm", L, *extra, **env)


def _layout(qdrant):
    from tests.helpers_airgap import REPO

    done = subprocess.run(
        ["python3", str(REPO / "scripts/airgap/legacy_layout.py"), "check", "--collection", L],
        input="\n", capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "QDRANT_URL": qdrant.url, "QDRANT_API_KEY": FakeQdrant.READ_KEY})
    return done.stdout.strip()


def test_copy_stage_keeps_a_verified_copy_and_deletes_nothing(tree, qdrant):
    from mainframe_rag.ingest.representation import manifest_point_id

    _legacy_site(qdrant)
    source = {i: dict(p) for i, p in qdrant.collections[L]["points"].items()}
    result = _copy(tree, qdrant)
    assert result.returncode == 0, result.stderr
    # Search keeps working: the legacy pair is untouched and still the layout.
    assert L in qdrant.collections and LC in qdrant.collections
    assert _layout(qdrant) == "LAYOUT flat_legacy"
    assert not any(op.split(":")[0] in ("delete", "snapshot_create") for op in qdrant.ops)
    assert qdrant.collections[K]["points"] == source
    assert qdrant.collections[K]["payload_schema"] == {"doc_id": {"data_type": "keyword"}}
    kept = qdrant.collections[KC]["points"]
    assert kept[manifest_point_id(KC)]["payload"] == kept[manifest_point_id(LC)]["payload"]
    assert "STATE copied" in result.stdout and "--stage retire" in result.stdout
    _assert_no_secret(tree, result)


@pytest.mark.parametrize("crash", [f"create:{K}", f"upsert:{K}", f"create:{KC}", f"upsert:{KC}", f"index:{K}"])
def test_copy_stage_crash_converges_on_rerun_and_never_deletes(tree, qdrant, crash):
    _legacy_site(qdrant)
    qdrant.faults[crash] = 1
    assert _copy(tree, qdrant).returncode != 0
    second = _copy(tree, qdrant)
    assert second.returncode == 0, second.stderr
    assert len(qdrant.collections[K]["points"]) == 600
    assert L in qdrant.collections and LC in qdrant.collections
    assert not any(op.startswith("delete") for op in qdrant.ops)


def test_a_partial_copy_is_resumed_not_restarted(tree, qdrant):
    _legacy_site(qdrant)
    qdrant.add_collection(K, _points(600)[:300])  # an earlier run stopped half-way
    result = _copy(tree, qdrant)
    assert result.returncode == 0, result.stderr
    assert len(qdrant.collections[K]["points"]) == 600
    assert f"create:{K}" not in qdrant.ops


def test_a_foreign_keep_collection_is_refused(tree, qdrant):
    _legacy_site(qdrant)
    qdrant.add_collection(K, _points(5, prefix="other-") + [{"id": 5000, "vector": {"dense": [0.0] * 4}}])
    result = _copy(tree, qdrant)
    assert result.returncode != 0
    assert "E_RETIRE_KEEP_MISMATCH" in result.stderr
    assert not any(op.startswith("delete") for op in qdrant.ops)


def test_missing_manifest_refuses(tree, qdrant):
    from mainframe_rag.ingest.representation import manifest_point_id

    _legacy_site(qdrant)
    del qdrant.collections[LC]["points"][manifest_point_id(LC)]
    result = _copy(tree, qdrant)
    assert result.returncode != 0
    assert "E_RETIRE_NO_MANIFEST" in result.stderr


# --- stage retire: the window ---------------------------------------------------


def test_retire_without_the_copy_stage_refuses_and_deletes_nothing(tree, qdrant):
    _legacy_site(qdrant)
    result = _retire(tree, qdrant)
    assert result.returncode != 0
    assert "E_RETIRE_NOT_COPIED" in result.stderr and "--stage copy" in result.stderr
    assert L in qdrant.collections and not any(op.startswith("delete") for op in qdrant.ops)


def test_retire_snapshots_binds_records_then_deletes(tree, qdrant):
    _legacy_site(qdrant)
    assert _copy(tree, qdrant).returncode == 0
    result = _retire(tree, qdrant)
    assert result.returncode == 0, result.stderr
    _assert_retired(qdrant)
    ops = qdrant.ops
    first_delete = ops.index(f"delete:{L}")
    assert ops.index(f"snapshot_create:{L}") < first_delete
    assert ops.index(f"snapshot_create:{LC}") < first_delete
    assert ops.index(f"upsert:{R}") < first_delete
    # The run record names exactly the snapshots this run created, bound to
    # the collections' contents, and outlives them.
    record = next(iter(qdrant.collections[R]["points"].values()))["payload"]
    names = {e["collection"]: e for e in record["snapshots"]}
    assert names[L]["points"] == 600 and names[LC]["points"] == 4
    assert names[L]["name"].startswith("mainframe_manuals-peer-2026-10-27-")
    assert f"RECORDED snapshot {L} on {qdrant.url}: {names[L]['name']}" in result.stdout
    assert "created by this run" in result.stdout
    assert "--rollback --confirm mainframe_manuals" in result.stdout
    _assert_no_secret(tree, result)


def test_rerun_after_success_is_a_no_op(tree, qdrant):
    _legacy_site(qdrant)
    assert _copy(tree, qdrant).returncode == 0
    assert _retire(tree, qdrant).returncode == 0
    before = len(qdrant.ops)
    again = _retire(tree, qdrant)
    assert again.returncode == 0, again.stderr
    assert "STATE retired" in again.stdout and "RECORDED snapshot" in again.stdout
    assert [op for op in qdrant.ops[before:] if op.split(":")[0] in FakeQdrant.WRITE_OPS] == []
    assert _copy(tree, qdrant).returncode == 0  # copy after retire: nothing to copy
    _assert_retired(qdrant)


@pytest.mark.parametrize("crash", [
    f"snapshot_create:{L}", f"snapshot_create:{LC}", f"create:{R}", f"upsert:{R}",
    f"delete:{L}", f"delete:{LC}",
])
def test_retire_crash_at_any_point_converges_on_rerun(tree, qdrant, crash):
    _legacy_site(qdrant)
    assert _copy(tree, qdrant).returncode == 0
    qdrant.faults[crash] = 1
    first = _retire(tree, qdrant)
    assert first.returncode != 0
    if not crash.startswith("delete"):
        assert L in qdrant.collections and LC in qdrant.collections
    second = _retire(tree, qdrant)
    assert second.returncode == 0, second.stderr
    _assert_retired(qdrant)
    _assert_no_secret(tree, second)


def test_a_stale_smaller_snapshot_is_never_proof(tree, qdrant):
    """B3: an older 300-point snapshot of L (and one of LC) is listed. Retire
    must take and report its own; the stale ones are never recorded or used."""
    _legacy_site(qdrant)
    stale = qdrant.add_snapshot(L, points=300)
    stale_c = qdrant.add_snapshot(LC, points=2)
    assert _copy(tree, qdrant).returncode == 0
    result = _retire(tree, qdrant)
    assert result.returncode == 0, result.stderr
    assert qdrant.ops.count(f"snapshot_create:{L}") == 1
    assert stale["name"] not in result.stdout and stale_c["name"] not in result.stdout
    record = next(iter(qdrant.collections[R]["points"].values()))["payload"]
    assert {e["name"] for e in record["snapshots"]}.isdisjoint({stale["name"], stale_c["name"]})


@pytest.mark.parametrize("failure", ["create", "unlisted"])
def test_a_stale_snapshot_does_not_allow_deletion_when_this_runs_fails(tree, qdrant, failure):
    _legacy_site(qdrant)
    qdrant.add_snapshot(L, points=300)
    assert _copy(tree, qdrant).returncode == 0
    if failure == "create":
        qdrant.faults[f"snapshot_create:{L}"] = 5
    else:
        qdrant.bad_snapshots.add(L)
    result = _retire(tree, qdrant)
    assert result.returncode != 0
    assert L in qdrant.collections and LC in qdrant.collections
    assert not any(op.startswith("delete") for op in qdrant.ops)


def test_a_recorded_snapshot_is_reused_only_while_its_binding_holds(tree, qdrant):
    _legacy_site(qdrant)
    assert _copy(tree, qdrant).returncode == 0
    qdrant.faults[f"delete:{L}"] = 1
    assert _retire(tree, qdrant).returncode != 0
    assert qdrant.ops.count(f"snapshot_create:{L}") == 1
    # Unchanged: the recorded snapshot is reused, not retaken.
    qdrant.faults[f"delete:{L}"] = 1
    assert _retire(tree, qdrant).returncode != 0
    assert qdrant.ops.count(f"snapshot_create:{L}") == 1
    # L (and its verified copy) gained a point: the binding no longer holds.
    extra = {"id": 9999, "vector": {"dense": [0.5] * 4}, "payload": {"doc_id": "late"}}
    qdrant.collections[L]["points"]["9999"] = dict(extra)
    qdrant.collections[K]["points"]["9999"] = dict(extra)
    result = _retire(tree, qdrant)
    assert result.returncode == 0, result.stderr
    assert qdrant.ops.count(f"snapshot_create:{L}") == 2
    record = next(iter(qdrant.collections[R]["points"].values()))["payload"]
    assert {e["collection"]: e["points"] for e in record["snapshots"]}[L] == 601


def test_a_write_during_the_snapshot_unbinds_it_and_nothing_is_deleted(tree, qdrant):
    _legacy_site(qdrant)
    assert _copy(tree, qdrant).returncode == 0
    qdrant.on_snapshot[L] = lambda fake: fake.collections[L]["points"].update(
        {"7777": {"id": 7777, "vector": {"dense": [0.0] * 4}, "payload": {}}})
    result = _retire(tree, qdrant)
    assert result.returncode != 0
    assert "E_RETIRE_SNAPSHOT_UNBOUND" in result.stderr
    assert L in qdrant.collections and not any(op.startswith("delete") for op in qdrant.ops)


def test_collection_gone_and_nothing_kept_refuses_unless_accepted(tree, qdrant):
    refused = _retire(tree, qdrant)
    assert refused.returncode != 0
    assert "E_RETIRE_NOTHING_KEPT" in refused.stderr and "--no-keep-ok" in refused.stderr
    accepted = _retire(tree, qdrant, "--no-keep-ok")
    assert accepted.returncode == 0, accepted.stderr
    assert "nothing to retire" in accepted.stdout
    assert [op for op in qdrant.ops if op.split(":")[0] in FakeQdrant.WRITE_OPS] == []


@pytest.mark.parametrize("stage", ["copy", "retire"])
def test_an_alias_name_is_never_deleted_or_copied(tree, qdrant, stage):
    qdrant.add_collection("mainframe_manuals__gen1", _points(3))
    qdrant.aliases[L] = "mainframe_manuals__gen1"
    result = _run(tree, qdrant, "--stage", stage, "--confirm", L)
    assert result.returncode == 0, result.stderr
    assert "STATE published" in result.stdout
    assert [op for op in qdrant.ops if op.split(":")[0] in FakeQdrant.WRITE_OPS] == []


def test_a_legacy_name_targeted_by_another_alias_is_refused(tree, qdrant):
    _legacy_site(qdrant)
    qdrant.aliases["mainframe_manuals__build_x"] = L
    result = _copy(tree, qdrant)
    assert result.returncode != 0
    assert "E_RETIRE_ALIAS_CONFLICT" in result.stderr
    assert [op for op in qdrant.ops if op.split(":")[0] in FakeQdrant.WRITE_OPS] == []


# --- free space -----------------------------------------------------------------


@pytest.mark.parametrize(("stage", "used", "free", "snap", "ok"), [
    ("copy", 50000, 50000, 0, True), ("copy", 50000, 49999, 90000, False),
    ("retire", 50000, 50000, 25000, True), ("retire", 50000, 50000, 24999, False),
    ("retire", 50000, 49999, 90000, False),
])
def test_free_space_gate_per_stage(tree, qdrant, stage, used, free, snap, ok):
    _legacy_site(qdrant)
    if stage == "retire":
        assert _copy(tree, qdrant, "--space-verified").returncode == 0
    before = len(qdrant.ops)
    result = _run(tree, qdrant, "--stage", stage, "--confirm", L,
                  DF_USED=str(used), DF_FREE=str(free), DF_SNAP_FREE=str(snap))
    assert (result.returncode == 0) is ok, result.stderr
    if not ok:
        assert "E_RETIRE_SPACE" in result.stderr and "--space-verified" in result.stderr
        assert [op for op in qdrant.ops[before:] if op.split(":")[0] in FakeQdrant.WRITE_OPS] == []


def test_unreadable_free_space_refuses_with_guidance_then_accepts_the_operator_check(tree, qdrant):
    _legacy_site(qdrant)
    refused = _copy(tree, qdrant, DF_FAIL="1")
    assert refused.returncode != 0
    assert "E_RETIRE_SPACE_UNKNOWN" in refused.stderr and "--space-verified" in refused.stderr
    assert [op for op in qdrant.ops if op.split(":")[0] in FakeQdrant.WRITE_OPS] == []
    assert _copy(tree, qdrant, "--space-verified", DF_FAIL="1").returncode == 0
    assert _retire(tree, qdrant, "--space-verified", DF_FAIL="1").returncode == 0
    _assert_retired(qdrant)


def test_space_is_checked_on_every_peer(tree, qdrant):
    _legacy_site(qdrant)
    result = _copy(tree, qdrant, STS_REPLICAS="3")
    assert result.returncode == 0, result.stderr
    for peer in ("qdrant-0", "qdrant-1", "qdrant-2"):
        assert f"exec {peer} -- df -Pk /qdrant/storage /qdrant/snapshots" in _log(tree)


# --- prerequisites and transport failures ---------------------------------------


@pytest.mark.parametrize(("seed", "env", "needle"), [
    (False, {"STS_ABSENT": "1"}, "E_RETIRE_NO_QDRANT"),
    (True, {"WRITER_KEY_B64": ""}, "E_RETIRE_NO_WRITER_KEY"),
], ids=["no-qdrant-release", "no-writer-key"])
def test_prerequisites_refuse_before_any_write(tree, qdrant, seed, env, needle):
    if seed:
        _legacy_site(qdrant)
    result = _copy(tree, qdrant, **env)
    assert result.returncode != 0
    assert needle in result.stderr
    assert [op for op in qdrant.ops if op.split(":")[0] in FakeQdrant.WRITE_OPS] == []


def test_no_agent_pod_refuses_the_stages(tree, qdrant):
    _legacy_site(qdrant)
    result = _copy(tree, qdrant, EXEC_FAIL="1")
    assert result.returncode != 0
    assert "container not found" not in result.stderr  # client text is never echoed
    assert L in qdrant.collections


def test_dry_run_reads_and_writes_nothing(tree, qdrant):
    _legacy_site(qdrant)
    result = _retire(tree, qdrant, AIRGAP_DRYRUN="1")
    assert result.returncode == 0, result.stderr
    assert "[dryrun]" in result.stdout
    assert _log(tree) == "" and qdrant.ops == []


# --- rollback ---------------------------------------------------------------------


def _went_live(tree, qdrant):
    _legacy_site(qdrant)
    assert _copy(tree, qdrant).returncode == 0
    assert _retire(tree, qdrant).returncode == 0
    qdrant.add_collection("mainframe_manuals__gen2", _points(7))
    qdrant.aliases[L] = "mainframe_manuals__gen2"


def test_rollback_points_the_alias_at_the_kept_copy_atomically(tree, qdrant):
    _went_live(tree, qdrant)
    result = _run(tree, qdrant, "--rollback", "--confirm", L)
    assert result.returncode == 0, result.stderr
    assert qdrant.aliases[L] == K
    assert "mainframe_manuals__gen2" in qdrant.collections  # the new generation is kept
    assert qdrant.ops.count("aliases_update") == 1
    again = _run(tree, qdrant, "--rollback", "--confirm", L)
    assert again.returncode == 0 and "already" in again.stdout
    assert qdrant.ops.count("aliases_update") == 1
    _assert_no_secret(tree, result)


def test_rollback_without_an_agent_pod_runs_through_a_port_forward(tree, qdrant):
    """B1: after go-live with no running agent (new one crashlooping, d53595e
    not yet redeployed), the rollback still runs: on the bastion, against the
    port-forwarded Service, writer key on stdin only, never via exec."""
    _went_live(tree, qdrant)
    refused = _run(tree, qdrant, "--rollback", "--confirm", L, EXEC_FAIL="1")
    assert refused.returncode != 0
    assert "port-forward svc/qdrant 16333:6333" in refused.stderr
    assert "--qdrant-url http://127.0.0.1:16333" in refused.stderr
    assert qdrant.aliases[L] == "mainframe_manuals__gen2"
    (tree / "kc.log").unlink()
    result = _run(tree, qdrant, "--rollback", "--confirm", L, "--qdrant-url", qdrant.url, EXEC_FAIL="1")
    assert result.returncode == 0, result.stderr
    assert qdrant.aliases[L] == K
    assert "exec" not in _log(tree)
    _assert_no_secret(tree, result)


def test_rollback_without_a_kept_copy_refuses(tree, qdrant):
    result = _run(tree, qdrant, "--rollback", "--confirm", L)
    assert result.returncode != 0
    assert "E_ROLLBACK_NO_KEEP" in result.stderr
    assert "aliases_update" not in qdrant.ops


def test_after_a_rollback_the_preflight_refuses_and_retire_reopens_the_upgrade(tree, qdrant):
    """Rolled back (alias -> kept copy): the layout reads `rolled_back`; the
    retire stage unpoints the alias, keeps the copy, and the next ordinary
    layout check is `empty` (deploy proceeds)."""
    _went_live(tree, qdrant)
    assert _run(tree, qdrant, "--rollback", "--confirm", L).returncode == 0
    assert _layout(qdrant) == "LAYOUT rolled_back"
    result = _retire(tree, qdrant)
    assert result.returncode == 0, result.stderr
    assert "UNPOINTED alias mainframe_manuals" in result.stdout
    assert L not in qdrant.aliases and len(qdrant.collections[K]["points"]) == 600
    assert _layout(qdrant) == "LAYOUT empty"


@pytest.mark.parametrize(("release", "name"), [
    ("qdrant", "qdrant"), ("rag-qdrant", "rag-qdrant"), ("vectors", "vectors-qdrant"),
])
def test_qdrant_resource_names_follow_the_chart_fullname(tree, qdrant, release, name):
    """The vendored chart names its StatefulSet, pods, headless Service and
    Secret after the release alone when it contains "qdrant", else
    `<release>-qdrant`; the retire tool reads exactly those."""
    _legacy_site(qdrant)
    assert _copy(tree, qdrant, QDRANT_RELEASE=release, STS_REPLICAS="3").returncode == 0
    log = _log(tree)
    assert f"get statefulset/{name} " in log and f"get secret {name}-apikey " in log
    assert f"exec {name}-2 -- df -Pk" in log
    # Retire derives peer URLs from the chart's headless names (unreachable
    # here: the snapshot step stops on a transport failure, nothing deleted).
    result = _retire(tree, qdrant, QDRANT_RELEASE=release, STS_REPLICAS="3", QDRANT_PEER_URLS="")
    assert result.returncode != 0
    assert L in qdrant.collections
    rollback = _run(tree, qdrant, "--rollback", "--confirm", L, QDRANT_RELEASE=release, EXEC_FAIL="1")
    assert f"port-forward svc/{name} " in rollback.stderr


# --- the in-pod program's own contracts --------------------------------------


def test_in_pod_ids_match_the_package_derivations():
    """The old agent cannot import this release: the program repeats two id
    derivations, pinned here to the package owners."""
    import importlib.util

    from mainframe_rag.ingest.publish import publication_metadata_point_id
    from mainframe_rag.ingest.representation import manifest_point_id
    from tests.helpers_airgap import REPO

    spec = importlib.util.spec_from_file_location("legacy_layout", REPO / "scripts/airgap/legacy_layout.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in (LC, KC, "mainframe_manuals__gen1__completions"):
        assert module.manifest_point_id(name) == manifest_point_id(name)
        assert module.publication_point_id(name) == publication_metadata_point_id(name)
    assert json.dumps(module.BUILD_FIELDS)  # the binding fields stay a fixed tuple


# --- #1118 item e: the verified-release guard (F1) --------------------------------

_MODES = [("--stage", "copy"), ("--stage", "retire"), ("--rollback",)]
_RELEASE = {"AIRGAP_UNVERIFIED_CHECKOUT": None, "CI_REHEARSAL": None}
_NO_MANIFEST = ("FAIL: checkout is not a verified signed release (no packed MANIFEST) — run from the "
                "checkout that bootstrap.sh creates from the signed bundle, with SNEAKERNET_TRUSTED_PUB set; "
                "only a connected developer or CI lane may set AIRGAP_UNVERIFIED_CHECKOUT=1 with "
                "CI_REHEARSAL=1 instead\n")
_OPT_OUT_REFUSED = ("FAIL: AIRGAP_UNVERIFIED_CHECKOUT=1 is refused without CI_REHEARSAL=1 in the process "
                    "environment — the unverified-checkout opt-out is for connected development and the "
                    "connected CI and Kind lanes only, never the air gap; run from a verified signed release "
                    "instead\n")
_HEAD_DIFFERS = ("FAIL: executing checkout HEAD does not match the packed MANIFEST sha — run from the approved "
                 "bundle checkout (git rev-parse HEAD and the MANIFEST sha: line must agree); overriding "
                 "IMAGE_SHA alone never changes which code executes\n")


def _anchor():
    from tests.helpers_airgap import release_anchor

    return str(release_anchor())


def _git_release(tree, manifest_sha=None):
    """Commit the fixture tree and pack a signed MANIFEST naming `manifest_sha`
    (default: the commit itself, i.e. a verified signed release)."""
    from tests.helpers_airgap import write_signed_manifest

    for args in (("init", "-q", "-b", "main"), ("add", "."),
                 ("-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "release")):
        subprocess.run(["git", *args], cwd=tree, check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tree, text=True).strip()
    write_signed_manifest(tree / "dist", f"sha: {manifest_sha or head}\n")
    return head


def _refused_untouched(tree, qdrant, result, message):
    assert result.returncode == 1
    assert result.stderr.endswith(message), result.stderr
    assert result.stderr.count("FAIL:") == 1
    assert _log(tree) == "", "kubectl/oc was invoked before the guard"
    assert qdrant.ops == [], "Qdrant was contacted before the guard"


@pytest.mark.parametrize("mode", _MODES, ids=["copy", "retire", "rollback"])
def test_an_unverified_clone_is_refused_before_any_cluster_or_qdrant_call(tree, qdrant, mode):
    _legacy_site(qdrant)
    result = _run(tree, qdrant, *mode, "--confirm", L, SNEAKERNET_TRUSTED_PUB=_anchor(), **_RELEASE)
    _refused_untouched(tree, qdrant, result, _NO_MANIFEST)


def test_the_port_forward_rollback_is_guarded_too(tree, qdrant):
    _went_live(tree, qdrant)
    (tree / "kc.log").unlink()
    qdrant.ops.clear()
    result = _run(tree, qdrant, "--rollback", "--confirm", L, "--qdrant-url", qdrant.url,
                  SNEAKERNET_TRUSTED_PUB=_anchor(), **_RELEASE)
    _refused_untouched(tree, qdrant, result, _NO_MANIFEST)
    assert qdrant.aliases[L] == "mainframe_manuals__gen2"


def test_a_clone_at_another_head_than_its_manifest_is_refused_even_with_the_opt_out(tree, qdrant):
    """F1: the opt-out keeps the pre-F1 rules, so a present MANIFEST is still
    checked against HEAD."""
    _legacy_site(qdrant)
    _git_release(tree, manifest_sha="0" * 40)
    for env in (_RELEASE, {"AIRGAP_UNVERIFIED_CHECKOUT": "1", "CI_REHEARSAL": "1"}):
        result = _copy(tree, qdrant, SNEAKERNET_TRUSTED_PUB=_anchor(), **env)
        _refused_untouched(tree, qdrant, result, _HEAD_DIFFERS)


@pytest.mark.parametrize("env, message", [
    ({"AIRGAP_UNVERIFIED_CHECKOUT": "1", "CI_REHEARSAL": None}, _OPT_OUT_REFUSED),
    ({"AIRGAP_UNVERIFIED_CHECKOUT": "1", "CI_REHEARSAL": "0"}, _OPT_OUT_REFUSED),
    ({"AIRGAP_UNVERIFIED_CHECKOUT": "true", "CI_REHEARSAL": "1"},
     ("FAIL: AIRGAP_UNVERIFIED_CHECKOUT must be 1 (connected opt-out, with CI_REHEARSAL=1) or unset; "
      "refusing to guess\n")),
], ids=["no-rehearsal", "rehearsal-0", "unknown-value"])
def test_the_opt_out_is_accepted_only_with_ci_rehearsal(tree, qdrant, env, message):
    _legacy_site(qdrant)
    result = _copy(tree, qdrant, **env)
    _refused_untouched(tree, qdrant, result, message)


def test_the_opt_out_cannot_come_from_the_operator_file(tree, qdrant):
    _legacy_site(qdrant)
    env_file = tree / "site.env"
    env_file.write_text("AIRGAP_UNVERIFIED_CHECKOUT=1\nCI_REHEARSAL=1\n")
    result = _copy(tree, qdrant, AIRGAP_ENV=str(env_file), SNEAKERNET_TRUSTED_PUB=_anchor(), **_RELEASE)
    assert result.returncode != 0
    assert _log(tree) == "" and qdrant.ops == []


def test_the_connected_opt_out_runs_with_a_notice(tree, qdrant):
    _legacy_site(qdrant)
    result = _copy(tree, qdrant, AIRGAP_UNVERIFIED_CHECKOUT="1", CI_REHEARSAL="1")
    assert result.returncode == 0, result.stderr
    assert "Notice: no packed MANIFEST — checkout not release-verified" in result.stderr
    assert K in qdrant.collections


def test_a_verified_signed_release_runs_both_stages_and_the_rollback(tree, qdrant):
    _legacy_site(qdrant)
    _git_release(tree)
    env = {"SNEAKERNET_TRUSTED_PUB": _anchor(), **_RELEASE}
    copied = _copy(tree, qdrant, **env)
    assert copied.returncode == 0, copied.stderr
    assert "not release-verified" not in copied.stderr
    retired = _retire(tree, qdrant, **env)
    assert retired.returncode == 0, retired.stderr
    _assert_retired(qdrant)
    rolled = _run(tree, qdrant, "--rollback", "--confirm", L, **env)
    assert rolled.returncode == 0, rolled.stderr
    assert qdrant.aliases[L] == K


def test_a_verified_release_without_the_anchor_is_refused_before_any_call(tree, qdrant):
    _legacy_site(qdrant)
    _git_release(tree)
    result = _copy(tree, qdrant, SNEAKERNET_TRUSTED_PUB=None, **_RELEASE)
    _refused_untouched(tree, qdrant, result,
                       "FAIL: no release trust anchor is configured for the bundle's origin (upstream) — set "
                       "SNEAKERNET_TRUSTED_PUB to the upstream release signing public key obtained out of band "
                       "(docs/release-verification.md); trust on first use never establishes a release\n")


def test_dry_run_needs_no_verified_release(tree, qdrant):
    _legacy_site(qdrant)
    result = _retire(tree, qdrant, AIRGAP_DRYRUN="1", **_RELEASE)
    assert result.returncode == 0, result.stderr
    assert _log(tree) == "" and qdrant.ops == []
