#!/usr/bin/env python3
"""Workspace isolation proof (#1118, ADR-0009 section 2 "The isolation proof").

Runs on the bastion as the operator (namespace admin in both namespaces) and
talks to the cluster only through `oc` (or `--kubectl kubectl`). It reads the
workspace contract (deploy/workspace/contract.json) for roles, ServiceAccounts,
allowed Secrets and egress classes, and the template parameter file for the
site values, and never hard-codes either. It checks:

1. `auth can-i --list` for every contract ServiceAccount, impersonated in both
   namespaces. An F9 ServiceAccount may hold nothing beyond the baseline of a
   freshly created ServiceAccount with no bindings (the negative control). The
   runner manager's ServiceAccount is listed, not judged.
2. A fixed negative matrix: each F9 ServiceAccount is refused secret reads, pod
   create/exec/attach, workload writes and RoleBinding create in both
   namespaces. The operator's own `get secrets` answer is the positive control.
3. Every pod in both namespaces, classified by its labels, with its Secret
   references checked against the contract's allowed sets.
4. The rendered runner configuration: no `*_overwrite_allowed` key, nothing
   privileged, the contract ServiceAccount and the CI namespace, and requests
   and limits for build, helper and service containers.
5. The production agent's ingress NetworkPolicies admit nothing from the CI
   namespace (`networkPolicy.allowedNamespaces` never lists it).
6. With --probe: short-lived probe pods carrying each role's labels (and the
   runner manager's) test TCP reachability and DNS. A probe pod in the
   production namespace that no policy selects is the negative control: a
   "blocked" result counts only for a target the control reached.

The record carries verbs, resources, object names and outcomes only: never a
Secret value, a token, an env value or manual text. Exit codes: 0 proven,
1 violation found, 2 usage or environment error, or an inconclusive proof.
Run one proof at a time: each run first removes any probe objects an earlier,
interrupted run left behind.

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
PROOF_LABEL = "isolation-proof.mainframe-rag.io/run"
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
    "output-unwritable": "the record could not be written",
}

# Negative matrix (step 2): (verb, resource, subresource). WebSocket exec and
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
)

# Runner configuration rules (ADR-0009 section 2): no job override setting,
# nothing privileged, requests and limits for build, helper and service containers.
OVERWRITE_SUFFIX = "_overwrite_allowed"
RUNNER_FALSE_KEYS = ("privileged", "allow_privilege_escalation")
RUNNER_RESOURCE_KEYS = tuple(
    f"{prefix}{resource}_{bound}"
    for prefix in ("", "helper_", "service_")
    for resource in ("cpu", "memory", "ephemeral_storage")
    for bound in ("request", "limit")
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
    extra_secrets_listed: bool
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
            extra_secrets_listed="operator_added_secrets" in spec,
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

    def json(self, args: list[str], step: str) -> dict[str, Any]:
        proc = self.run([*args, "-o", "json"], step)
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


def baseline_sa_name(run_id: str) -> str:
    return f"mfr-proof-baseline-{run_id}"


def check_rbac(kube: Kube, contract: Contract, run_id: str, manager_sas: Iterable[str],
               findings: Findings) -> dict[str, Any]:
    namespaces = (contract.prod, contract.ci)
    baseline: dict[str, dict[str, list[str]]] = {}
    for home in namespaces:
        name = baseline_sa_name(run_id)
        kube.create({"apiVersion": "v1", "kind": "ServiceAccount",
                     "metadata": {"name": name, "labels": {PROOF_LABEL: run_id}}},
                    home, "create baseline serviceaccount")
        baseline[home] = {ns: can_i_list(kube, sa_user(home, name), ns) for ns in namespaces}
        for ns, rules in baseline[home].items():
            if not rules:
                findings.inconclusive("rbac", f"baseline {home} in {ns}", "empty-listing")
    accounts = []
    for role in contract.roles.values():
        if not role.f9 or role.service_account is None:
            continue
        user = sa_user(role.namespace, role.service_account)
        listed = {ns: can_i_list(kube, user, ns) for ns in namespaces}
        excess = {ns: sorted(set(rules) - set(baseline[role.namespace][ns]))
                  for ns, rules in listed.items()}
        for ns, rules in listed.items():
            if not rules:
                findings.inconclusive("rbac", f"{role.name} in {ns}", "empty-listing")
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
    return {"baseline": baseline, "service_accounts": accounts, "manager": manager}


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
                if role is None or not role.f9 or role.namespace != ns or manager is not None:
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
                expected = manager.mounted | manager.image_pull
                row["service_account"] = sa
                row["secret_refs"] = [{"name": n, "via": v, "expected": n in expected}
                                      for n, v in refs]
                for n, _ in refs:
                    if n not in expected and not manager.extra_secrets_listed:
                        findings.violation("pods", subject, f"secret-not-allowed: {n}")
            elif ns == contract.prod:
                row["class"] = "release"
                owners = meta.get("ownerReferences") or []
                row["owner_kinds"] = sorted({str(o.get("kind", "")) for o in owners})
            else:
                row["class"] = "unclassified"
                findings.violation("pods", subject, "unclassified-pod")
            rows.append(row)
    return rows, manager_sas


def check_f9_pod(contract: Contract, role: Role, subject: str, labels: Mapping[str, str],
                 spec: Mapping[str, Any], refs: list[tuple[str, str]], token: bool,
                 row: dict[str, Any], findings: Findings) -> None:
    bad_labels = sorted(
        k for k in labels
        if k in contract.forbidden_label_keys
        or any(k.startswith(p) for p in contract.forbidden_label_prefixes)
    )
    for label in bad_labels:
        findings.violation("pods", subject, f"forbidden-label: {label}")
    sa = str(spec.get("serviceAccountName") or "default")
    if sa != role.service_account:
        findings.violation("pods", subject, f"service-account: {sa}")
    if token:
        findings.violation("pods", subject, "service-account-token-mounted")
    own_pull = injected_pull_pattern(contract, sa)
    checked = []
    for name, via in refs:
        if via == "imagePullSecrets":
            ok = name in role.image_pull or (
                own_pull.fullmatch(name) is not None
                and any(p.fullmatch(name) for p in role.pull_patterns))
        else:
            ok = name in role.mounted
        checked.append({"name": name, "via": via, "allowed": ok})
        if not ok:
            findings.violation("pods", subject, f"secret-not-allowed: {name} via {via}")
    row.update({"service_account": sa, "forbidden_labels": bad_labels,
                "token_mounted": token, "secret_refs": checked})


# --------------------------------------------------------------------------- step 4


def _find_keys(node: Any, suffix: str, path: str = "") -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key).endswith(suffix):
                yield here
            yield from _find_keys(value, suffix, here)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _find_keys(item, suffix, f"{path}[{index}]")


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
    """ADR-0009 section 2 runner rules, with names from the contract's runner job role."""
    role = runner_job_role(contract)
    prefix = params.get("REGISTRY_PATH").rstrip("/") + "/"
    rows = []
    for path in paths:
        for doc in _toml_documents(path):
            overwrite = list(_find_keys(doc, OVERWRITE_SUFFIX))
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
                labels = kube.get("pod_labels") or {}
                checks: dict[str, bool] = {
                    "no_overwrite_allowed_key": not overwrite,
                    "namespace": kube.get("namespace") == role.namespace,
                    "service_account": kube.get("service_account") == role.service_account,
                    "image_pull_secrets": sorted(kube.get("image_pull_secrets") or [])
                    == sorted(role.image_pull),
                    "pod_labels_role_only": labels == {contract.role_label_key: role.name},
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
                             "overwrite_keys": overwrite, "checks": checks})
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


