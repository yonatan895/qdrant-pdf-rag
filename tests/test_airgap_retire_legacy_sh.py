"""scripts/airgap/retire_legacy.sh + legacy_layout.py (issue #1070, R1).

The site runs release d53595e with a plain `mainframe_manuals` collection and
its `__completions` pair, no alias. The retire step keeps a verified
points-API copy (the rollback data), snapshots both collections on every peer,
and only then deletes them; it converges when re-run after a crash at any
point. kubectl is a stub that records every call; its `exec -i deploy/rag-agent`
arm runs the real in-pod program locally against a fake Qdrant HTTP server.
"""

from __future__ import annotations

import base64
import json
import subprocess

import pytest

from tests.helpers_airgap import FakeQdrant, make_bin_tree, write_stub

L, LC = "mainframe_manuals", "mainframe_manuals__completions"
K, KC = "mainframe_manuals_d53595e_keep", "mainframe_manuals_d53595e_keep__completions"

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


# --- confirmation and inputs -------------------------------------------------


@pytest.mark.parametrize("args", [(), ("--confirm",), ("--confirm", "other"), ("--confirm", "mainframe_manuals_x")])
def test_retire_without_the_exact_confirmation_refuses_before_any_call(tree, qdrant, args):
    _legacy_site(qdrant)
    result = _run(tree, qdrant, *args)
    assert result.returncode != 0
    assert "--confirm mainframe_manuals" in result.stderr
    assert _log(tree) == ""
    assert qdrant.ops == []


def test_unknown_argument_refuses(tree, qdrant):
    result = _run(tree, qdrant, "--confirm", L, "--force")
    assert result.returncode != 0
    assert "unknown argument" in result.stderr
    assert qdrant.ops == []


# --- the converging state machine ---------------------------------------------


def test_retire_keeps_a_verified_copy_snapshots_then_deletes(tree, qdrant):
    from mainframe_rag.ingest.representation import manifest_point_id

    _legacy_site(qdrant)
    source = {i: dict(p) for i, p in qdrant.collections[L]["points"].items()}
    result = _run(tree, qdrant, "--confirm", L)
    assert result.returncode == 0, result.stderr
    _assert_retired(qdrant)
    # The kept copy is the stored content, not just the same count.
    assert qdrant.collections[K]["points"] == source
    assert qdrant.collections[K]["payload_schema"] == {"doc_id": {"data_type": "keyword"}}
    assert qdrant.collections[K]["config"]["params"]["vectors"] == {"dense": {"size": 4, "distance": "Cosine"}}
    # The d53595e manifest is re-keyed for the kept control, payload verbatim.
    kept = qdrant.collections[KC]["points"]
    assert kept[manifest_point_id(KC)]["payload"] == kept[manifest_point_id(LC)]["payload"]
    # Order: both snapshots are verified before the first delete.
    ops = qdrant.ops
    first_delete = ops.index(f"delete:{L}")
    assert ops.index(f"snapshot_create:{L}") < first_delete
    assert ops.index(f"snapshot_create:{LC}") < first_delete
    assert "STATE retired" in result.stdout
    assert "SNAPSHOT peer 0 mainframe_manuals: mainframe_manuals-peer-" in result.stdout
    assert "--rollback --confirm mainframe_manuals" in result.stdout
    assert "snapshots/recover" in result.stdout
    _assert_no_secret(tree, result)


def test_rerun_after_success_is_a_no_op(tree, qdrant):
    _legacy_site(qdrant)
    assert _run(tree, qdrant, "--confirm", L).returncode == 0
    before = len(qdrant.ops)
    again = _run(tree, qdrant, "--confirm", L)
    assert again.returncode == 0, again.stderr
    assert "STATE retired" in again.stdout
    assert [op for op in qdrant.ops[before:] if op.split(":")[0] in FakeQdrant.WRITE_OPS] == []
    _assert_retired(qdrant)


