#!/usr/bin/env python3
"""Workspace isolation proof (#1118, ADR-0009 section 2 "The isolation proof").

Runs on the bastion as the operator (namespace admin in both namespaces) and
talks to the cluster only through `oc` (or `--kubectl kubectl`). It reads the
workspace contract (deploy/workspace/contract.json) for roles, ServiceAccounts,
allowed Secrets, egress classes and policy names, and a parameter file for the
site values. Only ADR rules and chart facts are constants here: the negative
matrix, the runner rules, the agent Service and the Qdrant ports. It checks:

1. `auth can-i --list` for every contract ServiceAccount, impersonated in both
   namespaces. An F9 ServiceAccount may hold nothing beyond the fixed,
   versioned EXPECTED_BASELINE, whichever group granted it. The runner
   manager's ServiceAccount is listed, not judged.
2. A fixed negative matrix: each F9 ServiceAccount is refused secret reads, pod
   create/exec/attach/port-forward/proxy, workload writes, token requests,
   impersonation and RBAC writes in both namespaces. Asked outright, it also
   catches a grant to a ServiceAccount group. The operator's own
   `get secrets` answer is the positive control.
3. Every pod in both namespaces, classified by its labels; workspace and
   manager pods' Secret references are checked against the contract's
   mounted and image-pull sets. CI holds exactly the contract's egress
   policies; in PROD every policy selecting the bastion is a contract one.
   No egress rule selecting a workspace or manager pod may be open (no peers,
   0.0.0.0/0, ::/0, every namespace, or the pod/Service network), and the
   manager selector must match no workspace pod.
4. The rendered runner configuration, against an allow-list of keys and
   values: no `*_overwrite_(max_)allowed` key, nothing
   privileged, the contract ServiceAccount and the CI namespace, and requests
   and limits for build, helper and service containers.
5. The production agent's ingress NetworkPolicies admit nothing from the CI
   namespace (`networkPolicy.allowedNamespaces` never lists it).
6. With --probe: short-lived probe pods carrying each role's labels (and the
   runner manager's) test TCP reachability and DNS. A probe pod in the
   production namespace that no policy selects is the negative control: a
   "blocked" result counts only for a target the control reached. A probe
   never carries a label a workload controller owns, and its controller
   reference points to the run's owner ConfigMap in its namespace, so no
   ReplicaSet, StatefulSet or Job adopts (and then deletes) it or a real pod.

The record carries verbs, resources, object names and outcomes only: never a
Secret value, a token, an env value or manual text. Exit codes: 0 proven,
1 violation found, 2 usage or environment error, an inconclusive proof, or a
run without --probe (never the full proof). Run one proof at a time: probe
objects (probe pods and owner ConfigMaps) are removed on every exit, SIGTERM
and SIGHUP included; after a SIGKILL the next run removes what the killed run
left, and deleting an owner ConfigMap also garbage-collects its probes.

Standard library only (Python 3.14).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import ipaddress
import json
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXIT_PROVEN = 0
EXIT_VIOLATION = 1
EXIT_ERROR = 2

RECORD_SCHEMA = "mfr-isolation-proof/v1"
CONTRACT_SCHEMA_VERSION = 1
PROOF_ANNOTATION = "isolation-proof.mainframe-rag.io/run"
PROBE_NAME_PREFIX = "mfr-proof-"
OWNER_NAME_PREFIX = "mfr-proof-owner-"
# Label keys a workload controller owns or selects on. A probe copies a live
# pod's labels without them, so a controller's selector (a ReplicaSet's
# includes pod-template-hash) does not match the probe.
CONTROLLER_LABEL_KEYS = frozenset({
    "pod-template-hash",
    "controller-revision-hash",
    "statefulset.kubernetes.io/pod-name",
    "apps.kubernetes.io/pod-index",
    "job-name",
    "controller-uid",
})
CONTROLLER_LABEL_PREFIXES = ("batch.kubernetes.io/",)
PROBE_MARKER = "MFR-PROBE "
CALL_TIMEOUT_S = 60

# Fixed refusal messages: the record and stderr never carry exception or
# subprocess text, which could echo cluster or Secret content.
MESSAGES = {
    "usage": "usage error: see --help",
    "contract-unreadable": "the workspace contract could not be read or is malformed",
    "param-file-unreadable": "the template parameter file could not be read",
    "param-missing": "a required template parameter is missing",
    "param-invalid": "a template parameter has an invalid value",
    "kubectl-missing": "the cluster client was not found on PATH",
    "kube-call-failed": "a cluster read or write failed",
    "runner-config-unreadable": "a runner configuration file could not be read or parsed",
    "probe-failed": "a probe pod did not complete",
    "cleanup-failed": "probe objects could not be removed",
    "interrupted": "the proof was interrupted; probe objects were removed",
    "internal": "the proof stopped on an unexpected error",
    "output-unwritable": "the record could not be written",
}

# The expected `can-i --list` of an identity with no bindings of its own
# (step 1), as `verb resource` / `verb /url` strings. Sources:
# - Kubernetes 1.33 bootstrap ClusterRoles bound to system:authenticated:
#   system:basic-user (self reviews), system:discovery and
#   system:public-info-viewer (discovery and health URLs).
# - OpenShift 4.20 defaults for system:authenticated: basic-user,
#   self-access-reviewer, cluster-status, system:oauth-token-deleter,
#   system:webhook, system:scope-impersonation and
#   system:openshift:public-info-viewer; and the per-namespace
#   system:image-pullers RoleBinding for system:serviceaccounts:<ns>.
# - Kubernetes 1.33 (and OpenShift) bootstrap ClusterRole
#   system:service-account-issuer-discovery, bound to system:serviceaccounts:
#   `get` on the four OIDC issuer discovery URLs (v2; found by the Kind lane).
# A site whose defaults differ fails closed: the extra rule is reported, and
# the baseline changes only in a reviewed PR that bumps BASELINE_VERSION.
BASELINE_VERSION = "k8s-1.33+openshift-4.20/v2"
BASELINE_CONTROL_RULE = "create selfsubjectaccessreviews.authorization.k8s.io"
EXPECTED_BASELINE = frozenset({
    "create selfsubjectaccessreviews.authorization.k8s.io",
    "create selfsubjectrulesreviews.authorization.k8s.io",
    "create selfsubjectreviews.authentication.k8s.io",
    "create selfsubjectaccessreviews.authorization.openshift.io",
    "create selfsubjectrulesreviews.authorization.openshift.io",
    "get users.user.openshift.io names=~",
    "list projectrequests.project.openshift.io",
    "list projects.project.openshift.io",
    "watch projects.project.openshift.io",
    "get clusterroles.rbac.authorization.k8s.io",
    "list clusterroles.rbac.authorization.k8s.io",
    "get clusterroles.authorization.openshift.io",
    "list clusterroles.authorization.openshift.io",
    "get storageclasses.storage.k8s.io",
    "list storageclasses.storage.k8s.io",
    "get useroauthaccesstokens.oauth.openshift.io",
    "list useroauthaccesstokens.oauth.openshift.io",
    "watch useroauthaccesstokens.oauth.openshift.io",
    "delete useroauthaccesstokens.oauth.openshift.io",
    "create buildconfigs.build.openshift.io/webhooks",
    "impersonate userextras.authentication.k8s.io/scopes.authorization.openshift.io",
    "get imagestreams.image.openshift.io/layers",
    *(f"get {url}" for url in (
        "/api", "/api/*", "/apis", "/apis/*", "/healthz", "/healthz/*", "/livez",
        "/livez/*", "/readyz", "/readyz/*", "/openapi", "/openapi/*", "/version",
        "/version/", "/version/*", "/.well-known", "/.well-known/*",
        "/.well-known/oauth-authorization-server")),
    # system:service-account-issuer-discovery
    *(f"get {url}" for url in (
        "/.well-known/openid-configuration", "/.well-known/openid-configuration/",
        "/openid/v1/jwks", "/openid/v1/jwks/")),
})

# Negative matrix (step 2): (verb, resource, subresource), asked of every F9
# ServiceAccount in both namespaces; each must be refused. WebSocket exec and
# attach upgrade a GET, so `get` on the subresources is asked as well.
NEGATIVE_MATRIX: tuple[tuple[str, str, str], ...] = (
    ("get", "secrets", ""),
    ("list", "secrets", ""),
    ("watch", "secrets", ""),
    ("create", "pods", ""),
    ("create", "pods", "exec"),
    ("get", "pods", "exec"),
    ("create", "pods", "attach"),
    ("get", "pods", "attach"),
    ("create", "deployments.apps", ""),
    ("update", "deployments.apps", ""),
    ("patch", "deployments.apps", ""),
    ("create", "statefulsets.apps", ""),
    ("update", "statefulsets.apps", ""),
    ("patch", "statefulsets.apps", ""),
    ("create", "rolebindings.rbac.authorization.k8s.io", ""),
    # Every other way to run code, reach a pod or borrow an identity: a grant
    # to a ServiceAccount group lands in the baseline too, so these are asked
    # outright rather than compared with it.
    ("create", "jobs.batch", ""),
    ("create", "cronjobs.batch", ""),
    ("create", "replicasets.apps", ""),
    ("create", "daemonsets.apps", ""),
    ("create", "replicationcontrollers", ""),
    ("create", "deploymentconfigs.apps.openshift.io", ""),
    ("patch", "pods", "ephemeralcontainers"),
    ("update", "pods", "ephemeralcontainers"),
    ("create", "pods", "portforward"),
    ("get", "pods", "portforward"),
    ("get", "pods", "proxy"),
    ("create", "pods", "proxy"),
    ("get", "services", "proxy"),
    ("create", "services", "proxy"),
    ("create", "serviceaccounts", "token"),
    ("impersonate", "serviceaccounts", ""),
    ("create", "roles.rbac.authorization.k8s.io", ""),
    ("update", "roles.rbac.authorization.k8s.io", ""),
    ("update", "rolebindings.rbac.authorization.k8s.io", ""),
    ("patch", "pods", ""),
    ("update", "pods", ""),
    ("delete", "pods", ""),
    *(("create", r, "") for r in ("configmaps", "secrets")),
    *(("update", r, "") for r in ("configmaps", "secrets")),
    *(("patch", r, "") for r in ("configmaps", "secrets")),
    *((v, "networkpolicies.networking.k8s.io", "")
      for v in ("create", "update", "patch", "delete")),
    *((v, "runners.apps.gitlab.com", "")
      for v in ("get", "list", "watch", "create", "update", "patch", "delete")),
)

# Runner configuration rules (ADR-0009 section 2): no job override setting,
# nothing privileged, requests and limits for build, helper and service containers.
OVERWRITE_KEY = re.compile(r"_overwrite_(max_)?allowed$")
RUNNER_FALSE_KEYS = ("privileged", "allow_privilege_escalation",
                     "automount_service_account_token")
RUNNER_RESOURCE_KEYS = tuple(
    f"{prefix}{resource}_{bound}"
    for prefix in ("", "helper_", "service_")
    for resource in ("cpu", "memory", "ephemeral_storage")
    for bound in ("request", "limit")
)

# The runner configuration is checked against an allow-list (fail closed):
# any key not listed here, at any level that can shape the job pod, is
# refused (volumes, host_path, pod_spec patches, annotations, node
# selectors, *_overwrite_* and any key a later GitLab Runner adds).
RUNNER_TOP_KEYS = frozenset({"concurrent", "check_interval", "log_level", "log_format",
                             "shutdown_timeout", "runners"})
RUNNER_ENTRY_KEYS = frozenset({"name", "url", "token", "executor", "limit",
                               "request_concurrency", "output_limit", "kubernetes"})
RUNNER_KUBERNETES_KEYS = frozenset({
    "namespace", "service_account", "image", "helper_image", "image_pull_secrets",
    "pull_policy", "allowed_images", "allowed_services", "privileged",
    "allow_privilege_escalation", "automount_service_account_token", "cap_drop", "pod_labels",
    "pod_security_context",
    *RUNNER_RESOURCE_KEYS,
})
RUNNER_POD_SECURITY_KEYS = frozenset({"run_as_non_root"})
RUNNER_PULL_POLICIES = frozenset({"if-not-present", "always", "never"})

# GitLab Runner's Kubernetes executor names a job pod, and any per-job pull
# Secret it creates, from one prefix: runner-<token>-project-<id>-concurrent-<n>.
RUNNER_JOB_NAME = re.compile(r"^(runner-[a-z0-9]+-project-[0-9]+-concurrent-[0-9]+)")
RUNNER_JOB_SECRET = re.compile(r"runner-[a-z0-9]+-project-[0-9]+-concurrent-[0-9]+[a-z0-9-]*")

# Labels GitLab Runner's Kubernetes executor adds to every job pod besides
# `pod` and the configured pod_labels; a runner probe without a live job pod
# carries them (with placeholder values) so a policy keyed on them applies.
RUNNER_DEFAULT_LABEL_KEYS = (
    "manager.runner.gitlab.com/name", "manager.runner.gitlab.com/id-short",
    "project.runner.gitlab.com/id", "project.runner.gitlab.com/name",
    "project.runner.gitlab.com/namespace", "project.runner.gitlab.com/namespace-id",
    "project.runner.gitlab.com/root-namespace", "job.runner.gitlab.com/id",
    "job.runner.gitlab.com/name", "job.runner.gitlab.com/ref",
    "job.runner.gitlab.com/sha", "job.runner.gitlab.com/before_sha",
    "job.runner.gitlab.com/timeout",
)

# Production targets every workspace probe must be refused (the chart's
# Qdrant ports and agent HTTP port; the agent Service name is a chart fact).
QDRANT_PORTS = (6333, 6334)
AGENT_PORT = 8080
DNS_PROBE_NAME = "kubernetes.default.svc"
# `kubernetes.default.svc` is the API server path the bastion and manager use
# (ADR-0009 D-3); it is probed with the egress class of that name.
KUBE_SERVICE_CLASS = "api_server"

REACHED = frozenset({"connected", "resolved"})
BLOCKED = frozenset({"timeout", "unreachable", "unresolved"})
PROBE_STATUSES = REACHED | BLOCKED | {"refused", "dns-error", "no-address", "error"}

DIGEST_IMAGE = re.compile(r"^(?P<host>[A-Za-z0-9.-]+(?::\d+)?)/[^@\s]+@sha256:[0-9a-f]{64}$")
CANI_ROW = re.compile(
    r"^(?P<res>\S*)\s*(?P<urls>\[[^\]]*\])\s+(?P<names>\[[^\]]*\])\s+(?P<verbs>\[[^\]]*\])\s*$"
)

# Runs inside each probe pod (python3 in the devkit image). It prints one marker
# line of target ids and outcome classes; no payload is read or printed.
PROBE_PROGRAM = r"""
import errno, json, os, socket, sys
out = []
for t in json.loads(sys.argv[1]):
    host, port = t.get("host"), t.get("port")
    if t["kind"] == "kube_service":
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = int(os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
    if t["kind"] == "dns":
        try:
            socket.getaddrinfo(host, None)
            status = "resolved"
        except OSError:
            status = "unresolved"
    elif not host:
        status = "no-address"
    else:
        try:
            with socket.create_connection((host, port), timeout=t["timeout"]):
                status = "connected"
        except socket.gaierror:
            status = "dns-error"
        except ConnectionRefusedError:
            status = "refused"
        except TimeoutError:
            status = "timeout"
        except OSError as exc:
            unreachable = {errno.EHOSTUNREACH, errno.ENETUNREACH}
            status = "unreachable" if exc.errno in unreachable else "error"
    out.append({"id": t["id"], "status": status})
print("MFR-PROBE " + json.dumps(out), flush=True)
"""


class ProofError(Exception):
    """A usage or environment error with a fixed message and a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Role:
    name: str
    namespace: str  # resolved namespace name
    f9: bool
    service_account: str | None
    mounted: frozenset[str]  # Secret names a container may read (volume, env)
    image_pull: frozenset[str]  # Secret names allowed only in imagePullSecrets
    pull_patterns: tuple[re.Pattern[str], ...]  # injected <sa>-dockercfg-<suffix> names
    egress: frozenset[str]
    selector: tuple[str, str] | None  # (label key, value) for the manager
    operator_secrets_listed: bool
    template_pods: bool  # False: its pods are created by another component


@dataclass(frozen=True)
class EgressClass:
    name: str
    cidrs: tuple[str, ...]  # ipBlock peers; empty for a namespaceSelector class
    tcp_ports: tuple[int, ...]
    dns: bool


@dataclass
class Contract:
    sha256: str
    role_label_key: str
    forbidden_label_keys: frozenset[str]
    forbidden_label_prefixes: tuple[str, ...]
    prod: str
    ci: str
    roles: dict[str, Role]
    raw: dict[str, Any]

    def namespace(self, key: str) -> str:
        return {"prod": self.prod, "ci": self.ci}[key]


@dataclass
class Findings:
    items: list[dict[str, str]] = field(default_factory=list)

    def violation(self, check: str, subject: str, reason: str) -> None:
        self.items.append({"check": check, "kind": "violation", "subject": subject, "reason": reason})

    def inconclusive(self, check: str, subject: str, reason: str) -> None:
        self.items.append(
            {"check": check, "kind": "inconclusive", "subject": subject, "reason": reason}
        )

    def count(self, kind: str) -> int:
        return sum(1 for f in self.items if f["kind"] == kind)


# --------------------------------------------------------------------------- params


class Params:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str) -> str:
        value = self._values.get(name, "").strip()
        if not value:
            raise ProofError("param-missing", name)
        return value

    def optional(self, name: str) -> str | None:
        value = self._values.get(name, "").strip()
        return value or None


