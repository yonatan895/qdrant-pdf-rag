"""Retained Kind storage proof at API/file boundaries; no cluster or daemon."""
from __future__ import annotations

import copy
import json

import pytest

from mainframe_rag.serve.colab_storage import StorageProofError, prove_qdrant_storage


@pytest.fixture
def retained():
    # These are the existing chart's generated launcher statements; no arbitrary
    # shell execution is hidden behind the read_container_file boundary.
    initializer = """#!/bin/sh
echo "Soft limits"
ulimit -a -S
echo "Hard limits"
ulimit -a -H
ulimit -n $(ulimit -Hn)
SET_INDEX=${HOSTNAME##*-}
echo "Starting initializing for pod $SET_INDEX"
if [ "$SET_INDEX" = "0" ]; then
  exec ./entrypoint.sh --uri 'http://qdrant-0.qdrant-headless:6335'
else
  exec ./entrypoint.sh --bootstrap 'http://qdrant-0.qdrant-headless:6335' --uri 'http://qdrant-'"$SET_INDEX"'.qdrant-headless:6335'
fi
"""
    service = {
        "metadata": {"name": "qdrant", "namespace": "retained", "uid": "service-original"},
        "spec": {"type": "ClusterIP", "selector": {"app": "qdrant"},
                 "ports": [{"port": 6333, "targetPort": "http", "protocol": "TCP"}]},
    }
    pod = {
        "metadata": {"name": "qdrant-0", "namespace": "retained", "uid": "pod-original",
                     "labels": {"app": "qdrant"}},
        "spec": {
            "nodeName": "kind-worker",
            "containers": [{
                "name": "qdrant", "image": "qdrant/qdrant:v1.19.0-unprivileged",
                "command": ["/bin/bash", "-c"], "args": ["./config/initialize.sh"],
                "securityContext": {"readOnlyRootFilesystem": True},
                "env": [{"name": "QDRANT_INIT_FILE_PATH", "value": "/qdrant/init/.qdrant-initialized"}],
                "ports": [{"name": "http", "containerPort": 6333}],
                "volumeMounts": [
                    {"name": "data", "mountPath": "/qdrant/storage"},
                    {"name": "config", "mountPath": "/qdrant/config/initialize.sh", "subPath": "initialize.sh"},
                    {"name": "config", "mountPath": "/qdrant/config/production.yaml", "subPath": "production.yaml"},
                    {"name": "auth", "mountPath": "/qdrant/config/local.yaml", "subPath": "local.yaml"},
                ],
            }],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data-qdrant-0"}},
                        {"name": "config", "configMap": {"name": "qdrant"}},
                        {"name": "auth", "secret": {"secretName": "qdrant-apikey"}}],
        },
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                   "containerStatuses": [{"name": "qdrant", "ready": True, "state": {"running": {}},
                                          "containerID": "containerd://original", "imageID": "sha256:qdrant"}]},
    }
    pvc = {
        "metadata": {"name": "data-qdrant-0", "namespace": "retained", "uid": "claim-original"},
        "spec": {"volumeName": "pv-original", "accessModes": ["ReadWriteOnce"],
                 "storageClassName": "standard", "volumeMode": "Filesystem"},
        "status": {"phase": "Bound"},
    }
    pv = {
        "metadata": {"name": "pv-original", "uid": "pv-original-uid"},
        "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "standard",
                 "volumeMode": "Filesystem", "hostPath": {"path": "/var/local-path/data"},
                 "claimRef": {"name": "data-qdrant-0", "namespace": "retained", "uid": "claim-original"},
                 "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [{
                     "key": "kubernetes.io/hostname", "operator": "In", "values": ["actual-node-hostname"]}]}]}}},
        "status": {"phase": "Bound"},
    }
    storage_class = {"metadata": {"name": "standard", "uid": "class-original"},
                     "provisioner": "rancher.io/local-path"}
    node = {"metadata": {"name": "kind-worker", "uid": "node-original",
                         "labels": {"kubernetes.io/hostname": "actual-node-hostname"}}}
    files = {
        "/qdrant/config/initialize.sh": initializer,
        "/qdrant/config/config.yaml": "storage:\n  storage_path: ./storage\nservice:\n  http_port: 6333\n  enable_tls: false\n",
        "/qdrant/config/production.yaml": "cluster:\n  enabled: true\nservice:\n  enable_tls: false\n",
        "/qdrant/config/local.yaml": "service:\n  api_key: ORIGINAL-FAKE-WRITER\n  read_only_api_key: ORIGINAL-FAKE-READER\n",
    }
    return {"service": service, "pod": pod, "pvc": pvc, "pv": pv,
            "class": storage_class, "node": node, "files": files, "file_reads": []}


