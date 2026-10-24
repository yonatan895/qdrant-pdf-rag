#!/usr/bin/env python3
"""Assert the rendered Qdrant release is the private, hardened shape.

Reads `helm template` output on stdin. Whatever values files and --set flags
produced it, the release path must render: no Ingress/Route, only ClusterIP
(or headless) Services, no host networking, a chart-managed API-key Secret
carrying the writer and read-only keys, non-privileged containers with a
read-only root filesystem, and the verified registry image.
Usage: check_qdrant_render.py IMAGE_PREFIX [RELEASE] < rendered.yaml   (exit 0 = accepted).
Fixed diagnostics only; manifest content is never echoed.
"""
from __future__ import annotations

import sys

import yaml

PODS = ("StatefulSet", "Deployment", "DaemonSet", "Job", "Pod")


def _container_problems(container: dict, pod: dict) -> list[str]:
    """Allowlist: a container passes only with the full hardened shape."""
    found: list[str] = []
    context = container.get("securityContext") or {}
    pod_context = pod.get("securityContext") or {}
    caps = context.get("capabilities") or {}
    if context.get("privileged") or context.get("allowPrivilegeEscalation") is not False:
        found.append("a container is privileged or may escalate")
    if context.get("readOnlyRootFilesystem") is not True:
        found.append("a container root filesystem is writable")
    if caps.get("add") or "ALL" not in (caps.get("drop") or []):
        found.append("a container adds capabilities or does not drop ALL")
    if context.get("runAsNonRoot") is False or not (
            context.get("runAsNonRoot") is True or pod_context.get("runAsNonRoot") is True):
        found.append("a container does not require runAsNonRoot")
    if context.get("runAsUser") == 0 or pod_context.get("runAsUser") == 0:
        found.append("a container runs as UID 0")
    if any(port.get("hostPort") for port in container.get("ports") or []):
        found.append("a container publishes a hostPort")
    return found


def problems(docs: list[dict], image_prefix: str, release: str = "") -> list[str]:
    found: list[str] = []
    kinds = [d.get("kind") for d in docs]
    if any(k in ("Ingress", "Route") for k in kinds):
        found.append("an Ingress or Route is rendered")
    if any(k in ("ClusterRole", "ClusterRoleBinding", "RoleBinding", "Role") for k in kinds):
        found.append("RBAC objects are rendered")
    for doc in docs:
        if doc.get("kind") == "Service" and (doc.get("spec") or {}).get("type", "ClusterIP") != "ClusterIP":
            found.append("a Service is not ClusterIP")
    secrets = [d for d in docs if d.get("kind") == "Secret" and str(d.get("metadata", {}).get("name", "")).endswith("-apikey")]
    if not any({"api-key", "read-only-api-key"} <= set(s.get("data") or s.get("stringData") or {}) for s in secrets):
        found.append("the API-key Secret (api-key, read-only-api-key) is missing")
    own_accounts = {release, f"{release}-qdrant"} if release else None
    for doc in docs:
        if doc.get("kind") == "ServiceAccount" and own_accounts is not None \
                and (doc.get("metadata") or {}).get("name") not in own_accounts:
            found.append("a ServiceAccount is not the chart's own")
    # `helm test` hook pods are never installed by `helm upgrade -i`.
    workloads = [d for d in docs if d.get("kind") in PODS
                 and "helm.sh/hook" not in ((d.get("metadata") or {}).get("annotations") or {})]
    if not any(d.get("kind") == "StatefulSet" for d in workloads):
        found.append("the StatefulSet is missing")
    for doc in workloads:
        pod = ((doc.get("spec") or {}).get("template") or {}).get("spec") or doc.get("spec") or {}
        if pod.get("hostNetwork") or pod.get("hostPID") or pod.get("hostIPC"):
            found.append("a pod uses the host namespaces")
        if any("hostPath" in v for v in pod.get("volumes") or []):
            found.append("a pod mounts a hostPath volume")
        account = pod.get("serviceAccountName")
        if own_accounts is not None and account is not None and account not in own_accounts:
            found.append("a pod uses a ServiceAccount that is not the chart's own")
        for container in (pod.get("containers") or []) + (pod.get("initContainers") or []):
            found += _container_problems(container, pod)
            image = str(container.get("image", ""))
            if doc.get("kind") == "StatefulSet" and not image.startswith(image_prefix):
                found.append("the Qdrant image is not the verified registry image")
    return sorted(set(found))


def main(argv: list[str]) -> int:
    try:
        docs = [d for d in yaml.safe_load_all(sys.stdin) if isinstance(d, dict)]
    except yaml.YAMLError:
        print("FAIL: rendered Qdrant manifest is not valid YAML", file=sys.stderr)
        return 2
    found = problems(docs, argv[1] if len(argv) > 1 else "", argv[2] if len(argv) > 2 else "")
    for item in found:
        print(f"FAIL: rendered Qdrant release refused: {item}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