def read_param_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProofError("param-file-unreadable") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


# --------------------------------------------------------------------------- contract


def _require(obj: Any, kind: type) -> Any:
    if not isinstance(obj, kind):
        raise ProofError("contract-unreadable")
    return obj


def _secret_names(refs: Mapping[str, Any], kind: str, params: Params) -> frozenset[str]:
    """Literal names under `kind` plus the values of `<kind>_parameters`."""
    found = {str(_require(n, str)) for n in _require(refs.get(kind, []), list)}
    found |= {params.get(_require(p, str)) for p in _require(refs.get(f"{kind}_parameters", []),
                                                            list)}
    return frozenset(found)


def load_contract(path: Path, params: Params) -> Contract:
    """The workspace contract (deploy/workspace/contract.json), resolved against params."""
    try:
        data = path.read_bytes()
        raw = json.loads(data)
    except (OSError, ValueError) as exc:
        raise ProofError("contract-unreadable") from exc
    _require(raw, dict)
    if raw.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise ProofError("contract-unreadable")
    namespaces = _require(raw.get("namespaces"), dict)
    if set(namespaces) != {"prod", "ci"}:
        raise ProofError("contract-unreadable")
    ns_names = {
        key: params.get(str(_require(_require(spec, dict).get("parameter"), str)))
        for key, spec in namespaces.items()
    }
    if ns_names["prod"] == ns_names["ci"]:
        raise ProofError("param-invalid", "CI_NAMESPACE")
    roles: dict[str, Role] = {}
    for name, spec in _require(raw.get("roles"), dict).items():
        _require(spec, dict)
        ns_key = spec.get("namespace")
        if ns_key not in ns_names:
            raise ProofError("contract-unreadable")
        f9 = spec.get("f9_defined")
        sa = spec.get("service_account")
        selector = None
        if "pod_selector_parameters" in spec:
            sel = _require(spec["pod_selector_parameters"], dict)
            selector = (params.get(_require(sel.get("key"), str)),
                        params.get(_require(sel.get("value"), str)))
        if not isinstance(f9, bool) or (f9 and not isinstance(sa, str)) or (
                not f9 and selector is None):
            raise ProofError("contract-unreadable")
        refs = _require(spec.get("allowed_secret_refs"), dict)
        try:
            patterns = tuple(re.compile(_require(p, str))
                             for p in _require(refs.get("image_pull_patterns", []), list))
        except re.error as exc:
            raise ProofError("contract-unreadable") from exc
        roles[name] = Role(
            name=name,
            namespace=ns_names[ns_key],
            f9=f9,
            service_account=sa if isinstance(sa, str) else None,
            mounted=_secret_names(refs, "mounted", params),
            image_pull=_secret_names(refs, "image_pull", params),
            pull_patterns=patterns,
            egress=frozenset(_require(spec.get("egress", []), list)),
            selector=selector,
            operator_secrets_listed="operator_added_secrets" in spec,
            template_pods=bool(_require(spec.get("pods", []), list)),
        )
    return Contract(
        sha256=hashlib.sha256(data).hexdigest(),
        role_label_key=str(_require(raw.get("label_key"), str)),
        forbidden_label_keys=frozenset(_require(raw.get("forbidden_label_keys", []), list)),
        forbidden_label_prefixes=tuple(_require(raw.get("forbidden_label_prefixes", []), list)),
        prod=ns_names["prod"],
        ci=ns_names["ci"],
        roles=roles,
        raw=raw,
    )