@pytest.mark.parametrize("crash", [
    f"create:{K}", f"upsert:{K}", f"create:{KC}", f"upsert:{KC}",
    f"snapshot_create:{L}", f"snapshot_create:{LC}", f"delete:{L}", f"delete:{LC}",
])
def test_a_crash_at_any_point_converges_on_the_next_run(tree, qdrant, crash):
    _legacy_site(qdrant)
    qdrant.faults[crash] = 1
    first = _run(tree, qdrant, "--confirm", L)
    assert first.returncode != 0
    assert "STATE retired" not in first.stdout
    if not crash.startswith("delete"):
        # Nothing is deleted before the copy, the re-key and the snapshots are verified.
        assert L in qdrant.collections and LC in qdrant.collections
    second = _run(tree, qdrant, "--confirm", L)
    assert second.returncode == 0, second.stderr
    _assert_retired(qdrant)
    assert len(qdrant.collections[L + "_d53595e_keep"]["points"]) == 600
    _assert_no_secret(tree, second)


def test_a_partial_copy_is_resumed_not_restarted(tree, qdrant):
    _legacy_site(qdrant)
    qdrant.add_collection(K, _points(600)[:300])  # an earlier run stopped half-way
    result = _run(tree, qdrant, "--confirm", L)
    assert result.returncode == 0, result.stderr
    _assert_retired(qdrant)
    assert f"create:{K}" not in qdrant.ops


def test_a_foreign_keep_collection_is_refused_and_nothing_deleted(tree, qdrant):
    _legacy_site(qdrant)
    qdrant.add_collection(K, _points(5, prefix="other-") + [{"id": 5000, "vector": {"dense": [0.0] * 4}}])
    result = _run(tree, qdrant, "--confirm", L)
    assert result.returncode != 0
    assert "E_RETIRE_KEEP_MISMATCH" in result.stderr
    assert L in qdrant.collections and LC in qdrant.collections
    assert not any(op.startswith("delete") for op in qdrant.ops)


@pytest.mark.parametrize("bad", [L, LC])
def test_an_unverifiable_snapshot_deletes_nothing(tree, qdrant, bad):
    _legacy_site(qdrant)
    qdrant.bad_snapshots.add(bad)
    result = _run(tree, qdrant, "--confirm", L)
    assert result.returncode != 0
    assert "E_RETIRE_SNAPSHOT_UNVERIFIED" in result.stderr
    assert L in qdrant.collections and LC in qdrant.collections
    assert not any(op.startswith("delete") for op in qdrant.ops)


def test_snapshot_failure_deletes_nothing(tree, qdrant):
    _legacy_site(qdrant)
    qdrant.faults[f"snapshot_create:{L}"] = 5
    result = _run(tree, qdrant, "--confirm", L)
    assert result.returncode != 0
    assert L in qdrant.collections and LC in qdrant.collections
    assert not any(op.startswith("delete") for op in qdrant.ops)
    _assert_no_secret(tree, result)


def test_an_existing_snapshot_is_reused_on_rerun(tree, qdrant):
    _legacy_site(qdrant)
    qdrant.faults[f"delete:{L}"] = 1
    assert _run(tree, qdrant, "--confirm", L).returncode != 0
    creates = qdrant.ops.count(f"snapshot_create:{L}")
    assert _run(tree, qdrant, "--confirm", L).returncode == 0
    assert qdrant.ops.count(f"snapshot_create:{L}") == creates == 1


def test_missing_manifest_refuses_before_any_delete(tree, qdrant):
    from mainframe_rag.ingest.representation import manifest_point_id

    _legacy_site(qdrant)
    del qdrant.collections[LC]["points"][manifest_point_id(LC)]
    result = _run(tree, qdrant, "--confirm", L)
    assert result.returncode != 0
    assert "E_RETIRE_NO_MANIFEST" in result.stderr
    assert L in qdrant.collections


