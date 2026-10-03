"""Current controller/replica imageID checks for release workloads (#272)."""

import copy
import importlib.util
import json
import subprocess
import sys

import pytest

from tests.helpers_airgap import REPO

SCRIPT = REPO / "scripts" / "airgap" / "check_pod_images.py"
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
REPO_Q = "reg.internal:5000/qdrant/qdrant"
REVISION = "deployment.kubernetes.io/revision"

_spec = importlib.util.spec_from_file_location("check_pod_images", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def owner(kind, uid):
    return [{"kind": kind, "uid": uid, "controller": True}]


def rollout(kind="StatefulSet", replicas=1, name="qdrant", container="qdrant"):
    template = {"containers": [{"name": container, "image": f"{REPO_Q}:v1"}]}
    uid = "workload-uid"
    workload = {
        "kind": kind, "metadata": {"name": name, "namespace": "ns", "uid": uid,
                                    "annotations": {REVISION: "2"}},
        "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                 "template": {"spec": copy.deepcopy(template)}},
        "status": {"updateRevision": "qdrant-revision-2"},
    }
    items = [workload]
    pod_kind = kind
    if kind == "Deployment":
        pod_kind, uid = "ReplicaSet", "rs-current"
        items.append({"kind": "ReplicaSet", "metadata": {"name": "rs-current", "namespace": "ns", "uid": uid,
                      "ownerReferences": owner(kind, "workload-uid"), "annotations": {REVISION: "2"}}})
    for n in range(replicas):
        items.append({
            "kind": "Pod", "metadata": {"name": f"{name}-{n}", "namespace": "ns",
                "ownerReferences": owner(pod_kind, uid),
                "labels": {"app": name, "controller-revision-hash": "qdrant-revision-2"}},
            "spec": copy.deepcopy(template), "status": {"phase": "Running", "containerStatuses": [
                {"name": container, "imageID": f"{REPO_Q}@{DIGEST}"}]},
        })
    return {"items": items}


def check(document, role="qdrant", digest=DIGEST):
    return mod.check(document, {role: (REPO_Q, digest)}, "ns", "qdrant")


def diagnostic(phase="Succeeded"):
    pod = rollout()["items"][-1]
    pod["metadata"].update(name="retained-diagnostic", ownerReferences=owner("Job", "diagnostic-job"))
    pod["status"].update(phase=phase)
    pod["status"]["containerStatuses"][0]["imageID"] = f"{REPO_Q}@{OTHER}"
    return pod


def test_repository_keeps_registry_port():
    assert mod.repository(f"{REPO_Q}:v1-unprivileged") == REPO_Q
    assert mod.repository(f"{REPO_Q}@{DIGEST}") == REPO_Q
    assert mod.repository(REPO_Q) == REPO_Q


@pytest.mark.parametrize("prefix", ["", "docker-pullable://"])
def test_runtime_prefixes_and_every_replica(prefix):
    document = rollout(replicas=3)
    for pod in document["items"][1:]:
        pod["status"]["containerStatuses"][0]["imageID"] = f"{prefix}{REPO_Q}@{DIGEST}"
    assert check(document) == []


@pytest.mark.parametrize("image_id", [OTHER, ""])
def test_different_or_missing_digest_is_reported(image_id):
    document = rollout()
    document["items"][-1]["status"]["containerStatuses"][0]["imageID"] = f"{REPO_Q}@{image_id}"
    problems = check(document)
    assert len(problems) == 1 and "qdrant-0" in problems[0]


@pytest.mark.parametrize("phase", ["Succeeded", "Failed", "Running"])
def test_unrelated_diagnostic_jobs_do_not_invalidate_rollout(phase):
    document = rollout()
    document["items"].append(diagnostic(phase))
    assert check(document) == []


def test_wrong_target_repository_cannot_hide_behind_expected_diagnostic():
    document = rollout("Deployment", name="rag-agent", container="agent")
    document["items"][-1]["spec"]["containers"][0]["image"] = "reg.internal/wrong:tag"
    extra = diagnostic("Running")
    extra["status"]["containerStatuses"][0]["imageID"] = f"{REPO_Q}@{DIGEST}"
    document["items"].append(extra)
    assert "unexpected repository" in " ".join(check(document, "agent"))


def test_wrong_workload_template_repository_is_rejected():
    document = rollout()
    document["items"][0]["spec"]["template"]["spec"]["containers"][0]["image"] = "reg.internal/wrong:tag"
    assert "unexpected repository" in " ".join(check(document))


def test_required_replica_without_status_fails_despite_healthy_replica():
    document = rollout("Deployment", replicas=2, name="rag-agent", container="agent")
    document["items"][-1]["status"]["containerStatuses"] = []
    assert "rag-agent-1 container agent runs an unknown digest" in " ".join(check(document, "agent"))


def test_missing_replica_cannot_be_supplied_by_unrelated_pod():
    document = rollout(replicas=3)
    document["items"].pop()
    document["items"].append(diagnostic("Running"))
    assert "has 2 current pods, expected 3" in " ".join(check(document))


def test_terminating_old_replica_does_not_invalidate_current_rollout():
    document = rollout()
    old = copy.deepcopy(document["items"][-1])
    old["metadata"].update(name="old-replica", deletionTimestamp="2026-10-03T00:00:00Z")
    old["status"]["containerStatuses"][0]["imageID"] = f"{REPO_Q}@{OTHER}"
    document["items"].append(old)
    assert check(document) == []


def test_old_deployment_replicaset_does_not_supply_current_pods():
    document = rollout("Deployment", name="rag-agent", container="agent")
    old_rs = copy.deepcopy(document["items"][1])
    old_rs["metadata"].update(uid="rs-old", name="rs-old", annotations={REVISION: "1"})
    old_pod = copy.deepcopy(document["items"][-1])
    old_pod["metadata"].update(name="old-agent", ownerReferences=owner("ReplicaSet", "rs-old"))
    old_pod["status"]["containerStatuses"][0]["imageID"] = f"{REPO_Q}@{OTHER}"
    document["items"] += [old_rs, old_pod]
    assert check(document, "agent") == []
    document["items"].pop(2)
    assert "has 0 current pods, expected 1" in " ".join(check(document, "agent"))


@pytest.mark.parametrize("drift", ["revision", "selector", "owner", "phase"])
def test_current_workload_selection_is_required(drift):
    document = rollout()
    pod = document["items"][-1]
    if drift == "revision":
        pod["metadata"]["labels"]["controller-revision-hash"] = "old"
    elif drift == "selector":
        pod["metadata"]["labels"]["app"] = "other"
    elif drift == "owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = "other"
    else:
        pod["status"]["phase"] = "Succeeded"
    assert check(document)


def test_completed_init_container_still_requires_verified_image():
    document = rollout()
    init = {"name": "ensure-dir-ownership", "image": f"{REPO_Q}:v1"}
    document["items"][0]["spec"]["template"]["spec"]["initContainers"] = [init]
    pod = document["items"][-1]
    pod["spec"]["initContainers"] = [init]
    pod["status"]["initContainerStatuses"] = [
        {"name": "ensure-dir-ownership", "imageID": f"{REPO_Q}@{DIGEST}",
         "state": {"terminated": {"exitCode": 0}}}]
    assert check(document) == []
    pod["status"]["initContainerStatuses"][0]["imageID"] = f"{REPO_Q}@{OTHER}"
    assert "ensure-dir-ownership" in " ".join(check(document))


def test_cli_exit_codes_and_fixed_refusals():
    run = lambda stdin, *args: subprocess.run(
        [sys.executable, str(SCRIPT), "ns", "qdrant", *args], input=stdin,
        capture_output=True, text=True, check=False)
    ok = json.dumps(rollout())
    assert run(ok, f"qdrant={REPO_Q}@{DIGEST}").returncode == 0
    assert run(ok, f"qdrant={REPO_Q}@{OTHER}").returncode == 1
    assert run("not json", f"qdrant={REPO_Q}@{DIGEST}").returncode == 1
    assert run("null", f"qdrant={REPO_Q}@{DIGEST}").returncode == 1
    assert run(ok, "bad-argument").returncode == 2


def test_service_container_cannot_be_replaced_by_same_named_init_container():
    document = rollout()
    pod = document["items"][-1]
    pod["spec"]["initContainers"] = pod["spec"].pop("containers")
    pod["status"]["initContainerStatuses"] = pod["status"].pop("containerStatuses")
    assert "qdrant runs an unknown digest" in " ".join(check(document))


def test_oauth_proxy_requires_its_own_container_status():
    document = rollout("Deployment", name="rag-agent", container="agent")
    oauth_repo = "reg.internal/openshift4/ose-oauth-proxy"
    oauth = {"name": "oauth-proxy", "image": f"{oauth_repo}:v4.14"}
    document["items"][0]["spec"]["template"]["spec"]["containers"].append(oauth)
    pod = document["items"][-1]
    pod["spec"]["containers"].append(oauth)
    expected = {"agent": (REPO_Q, DIGEST), "oauth_proxy": (oauth_repo, OTHER)}
    assert "oauth-proxy runs an unknown digest" in " ".join(mod.check(document, expected, "ns", "qdrant"))
    pod["status"]["containerStatuses"].append({"name": "oauth-proxy", "imageID": f"{oauth_repo}@{OTHER}"})
    assert mod.check(document, expected, "ns", "qdrant") == []


def test_qdrant_init_role_does_not_disappear_when_template_repository_changes():
    document = rollout()
    init = {"name": "ensure-dir-ownership", "image": "reg.internal/other:tag"}
    document["items"][0]["spec"]["template"]["spec"]["initContainers"] = [init]
    pod = document["items"][-1]
    pod["spec"]["initContainers"] = [init]
    pod["status"]["initContainerStatuses"] = [
        {"name": "ensure-dir-ownership", "imageID": f"reg.internal/other@{DIGEST}",
         "state": {"terminated": {"exitCode": 0}}}]
    assert "ensure-dir-ownership has an unexpected repository" in " ".join(check(document))
