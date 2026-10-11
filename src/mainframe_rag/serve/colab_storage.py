"""Read-only proof of the retained Kind Qdrant producer used by Colab tooling.

This supports the existing chart launch and local-path filesystem volumes. It
does not qualify production block storage. Injected reads must be bounded; live
config reads must never log raw content (local.yaml contains credentials).
The caller pins the returned pod for forwarding, rechecks its UID/container ID,
and treats replacement or forward loss as refusal, never a new pod selection.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import yaml

Object = dict[str, Any]
ObjectReader = Callable[[str], Object]
ConfigReader = Callable[[str, str, str], str]


class StorageProofError(ValueError):
    """Fixed diagnostics only; no Kubernetes/config/credential text."""


def _identity(obj: Object, name: str, namespace: str | None = None) -> Object:
    metadata = obj["metadata"]
    if (metadata["name"] != name or not metadata.get("uid")
            or metadata.get("deletionTimestamp")
            or namespace is not None and metadata.get("namespace") != namespace):
        raise StorageProofError("Qdrant storage object identity is incomplete")
    return metadata


def _config(text: str) -> Object:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 64 * 1024:
        raise StorageProofError("unsupported Qdrant launch or config")
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError:
        raise StorageProofError("unsupported Qdrant launch or config") from None
    if (not isinstance(value, dict)
            or any(not isinstance(key, str) or "." in key for key in value)):
        raise StorageProofError("unsupported Qdrant launch or config")
    return value


def _effective_config(pod: Object, container: Object, read_file: ConfigReader) -> None:
    """Inspect live files, including the image base config, in chart load order.

    ConfigMap/Secret API bytes are insufficient: these chart mounts use subPath
    and retain their original inode after the API object changes. Accept only
    the chart's exact initializer statements, never an arbitrary shell program.
    """
    if (container.get("command") != ["/bin/bash", "-c"]
            or container.get("args") != ["./config/initialize.sh"]
            or container.get("workingDir", "/qdrant") != "/qdrant"
            or container.get("envFrom") or container.get("lifecycle")
            or container.get("securityContext", {}).get("readOnlyRootFilesystem") is not True):
        raise StorageProofError("unsupported Qdrant launch or config")
    for variable in container.get("env", []):
        if variable != {
                "name": "QDRANT_INIT_FILE_PATH", "value": "/qdrant/init/.qdrant-initialized"}:
            raise StorageProofError("unsupported Qdrant launch or config")

    volumes = {volume["name"]: volume for volume in pod["spec"]["volumes"]}
    config_mounts = {}
    for mount in container.get("volumeMounts", []):
        path = mount["mountPath"]
        if path in {"/", "/bin", "/bin/bash", "/qdrant/entrypoint.sh", "/qdrant/qdrant"}:
            raise StorageProofError("unsupported Qdrant launch or config")
        if path == "/qdrant" or path == "/qdrant/config" or path.startswith("/qdrant/config/"):
            filename = path.removeprefix("/qdrant/config/")
            if filename not in {"initialize.sh", "production.yaml", "local.yaml"}:
                raise StorageProofError("unsupported Qdrant launch or config")
            volume = volumes[mount["name"]]
            if (filename in config_mounts or mount.get("subPath") != filename
                    or mount.get("subPathExpr")
                    or not ("configMap" in volume or "secret" in volume)):
                raise StorageProofError("unsupported Qdrant launch or config")
            config_mounts[filename] = mount
    # Authenticated retained Kind uses the chart's Secret-backed local.yaml.
    # Unknown image-local config and alternate launch modes are unsupported.
    if set(config_mounts) != {"initialize.sh", "production.yaml", "local.yaml"}:
        raise StorageProofError("unsupported Qdrant launch or config")

    pod_name, container_name = pod["metadata"]["name"], container["name"]
    stem, separator, index = pod_name.rpartition("-")
    if not separator or not index.isdecimal():
        raise StorageProofError("unsupported Qdrant launch or config")
    uri = f"http://{stem}-0.{stem}-headless:6335"
    expected = [
        "#!/bin/sh", 'echo "Soft limits"', "ulimit -a -S", 'echo "Hard limits"',
        "ulimit -a -H", "ulimit -n $(ulimit -Hn)", "SET_INDEX=${HOSTNAME##*-}",
        'echo "Starting initializing for pod $SET_INDEX"',
        'if [ "$SET_INDEX" = "0" ]; then', f"exec ./entrypoint.sh --uri '{uri}'", "else",
        f"exec ./entrypoint.sh --bootstrap '{uri}' --uri "
        f"'http://{stem}-'\"$SET_INDEX\"'.{stem}-headless:6335'", "fi",
    ]
    initializer = read_file(pod_name, container_name, "/qdrant/config/initialize.sh")
    if (not isinstance(initializer, str) or len(initializer.encode("utf-8")) > 64 * 1024
            or [line.strip() for line in initializer.splitlines() if line.strip()] != expected):
        raise StorageProofError("unsupported Qdrant launch or config")

    storage_path, http_port, tls = "./storage", 6333, False
    for filename in ("config.yaml", "production.yaml", "local.yaml"):
        config = _config(read_file(pod_name, container_name, f"/qdrant/config/{filename}"))
        storage, service = config.get("storage", {}), config.get("service", {})
        if (not isinstance(storage, dict) or not isinstance(service, dict)
                or any(not isinstance(key, str) or "." in key for key in (*storage, *service))):
            raise StorageProofError("unsupported Qdrant launch or config")
        storage_path = storage.get("storage_path", storage_path)
        http_port = service.get("http_port", http_port)
        tls = service.get("enable_tls", tls)
    if storage_path not in ("./storage", "/qdrant/storage"):
        raise StorageProofError("Qdrant effective data path must be /qdrant/storage")
    if type(http_port) is not int or http_port != 6333 or tls is not False:
        raise StorageProofError("unsupported Qdrant launch or config")


def prove_qdrant_storage(
    service: Object, pods: list[Object], *, read_pvc: ObjectReader,
    read_pv: ObjectReader, read_storage_class: ObjectReader, read_node: ObjectReader,
    read_container_file: ConfigReader,
) -> Object:
    """Return exact producer identities or refuse unproven/unsupported storage.

    Reads do not execute source or shell: the adapter reads fixed config paths
    from the exact pod/container. Only currently ready selected pods participate;
    all must prove storage before deterministic selection of the forwarded pod.
    The known image/entrypoint is trusted. Read-only root/config mounts preserve
    its startup files, but administrator mutation can invalidate the observation.
    This is not a lease: the caller must recheck after forwarding and monitor the
    owned forward throughout the workload.
    """
    try:
        metadata = service["metadata"]
        namespace, service_name = metadata["namespace"], metadata["name"]
        _identity(service, service_name, namespace)
        selector = service["spec"].get("selector")
        ports = [port for port in service["spec"]["ports"]
                 if port["port"] == 6333 and port.get("protocol", "TCP") == "TCP"]
        if not selector or len(ports) != 1 or service["spec"].get("type", "ClusterIP") != "ClusterIP":
            raise StorageProofError("selected service must expose exactly one Qdrant HTTP target")
        target = ports[0].get("targetPort", 6333)
        ready = [pod for pod in pods
                 if pod["metadata"].get("namespace") == namespace
                 and all(pod["metadata"].get("labels", {}).get(key) == value
                         for key, value in selector.items())
                 and not pod["metadata"].get("deletionTimestamp")
                 and pod["status"].get("phase") == "Running"
                 and any(item.get("type") == "Ready" and item.get("status") == "True"
                         for item in pod["status"].get("conditions", []))]
        if not ready:
            raise StorageProofError("selected service has no ready Qdrant pod")
        proofs = []
        for pod in sorted(ready, key=lambda item: item["metadata"]["name"]):
            pod_meta = _identity(pod, pod["metadata"]["name"], namespace)
            targets = [(container, port) for container in pod["spec"]["containers"]
                       for port in container.get("ports", [])
                       if port.get("protocol", "TCP") == "TCP"
                       and (type(target) is int and port["containerPort"] == target
                            or type(target) is str and port.get("name") == target)]
            if len(targets) != 1 or targets[0][0]["name"] != "qdrant" or targets[0][1]["containerPort"] != 6333:
                raise StorageProofError("Qdrant serving container identity is ambiguous or not ready")
            container = targets[0][0]
            if sum(port.get("containerPort") == 6333
                   and port.get("protocol", "TCP") == "TCP"
                   for item in pod["spec"]["containers"]
                   for port in item.get("ports", [])) != 1:
                raise StorageProofError("Qdrant serving container identity is ambiguous or not ready")
            statuses = [status for status in pod["status"].get("containerStatuses", [])
                        if status.get("name") == container["name"]]
            if (len(statuses) != 1 or statuses[0].get("ready") is not True
                    or "running" not in statuses[0].get("state", {})
                    or not statuses[0].get("containerID") or not statuses[0].get("imageID")):
                raise StorageProofError("Qdrant serving container identity is ambiguous or not ready")
            _effective_config(pod, container, read_container_file)
            mounts = container.get("volumeMounts", [])
            data_mounts = [mount for mount in mounts if mount["mountPath"] == "/qdrant/storage"]
            if (len(data_mounts) != 1 or data_mounts[0].get("readOnly")
                    or data_mounts[0].get("subPath") or data_mounts[0].get("subPathExpr")
                    or any(mount["mountPath"].startswith("/qdrant/storage/") for mount in mounts)):
                raise StorageProofError("Qdrant data must be a writable PVC mount")
            volume_name = data_mounts[0]["name"]
            volumes = [volume for volume in pod["spec"]["volumes"] if volume["name"] == volume_name]
            if (len(volumes) != 1 or "persistentVolumeClaim" not in volumes[0]
                    or volumes[0]["persistentVolumeClaim"].get("readOnly")):
                raise StorageProofError("Qdrant data must be a writable PVC mount")
            claim_name = volumes[0]["persistentVolumeClaim"]["claimName"]
            pvc = read_pvc(claim_name)
            claim_meta = _identity(pvc, claim_name, namespace)
            pv_name = pvc["spec"].get("volumeName")
            if (pvc["status"].get("phase") != "Bound" or not pv_name
                    or pvc["spec"].get("accessModes") != ["ReadWriteOnce"]
                    or pvc["spec"].get("volumeMode", "Filesystem") != "Filesystem"):
                raise StorageProofError("Qdrant data PVC and PV must be mutually Bound RWO filesystem storage")
            pv = read_pv(pv_name)
            pv_meta = _identity(pv, pv_name)
            binding = pv["spec"].get("claimRef", {})
            if (pv["status"].get("phase") != "Bound"
                    or pv["spec"].get("accessModes") != ["ReadWriteOnce"]
                    or pv["spec"].get("volumeMode", "Filesystem") != "Filesystem"
                    or any(binding.get(key) != value for key, value in {
                        "namespace": namespace, "name": claim_name, "uid": claim_meta["uid"]}.items())):
                raise StorageProofError("Qdrant data PVC and PV must be mutually Bound RWO filesystem storage")
            class_name = pvc["spec"].get("storageClassName")
            if not class_name or pv["spec"].get("storageClassName") != class_name:
                raise StorageProofError("unsupported Qdrant storage; retained Kind local-path is required")
            storage_class = read_storage_class(class_name)
            class_meta = _identity(storage_class, class_name)
            source_keys = set(pv["spec"]) - {
                "capacity", "accessModes", "claimRef", "persistentVolumeReclaimPolicy",
                "storageClassName", "mountOptions", "volumeMode", "nodeAffinity",
            }
            if (storage_class.get("provisioner") != "rancher.io/local-path"
                    or source_keys not in ({"local"}, {"hostPath"})
                    or not pv["spec"][next(iter(source_keys))].get("path", "").startswith("/")):
                raise StorageProofError("unsupported Qdrant storage; retained Kind local-path is required")
            node_name = pod["spec"]["nodeName"]
            node = read_node(node_name)
            node_meta = _identity(node, node_name)
            hostname = node_meta.get("labels", {}).get("kubernetes.io/hostname")
            affinity = pv["spec"].get("nodeAffinity", {}).get("required", {})
            if not hostname or affinity != {"nodeSelectorTerms": [{"matchExpressions": [{
                    "key": "kubernetes.io/hostname", "operator": "In", "values": [hostname]}]}]}:
                raise StorageProofError("Qdrant local-path volume does not match its scheduled node")
            proofs.append({
                "namespace": namespace, "service_name": service_name, "service_uid": metadata["uid"],
                "pod_name": pod_meta["name"], "pod_uid": pod_meta["uid"],
                "container_name": container["name"], "container_id": statuses[0]["containerID"],
                "image_id": statuses[0]["imageID"], "target_port": 6333,
                "storage_path": "/qdrant/storage", "volume_name": volume_name,
                "claim_name": claim_name, "claim_uid": claim_meta["uid"],
                "pv_name": pv_name, "pv_uid": pv_meta["uid"],
                "storage_class": class_name, "storage_class_uid": class_meta["uid"],
                "provisioner": "rancher.io/local-path", "node_name": node_name,
                "node_uid": node_meta["uid"], "node_hostname": hostname,
            })
        return proofs[0]
    except (KeyError, IndexError, TypeError, AttributeError):
        raise StorageProofError("Qdrant storage proof is incomplete") from None