def test_collection_gone_and_nothing_kept_refuses_unless_accepted(tree, qdrant):
    refused = _run(tree, qdrant, "--confirm", L)
    assert refused.returncode != 0
    assert "E_RETIRE_NOTHING_KEPT" in refused.stderr
    assert "--no-keep-ok" in refused.stderr
    accepted = _run(tree, qdrant, "--confirm", L, "--no-keep-ok")
    assert accepted.returncode == 0, accepted.stderr
    assert "nothing to retire" in accepted.stdout
    assert _writes(qdrant) == []


def test_an_alias_name_is_never_deleted(tree, qdrant):
    qdrant.add_collection("mainframe_manuals__gen1", _points(3))
    qdrant.aliases[L] = "mainframe_manuals__gen1"
    result = _run(tree, qdrant, "--confirm", L)
    assert result.returncode == 0, result.stderr
    assert "nothing to retire" in result.stdout
    assert _writes(qdrant) == []
    assert "mainframe_manuals__gen1" in qdrant.collections


def test_a_legacy_name_targeted_by_another_alias_is_refused(tree, qdrant):
    _legacy_site(qdrant)
    qdrant.aliases["mainframe_manuals__build_x"] = L
    result = _run(tree, qdrant, "--confirm", L)
    assert result.returncode != 0
    assert "E_RETIRE_ALIAS_CONFLICT" in result.stderr
    assert _writes(qdrant) == []


# --- free space ---------------------------------------------------------------


def test_insufficient_free_space_refuses_before_any_write(tree, qdrant):
    _legacy_site(qdrant)
    result = _run(tree, qdrant, "--confirm", L, DF_USED="50000", DF_FREE="60000")
    assert result.returncode != 0
    assert "E_RETIRE_SPACE" in result.stderr
    assert _writes(qdrant) == []


def test_unreadable_free_space_refuses_with_guidance_then_accepts_the_operator_check(tree, qdrant):
    _legacy_site(qdrant)
    refused = _run(tree, qdrant, "--confirm", L, DF_FAIL="1")
    assert refused.returncode != 0
    assert "E_RETIRE_SPACE_UNKNOWN" in refused.stderr
    assert "--space-verified" in refused.stderr
    assert _writes(qdrant) == []
    accepted = _run(tree, qdrant, "--confirm", L, "--space-verified", DF_FAIL="1")
    assert accepted.returncode == 0, accepted.stderr
    _assert_retired(qdrant)


def test_space_is_checked_on_every_peer(tree, qdrant):
    _legacy_site(qdrant)
    result = _run(tree, qdrant, "--confirm", L, STS_REPLICAS="3")
    assert result.returncode == 0, result.stderr
    log = _log(tree)
    for peer in ("qdrant-0", "qdrant-1", "qdrant-2"):
        assert f"exec {peer} -- df -Pk /qdrant/storage /qdrant/snapshots" in log


# --- prerequisites and transport failures -------------------------------------


def test_no_qdrant_release_refuses(tree, qdrant):
    result = _run(tree, qdrant, "--confirm", L, STS_ABSENT="1")
    assert result.returncode != 0
    assert "E_RETIRE_NO_QDRANT" in result.stderr
    assert qdrant.ops == []


def test_missing_writer_key_refuses_without_a_write(tree, qdrant):
    _legacy_site(qdrant)
    result = _run(tree, qdrant, "--confirm", L, WRITER_KEY_B64="")
    assert result.returncode != 0
    assert "E_RETIRE_NO_WRITER_KEY" in result.stderr
    assert _writes(qdrant) == []


def test_no_agent_pod_refuses(tree, qdrant):
    _legacy_site(qdrant)
    result = _run(tree, qdrant, "--confirm", L, EXEC_FAIL="1")
    assert result.returncode != 0
    assert "container not found" not in result.stderr  # client text is never echoed
    assert L in qdrant.collections


def test_dry_run_reads_and_writes_nothing(tree, qdrant):
    _legacy_site(qdrant)
    result = _run(tree, qdrant, "--confirm", L, AIRGAP_DRYRUN="1")
    assert result.returncode == 0, result.stderr
    assert "[dryrun]" in result.stdout
    assert _log(tree) == "" and qdrant.ops == []