def prove(retained, *, extra_pods=()):
    def read_file(pod_name, container_name, path):
        retained["file_reads"].append((pod_name, container_name, path))
        return retained["files"][path]

    return prove_qdrant_storage(
        retained["service"], [retained["pod"], *extra_pods],
        read_pvc={"data-qdrant-0": retained["pvc"]}.__getitem__,
        read_pv={"pv-original": retained["pv"]}.__getitem__,
        read_storage_class={"standard": retained["class"]}.__getitem__,
        read_node={"kind-worker": retained["node"]}.__getitem__, read_container_file=read_file,
    )


def test_chart_auth_and_local_path_prove_exact_forward_producer_without_credentials(retained):
    assert prove(retained) == {
        "namespace": "retained", "service_name": "qdrant", "service_uid": "service-original",
        "pod_name": "qdrant-0", "pod_uid": "pod-original", "container_name": "qdrant",
        "container_id": "containerd://original", "image_id": "sha256:qdrant", "target_port": 6333,
        "storage_path": "/qdrant/storage", "volume_name": "data",
        "claim_name": "data-qdrant-0", "claim_uid": "claim-original",
        "pv_name": "pv-original", "pv_uid": "pv-original-uid",
        "storage_class": "standard", "storage_class_uid": "class-original",
        "provisioner": "rancher.io/local-path", "node_name": "kind-worker",
        "node_uid": "node-original", "node_hostname": "actual-node-hostname",
    }
    serialized = json.dumps(prove(retained))
    assert "ORIGINAL-FAKE" not in serialized
    assert set(retained["file_reads"]) == {
        ("qdrant-0", "qdrant", "/qdrant/config/initialize.sh"),
        ("qdrant-0", "qdrant", "/qdrant/config/config.yaml"),
        ("qdrant-0", "qdrant", "/qdrant/config/production.yaml"),
        ("qdrant-0", "qdrant", "/qdrant/config/local.yaml"),
    }


def test_sidecar_pvc_cannot_hide_ephemeral_serving_container_then_valid_proof_succeeds(retained):
    pod = retained["pod"]
    pod["spec"]["containers"].append({"name": "sidecar", "volumeMounts": [
        {"name": "data", "mountPath": "/qdrant/storage"}]})
    pod["spec"]["volumes"].append({"name": "ephemeral", "emptyDir": {}})
    pod["spec"]["containers"][0]["volumeMounts"][0]["name"] = "ephemeral"
    with pytest.raises(StorageProofError, match="writable PVC"):
        prove(retained)
    pod["spec"]["containers"][0]["volumeMounts"][0]["name"] = "data"
    assert prove(retained)["claim_uid"] == "claim-original"


@pytest.mark.parametrize("target", ["sidecar-http", 8080])
def test_service_http_port_cannot_mask_a_different_target_container(retained, target):
    retained["service"]["spec"]["ports"][0]["targetPort"] = target
    retained["pod"]["spec"]["containers"].append({"name": "sidecar", "ports": [
        {"name": "sidecar-http", "containerPort": 8080}]})
    with pytest.raises(StorageProofError, match="container identity"):
        prove(retained)