def runner_job_role(contract: Contract) -> Role:
    """The one F9 role whose pods no template creates: the runner manager makes them."""
    roles = [r for r in contract.roles.values() if r.f9 and not r.template_pods]
    if len(roles) != 1:
        raise ProofError("contract-unreadable")
    return roles[0]


def _ip_block_cidrs(raw: str, parameter: str) -> tuple[str, ...]:
    """CIDRs of a JSON list of NetworkPolicy ipBlock peers (the template's form)."""
    try:
        peers = json.loads(raw)
        cidrs = tuple(str(peer["ipBlock"]["cidr"]) for peer in peers)
        for cidr in cidrs:
            ipaddress.ip_network(cidr, strict=False)
    except (ValueError, TypeError, KeyError) as exc:
        raise ProofError("param-invalid", parameter) from exc
    if not cidrs:
        raise ProofError("param-invalid", parameter)
    return cidrs


def egress_classes(contract: Contract, params: Params) -> dict[str, EgressClass]:
    out: dict[str, EgressClass] = {}
    for name, spec in _require(contract.raw.get("egress_classes"), dict).items():
        _require(spec, dict)
        kind = spec.get("peer_kind")
        cidrs: tuple[str, ...] = ()
        if kind == "ipBlock":
            parameter = str(_require(spec.get("peers_parameter"), str))
            cidrs = _ip_block_cidrs(params.get(parameter), parameter)
        elif kind != "namespaceSelector":
            raise ProofError("contract-unreadable")
        ports = []
        for port in _require(spec.get("ports", []), list):
            if _require(port, dict).get("protocol") != "TCP":
                continue
            if "port_parameter" in port:
                value = params.optional(port["port_parameter"]) or str(port.get("default", ""))
                if not value.isdigit():
                    raise ProofError("param-missing" if not value else "param-invalid",
                                     port["port_parameter"])
                ports.append(int(value))
            else:
                ports.append(int(_require(port.get("port"), int)))
        out[name] = EgressClass(name=name, cidrs=cidrs, tcp_ports=tuple(ports),
                                dns=kind == "namespaceSelector")
    return out


def injected_pull_pattern(contract: Contract, service_account: str) -> re.Pattern[str]:
    """The contract's `<serviceaccount>-dockercfg-<suffix>` pattern, bound to one SA."""
    template = str(_require(_require(contract.raw.get("secret_refs"), dict)
                            .get("injected_image_pull_pattern"), str))
    head, sep, tail = template.partition("{service_account}")
    if not sep:
        raise ProofError("contract-unreadable")
    return re.compile(head + re.escape(service_account) + tail)


def probe_image(contract: Contract, params: Params) -> str:
    """DEVKIT_REPOSITORY@DEVKIT_DIGEST, by the parameter names the contract gives."""
    names = _require(_require(contract.raw.get("bastion_pod"), dict).get("image_parameters"),
                     dict)
    repo_param = str(_require(names.get("repository"), str))
    digest_param = str(_require(names.get("digest"), str))
    image = f"{params.get(repo_param)}@{params.get(digest_param)}"
    if not DIGEST_IMAGE.match(image):
        raise ProofError("param-invalid", f"{repo_param}@{digest_param}")
    return image


# --------------------------------------------------------------------------- cluster