# --- rollback ---------------------------------------------------------------


def test_rollback_points_the_alias_at_the_kept_copy_atomically(tree, qdrant):
    _legacy_site(qdrant)
    assert _run(tree, qdrant, "--confirm", L).returncode == 0
    qdrant.add_collection("mainframe_manuals__gen2", _points(7))
    qdrant.aliases[L] = "mainframe_manuals__gen2"  # the new release went live
    result = _run(tree, qdrant, "--rollback", "--confirm", L)
    assert result.returncode == 0, result.stderr
    assert qdrant.aliases[L] == K
    assert "mainframe_manuals__gen2" in qdrant.collections  # the new generation is kept
    assert qdrant.ops.count("aliases_update") == 1
    again = _run(tree, qdrant, "--rollback", "--confirm", L)
    assert again.returncode == 0 and "already" in again.stdout
    assert qdrant.ops.count("aliases_update") == 1
    _assert_no_secret(tree, result)


def test_rollback_without_a_kept_copy_refuses(tree, qdrant):
    result = _run(tree, qdrant, "--rollback", "--confirm", L)
    assert result.returncode != 0
    assert "E_ROLLBACK_NO_KEEP" in result.stderr
    assert "aliases_update" not in qdrant.ops


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


def test_after_a_rollback_the_preflight_refuses_and_retire_reopens_the_upgrade(tree, qdrant):
    """Rolled back (alias -> kept copy): the next upgrade attempt is refused
    with the step that reopens it; retire unpoints the alias, keeps the copy,
    and the next ordinary layout check is `empty` (deploy proceeds)."""
    _legacy_site(qdrant)
    assert _run(tree, qdrant, "--confirm", L).returncode == 0
    assert _run(tree, qdrant, "--rollback", "--confirm", L).returncode == 0
    check = subprocess.run(
        ["python3", str(tree / "scripts/airgap/legacy_layout.py"), "check", "--collection", L],
        input="\n", capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "QDRANT_URL": qdrant.url, "QDRANT_API_KEY": FakeQdrant.READ_KEY})
    assert check.stdout.strip() == "LAYOUT rolled_back"
    result = _run(tree, qdrant, "--confirm", L)
    assert result.returncode == 0, result.stderr
    assert "UNPOINTED alias mainframe_manuals" in result.stdout
    assert qdrant.aliases == {} and len(qdrant.collections[K]["points"]) == 600
    check = subprocess.run(
        ["python3", str(tree / "scripts/airgap/legacy_layout.py"), "check", "--collection", L],
        input="\n", capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "QDRANT_URL": qdrant.url, "QDRANT_API_KEY": FakeQdrant.READ_KEY})
    assert check.stdout.strip() == "LAYOUT empty"


@pytest.mark.parametrize(("release", "name"), [
    ("qdrant", "qdrant"), ("rag-qdrant", "rag-qdrant"), ("vectors", "vectors-qdrant"),
])
def test_qdrant_resource_names_follow_the_chart_fullname(tree, qdrant, release, name):
    """The vendored chart names its StatefulSet, pods, headless Service and
    Secret after the release alone when it contains "qdrant", else
    `<release>-qdrant`; the retire step reads exactly those."""
    _legacy_site(qdrant)
    result = _run(tree, qdrant, "--confirm", L, "--space-verified", QDRANT_RELEASE=release,
                  STS_REPLICAS="3", QDRANT_PEER_URLS="")
    log = _log(tree)
    assert f"get statefulset/{name} " in log
    assert f"get secret {name}-apikey " in log
    # Peer URLs derive from the chart's headless names (unreachable here, so
    # the snapshot step stops with a transport failure and deletes nothing).
    assert result.returncode != 0
    assert L in qdrant.collections
    space = _run(tree, qdrant, "--confirm", L, QDRANT_RELEASE=release, STS_REPLICAS="3", DF_FAIL="1")
    assert f"cannot read the volume usage of {name}-0" in space.stderr