def test_named_service_target_also_refuses_duplicate_numeric_listener_ownership(retained):
    retained["pod"]["spec"]["containers"].append({"name": "sidecar", "ports": [
        {"name": "other", "containerPort": 6333}]})
    with pytest.raises(StorageProofError, match="container identity"):
        prove(retained)


def test_pod_ready_does_not_replace_actual_qdrant_container_readiness(retained):
    retained["pod"]["status"]["containerStatuses"][0]["ready"] = False
    with pytest.raises(StorageProofError, match="container identity"):
        prove(retained)


@pytest.mark.parametrize("filename", ["config.yaml", "production.yaml", "local.yaml"])
def test_effective_config_redirect_to_emptydir_refuses_nominal_pvc(retained, filename):
    retained["pod"]["spec"]["volumes"].append({"name": "other", "emptyDir": {}})
    retained["pod"]["spec"]["containers"][0]["volumeMounts"].append({
        "name": "other", "mountPath": "/data"})
    retained["files"][f"/qdrant/config/{filename}"] = "storage:\n  storage_path: /data\n"
    with pytest.raises(StorageProofError, match="effective data path"):
        prove(retained)


def test_subpath_pod_mounted_bytes_override_newer_clean_api_config(retained):
    # An API-object update cannot change these mounted subPath bytes. The file
    # boundary is the oracle even when the newer API object would look safe.
    retained["current_configmap"] = {"data": {"production.yaml": "storage:\n  storage_path: /qdrant/storage\n"}}
    retained["files"]["/qdrant/config/production.yaml"] = "storage:\n  storage_path: /ephemeral\n"
    with pytest.raises(StorageProofError, match="effective data path"):
        prove(retained)


@pytest.mark.parametrize("variable", [
    {"name": "QDRANT__STORAGE__STORAGE_PATH", "value": "/data"},
    {"name": "RUN_MODE", "value": "custom"},
    {"name": "BASH_ENV", "value": "/data/override.sh"},
])
def test_environment_cannot_redirect_config_or_chart_launch(retained, variable):
    retained["pod"]["spec"]["containers"][0]["env"].append(variable)
    with pytest.raises(StorageProofError, match="unsupported Qdrant launch"):
        prove(retained)


def test_writable_image_config_cannot_establish_process_start_files(retained):
    retained["pod"]["spec"]["containers"][0]["securityContext"]["readOnlyRootFilesystem"] = False
    with pytest.raises(StorageProofError, match="unsupported Qdrant launch"):
        prove(retained)


@pytest.mark.parametrize("config", [
    "storage.storage_path: /ephemeral\n",
    "storage:\n  storage_path: /qdrant/storage\n  nested.path: /ephemeral\n",
    "service: [ORIGINAL-FAKE-WRITER]\n",
    "service: [ORIGINAL-FAKE-WRITER\n",
])
def test_unknown_or_malformed_config_refuses_with_fixed_diagnostic(retained, config):
    retained["files"]["/qdrant/config/local.yaml"] = config
    with pytest.raises(StorageProofError, match="^unsupported Qdrant launch or config$"):
        prove(retained)


@pytest.mark.parametrize("change", ["envFrom", "command", "initializer", "unknown-source"])
def test_unproven_launch_and_config_sources_refuse(retained, change):
    container = retained["pod"]["spec"]["containers"][0]
    if change == "envFrom":
        container["envFrom"] = [{"configMapRef": {"name": "unknown"}}]
    elif change == "command":
        container["args"] = ["./qdrant --config-path /data/custom.yaml"]
    elif change == "initializer":
        retained["files"]["/qdrant/config/initialize.sh"] += "exec ./qdrant --config-path /data/custom.yaml\n"
    else:
        retained["pod"]["spec"]["volumes"][1] = {"name": "config", "emptyDir": {}}
    with pytest.raises(StorageProofError, match="unsupported Qdrant launch"):
        prove(retained)