class Kube:
    def __init__(self, binary: str) -> None:
        path = shutil.which(binary)
        if path is None:
            raise ProofError("kubectl-missing")
        self.binary = path

    def run(self, args: list[str], step: str, stdin: str | None = None,
            ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                [self.binary, *args], input=stdin, capture_output=True, text=True,
                timeout=CALL_TIMEOUT_S, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProofError("kube-call-failed", step) from exc
        if proc.returncode not in ok:
            raise ProofError("kube-call-failed", step)
        return proc

    def json(self, args: list[str], step: str, stdin: str | None = None) -> dict[str, Any]:
        proc = self.run([*args, "-o", "json"], step, stdin=stdin)
        try:
            value = json.loads(proc.stdout)
        except ValueError as exc:
            raise ProofError("kube-call-failed", step) from exc
        if not isinstance(value, dict):
            raise ProofError("kube-call-failed", step)
        return value

    def try_json(self, args: list[str]) -> dict[str, Any] | None:
        try:
            return self.json(args, "optional")
        except ProofError:
            return None

    def create(self, obj: dict[str, Any], namespace: str, step: str) -> None:
        self.run(["create", "-n", namespace, "-f", "-", "-o", "name"], step, stdin=json.dumps(obj))

    def create_object(self, obj: dict[str, Any], namespace: str, step: str) -> dict[str, Any]:
        """Create and return the object as the API server stored it (with its uid)."""
        return self.json(["create", "-n", namespace, "-f", "-"], step, stdin=json.dumps(obj))

    def items(self, args: list[str], step: str) -> list[dict[str, Any]]:
        items = self.json(args, step).get("items")
        if not isinstance(items, list):
            raise ProofError("kube-call-failed", step)
        return items


def sa_user(namespace: str, sa: str) -> str:
    return f"system:serviceaccount:{namespace}:{sa}"


def parse_can_i_list(text: str) -> list[str]:
    """Rules from `auth can-i --list` table output as 'verb resource' strings."""
    rules: set[str] = set()
    for line in text.splitlines():
        match = CANI_ROW.match(line)
        if not match:
            continue
        verbs = match["verbs"][1:-1].split()
        urls = match["urls"][1:-1].split()
        names = match["names"][1:-1].split()
        suffix = f" names={','.join(sorted(names))}" if names else ""
        for verb in verbs:
            if match["res"]:
                rules.add(f"{verb} {match['res']}{suffix}")
            for url in urls:
                rules.add(f"{verb} {url}")
    return sorted(rules)


def can_i_list(kube: Kube, user: str, namespace: str) -> list[str]:
    proc = kube.run(["auth", "can-i", "--list", f"--as={user}", "-n", namespace],
                    "auth can-i --list")
    return parse_can_i_list(proc.stdout)


def can_i(kube: Kube, verb: str, resource: str, subresource: str, namespace: str,
          user: str | None) -> bool:
    args = ["auth", "can-i", verb, resource, "-n", namespace]
    if subresource:
        args.append(f"--subresource={subresource}")
    if user:
        args.append(f"--as={user}")
    proc = kube.run(args, "auth can-i", ok=(0, 1))
    answer = proc.stdout.strip().split()[:1]
    if answer == ["yes"] and proc.returncode == 0:
        return True
    if answer == ["no"] and proc.returncode == 1:
        return False
    raise ProofError("kube-call-failed", "auth can-i")


# --------------------------------------------------------------------------- step 1, 2


def check_rbac(kube: Kube, contract: Contract, manager_sas: Iterable[str],
               findings: Findings) -> dict[str, Any]:
    """Each F9 ServiceAccount's `can-i --list` against the fixed expected baseline.

    A fresh ServiceAccount is no baseline: a grant to `system:serviceaccounts`
    or `system:serviceaccounts:<ns>` applies to it as well. Every rule outside
    EXPECTED_BASELINE is a violation, whichever binding or group granted it.
    """
    namespaces = (contract.prod, contract.ci)
    accounts = []
    for role in contract.roles.values():
        if not role.f9 or role.service_account is None:
            continue
        user = sa_user(role.namespace, role.service_account)
        listed = {ns: can_i_list(kube, user, ns) for ns in namespaces}
        excess = {ns: sorted(set(rules) - EXPECTED_BASELINE) for ns, rules in listed.items()}
        for ns, rules in listed.items():
            # Every authenticated identity holds the self-review rule: an
            # answer without it is a broken listing, not a clean one.
            if BASELINE_CONTROL_RULE not in rules:
                findings.inconclusive("rbac", f"{role.name} in {ns}", "listing-without-self-review")
            for rule in excess[ns]:
                findings.violation("rbac", f"{role.name} in {ns}", f"beyond-baseline: {rule}")
        accounts.append({"role": role.name, "namespace": role.namespace,
                         "service_account": role.service_account,
                         "rules": listed, "beyond_baseline": excess})
    manager = []
    for role in contract.roles.values():
        if role.f9:
            continue
        for sa in sorted(set(manager_sas)):
            manager.append({"role": role.name, "namespace": role.namespace,
                            "service_account": sa,
                            "rules": {ns: can_i_list(kube, sa_user(role.namespace, sa), ns)
                                      for ns in namespaces},
                            "judged": False})
    return {"baseline_version": BASELINE_VERSION, "service_accounts": accounts,
            "manager": manager}


def check_negative_matrix(kube: Kube, contract: Contract, findings: Findings) -> dict[str, Any]:
    namespaces = (contract.prod, contract.ci)
    control = {ns: can_i(kube, "get", "secrets", "", ns, None) for ns in namespaces}
    for ns, allowed in control.items():
        if not allowed:
            findings.inconclusive("negative-matrix", f"operator in {ns}",
                                  "positive-control-refused")
    results = []
    for role in contract.roles.values():
        if not role.f9 or role.service_account is None:
            continue
        user = sa_user(role.namespace, role.service_account)
        for ns in namespaces:
            for verb, resource, sub in NEGATIVE_MATRIX:
                allowed = can_i(kube, verb, resource, sub, ns, user)
                target = f"{resource}/{sub}" if sub else resource
                results.append({"role": role.name, "namespace": ns, "verb": verb,
                                "resource": target, "allowed": allowed})
                if allowed:
                    findings.violation("negative-matrix", f"{role.name} in {ns}",
                                       f"allowed: {verb} {target}")
    return {"operator_control": control, "results": results}


# --------------------------------------------------------------------------- step 3


def _walk_secret_keys(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "secretName" and isinstance(value, str):
                yield value
            elif key.endswith("SecretRef") or key == "secretRef":
                if isinstance(value, dict) and isinstance(value.get("name"), str):
                    yield value["name"]
            else:
                yield from _walk_secret_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_secret_keys(item)


def secret_refs(spec: Mapping[str, Any]) -> tuple[list[tuple[str, str]], bool]:
    """(Secret name, reference kind) pairs, and whether a token is projected.

    Kinds: `volume` (including projected sources and any volume plugin's
    Secret reference) and `env` (env and envFrom) are the contract's
    "mounted" references; `imagePullSecrets` is its "image_pull" kind.
    """
    refs: set[tuple[str, str]] = set()
    token = False
    for volume in spec.get("volumes") or []:
        for key, source in volume.items():
            if key == "name":
                continue
            if key == "secret":
                refs.add((str(source.get("secretName", "")), "volume"))
            elif key == "projected":
                for item in source.get("sources") or []:
                    if "secret" in item:
                        refs.add((str(item["secret"].get("name", "")), "volume"))
                    if "serviceAccountToken" in item:
                        token = True
            else:
                refs.update((name, "volume") for name in _walk_secret_keys(source))
    for group in ("initContainers", "containers", "ephemeralContainers"):
        for container in spec.get(group) or []:
            for env in container.get("env") or []:
                ref = (env.get("valueFrom") or {}).get("secretKeyRef")
                if ref:
                    refs.add((str(ref.get("name", "")), "env"))
            for env_from in container.get("envFrom") or []:
                ref = env_from.get("secretRef")
                if ref:
                    refs.add((str(ref.get("name", "")), "env"))
    for pull in spec.get("imagePullSecrets") or []:
        refs.add((str(pull.get("name", "")), "imagePullSecrets"))
    return sorted(refs), token


def classify_pods(contract: Contract, pods: Mapping[str, list[dict[str, Any]]],
                  findings: Findings) -> tuple[list[dict[str, Any]], set[str]]:
    rows = []
    manager_sas: set[str] = set()
    key = contract.role_label_key
    managers = [r for r in contract.roles.values() if not r.f9]
    for ns, items in pods.items():
        for pod in items:
            meta = pod.get("metadata") or {}
            spec = pod.get("spec") or {}
            name = str(meta.get("name", ""))
            labels = meta.get("labels") or {}
            subject = f"{ns}/{name}"
            role_value = labels.get(key)
            manager = next((r for r in managers if r.selector and r.namespace == ns
                            and labels.get(r.selector[0]) == r.selector[1]), None)
            refs, token = secret_refs(spec)
            row: dict[str, Any] = {"namespace": ns, "name": name}
            role = contract.roles.get(role_value) if role_value is not None else None
            if role_value is not None:
                if manager is not None:
                    # The manager's allow-list includes the API server.
                    row["class"] = "unexpected"
                    findings.violation("pods", subject, "manager-selector-matches-workspace-pod")
                    rows.append(row)
                    continue
                if role is None or not role.f9 or role.namespace != ns:
                    row["class"] = "unexpected"
                    findings.violation("pods", subject, "unexpected-role-label")
                    rows.append(row)
                    continue
                row["class"] = role.name
                check_f9_pod(contract, role, subject, labels, spec, refs, token, row, findings)
            elif manager is not None:
                row["class"] = manager.name
                sa = str(spec.get("serviceAccountName") or "default")
                manager_sas.add(sa)
                row["service_account"] = sa
                row["secret_refs"] = []
                for n, v in refs:
                    pull = v == "imagePullSecrets"
                    if n in (manager.image_pull if pull else manager.mounted):
                        status = "expected"
                    elif n in manager.image_pull | manager.mounted:
                        status = "wrong-kind"
                        findings.violation("pods", subject, f"secret-not-allowed: {n} via {v}")
                    elif manager.operator_secrets_listed:
                        status = "operator-added"
                    else:
                        status = "not-allowed"
                        findings.violation("pods", subject, f"secret-not-allowed: {n} via {v}")
                    row["secret_refs"].append({"name": n, "via": v, "status": status})
            elif ns == contract.prod:
                row["class"] = "release"
                owners = meta.get("ownerReferences") or []
                row["owner_kinds"] = sorted({str(o.get("kind", "")) for o in owners})
            else:
                row["class"] = "unclassified"
                findings.violation("pods", subject, "unclassified-pod")
            rows.append(row)
    return rows, manager_sas


def runner_job_pull_prefix(pod_name: str) -> str | None:
    """The GitLab Runner job name prefix a per-job pull Secret shares with its pod."""
    match = RUNNER_JOB_NAME.match(pod_name)
    return match[1] if match else None


def check_f9_pod(contract: Contract, role: Role, subject: str, labels: Mapping[str, str],
                 spec: Mapping[str, Any], refs: list[tuple[str, str]], token: bool,
                 row: dict[str, Any], findings: Findings) -> None:
    runner = role.name == runner_job_role(contract).name
    bad_labels = sorted(
        k for k in labels
        if k in contract.forbidden_label_keys
        or any(k.startswith(p) for p in contract.forbidden_label_prefixes)
    )
    for label in bad_labels:
        findings.violation("pods", subject, f"forbidden-label: {label}")
    # Template pods carry only the role label; GitLab Runner adds its own to job pods.
    extra_labels = sorted(set(labels) - {contract.role_label_key} - set(bad_labels))
    if not runner:
        for label in extra_labels:
            findings.violation("pods", subject, f"unexpected-label: {label}")
    if spec.get("ephemeralContainers"):
        findings.violation("pods", subject, "ephemeral-container")
    sa = str(spec.get("serviceAccountName") or "default")
    if sa != role.service_account:
        findings.violation("pods", subject, f"service-account: {sa}")
    if token:
        findings.violation("pods", subject, "service-account-token-mounted")
    own_pull = injected_pull_pattern(contract, sa)
    pod_name = subject.split("/", 1)[1]
    job_prefix = runner_job_pull_prefix(pod_name) if runner else None
    checked = []
    for name, via in refs:
        if via == "imagePullSecrets":
            ok = name in role.image_pull or (
                own_pull.fullmatch(name) is not None
                and any(p.fullmatch(name) for p in role.pull_patterns)
            ) or (job_prefix is not None and RUNNER_JOB_SECRET.fullmatch(name) is not None
                  and name.startswith(job_prefix) and name != pod_name)
        else:
            ok = name in role.mounted
        checked.append({"name": name, "via": via, "allowed": ok})
        if not ok:
            findings.violation("pods", subject, f"secret-not-allowed: {name} via {via}")
    row.update({"service_account": sa, "forbidden_labels": bad_labels,
                "runner_added_labels": extra_labels if runner else [],
                "token_mounted": token, "secret_refs": checked})


# --------------------------------------------------------------------------- step 4


def _find_keys(node: Any, pattern: re.Pattern[str], path: str = "") -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if pattern.search(str(key)):
                yield here
            yield from _find_keys(value, pattern, here)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _find_keys(item, pattern, f"{path}[{index}]")


def _toml_documents(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProofError("runner-config-unreadable", path.name) from exc
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    try:
        if doc is None:
            return [tomllib.loads(text)]
        # `oc process -o json` output: a List, or one ConfigMap, holding TOML.
        objects = doc.get("items", [doc]) if isinstance(doc, dict) else []
        found = []
        for obj in objects:
            if isinstance(obj, dict) and obj.get("kind") == "ConfigMap":
                for key, value in (obj.get("data") or {}).items():
                    if str(key).endswith(".toml") and isinstance(value, str):
                        found.append(tomllib.loads(value))
        return found
    except tomllib.TOMLDecodeError as exc:
        raise ProofError("runner-config-unreadable", path.name) from exc


def check_runner_config(contract: Contract, params: Params, paths: list[Path],
                        findings: Findings) -> list[dict[str, Any]]:
    """ADR-0009 section 2 runner rules, with names from the contract's runner job role.

    Every key must be on the allow-list and every listed key must hold an
    allowed value; nothing else in the file may shape a job pod.
    """
    role = runner_job_role(contract)
    prefix = params.get("REGISTRY_PATH").rstrip("/") + "/"
    rows = []
    for path in paths:
        for doc in _toml_documents(path):
            overwrite = list(_find_keys(doc, OVERWRITE_KEY))
            placeholders = "${" in json.dumps(doc)
            if placeholders:
                findings.inconclusive("runner-config", path.name, "unrendered-placeholder")
            runners = doc.get("runners", [])
            entries = runners if isinstance(runners, list) else [runners]
            for index, runner in enumerate(entries):
                kube = runner.get("kubernetes") if isinstance(runner, dict) else None
                subject = f"{path.name}#{index}"
                if not isinstance(kube, dict):
                    findings.violation("runner-config", subject, "no-kubernetes-section")
                    continue
                refused = sorted(
                    [k for k in doc if k not in RUNNER_TOP_KEYS]
                    + [f"runners.{k}" for k in runner if k not in RUNNER_ENTRY_KEYS]
                    + [f"runners.kubernetes.{k}" for k in kube
                       if k not in RUNNER_KUBERNETES_KEYS])
                security = kube.get("pod_security_context", {})
                if isinstance(security, dict):
                    refused += [f"runners.kubernetes.pod_security_context.{k}"
                                for k in security if k not in RUNNER_POD_SECURITY_KEYS]
                for key in refused:
                    findings.violation("runner-config", subject, f"refused-key: {key}")
                labels = kube.get("pod_labels") or {}
                pull_policy = kube.get("pull_policy", [])
                pull_policy = [pull_policy] if isinstance(pull_policy, str) else pull_policy
                checks: dict[str, bool] = {
                    "executor_kubernetes": runner.get("executor") == "kubernetes",
                    "images_pinned_in_site_registry": all(
                        isinstance(kube.get(k), str) and kube[k].startswith(prefix)
                        and "@sha256:" in kube[k] for k in ("image", "helper_image")),
                    "pull_policy": isinstance(pull_policy, list)
                    and set(pull_policy) <= RUNNER_PULL_POLICIES,
                    "cap_drop_all": "ALL" in (kube.get("cap_drop") or []),
                    "pod_security_context": isinstance(security, dict)
                    and security.get("run_as_non_root", True) is True,
                    "no_overwrite_allowed_key": not overwrite,
                    "namespace": kube.get("namespace") == role.namespace,
                    "service_account": kube.get("service_account") == role.service_account,
                    "image_pull_secrets": sorted(kube.get("image_pull_secrets") or [])
                    == sorted(role.image_pull),
                    "pod_labels_role_only": labels == {contract.role_label_key: role.name},
                    # The manager's allow-list includes the API server.
                    "pod_labels_outside_manager_selector": not any(
                        r.selector and labels.get(r.selector[0]) == r.selector[1]
                        for r in contract.roles.values() if not r.f9),
                }
                for key in RUNNER_FALSE_KEYS:
                    checks[f"{key}_false"] = kube.get(key) is False
                for key in RUNNER_RESOURCE_KEYS:
                    checks[f"has_{key}"] = bool(str(kube.get(key, "")).strip())
                for key in ("allowed_images", "allowed_services"):
                    values = kube.get(key) or []
                    checks[f"{key}_site_registry"] = bool(values) and all(
                        isinstance(v, str) and v.startswith(prefix) for v in values
                    )
                for name, ok in checks.items():
                    if not ok:
                        findings.violation("runner-config", subject, f"failed: {name}")
                rows.append({"file": path.name, "index": index,
                             "name": str(runner.get("name", "")),
                             "overwrite_keys": overwrite, "refused_keys": refused,
                             "pod_labels": dict(labels) if isinstance(labels, dict) else {},
                             "checks": checks})
    if not rows:
        findings.inconclusive("runner-config", "runner configuration", "no-runner-entries")
    return rows


# --------------------------------------------------------------------------- step 5


def selector_matches(selector: Mapping[str, Any] | None, labels: Mapping[str, str]) -> bool:
    if selector is None:
        return False
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr.get("key"), expr.get("operator"), expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and key in labels and labels[key] in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def _selector_keys(selector: Mapping[str, Any]) -> set[str]:
    keys = set((selector.get("matchLabels") or {}).keys())
    keys.update(str(e.get("key")) for e in selector.get("matchExpressions") or [])
    return keys


def _port_admits(ports: list[dict[str, Any]] | None, port: int, port_names: set[str]) -> bool:
    if not ports:
        return True
    for entry in ports:
        value = entry.get("port")
        end = entry.get("endPort")
        if value is None or value == port or value in port_names:
            return True
        if isinstance(value, int) and isinstance(end, int) and value <= port <= end:
            return True
    return False


def role_pod_selector(contract: Contract, role: Role) -> dict[str, Any]:
    """The podSelector the workspace templates render for a role's allow policy."""
    if role.selector:
        return {"matchExpressions": [{"key": role.selector[0], "operator": "In",
                                      "values": [role.selector[1]]}]}
    return {"matchLabels": {contract.role_label_key: role.name}}


def expected_egress_rule(spec: Mapping[str, Any], params: Params) -> dict[str, Any]:
    """One egress class as the templates render it: its peers and its ports."""
    ports = []
    for port in _require(spec.get("ports", []), list):
        if "port_parameter" in port:
            value = params.optional(port["port_parameter"]) or str(port.get("default", ""))
            if not value.isdigit():
                raise ProofError("param-missing" if not value else "param-invalid",
                                 port["port_parameter"])
            number = int(value)
        else:
            number = int(_require(port.get("port"), int))
        ports.append({"protocol": port.get("protocol"), "port": number})
    if spec.get("peer_kind") == "ipBlock":
        parameter = str(_require(spec.get("peers_parameter"), str))
        try:
            peers = json.loads(params.get(parameter))
        except ValueError as exc:
            raise ProofError("param-invalid", parameter) from exc
    else:
        peers = [{"namespaceSelector": spec.get("namespace_selector")}]
    return {"to": peers, "ports": ports}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def check_egress_policies(contract: Contract, params: Params,
                          policies: Mapping[str, list[dict[str, Any]]],
                          pods: Mapping[str, list[dict[str, Any]]],
                          findings: Findings) -> list[dict[str, Any]]:
    """The egress policies equal the contract's, exactly.

    CI: the namespace holds exactly the contract's policies, so every egress
    path of every CI pod comes from the contract, whatever an extra policy
    would select. PROD: every policy that selects the bastion (its contract
    label set or a live bastion pod's labels) is one of the bastion's
    contract policies; release policies that do not select it are ignored.
    Each contract policy must equal its rendered form.
    """
    policy_specs = _require(contract.raw.get("policies"), dict)
    if set(policy_specs) != {"prod", "ci"}:
        raise ProofError("contract-unreadable")
    classes = _require(contract.raw.get("egress_classes"), dict)
    rows = []
    for ns_key, spec in policy_specs.items():
        ns = contract.namespace(ns_key)
        by_name = {str((p.get("metadata") or {}).get("name", "")): p for p in policies[ns]}
        deny = _require(spec.get("default_deny"), dict)
        allow_names = [str(n) for n in _require(spec.get("allow", []), list)]
        roles = {r.name: r for r in contract.roles.values() if r.namespace == ns}
        allow_role = {}
        for role_name, body in _require(contract.raw.get("roles"), dict).items():
            if role_name in roles and body.get("allow_policy") in allow_names:
                allow_role[str(body["allow_policy"])] = roles[role_name]
        if set(allow_role) != set(allow_names):
            raise ProofError("contract-unreadable")
        expected: dict[str, dict[str, Any]] = {str(deny.get("name")): {
            "podSelector": deny.get("pod_selector"),
            "policyTypes": deny.get("policy_types"),
            "egress": []}}
        for name, role in allow_role.items():
            expected[name] = {
                "podSelector": role_pod_selector(contract, role),
                "policyTypes": ["Egress"],
                "egress": [expected_egress_rule(_require(classes.get(c), dict), params)
                           for c in sorted(role.egress)]}
        for name, want in expected.items():
            row: dict[str, Any] = {"namespace": ns, "name": name, "present": name in by_name}
            if name not in by_name:
                findings.violation("egress-policies", f"{ns}/{name}", "missing")
            else:
                body = by_name[name].get("spec") or {}
                have = {"podSelector": body.get("podSelector") or {},
                        "policyTypes": body.get("policyTypes") or [],
                        "egress": sorted(_canonical(r) for r in body.get("egress") or [])}
                want_c = {"podSelector": want["podSelector"] or {},
                          "policyTypes": want["policyTypes"] or [],
                          "egress": sorted(_canonical(r) for r in want["egress"])}
                extra_keys = set(body) - {"podSelector", "policyTypes", "egress"}
                matches = _canonical(have) == _canonical(want_c) and not extra_keys
                row["matches_contract"] = matches
                if not matches:
                    findings.violation("egress-policies", f"{ns}/{name}", "differs-from-contract")
            rows.append(row)
        if ns_key == "ci":
            extra = sorted(set(by_name) - set(expected))
        else:
            bastion_sets = workspace_label_sets(contract, ns, pods[ns])
            extra = sorted(
                name for name, p in by_name.items() if name not in expected
                and any(selector_matches((p.get("spec") or {}).get("podSelector") or {}, labels)
                        for labels in bastion_sets))
        for name in extra:
            findings.violation("egress-policies", f"{ns}/{name}", "policy-outside-contract")
        rows.append({"namespace": ns, "outside_contract": extra})
    return rows


def workspace_label_sets(contract: Contract, ns: str,
                         pods: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Label sets of workspace and manager pods in `ns`: the contract's, and live ones."""
    sets: list[dict[str, str]] = []
    selectors = [r.selector for r in contract.roles.values()
                 if not r.f9 and r.selector and r.namespace == ns]
    for role in contract.roles.values():
        if role.f9 and role.namespace == ns:
            sets.append({contract.role_label_key: role.name})
    sets.extend({key: value} for key, value in selectors)
    for pod in pods:
        labels = (pod.get("metadata") or {}).get("labels") or {}
        if (labels.get(contract.role_label_key) in contract.roles
                or any(labels.get(k) == v for k, v in selectors)):
            sets.append(dict(labels))
    return sets


def cluster_networks(kube: Kube, given: list[str]) -> list[str] | None:
    """Pod and Service networks: --cluster-network values, else the cluster's config."""
    if given:
        return given
    config = kube.try_json(["get", "network.config.openshift.io", "cluster"])
    if config is None:
        return None
    status = config.get("status") or {}
    nets = [str(n.get("cidr")) for n in status.get("clusterNetwork") or [] if n.get("cidr")]
    nets += [str(n) for n in status.get("serviceNetwork") or []]
    return nets or None


def _covers_network(block: Mapping[str, Any], networks: list[ipaddress.IPv4Network
                                                             | ipaddress.IPv6Network]) -> bool:
    try:
        cidr = ipaddress.ip_network(str(block.get("cidr")), strict=False)
        excepts = [ipaddress.ip_network(str(e), strict=False) for e in block.get("except") or []]
    except ValueError:
        return True  # an unparseable block is never accepted
    for net in networks:
        if cidr.version != net.version or not cidr.overlaps(net):
            continue
        if not any(e.version == net.version and e.supernet_of(net) for e in excepts):  # type: ignore[arg-type]
            return True
    return False


def check_open_egress(contract: Contract, policies: Mapping[str, list[dict[str, Any]]],
                      pods: Mapping[str, list[dict[str, Any]]], networks: list[str] | None,
                      findings: Findings) -> dict[str, Any]:
    """No egress rule selecting a workspace or manager pod opens more than its peers.

    A Template cannot validate its parameters: an empty or absent `to`, an
    ipBlock of 0.0.0.0/0 or ::/0, an empty namespaceSelector, or an ipBlock
    over the pod or Service network all render and apply cleanly.
    """
    parsed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for net in networks or []:
        try:
            parsed.append(ipaddress.ip_network(net, strict=False))
        except ValueError as exc:
            raise ProofError("param-invalid", "cluster-network") from exc
    rows = []
    for ns in (contract.prod, contract.ci):
        label_sets = workspace_label_sets(contract, ns, pods[ns])
        for policy in policies[ns]:
            name = str((policy.get("metadata") or {}).get("name", ""))
            spec = policy.get("spec") or {}
            if "Egress" not in (spec.get("policyTypes") or []):
                continue
            selector = spec.get("podSelector") or {}
            if not any(selector_matches(selector, labels) for labels in label_sets):
                continue
            subject = f"{ns}/{name}"
            reasons = []
            for index, rule in enumerate(spec.get("egress") or []):
                peers = rule.get("to")
                if not peers:
                    reasons.append(f"rule {index}: no-peers")
                    continue
                for peer in peers:
                    block = peer.get("ipBlock")
                    ns_sel = peer.get("namespaceSelector")
                    if block is not None:
                        cidr = str(block.get("cidr"))
                        if cidr in ("0.0.0.0/0", "::/0"):
                            reasons.append(f"rule {index}: ipblock-any {cidr}")
                        elif parsed and _covers_network(block, parsed):
                            reasons.append(f"rule {index}: ipblock-cluster-network {cidr}")
                    elif ns_sel is not None and not _selector_keys(ns_sel):
                        reasons.append(f"rule {index}: every-namespace")
            for reason in reasons:
                findings.violation("open-egress", subject, reason)
            rows.append({"namespace": ns, "name": name, "open_rules": reasons})
    if networks is None:
        findings.inconclusive("open-egress", "cluster network", "unchecked")
    return {"policies": rows,
            "cluster_network_check": "checked" if networks is not None else "unchecked",
            "cluster_networks": list(networks or [])}


def check_agent_policy(kube: Kube, contract: Contract, agent_service: str,
                       prod_pods: list[dict[str, Any]], policies: list[dict[str, Any]],
                       findings: Findings) -> dict[str, Any]:
    service = kube.try_json(["get", "service", agent_service, "-n", contract.prod])
    if service is None:
        findings.inconclusive("agent-policy", agent_service, "agent-service-not-found")
        return {"service": agent_service, "found": False}
    selector = (service.get("spec") or {}).get("selector") or {}
    agent_pods = [
        (p.get("metadata") or {}).get("labels") or {} for p in prod_pods
        if selector and all(((p.get("metadata") or {}).get("labels") or {}).get(k) == v
                            for k, v in selector.items())
    ] or [selector]
    port_names = {str(p.get("name")) for p in (service.get("spec") or {}).get("ports") or []
                  if p.get("port") == AGENT_PORT}
    port_names |= {str(p.get("targetPort")) for p in (service.get("spec") or {}).get("ports") or []
                   if p.get("port") == AGENT_PORT and isinstance(p.get("targetPort"), str)}
    ci_labels = None
    for kind in ("namespace", "project"):
        obj = kube.try_json(["get", kind, contract.ci])
        if obj is not None:
            ci_labels = (obj.get("metadata") or {}).get("labels") or {}
            break
    known = ci_labels if ci_labels is not None else {"kubernetes.io/metadata.name": contract.ci}
    rows = []
    for policy in policies:
        name = str((policy.get("metadata") or {}).get("name", ""))
        spec = policy.get("spec") or {}
        types = spec.get("policyTypes") or ["Ingress"]
        if "Ingress" not in types:
            continue
        if not any(selector_matches(spec.get("podSelector") or {}, labels)
                   for labels in agent_pods):
            continue
        allowed_namespaces: list[str] = []
        admits_ci = False
        for rule in spec.get("ingress") or []:
            if not _port_admits(rule.get("ports"), AGENT_PORT, port_names):
                continue
            peers = rule.get("from")
            if not peers:
                admits_ci = True
                findings.violation("agent-policy", name, "rule-admits-every-source")
                continue
            for peer in peers:
                if "ipBlock" in peer:
                    findings.inconclusive("agent-policy", name, "ipblock-peer")
                    continue
                ns_sel = peer.get("namespaceSelector")
                if ns_sel is None:
                    continue
                named = (ns_sel.get("matchLabels") or {}).get("kubernetes.io/metadata.name")
                if isinstance(named, str):
                    allowed_namespaces.append(named)
                if selector_matches(ns_sel, known):
                    admits_ci = True
                    findings.violation("agent-policy", name, "admits-ci-namespace")
                elif ci_labels is None and _selector_keys(ns_sel) - {"kubernetes.io/metadata.name"}:
                    findings.inconclusive("agent-policy", name, "ci-namespace-labels-unreadable")
        rows.append({"name": name, "allowed_namespaces": sorted(set(allowed_namespaces)),
                     "admits_ci_namespace": admits_ci})
    if not rows:
        findings.violation("agent-policy", agent_service, "no-ingress-policy-selects-agent")
    return {"service": agent_service, "found": True, "policies": rows,
            "ci_namespace_labels_read": ci_labels is not None}


# --------------------------------------------------------------------------- step 6


@dataclass(frozen=True)
class Identity:
    name: str
    namespace: str
    labels: dict[str, str]
    service_account: str | None
    allowed: frozenset[str] | None  # None: the negative control, expected to reach all


def runner_default_labels(run_id: str) -> dict[str, str]:
    """The labels GitLab Runner's Kubernetes executor puts on every job pod."""
    job = f"runner-proof{run_id}-project-0-concurrent-0"
    return {"pod": job, **{key: "0" for key in RUNNER_DEFAULT_LABEL_KEYS}}


def probe_identities(contract: Contract, live: Mapping[str, dict[str, str]],
                     runner_labels: Mapping[str, str]) -> list[Identity]:
    """One probe identity per role, labelled as the real pods are.

    `live` maps a role to the labels of one of its live pods, which the probe
    copies whole, so a policy keyed on any of them applies to the probe. With
    no live pod the contract's label set is used; runner jobs also get the
    labels GitLab Runner adds and the runner configuration's pod_labels.
    """
    out = []
    runner = runner_job_role(contract).name
    for role in contract.roles.values():
        if role.f9:
            labels = {contract.role_label_key: role.name}
            if role.name == runner:
                labels = {**dict(runner_labels), **labels}
            labels = dict(live.get(role.name, labels))
            out.append(Identity(role.name, role.namespace, labels,
                                role.service_account, role.egress))
        elif role.selector:
            # The manager's own ServiceAccount is never borrowed.
            labels = dict(live.get(role.name, {role.selector[0]: role.selector[1]}))
            out.append(Identity(role.name, role.namespace, labels, None, role.egress))
    out.append(Identity("control", contract.prod, {}, None, None))
    return out


def _host_addresses(cidrs: Iterable[str]) -> tuple[list[str], list[str]]:
    hosts, wide = [], []
    for cidr in cidrs:
        net = ipaddress.ip_network(cidr, strict=False)
        if net.num_addresses == 1:
            hosts.append(str(net.network_address))
        else:
            wide.append(cidr)
    return hosts, wide


def probe_targets(kube: Kube, contract: Contract, params: Params, agent_service: str,
                  extra_hosts: Mapping[str, list[str]], timeout: float,
                  findings: Findings) -> tuple[list[dict[str, Any]], list[str]]:
    """Probe targets per class, and the CIDRs wider than one host (not probed).

    A wide CIDR is only recorded: a class still needs at least one probe
    address (a host CIDR or --probe-host), or it is inconclusive.
    """
    classes = egress_classes(contract, params)
    targets: list[dict[str, Any]] = []
    unprobed: list[str] = []

    def add(cls: str, kind: str, host: str | None, port: int | None) -> None:
        targets.append({"id": f"t{len(targets)}", "class": cls, "kind": kind,
                        "host": host, "port": port, "timeout": timeout})

    services = kube.items(["get", "services", "-n", contract.prod], "get services")
    for svc in services:
        spec = svc.get("spec") or {}
        ip = spec.get("clusterIP")
        name = (svc.get("metadata") or {}).get("name")
        if not ip or ip == "None":
            continue
        ports = {p.get("port") for p in spec.get("ports") or []}
        for port in QDRANT_PORTS:
            if port in ports:
                add("prod_qdrant", "tcp", ip, port)
        if name == agent_service and AGENT_PORT in ports:
            add("prod_agent", "tcp", ip, AGENT_PORT)
    for cls_name, cls in classes.items():
        if cls.dns:
            add(cls_name, "dns", DNS_PROBE_NAME, None)
            continue
        if cls_name == KUBE_SERVICE_CLASS:
            add(cls_name, "kube_service", None, None)
        hosts, wide = _host_addresses(cls.cidrs)
        hosts.extend(extra_hosts.get(cls_name, []))
        unprobed.extend(f"{cls_name} {cidr}" for cidr in wide)
        for host in dict.fromkeys(hosts):
            for port in cls.tcp_ports:
                add(cls_name, "tcp", host, port)
    present = {t["class"] for t in targets if t["kind"] != "kube_service"}
    for cls_name in ("prod_qdrant", "prod_agent", *classes):
        if cls_name not in present:
            findings.inconclusive("probes", cls_name, "no-probe-target")
    return targets, unprobed


def probe_labels(labels: Mapping[str, str]) -> dict[str, str]:
    """A live pod's labels without the keys a workload controller owns."""
    return {k: v for k, v in labels.items()
            if k not in CONTROLLER_LABEL_KEYS and not k.startswith(CONTROLLER_LABEL_PREFIXES)}


def create_owner(kube: Kube, namespace: str, run_id: str) -> dict[str, Any]:
    """Create the run's owner ConfigMap in `namespace`; return the probes' ownerReference.

    A controller adopts only a pod with no controller reference, so every probe
    names this ConfigMap as its controller. blockOwnerDeletion is false: setting
    it true needs update on the owner's finalizers, and nothing waits on it.
    """
    name = f"{OWNER_NAME_PREFIX}{run_id}"
    created = kube.create_object(
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": name, "annotations": {PROOF_ANNOTATION: run_id}}},
        namespace, "create probe owner")
    uid = (created.get("metadata") or {}).get("uid")
    if not isinstance(uid, str) or not uid:
        raise ProofError("kube-call-failed", "create probe owner")
    return {"apiVersion": "v1", "kind": "ConfigMap", "name": name, "uid": uid,
            "controller": True, "blockOwnerDeletion": False}


def probe_pod(identity: Identity, run_id: str, image: str, targets: list[dict[str, Any]],
              resources: Mapping[str, str], deadline: int,
              owner: Mapping[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "activeDeadlineSeconds": deadline,
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "probe",
            "image": image,
            "command": ["python3", "-I", "-c", PROBE_PROGRAM, json.dumps(targets)],
            "securityContext": {"allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]}, "runAsNonRoot": True},
            "resources": {
                "requests": {"cpu": resources["cpu"], "memory": resources["memory"],
                             "ephemeral-storage": resources["ephemeral-storage"]},
                "limits": {"cpu": resources["cpu"], "memory": resources["memory"],
                           "ephemeral-storage": resources["ephemeral-storage"]},
            },
        }],
    }
    if identity.service_account:
        spec["serviceAccountName"] = identity.service_account
    return {"apiVersion": "v1", "kind": "Pod",
            # No proof label: the probe carries the identity's labels minus the
            # controller-owned keys, so no policy selector can tell it from the
            # real pod. The run is an annotation; the owner keeps controllers off.
            "metadata": {"name": f"{PROBE_NAME_PREFIX}{identity.name}-{run_id}",
                         "labels": probe_labels(identity.labels),
                         "annotations": {PROOF_ANNOTATION: run_id},
                         "ownerReferences": [dict(owner)]},
            "spec": spec}


def wait_for_pod(kube: Kube, name: str, namespace: str, timeout: float, interval: float) -> None:
    end = time.monotonic() + timeout
    while True:
        pod = kube.json(["get", "pod", name, "-n", namespace], "get probe pod")
        phase = (pod.get("status") or {}).get("phase")
        if phase == "Succeeded":
            return
        if phase == "Failed" or time.monotonic() >= end:
            raise ProofError("probe-failed", name)
        time.sleep(interval)


def read_probe(kube: Kube, name: str, namespace: str, ids: set[str]) -> dict[str, str]:
    proc = kube.run(["logs", name, "-n", namespace], "probe logs")
    for line in proc.stdout.splitlines():
        if line.startswith(PROBE_MARKER):
            try:
                results = json.loads(line[len(PROBE_MARKER):])
                out = {str(r["id"]): str(r["status"]) for r in results}
            except (ValueError, KeyError, TypeError) as exc:
                raise ProofError("probe-failed", name) from exc
            if set(out) != ids or not set(out.values()) <= PROBE_STATUSES:
                raise ProofError("probe-failed", name)
            return out
    raise ProofError("probe-failed", name)


def live_role_labels(contract: Contract,
                     pods: Mapping[str, list[dict[str, Any]]]) -> dict[str, dict[str, str]]:
    """The full labels of one live pod per role (F9 role label or manager selector)."""
    out: dict[str, dict[str, str]] = {}
    for ns, items in pods.items():
        for pod in items:
            labels = dict((pod.get("metadata") or {}).get("labels") or {})
            role = contract.roles.get(labels.get(contract.role_label_key, ""))
            if role is not None and role.f9 and role.namespace == ns:
                out.setdefault(role.name, labels)
                continue
            for r in contract.roles.values():
                if (not r.f9 and r.selector and r.namespace == ns
                        and labels.get(r.selector[0]) == r.selector[1]):
                    out.setdefault(r.name, labels)
    return out


def run_probes(kube: Kube, contract: Contract, params: Params, run_id: str,
               args: argparse.Namespace, live: Mapping[str, dict[str, str]],
               runner_labels: Mapping[str, str], findings: Findings) -> dict[str, Any]:
    image = probe_image(contract, params)
    extra: dict[str, list[str]] = {}
    for item in args.probe_host:
        cls, _, host = item.partition("=")
        extra.setdefault(cls, []).append(host)
    targets, unprobed = probe_targets(kube, contract, params, args.agent_service, extra,
                            args.probe_connect_timeout, findings)
    resources = {"cpu": args.probe_cpu, "memory": args.probe_memory,
                 "ephemeral-storage": args.probe_ephemeral_storage}
    identities = probe_identities(contract, live, runner_labels)
    owners = {ns: create_owner(kube, ns, run_id)
              for ns in dict.fromkeys(i.namespace for i in identities)}
    pods = []
    for identity in identities:
        pod = probe_pod(identity, run_id, image, targets, resources, int(args.probe_timeout),
                        owners[identity.namespace])
        kube.create(pod, identity.namespace, "create probe pod")
        pods.append((identity, pod["metadata"]["name"]))
    ids = {t["id"] for t in targets}
    outcomes: dict[str, dict[str, str]] = {}
    for identity, name in pods:
        wait_for_pod(kube, name, identity.namespace, args.probe_timeout, args.probe_poll)
        outcomes[identity.name] = read_probe(kube, name, identity.namespace, ids)
    control = outcomes["control"]
    results = []
    for identity in identities:
        if identity.allowed is None:
            continue
        for target in targets:
            status = outcomes[identity.name][target["id"]]
            expect = "reach" if target["class"] in identity.allowed else "block"
            label = f"{target['class']} {target['host'] or 'kubernetes-service'}:{target['port']}"
            subject = f"{identity.name} -> {label}"
            if expect == "reach":
                verdict = "ok" if status in REACHED or status == "refused" else "unexpected-block"
                if verdict != "ok":
                    findings.violation("probes", subject, f"unexpected-block: {status}")
            elif status in REACHED:
                verdict = "unexpected-reach"
                findings.violation("probes", subject, "unexpected-reach")
            elif status in BLOCKED and control[target["id"]] in REACHED:
                verdict = "ok"
            else:
                verdict = "inconclusive"
                reason = ("control-did-not-reach" if status in BLOCKED
                          else f"ambiguous-outcome: {status}")
                findings.inconclusive("probes", subject, reason)
            results.append({"identity": identity.name, "namespace": identity.namespace,
                            "class": target["class"], "target": label, "expect": expect,
                            "status": status, "verdict": verdict})
    return {
        "status": "ran",
        "targets": [{k: t[k] for k in ("id", "class", "kind", "host", "port")} for t in targets],
        "cidrs_not_probed": unprobed,
        "control": {"namespace": contract.prod,
                    "results": {t["id"]: control[t["id"]] for t in targets}},
        "results": results,
    }


# --------------------------------------------------------------------------- lifecycle


# Probe objects in deletion order: pods first, then the owners (whose deletion
# also garbage-collects any probe a failed pod delete left behind).
PROOF_OBJECTS = (("pods", "pod", PROBE_NAME_PREFIX), ("configmaps", "configmap", OWNER_NAME_PREFIX))


def proof_objects(kube: Kube, resource: str, namespace: str, run_id: str | None) -> list[str]:
    """Probe objects by name prefix and run annotation (any run when run_id is None)."""
    prefix = next(p for r, _, p in PROOF_OBJECTS if r == resource)
    names = []
    for obj in kube.items(["get", resource, "-n", namespace], f"list probe {resource}"):
        meta = obj.get("metadata") or {}
        name = str(meta.get("name", ""))
        run = (meta.get("annotations") or {}).get(PROOF_ANNOTATION)
        if run is not None and name.startswith(prefix) and run_id in (None, run):
            names.append(name)
    return names


def sweep(kube: Kube, namespaces: Iterable[str],
          run_id: str | None) -> dict[str, dict[str, int]]:
    """Delete probe pods, then owners, in every namespace; one failure skips nothing else."""
    removed: dict[str, dict[str, int]] = {}
    failed: ProofError | None = None
    for ns in namespaces:
        counts: dict[str, int] = {}
        for resource, kind, _ in PROOF_OBJECTS:
            try:
                names = proof_objects(kube, resource, ns, run_id)
                if names:
                    kube.run(["delete", kind, *names, "-n", ns, "--ignore-not-found",
                              "--wait=true"], f"delete probe {resource}")
            except ProofError as exc:
                failed = failed or exc
                continue
            counts[resource] = len(names)
        if len(counts) == len(PROOF_OBJECTS):
            removed[ns] = counts
    if failed is not None:
        raise failed
    return removed


def cleanup(kube: Kube, contract: Contract, run_id: str) -> dict[str, Any]:
    namespaces = (contract.prod, contract.ci)
    try:
        deleted = sweep(kube, namespaces, run_id)
        remaining = {ns: {r: len(proof_objects(kube, r, ns, run_id)) for r, _, _ in PROOF_OBJECTS}
                     for ns in namespaces}
    except ProofError as exc:
        raise ProofError("cleanup-failed", exc.detail) from exc
    if any(n for counts in remaining.values() for n in counts.values()):
        raise ProofError("cleanup-failed")
    return {"deleted": deleted, "remaining": remaining}


def verdict_of(findings: Findings, error: ProofError | None, probed: bool) -> tuple[str, int]:
    if findings.count("violation"):
        return "violation", EXIT_VIOLATION
    if error is not None:
        return "error", EXIT_ERROR
    if findings.count("inconclusive"):
        return "inconclusive", EXIT_ERROR
    # Without the live probes the proof is incomplete: never exit 0 for it.
    return ("proven", EXIT_PROVEN) if probed else ("static-only", EXIT_ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="isolation_proof.py",
        description="Prove the workspace isolation of ADR-0009 section 2.")
    parser.add_argument("--contract", type=Path, required=True,
                        help="deploy/workspace/contract.json")
    parser.add_argument("--param-file", type=Path, required=True,
                        help="the template parameter file (KEY=VALUE lines)")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                        help="override one parameter")
    parser.add_argument("--runner-config", type=Path, action="append", required=True,
                        help="rendered runner config.toml, or `oc process -o json` output")
    parser.add_argument("--kubectl", default="oc", help="cluster client (default: oc)")
    parser.add_argument("--agent-service", default="rag-agent",
                        help="the release's agent Service (default: rag-agent)")
    parser.add_argument("--output", type=Path, help="write the JSON record here")
    parser.add_argument("--probe", action="store_true", help="run the live network probes")
    parser.add_argument("--cluster-network", action="append", default=[], metavar="CIDR",
                        help="a pod or Service network CIDR, when the operator cannot read "
                             "network.config.openshift.io/cluster")
    parser.add_argument("--probe-host", action="append", default=[], metavar="CLASS=HOST",
                        help="an extra probe address for an egress class")
    parser.add_argument("--probe-timeout", type=float, default=180.0)
    parser.add_argument("--probe-poll", type=float, default=2.0)
    parser.add_argument("--probe-connect-timeout", type=float, default=3.0)
    parser.add_argument("--probe-cpu", default="100m")
    parser.add_argument("--probe-memory", default="64Mi")
    parser.add_argument("--probe-ephemeral-storage", default="64Mi")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_ERROR if exc.code else EXIT_PROVEN
    findings = Findings()
    record: dict[str, Any] = {
        "schema": RECORD_SCHEMA,
        "started": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "probe": bool(args.probe),
        "checks": {},
    }
    error: ProofError | None = None
    kube: Kube | None = None
    contract: Contract | None = None
    run_id = secrets.token_hex(4)
    record["run_id"] = run_id

    def interrupt(signum: int, frame: object) -> None:
        raise ProofError("interrupted")

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        values = read_param_file(args.param_file)
        for item in args.param:
            key, sep, value = item.partition("=")
            if not sep or not key:
                raise ProofError("usage")
            values[key] = value
        if any("=" not in item or not item.split("=", 1)[1] for item in args.probe_host):
            raise ProofError("usage")
        params = Params(values)
        contract = load_contract(args.contract, params)
        record["contract_sha256"] = contract.sha256
        record["namespaces"] = {"prod": contract.prod, "ci": contract.ci}
        checks = record["checks"]
        checks["runner_config"] = check_runner_config(contract, params, args.runner_config,
                                                     findings)
        kube = Kube(args.kubectl)
        record["leftovers_removed"] = sweep(kube, (contract.prod, contract.ci), None)
        pods = {ns: kube.items(["get", "pods", "-n", ns], "get pods")
                for ns in (contract.prod, contract.ci)}
        checks["pods"], manager_sas = classify_pods(contract, pods, findings)
        if not manager_sas:
            findings.inconclusive("pods", contract.ci, "no-runner-manager-pod")
        checks["rbac"] = check_rbac(kube, contract, manager_sas, findings)
        checks["negative_matrix"] = check_negative_matrix(kube, contract, findings)
        policies = {ns: kube.items(["get", "networkpolicies", "-n", ns], "get networkpolicies")
                    for ns in (contract.prod, contract.ci)}
        checks["egress_policies"] = check_egress_policies(contract, params, policies, pods,
                                                          findings)
        checks["open_egress"] = check_open_egress(
            contract, policies, pods, cluster_networks(kube, args.cluster_network), findings)
        checks["agent_policy"] = check_agent_policy(kube, contract, args.agent_service,
                                                    pods[contract.prod], policies[contract.prod],
                                                    findings)
        if args.probe:
            runner_labels = runner_default_labels(run_id)
            for row in checks["runner_config"]:
                runner_labels.update(row.get("pod_labels") or {})
            checks["probes"] = run_probes(kube, contract, params, run_id, args,
                                          live_role_labels(contract, pods), runner_labels,
                                          findings)
        else:
            checks["probes"] = {"status": "not-run"}
    except ProofError as exc:
        error = exc
    except KeyboardInterrupt:
        error = ProofError("interrupted")
    except Exception:  # noqa: BLE001 - the record still gets written, with no exception text
        error = ProofError("internal")
    finally:
        try:
            if kube is not None and contract is not None:
                try:
                    record["cleanup"] = cleanup(kube, contract, run_id)
                except ProofError as exc:
                    record["cleanup"] = {"status": "failed"}
                    error = error or exc
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    verdict, code = verdict_of(findings, error, bool(args.probe))
    record["verdict"] = verdict
    record["findings"] = findings.items
    if error is not None:
        record["error"] = {"code": error.code, "message": MESSAGES[error.code],
                           "detail": error.detail}
    if args.output is not None:
        try:
            args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8")
        except OSError:
            print(f"isolation-proof: {MESSAGES['output-unwritable']}", file=sys.stderr)
            return EXIT_ERROR
    summary = (f"isolation-proof: {verdict} ({findings.count('violation')} violations, "
               f"{findings.count('inconclusive')} inconclusive)")
    if error is not None:
        summary += f"; error {error.code}: {MESSAGES[error.code]}"
        if error.detail:
            summary += f" [{error.detail}]"
    print(summary, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
