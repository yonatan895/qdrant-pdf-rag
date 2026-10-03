"""scripts/airgap/check_pod_images.py: running imageID vs verified digests (#272)."""

import importlib.util
import json
import subprocess
import sys

from tests.helpers_airgap import REPO

SCRIPT = REPO / "scripts" / "airgap" / "check_pod_images.py"
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
REPO_Q = "reg.internal:5000/qdrant/qdrant"

_spec = importlib.util.spec_from_file_location("check_pod_images", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def pod(name, image, image_id, deleting=False):
    meta = {"name": name}
    if deleting:
        meta["deletionTimestamp"] = "2026-10-03T00:00:00Z"
    return {
        "metadata": meta,
        "spec": {"containers": [{"name": "c", "image": image}]},
        "status": {"containerStatuses": [{"name": "c", "imageID": image_id}]},
    }


def test_repository_strips_tag_and_digest_but_keeps_registry_port():
    assert mod.repository(f"{REPO_Q}:v1-unprivileged") == REPO_Q
    assert mod.repository(f"{REPO_Q}@{DIGEST}") == REPO_Q
    assert mod.repository(REPO_Q) == REPO_Q


def test_imageid_runtime_prefixes_are_accepted():
    for image_id in (f"{REPO_Q}@{DIGEST}", f"docker-pullable://{REPO_Q}@{DIGEST}"):
        assert mod.check({"items": [pod("q-0", f"{REPO_Q}:v1", image_id)]}, {REPO_Q: DIGEST}) == []


def test_different_or_missing_digest_is_reported():
    problems = mod.check({"items": [pod("q-0", f"{REPO_Q}:v1", f"{REPO_Q}@{OTHER}")]}, {REPO_Q: DIGEST})
    assert len(problems) == 1 and OTHER in problems[0] and "q-0" in problems[0]
    assert mod.check({"items": [pod("q-0", f"{REPO_Q}:v1", "")]}, {REPO_Q: DIGEST})


def test_terminating_and_foreign_pods_are_ignored_but_a_match_is_required():
    items = [
        pod("old", f"{REPO_Q}:v1", f"{REPO_Q}@{OTHER}", deleting=True),
        pod("other", "reg.internal:5000/other:1", "reg.internal:5000/other@" + OTHER),
    ]
    assert mod.check({"items": items}, {REPO_Q: DIGEST}) == [f"no running container found for {REPO_Q}"]


def test_cli_exit_codes_and_fixed_output():
    ok = json.dumps({"items": [pod("q-0", f"{REPO_Q}:v1", f"{REPO_Q}@{DIGEST}")]})
    run = lambda stdin, *args: subprocess.run(
        [sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True, check=False
    )
    assert run(ok, f"{REPO_Q}={DIGEST}").returncode == 0
    assert run(ok, f"{REPO_Q}={OTHER}").returncode == 1
    assert run("not json", f"{REPO_Q}={DIGEST}").returncode == 1
    assert run(ok, "bad-argument").returncode == 2