def check_egress_policies(contract: Contract, policies: Mapping[str, list[dict[str, Any]]],
                          findings: Findings) -> list[dict[str, Any]]:
    """The contract's default-deny and allow egress policies exist, the deny unaltered."""
    rows = []
    for ns_key, spec in _require(contract.raw.get("policies"), dict).items():
        ns = contract.namespace(ns_key)
        by_name = {str((p.get("metadata") or {}).get("name", "")): p for p in policies[ns]}
        deny = _require(spec.get("default_deny"), dict)
        expected = [str(deny.get("name"))] + [str(n) for n in spec.get("allow", [])]
        for name in expected:
            present = name in by_name
            row: dict[str, Any] = {"namespace": ns, "name": name, "present": present}
            if not present:
                findings.violation("egress-policies", f"{ns}/{name}", "missing")
            elif name == deny.get("name"):
                body = by_name[name].get("spec") or {}
                intact = ((body.get("podSelector") or {}) == deny.get("pod_selector")
                          and sorted(body.get("policyTypes") or []) == sorted(
                              deny.get("policy_types") or [])
                          and not body.get("egress"))
                row["default_deny_intact"] = intact
                if not intact:
                    findings.violation("egress-policies", f"{ns}/{name}", "default-deny-altered")
            rows.append(row)
        others = sorted(set(by_name) - set(expected))
        rows.append({"namespace": ns, "other_policies": others})
    return rows


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