@pytest.mark.parametrize("object_name,field,value", [
    ("pvc", "uid", "new-claim"), ("pvc", "namespace", "other"),
    ("pv", "uid", "older-claim"), ("pv", "name", "other-claim"),
    ("pv", "namespace", "other"),
])
def test_exact_pv_claim_binding_rejects_name_reuse_and_other_namespace(retained, object_name, field, value):
    destination = retained["pvc"]["metadata"] if object_name == "pvc" else retained["pv"]["spec"]["claimRef"]
    destination[field] = value
    with pytest.raises(StorageProofError):
        prove(retained)


@pytest.mark.parametrize("object_name", ["pvc", "pv"])
@pytest.mark.parametrize("field,value", [
    ("accessModes", ["ReadWriteMany"]), ("volumeMode", "Block"), ("phase", "Pending"),
])
def test_bound_rwo_filesystem_is_required_on_both_sides(retained, object_name, field, value):
    retained[object_name]["status" if field == "phase" else "spec"][field] = value
    with pytest.raises(StorageProofError, match="mutually Bound RWO filesystem"):
        prove(retained)


def test_rwo_filesystem_nfs_is_not_local_path_storage(retained):
    del retained["pv"]["spec"]["hostPath"]
    retained["pv"]["spec"]["nfs"] = {"server": "nfs.example.invalid", "path": "/data"}
    with pytest.raises(StorageProofError, match="retained Kind local-path"):
        prove(retained)


def test_arbitrary_class_name_is_not_evidence_of_backend(retained):
    retained["class"]["provisioner"] = "example.invalid/unknown"
    with pytest.raises(StorageProofError, match="retained Kind local-path"):
        prove(retained)


def test_local_path_affinity_uses_actual_node_hostname_not_assumed_node_name(retained):
    retained["pv"]["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"] = ["kind-worker"]
    with pytest.raises(StorageProofError, match="scheduled node"):
        prove(retained)


@pytest.mark.parametrize("change", ["readonly", "subpath", "nested-ephemeral"])
def test_writable_complete_data_directory_cannot_be_masked(retained, change):
    mounts = retained["pod"]["spec"]["containers"][0]["volumeMounts"]
    if change == "readonly":
        mounts[0]["readOnly"] = True
    elif change == "subpath":
        mounts[0]["subPath"] = "unrelated-generation"
    else:
        mounts.append({"name": "nested", "mountPath": "/qdrant/storage/collections"})
        retained["pod"]["spec"]["volumes"].append({"name": "nested", "emptyDir": {}})
    with pytest.raises(StorageProofError, match="writable PVC"):
        prove(retained)


def test_no_ready_selected_pod_refuses_unrelated_ready_pod(retained):
    retained["pod"]["metadata"]["labels"] = {"app": "unrelated"}
    with pytest.raises(StorageProofError, match="no ready Qdrant pod"):
        prove(retained)


def test_another_ready_selected_ephemeral_pod_cannot_hide_behind_valid_first_peer(retained):
    second = copy.deepcopy(retained["pod"])
    second["metadata"].update(name="qdrant-1", uid="pod-second")
    second["spec"]["volumes"][0] = {"name": "data", "emptyDir": {}}
    with pytest.raises(StorageProofError, match="writable PVC"):
        prove(retained, extra_pods=[second])


def test_next_observation_returns_replacement_pod_uid_for_caller_to_reject(retained):
    first = prove(retained)
    retained["pod"]["metadata"]["uid"] = "pod-replacement"
    retained["pod"]["status"]["containerStatuses"][0]["containerID"] = "containerd://replacement"
    following = prove(retained)
    assert (first["pod_uid"], first["container_id"]) == ("pod-original", "containerd://original")
    assert (following["pod_uid"], following["container_id"]) == ("pod-replacement", "containerd://replacement")