def probe_identities(contract: Contract) -> list[Identity]:
    out = []
    for role in contract.roles.values():
        if role.f9:
            out.append(Identity(role.name, role.namespace, {contract.role_label_key: role.name},
                                role.service_account, role.egress))
        elif role.selector:
            # The manager's own ServiceAccount is never borrowed.
            out.append(Identity(role.name, role.namespace, {role.selector[0]: role.selector[1]},
                                None, role.egress))
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
    present = {t["class"] for t in targets}
    for cls_name in ("prod_qdrant", "prod_agent", *classes):
        if cls_name not in present:
            findings.inconclusive("probes", cls_name, "no-probe-target")
    return targets, unprobed


def probe_pod(identity: Identity, run_id: str, image: str, targets: list[dict[str, Any]],
              resources: Mapping[str, str], deadline: int) -> dict[str, Any]:
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
            "metadata": {"name": f"mfr-proof-{identity.name}-{run_id}",
                         "labels": {**identity.labels, PROOF_LABEL: run_id}},
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
            if set(out) != ids:
                raise ProofError("probe-failed", name)
            return out
    raise ProofError("probe-failed", name)


def run_probes(kube: Kube, contract: Contract, params: Params, run_id: str,
               args: argparse.Namespace, findings: Findings) -> dict[str, Any]:
    image = probe_image(contract, params)
    extra: dict[str, list[str]] = {}
    for item in args.probe_host:
        cls, _, host = item.partition("=")
        extra.setdefault(cls, []).append(host)
    targets, unprobed = probe_targets(kube, contract, params, args.agent_service, extra,
                            args.probe_connect_timeout, findings)
    resources = {"cpu": args.probe_cpu, "memory": args.probe_memory,
                 "ephemeral-storage": args.probe_ephemeral_storage}
    identities = probe_identities(contract)
    pods = []
    for identity in identities:
        pod = probe_pod(identity, run_id, image, targets, resources, int(args.probe_timeout))
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


def sweep(kube: Kube, namespaces: Iterable[str], selector: str) -> dict[str, int]:
    removed = {}
    for ns in namespaces:
        proc = kube.run(["delete", "pods,serviceaccounts", "-l", selector, "-n", ns,
                         "--ignore-not-found", "--wait=true", "-o", "name"], "delete probe objects")
        removed[ns] = sum(1 for line in proc.stdout.splitlines() if line.strip())
    return removed


def cleanup(kube: Kube, contract: Contract, run_id: str) -> dict[str, Any]:
    namespaces = (contract.prod, contract.ci)
    deleted = sweep(kube, namespaces, f"{PROOF_LABEL}={run_id}")
    remaining = {}
    for ns in namespaces:
        remaining[ns] = len(kube.items(["get", "pods,serviceaccounts", "-l",
                                        f"{PROOF_LABEL}={run_id}", "-n", ns],
                                       "verify cleanup"))
    if any(remaining.values()):
        raise ProofError("cleanup-failed")
    return {"deleted": deleted, "remaining": remaining}


def verdict_of(findings: Findings, error: ProofError | None, probed: bool) -> tuple[str, int]:
    if findings.count("violation"):
        return "violation", EXIT_VIOLATION
    if error is not None:
        return "error", EXIT_ERROR
    if findings.count("inconclusive"):
        return "inconclusive", EXIT_ERROR
    return ("proven", EXIT_PROVEN) if probed else ("proven-static-only", EXIT_PROVEN)


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
        record["leftovers_removed"] = sweep(kube, (contract.prod, contract.ci), PROOF_LABEL)
        pods = {ns: kube.items(["get", "pods", "-n", ns], "get pods")
                for ns in (contract.prod, contract.ci)}
        checks["pods"], manager_sas = classify_pods(contract, pods, findings)
        if not manager_sas:
            findings.inconclusive("pods", contract.ci, "no-runner-manager-pod")
        checks["rbac"] = check_rbac(kube, contract, run_id, manager_sas, findings)
        checks["negative_matrix"] = check_negative_matrix(kube, contract, findings)
        policies = {ns: kube.items(["get", "networkpolicies", "-n", ns], "get networkpolicies")
                    for ns in (contract.prod, contract.ci)}
        checks["egress_policies"] = check_egress_policies(contract, policies, findings)
        checks["agent_policy"] = check_agent_policy(kube, contract, args.agent_service,
                                                    pods[contract.prod], policies[contract.prod],
                                                    findings)
        if args.probe:
            checks["probes"] = run_probes(kube, contract, params, run_id, args, findings)
        else:
            checks["probes"] = {"status": "not-run"}
    except ProofError as exc:
        error = exc
    finally:
        if kube is not None and contract is not None:
            try:
                record["cleanup"] = cleanup(kube, contract, run_id)
            except ProofError as exc:
                record["cleanup"] = {"status": "failed"}
                error = error or exc
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
