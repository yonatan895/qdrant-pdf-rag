"""F9 workspace manifests (deploy/workspace/) against their contract (GH#1118, ADR-0009 §2/§8).

The operator renders the templates with `oc process --local -f <template> -p ...`.
The renderer below reproduces that command's parameter semantics on its own,
independent of any shipped code: `${P}` substitutes a string, `${{P}}` a JSON
value (a value that is not JSON stays a string), required parameters must be
non-empty, defaults apply, an unknown `-p` name is an error and a reference to
an undeclared name stays literal. Every check runs on rendered objects; the
negative controls mutate a rendered object and show that the same check fails.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "deploy" / "workspace"
CONTRACT: dict[str, Any] = json.loads((WORKSPACE / "contract.json").read_text(encoding="utf-8"))
LABEL_KEY = "workspace.mainframe-rag.io/role"
SESSION_DIR = "/run/mfr-session"

_STRING_PARAM = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
_JSON_PARAM = re.compile(r"\$\{\{([A-Za-z0-9_]+)\}\}")
# <registry host[:port]>/<path>@sha256:<64 lowercase hex>, nothing else.
_DIGEST_REF = re.compile(
    r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$"
)

Obj = dict[str, Any]


class TemplateError(ValueError):
    pass


def render(template: Obj, params: dict[str, str]) -> list[Obj]:
    declared: dict[str, Obj] = {}
    for parameter in template.get("parameters", []):
        if "generate" in parameter:
            raise TemplateError(f"generated parameter {parameter['name']} is not supported")
        declared[parameter["name"]] = parameter
    unknown = sorted(set(params) - set(declared))
    if unknown:
        raise TemplateError(f"unknown parameter name {unknown[0]!r}")
    values: dict[str, str] = {}
    for name, parameter in declared.items():
        value = params.get(name, str(parameter.get("value", "")))
        if parameter.get("required") and value == "":
            raise TemplateError(f"parameter {name} is required and must be specified")
        values[name] = value

    def substitute(text: str) -> Any:
        out = _STRING_PARAM.sub(lambda m: values.get(m.group(1), m.group(0)), text)
        as_json = False

        def json_value(match: re.Match[str]) -> str:
            nonlocal as_json
            if match.group(1) not in values:
                return match.group(0)
            as_json = True
            return values[match.group(1)]

        out = _JSON_PARAM.sub(json_value, out)
        if not as_json:
            return out
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return substitute(node)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {substitute(k) if isinstance(k, str) else k: walk(v) for k, v in node.items()}
        return node

    return [walk(obj) for obj in template["objects"]]


def load_template(namespace_key: str) -> Obj:
    path = ROOT / CONTRACT["namespaces"][namespace_key]["template"]
    loaded: Obj = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


DIGEST = "sha256:" + "0123456789abcdef" * 4
REGISTRY_PATH = "registry.site.example:5000/mfr"


def _peers(*cidrs: str) -> str:
    return json.dumps([{"ipBlock": {"cidr": c}} for c in cidrs])


COMMON_PARAMS = {
    "DEVKIT_REPOSITORY": f"{REGISTRY_PATH}/devkit",
    "DEVKIT_DIGEST": DIGEST,
    "STORAGE_CLASS": "block-rwo",
    "API_SERVER_CIDRS": _peers("10.0.0.10/32", "10.0.0.11/32"),
    "GITLAB_CIDRS": _peers("10.2.0.7/32"),
}
PARAMS: dict[str, dict[str, str]] = {
    "prod": {
        **COMMON_PARAMS,
        "PROD_NAMESPACE": "site-rag",
        "PULL_SECRET": "mfr-prod-pull",
        "REGISTRY_CIDRS": _peers("10.1.0.5/32"),
    },
    "ci": {
        **COMMON_PARAMS,
        "CI_NAMESPACE": "site-rag-ci",
        "PULL_SECRET": "mfr-ci-pull",
        "QDRANT_REPOSITORY": f"{REGISTRY_PATH}/qdrant",
        "QDRANT_DIGEST": DIGEST,
        "LITELLM_CIDRS": _peers("10.3.0.9/32", "10.3.1.0/24"),
        "LITELLM_PORT": "4000",
        "MANAGER_POD_LABEL_KEY": "runner.site.example/manager",
        "MANAGER_POD_LABEL_VALUE": "mfr",
    },
}
NAMESPACES = ("prod", "ci")


def rendered(namespace_key: str, **overrides: str) -> list[Obj]:
    return render(load_template(namespace_key), {**PARAMS[namespace_key], **overrides})


def _of_kind(objects: list[Obj], kind: str) -> list[Obj]:
    return [o for o in objects if o["kind"] == kind]


def _named(objects: list[Obj], kind: str, name: str) -> Obj:
    matches = [o for o in _of_kind(objects, kind) if o["metadata"]["name"] == name]
    assert len(matches) == 1, f"{kind}/{name}: {len(matches)} objects"
    return matches[0]


def _roles_in(namespace_key: str) -> list[str]:
    return [r for r, spec in CONTRACT["roles"].items() if spec["namespace"] == namespace_key]


# --- checks shared by the positive tests and the negative controls -------------


def forbidden_label_violations(obj: Obj) -> list[str]:
    labels = dict(obj.get("metadata", {}).get("labels") or {})
    spec = obj.get("spec") or {}
    labels.update((spec.get("podSelector") or {}).get("matchLabels") or {})
    bad = [
        k
        for k in labels
        if k in CONTRACT["forbidden_label_keys"]
        or any(k.startswith(p) for p in CONTRACT["forbidden_label_prefixes"])
    ]
    return [f"{obj['kind']}/{obj['metadata']['name']}: forbidden label {k}" for k in bad]


def _containers(spec: Obj) -> list[Obj]:
    return list(spec.get("containers") or []) + list(spec.get("initContainers") or [])


def secret_references(spec: Obj) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for volume in spec.get("volumes") or []:
        if "secret" in volume:
            refs.add(("volume", volume["secret"]["secretName"]))
        for source in (volume.get("projected") or {}).get("sources") or []:
            if "secret" in source:
                refs.add(("volume", source["secret"]["name"]))
    for container in _containers(spec):
        for env in container.get("env") or []:
            key_ref = (env.get("valueFrom") or {}).get("secretKeyRef")
            if key_ref:
                refs.add(("env", key_ref["name"]))
        for env_from in container.get("envFrom") or []:
            if "secretRef" in env_from:
                refs.add(("env", env_from["secretRef"]["name"]))
    for pull in spec.get("imagePullSecrets") or []:
        refs.add(("imagePullSecrets", pull["name"]))
    return refs


def allowed_image_pull_names(role: str, params: dict[str, str]) -> set[str]:
    allowed = CONTRACT["roles"][role]["allowed_secret_refs"]
    return {params[p] for p in allowed["image_pull_parameters"]}


def secret_ref_violations(spec: Obj, role: str, params: dict[str, str]) -> list[str]:
    """Mounts (volumes, env, envFrom) must equal the contract; image pulls are a subset."""
    allowed = CONTRACT["roles"][role]["allowed_secret_refs"]
    refs = secret_references(spec)
    mounted = {name for kind, name in refs if kind != "imagePullSecrets"}
    pulls = {name for kind, name in refs if kind == "imagePullSecrets"}
    out = []
    want = set(allowed["mounted"]) | {params[p] for p in allowed["mounted_parameters"]}
    if mounted != want:
        out.append(f"mounted secret references {sorted(mounted)}")
    for name in sorted(pulls - allowed_image_pull_names(role, params)):
        if not any(re.fullmatch(pattern, name) for pattern in allowed["image_pull_patterns"]):
            out.append(f"image pull secret {name} is not allowed")
    return out


def pod_violations(pod: Obj, role: str, params: dict[str, str]) -> list[str]:
    contract = CONTRACT["roles"][role]
    spec = pod["spec"]
    out: list[str] = []
    if (pod["metadata"].get("labels") or {}) != {LABEL_KEY: role}:
        out.append(f"labels are {pod['metadata'].get('labels')}, not only the {role} role")
    out.extend(forbidden_label_violations(pod))
    if spec.get("serviceAccountName") != contract["service_account"]:
        out.append(f"serviceAccountName {spec.get('serviceAccountName')}")
    if spec.get("automountServiceAccountToken") is not False:
        out.append("pod automountServiceAccountToken is not false")
    out.extend(secret_ref_violations(spec, role, params))
    for volume in spec.get("volumes") or []:
        if "hostPath" in volume:
            out.append(f"hostPath volume {volume['name']}")
        for source in (volume.get("projected") or {}).get("sources") or []:
            if "serviceAccountToken" in source:
                out.append(f"projected token in {volume['name']}")
    for host in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(host):
            out.append(host)
    pod_sc = spec.get("securityContext") or {}
    if pod_sc.get("runAsNonRoot") is not True:
        out.append("pod runAsNonRoot is not true")
    for container in _containers(spec):
        name = container["name"]
        sc = container.get("securityContext") or {}
        for fixed in ("runAsUser", "runAsGroup"):
            if fixed in sc or fixed in pod_sc:
                out.append(f"{name}: fixed {fixed}")
        if "fsGroup" in pod_sc:
            out.append("fixed fsGroup")
        if sc.get("privileged"):
            out.append(f"{name}: privileged")
        if sc.get("allowPrivilegeEscalation") is not False:
            out.append(f"{name}: allowPrivilegeEscalation is not false")
        caps = sc.get("capabilities") or {}
        if caps.get("drop") != ["ALL"] or caps.get("add"):
            out.append(f"{name}: capabilities {caps}")
        seccomp = (sc.get("seccompProfile") or pod_sc.get("seccompProfile") or {}).get("type")
        if seccomp != "RuntimeDefault":
            out.append(f"{name}: seccomp {seccomp}")
        if sc.get("runAsNonRoot") is False:
            out.append(f"{name}: runAsNonRoot false")
        resources = container.get("resources") or {}
        for bound in ("requests", "limits"):
            for resource in ("cpu", "memory"):
                if not (resources.get(bound) or {}).get(resource):
                    out.append(f"{name}: no {resource} {bound}")
        if not _DIGEST_REF.match(container.get("image", "")):
            out.append(f"{name}: image {container.get('image')!r} is not digest-pinned")
    return out


def service_account_violations(sa: Obj, role: str) -> list[str]:
    out = forbidden_label_violations(sa)
    if (sa["metadata"].get("labels") or {}) != {LABEL_KEY: role}:
        out.append(f"ServiceAccount labels {sa['metadata'].get('labels')}")
    if sa.get("automountServiceAccountToken") is not False:
        out.append("ServiceAccount automountServiceAccountToken is not false")
    for field in ("secrets", "imagePullSecrets"):
        if sa.get(field):
            out.append(f"ServiceAccount {field} {sa[field]}")
    return out


def _expected_rule(class_name: str, params: dict[str, str]) -> Obj:
    spec = CONTRACT["egress_classes"][class_name]
    ports = []
    for port in spec["ports"]:
        if "port_parameter" in port:
            number = int(params.get(port["port_parameter"], port.get("default", -1)))
        else:
            number = port["port"]
        ports.append({"protocol": port["protocol"], "port": number})
    if spec["peer_kind"] == "ipBlock":
        peers = json.loads(params[spec["peers_parameter"]])
    else:
        peers = [{"namespaceSelector": spec["namespace_selector"]}]
    return {"to": peers, "ports": ports}


def _role_selector(role: str, params: dict[str, str]) -> Obj:
    selector = CONTRACT["roles"][role].get("pod_selector_parameters")
    if selector:
        key, value = params[selector["key"]], params[selector["value"]]
        return {"matchExpressions": [{"key": key, "operator": "In", "values": [value]}]}
    return {"matchLabels": {LABEL_KEY: role}}


def allow_policy_violations(policy: Obj, role: str, params: dict[str, str]) -> list[str]:
    contract = CONTRACT["roles"][role]
    spec = policy["spec"]
    out = forbidden_label_violations(policy)
    if spec.get("podSelector") != _role_selector(role, params):
        out.append(f"podSelector {spec.get('podSelector')}")
    if spec.get("policyTypes") != ["Egress"] or "ingress" in spec:
        out.append(f"policyTypes {spec.get('policyTypes')}")
    expected = {c: _expected_rule(c, params) for c in contract["egress"]}
    matched: list[str] = []
    for rule in spec.get("egress") or []:
        classes = [c for c, want in expected.items() if rule == want]
        if len(classes) != 1:
            out.append(f"egress rule maps to no contract class of {role}: {rule}")
            continue
        matched.append(classes[0])
        if CONTRACT["egress_classes"][classes[0]]["peer_kind"] == "ipBlock":
            if not isinstance(rule.get("to"), list) or not rule["to"]:
                # An empty or absent `to` allows every destination.
                out.append(f"{classes[0]}: no peers, which allows every destination")
                continue
            for peer in rule["to"]:
                if (
                    not isinstance(peer, dict)
                    or set(peer) != {"ipBlock"}
                    or set(peer["ipBlock"]) != {"cidr"}
                ):
                    out.append(f"{classes[0]}: peer {peer} is not a plain ipBlock")
                    continue
                try:
                    ipaddress.ip_network(peer["ipBlock"]["cidr"], strict=False)
                except ValueError:
                    out.append(f"{classes[0]}: invalid cidr {peer['ipBlock']['cidr']!r}")
    if sorted(matched) != sorted(contract["egress"]):
        out.append(f"egress classes {sorted(matched)}, contract {sorted(contract['egress'])}")
    return out


def deny_policy_violations(policy: Obj, namespace_key: str) -> list[str]:
    contract = CONTRACT["policies"][namespace_key]["default_deny"]
    spec = policy["spec"]
    out = forbidden_label_violations(policy)
    if spec.get("podSelector") != contract["pod_selector"]:
        out.append(f"default deny selects {spec.get('podSelector')}")
    if spec.get("policyTypes") != contract["policy_types"]:
        out.append(f"default deny policyTypes {spec.get('policyTypes')}")
    if spec.get("egress") or "ingress" in spec:
        out.append("default deny allows traffic")
    return out


def _session_mounts(objects: list[Obj]) -> list[str]:
    found = []
    for pod in _of_kind(objects, "Pod"):
        for container in _containers(pod["spec"]):
            for mount in container.get("volumeMounts") or []:
                path = mount["mountPath"].rstrip("/")
                if path == SESSION_DIR or path.startswith(SESSION_DIR + "/"):
                    found.append(f"{pod['metadata']['name']}/{container['name']}")
    return found


# --- the renderer reproduces `oc process --local` ------------------------------


_MINI = {
    "parameters": [
        {"name": "NAME", "required": True},
        {"name": "PORT", "value": "6443"},
        {"name": "PEERS", "required": True},
        {"name": "OPTIONAL"},
    ],
    "objects": [
        {
            "kind": "X",
            "metadata": {"name": "x-${NAME}", "labels": {"${NAME}": "v"}},
            "spec": {
                "port": "${{PORT}}",
                "to": "${{PEERS}}",
                "plain": "${PORT}",
                "optional": "${OPTIONAL}",
                "undeclared": "${UNDECLARED}",
            },
        }
    ],
}


def test_renderer_reproduces_oc_process_parameter_semantics():
    [obj] = render(_MINI, {"NAME": "a", "PEERS": '[{"ipBlock": {"cidr": "10.0.0.1/32"}}]'})
    assert obj["metadata"] == {"name": "x-a", "labels": {"a": "v"}}
    assert obj["spec"] == {
        "port": 6443,
        "to": [{"ipBlock": {"cidr": "10.0.0.1/32"}}],
        "plain": "6443",
        "optional": "",
        "undeclared": "${UNDECLARED}",
    }
    [obj] = render(_MINI, {"NAME": "a", "PEERS": "notjson", "PORT": "7"})
    assert obj["spec"]["to"] == "notjson" and obj["spec"]["port"] == 7


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"PEERS": "[]"}, "parameter NAME is required"),
        ({"NAME": "", "PEERS": "[]"}, "parameter NAME is required"),
        ({"NAME": "a", "PEERS": "[]", "Z": "1"}, "unknown parameter name 'Z'"),
    ],
)
def test_renderer_refuses_what_oc_process_refuses(params, message):
    with pytest.raises(TemplateError, match=message):
        render(_MINI, params)


# --- contract.json records the approved decisions ------------------------------


def test_contract_records_the_approved_identities_and_policies():
    roles = CONTRACT["roles"]
    assert CONTRACT["label_key"] == LABEL_KEY
    assert {r: (s["namespace"], s["service_account"]) for r, s in roles.items()} == {
        "bastion": ("prod", "mfr-bastion"),
        "dev": ("ci", "mfr-dev"),
        "runner": ("ci", "mfr-runner-job"),
        "manager": ("ci", None),
    }
    assert {r: sorted(s["egress"]) for r, s in roles.items()} == {
        "bastion": ["api_server", "dns", "gitlab", "registry"],
        "dev": ["dns", "gitlab", "litellm"],
        "runner": ["dns", "gitlab"],
        "manager": ["api_server", "dns", "gitlab"],
    }
    assert {r: s["allowed_secret_refs"] for r, s in roles.items()} == {
        "bastion": {
            "mounted": [],
            "mounted_parameters": [],
            "image_pull_parameters": ["PULL_SECRET"],
            "image_pull_patterns": ["^mfr-bastion-dockercfg-[a-z0-9]{5}$"],
        },
        "dev": {
            "mounted": ["mfr-dev-gitlab", "mfr-dev-litellm"],
            "mounted_parameters": [],
            "image_pull_parameters": ["PULL_SECRET"],
            "image_pull_patterns": ["^mfr-dev-dockercfg-[a-z0-9]{5}$"],
        },
        "runner": {
            "mounted": [],
            "mounted_parameters": [],
            "image_pull_parameters": ["PULL_SECRET"],
            "image_pull_patterns": ["^mfr-runner-job-dockercfg-[a-z0-9]{5}$"],
        },
        "manager": {
            "mounted": [],
            "mounted_parameters": ["RUNNER_TOKEN_SECRET"],
            "image_pull_parameters": ["PULL_SECRET"],
            "image_pull_patterns": [],
        },
    }
    for role in ("bastion", "dev", "runner"):
        assert roles[role]["rbac"] == "none"
        assert roles[role]["automount_service_account_token"] is False
    assert CONTRACT["policies"]["ci"]["default_deny"]["name"] == "mfr-ci-default-deny-egress"
    assert CONTRACT["policies"]["ci"]["default_deny"]["pod_selector"] == {}
    assert CONTRACT["policies"]["prod"]["default_deny"]["name"] == "mfr-bastion-default-deny-egress"
    assert CONTRACT["policies"]["prod"]["default_deny"]["pod_selector"] == {
        "matchLabels": {LABEL_KEY: "bastion"}
    }
    dns = CONTRACT["egress_classes"]["dns"]
    assert dns["namespace_selector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}
    }
    assert sorted((p["protocol"], p["port"]) for p in dns["ports"]) == [
        ("TCP", 53),
        ("TCP", 5353),
        ("UDP", 53),
        ("UDP", 5353),
    ]
    assert CONTRACT["egress_classes"]["api_server"]["ports"][0]["default"] == 6443
    assert {k: v["concurrent"] for k, v in CONTRACT["runner_registrations"].items()} == {
        "gate": 1,
        "wip": 3,
    }
    assert CONTRACT["bastion_pod"]["session"]["credentials_dir"] == SESSION_DIR + "/private"


# --- rendering and the contract ------------------------------------------------


@pytest.mark.parametrize("namespace_key", NAMESPACES)
def test_template_parameters_are_declared_used_and_fully_rendered(namespace_key):
    template = load_template(namespace_key)
    assert "labels" not in template, "template labels would be added to every object"
    declared = {p["name"] for p in template["parameters"]}
    raw = json.dumps(template["objects"])
    referenced = set(_STRING_PARAM.findall(raw)) | set(_JSON_PARAM.findall(raw))
    assert referenced == declared
    assert CONTRACT["namespaces"][namespace_key]["parameter"] in declared
    objects = rendered(namespace_key)
    assert "${" not in json.dumps(objects)
    namespace = PARAMS[namespace_key][CONTRACT["namespaces"][namespace_key]["parameter"]]
    assert {o["metadata"]["namespace"] for o in objects} == {namespace}
    assert {o["kind"] for o in objects} <= {
        "NetworkPolicy",
        "ServiceAccount",
        "PersistentVolumeClaim",
        "Pod",
    }, "F9 roles hold no RBAC objects"


@pytest.mark.parametrize("namespace_key", NAMESPACES)
def test_policies_are_applied_before_any_pod(namespace_key):
    kinds = [o["kind"] for o in rendered(namespace_key)]
    last_policy = max(i for i, k in enumerate(kinds) if k == "NetworkPolicy")
    first_pod = min(i for i, k in enumerate(kinds) if k == "Pod")
    assert last_policy < first_pod


@pytest.mark.parametrize("namespace_key", NAMESPACES)
def test_every_f9_pod_and_service_account_meets_the_contract(namespace_key):
    objects = rendered(namespace_key)
    params = PARAMS[namespace_key]
    pods = _of_kind(objects, "Pod")
    expected_pods = {p for r in _roles_in(namespace_key) for p in CONTRACT["roles"][r]["pods"]}
    assert {p["metadata"]["name"] for p in pods} == expected_pods
    for pod in pods:
        role = pod["metadata"]["labels"][LABEL_KEY]
        assert pod_violations(pod, role, params) == []
    template_sas = {
        CONTRACT["roles"][r]["service_account"]: r
        for r in _roles_in(namespace_key)
        if CONTRACT["roles"][r]["service_account_owner"] == "template"
    }
    service_accounts = _of_kind(objects, "ServiceAccount")
    assert {sa["metadata"]["name"] for sa in service_accounts} == set(template_sas)
    for sa in service_accounts:
        assert service_account_violations(sa, template_sas[sa["metadata"]["name"]]) == []
    for obj in objects:
        assert forbidden_label_violations(obj) == []


@pytest.mark.parametrize("namespace_key", NAMESPACES)
def test_network_policies_match_the_contract(namespace_key):
    objects = rendered(namespace_key)
    params = PARAMS[namespace_key]
    policies = {p["metadata"]["name"]: p for p in _of_kind(objects, "NetworkPolicy")}
    contract = CONTRACT["policies"][namespace_key]
    assert set(policies) == {contract["default_deny"]["name"], *contract["allow"]}
    assert deny_policy_violations(policies[contract["default_deny"]["name"]], namespace_key) == []
    for role in _roles_in(namespace_key):
        name = CONTRACT["roles"][role]["allow_policy"]
        assert name in contract["allow"]
        assert allow_policy_violations(policies[name], role, params) == []


@pytest.mark.parametrize("namespace_key", NAMESPACES)
def test_dns_is_reachable_on_53_and_5353_from_every_role(namespace_key):
    objects = rendered(namespace_key)
    for role in _roles_in(namespace_key):
        policy = _named(objects, "NetworkPolicy", CONTRACT["roles"][role]["allow_policy"])
        dns_rules = [
            r
            for r in policy["spec"]["egress"]
            if r["to"]
            == [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}
                    }
                }
            ]
        ]
        assert len(dns_rules) == 1, role
        assert sorted((p["protocol"], p["port"]) for p in dns_rules[0]["ports"]) == [
            ("TCP", 53),
            ("TCP", 5353),
            ("UDP", 53),
            ("UDP", 5353),
        ]


# --- the bastion ---------------------------------------------------------------


def test_bastion_session_directory_is_memory_only_and_unique_to_the_bastion():
    prod, ci = rendered("prod"), rendered("ci")
    pod = _named(prod, "Pod", "mfr-bastion")
    [container] = pod["spec"]["containers"]
    mounts = {m["mountPath"]: m["name"] for m in container["volumeMounts"]}
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    session = volumes[mounts[SESSION_DIR]]
    assert session["emptyDir"]["medium"] == "Memory"
    assert session["emptyDir"]["sizeLimit"]
    assert _session_mounts(prod + ci) == ["mfr-bastion/bastion"]


def test_bastion_has_the_ca_namespace_and_work_claim():
    objects = rendered("prod")
    pod = _named(objects, "Pod", "mfr-bastion")
    [container] = pod["spec"]["containers"]
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    ca = CONTRACT["bastion_pod"]["kube_root_ca"]
    assert mounts[ca["mount_path"]]["readOnly"] is True
    ca_volume = volumes[mounts[ca["mount_path"]]["name"]]["configMap"]
    assert ca_volume["name"] == ca["config_map"]
    assert [item["key"] for item in ca_volume["items"]] == [ca["key"]]
    env = {e["name"]: e for e in container["env"]}
    assert env["MFR_NAMESPACE"]["valueFrom"] == {"fieldRef": {"fieldPath": "metadata.namespace"}}
    work = CONTRACT["bastion_pod"]["work_claim"]
    claim = volumes[mounts[work["mount_path"]]["name"]]["persistentVolumeClaim"]["claimName"]
    assert claim == work["name"]
    pvc = _named(objects, "PersistentVolumeClaim", work["name"])
    assert pvc["spec"]["accessModes"] == work["access_modes"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == work["size"]
    assert pvc["spec"]["storageClassName"] == PARAMS["prod"]["STORAGE_CLASS"]
    assert container["command"], "a long-running command keeps `oc exec` available"


IMAGES = [("prod", "DEVKIT", "bastion"), ("ci", "DEVKIT", "dev"), ("ci", "QDRANT", "qdrant")]


@pytest.mark.parametrize(("namespace_key", "prefix", "container"), IMAGES)
def test_image_parameters_are_required_with_no_default(namespace_key, prefix, container):
    # A Template cannot refuse a value (a parameter's `from` only feeds
    # `generate`), so the image always renders as <repository>@<digest>:
    # a tag in either parameter yields a reference that is not runnable.
    template = load_template(namespace_key)
    parameters = {p["name"]: p for p in template["parameters"]}
    for name in (f"{prefix}_REPOSITORY", f"{prefix}_DIGEST"):
        assert parameters[name].get("required") is True and "value" not in parameters[name]
    pod = _of_kind(rendered(namespace_key), "Pod")[0]
    [image] = [c["image"] for c in pod["spec"]["containers"] if c["name"] == container]
    params = PARAMS[namespace_key]
    assert image == f"{params[f'{prefix}_REPOSITORY']}@{params[f'{prefix}_DIGEST']}"


@pytest.mark.parametrize(("namespace_key", "prefix", "container"), IMAGES)
@pytest.mark.parametrize(
    ("field", "bad"),
    [
        pytest.param("DIGEST", "latest", id="tag-as-digest"),
        pytest.param("DIGEST", "0123456789abcdef" * 4, id="digest-without-algorithm"),
        pytest.param("DIGEST", "sha256:" + "0" * 63, id="short-digest"),
        pytest.param("DIGEST", "sha256:" + "A" * 64, id="uppercase-digest"),
        pytest.param("REPOSITORY", f"{REGISTRY_PATH}/devkit:v1", id="repository-with-tag"),
        pytest.param("REPOSITORY", f"{REGISTRY_PATH}/devkit@{DIGEST}", id="repository-with-digest"),
        pytest.param("REPOSITORY", "devkit", id="repository-without-registry"),
    ],
)
def test_image_is_refused_unless_repository_at_digest(namespace_key, prefix, container, field, bad):
    params = {**PARAMS[namespace_key], f"{prefix}_{field}": bad}
    pod = _of_kind(render(load_template(namespace_key), params), "Pod")[0]
    image = f"{params[f'{prefix}_REPOSITORY']}@{params[f'{prefix}_DIGEST']}"
    role = pod["metadata"]["labels"][LABEL_KEY]
    assert pod_violations(pod, role, params) == [
        f"{container}: image {image!r} is not digest-pinned"
    ]


@pytest.mark.parametrize("namespace_key", NAMESPACES)
def test_pods_pull_with_the_pull_secret_as_a_kubelet_credential_only(namespace_key):
    params = PARAMS[namespace_key]
    for pod in _of_kind(rendered(namespace_key), "Pod"):
        assert pod["spec"]["imagePullSecrets"] == [{"name": params["PULL_SECRET"]}]
        mounted = {n for k, n in secret_references(pod["spec"]) if k != "imagePullSecrets"}
        assert params["PULL_SECRET"] not in mounted


def test_dev_pod_mounts_its_two_secrets_read_only_beside_a_localhost_qdrant():
    objects = rendered("ci")
    pod = _named(objects, "Pod", "mfr-dev")
    containers = {c["name"]: c for c in pod["spec"]["containers"]}
    assert set(containers) == {"dev", "qdrant"}
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    secret_mounts = [m for m in containers["dev"]["volumeMounts"] if "secret" in volumes[m["name"]]]
    assert sorted(volumes[m["name"]]["secret"]["secretName"] for m in secret_mounts) == [
        "mfr-dev-gitlab",
        "mfr-dev-litellm",
    ]
    assert all(m.get("readOnly") is True for m in secret_mounts)
    assert not any("secret" in volumes[m["name"]] for m in containers["qdrant"]["volumeMounts"])
    qdrant_env = {e["name"]: e["value"] for e in containers["qdrant"]["env"]}
    assert qdrant_env["QDRANT__SERVICE__HOST"] == "127.0.0.1"
    dev_env = {e["name"]: e["value"] for e in containers["dev"]["env"]}
    assert dev_env["QDRANT_URL"] == "http://127.0.0.1:6333"
    work = CONTRACT["dev_pod"]["work_claim"]
    pvc = _named(objects, "PersistentVolumeClaim", work["name"])
    assert pvc["spec"]["resources"]["requests"]["storage"] == work["size"]
    assert pvc["spec"]["accessModes"] == work["access_modes"]


# --- runner registrations ------------------------------------------------------


RUNNER_PARAMS = {
    "CI_NAMESPACE": PARAMS["ci"]["CI_NAMESPACE"],
    "PULL_SECRET": "mfr-ci-pull",
    "REGISTRY_PATH": REGISTRY_PATH,
    "DEVKIT_REPOSITORY": f"{REGISTRY_PATH}/devkit",
    "DEVKIT_DIGEST": DIGEST,
    "RUNNER_HELPER_REPOSITORY": f"{REGISTRY_PATH}/gitlab-runner-helper",
    "RUNNER_HELPER_DIGEST": DIGEST,
    **{
        f"{part}_{resource}_{bound}": "1" if resource == "CPU" else "2Gi"
        for part in ("JOB", "HELPER", "SERVICE")
        for resource in ("CPU", "MEMORY", "EPHEMERAL_STORAGE")
        for bound in ("REQUEST", "LIMIT")
    },
}


def render_runner(text: str, params: dict[str, str]) -> Obj:
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    placeholders = set(_STRING_PARAM.findall(body))
    assert placeholders == set(params), sorted(placeholders ^ set(params))
    return tomllib.loads(_STRING_PARAM.sub(lambda m: params[m.group(1)], body))


def _keys(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [k for k in node] + [k for v in node.values() for k in _keys(v)]
    if isinstance(node, list):
        return [k for item in node for k in _keys(item)]
    return []


def runner_violations(config: Obj, registration: str, params: dict[str, str]) -> list[str]:
    out = [f"override key {k}" for k in _keys(config) if "overwrite" in k]
    concurrent = CONTRACT["runner_registrations"][registration]["concurrent"]
    if config.get("concurrent") != concurrent:
        out.append(f"concurrent {config.get('concurrent')}")
    runners = config.get("runners") or []
    if len(runners) != 1:
        return [*out, f"{len(runners)} [[runners]] sections"]
    runner = runners[0]
    if runner.get("limit") != concurrent or runner.get("executor") != "kubernetes":
        out.append(f"limit {runner.get('limit')} executor {runner.get('executor')}")
    k8s = runner.get("kubernetes") or {}
    runner_role = CONTRACT["roles"]["runner"]
    if k8s.get("namespace") != params["CI_NAMESPACE"]:
        out.append(f"namespace {k8s.get('namespace')}")
    if k8s.get("service_account") != runner_role["service_account"]:
        out.append(f"service_account {k8s.get('service_account')}")
    if k8s.get("automount_service_account_token") is not False:
        out.append("automount_service_account_token is not false")
    if k8s.get("privileged") is not False or k8s.get("allow_privilege_escalation") is not False:
        out.append("privileged or privilege escalation not false")
    if k8s.get("pod_labels") != {LABEL_KEY: "runner"}:
        out.append(f"pod_labels {k8s.get('pod_labels')}")
    if set(k8s.get("image_pull_secrets") or []) != allowed_image_pull_names("runner", params):
        out.append(f"image_pull_secrets {k8s.get('image_pull_secrets')}")
    for field in ("allowed_images", "allowed_services"):
        allowed = k8s.get(field) or []
        if not allowed or not all(a.startswith(params["REGISTRY_PATH"] + "/") for a in allowed):
            out.append(f"{field} {allowed}")
    for field in ("image", "helper_image"):
        if not _DIGEST_REF.match(k8s.get(field, "")):
            out.append(f"{field} is not digest-pinned")
    for prefix in ("", "helper_", "service_"):
        for resource in ("cpu", "memory", "ephemeral_storage"):
            for bound in ("request", "limit"):
                if not k8s.get(f"{prefix}{resource}_{bound}"):
                    out.append(f"no {prefix}{resource}_{bound}")
    return out


def _runner_text(registration: str) -> str:
    path = ROOT / CONTRACT["runner_registrations"][registration]["config"]
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("registration", ["gate", "wip"])
def test_runner_configuration_allows_no_override(registration):
    config = render_runner(_runner_text(registration), RUNNER_PARAMS)
    assert runner_violations(config, registration, RUNNER_PARAMS) == []
    assert config["concurrent"] == {"gate": 1, "wip": 3}[registration]
    assert config["runners"][0]["kubernetes"]["service_account"] == "mfr-runner-job"
    assert config["runners"][0]["kubernetes"]["privileged"] is False


def test_runner_registrations_differ_only_in_name_and_slots():
    gate, wip = (render_runner(_runner_text(r), RUNNER_PARAMS) for r in ("gate", "wip"))
    for config in (gate, wip):
        config.pop("concurrent")
        config["runners"][0].pop("name")
        config["runners"][0].pop("limit")
    assert gate == wip


# --- negative controls ---------------------------------------------------------


def _bastion() -> Obj:
    return copy.deepcopy(_named(rendered("prod"), "Pod", "mfr-bastion"))


def _mutated_bastion_violations(mutate: Any) -> list[str]:
    pod = _bastion()
    mutate(pod)
    return pod_violations(pod, "bastion", PARAMS["prod"])


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda p: p["metadata"]["labels"].update({"app": "rag-agent"}), "forbidden label app"),
        (
            lambda p: p["metadata"]["labels"].update({"app.kubernetes.io/name": "qdrant"}),
            "forbidden label app.kubernetes.io/name",
        ),
        (
            lambda p: p["spec"]["volumes"].append({"name": "k", "secret": {"secretName": "x"}}),
            "mounted secret references",
        ),
        (
            lambda p: (
                p["spec"]["containers"][0]
                .setdefault("envFrom", [])
                .append({"secretRef": {"name": "rag-qdrant-apikey"}})
            ),
            "mounted secret references",
        ),
        (
            lambda p: p["spec"]["volumes"].append(
                {"name": "pull", "secret": {"secretName": "mfr-prod-pull"}}
            ),
            "mounted secret references",
        ),
        (
            lambda p: p["spec"]["imagePullSecrets"].append({"name": "push"}),
            "image pull secret push is not allowed",
        ),
        (
            lambda p: p["spec"]["imagePullSecrets"].append({"name": "mfr-dev-dockercfg-x7k2p"}),
            "image pull secret mfr-dev-dockercfg-x7k2p is not allowed",
        ),
        (
            lambda p: p["spec"]["volumes"].append(
                {"name": "cfg", "secret": {"secretName": "mfr-bastion-dockercfg-x7k2p"}}
            ),
            "mounted secret references",
        ),
        (lambda p: p["spec"].pop("automountServiceAccountToken"), "automountServiceAccountToken"),
        (lambda p: p["spec"].update({"serviceAccountName": "default"}), "serviceAccountName"),
        (
            lambda p: p["spec"]["containers"][0]["resources"].pop("limits"),
            "no cpu limits",
        ),
        (
            lambda p: p["spec"]["containers"][0]["securityContext"].update({"runAsUser": 0}),
            "fixed runAsUser",
        ),
        (
            lambda p: p["spec"]["containers"][0]["securityContext"]["capabilities"].update(
                {"add": ["NET_RAW"]}
            ),
            "capabilities",
        ),
    ],
)
def test_pod_check_catches_a_mutated_bastion(mutate, expected):
    violations = _mutated_bastion_violations(mutate)
    assert any(expected in v for v in violations), violations


def test_injected_dockercfg_pull_secret_of_the_own_service_account_is_allowed():
    pod = _bastion()
    pod["spec"]["imagePullSecrets"].append({"name": "mfr-bastion-dockercfg-x7k2p"})
    assert pod_violations(pod, "bastion", PARAMS["prod"]) == []


def test_service_account_check_catches_a_dropped_automount_false():
    sa = copy.deepcopy(_named(rendered("ci"), "ServiceAccount", "mfr-runner-job"))
    sa.pop("automountServiceAccountToken")
    assert service_account_violations(sa, "runner") == [
        "ServiceAccount automountServiceAccountToken is not false"
    ]


def test_session_check_catches_a_second_session_mount():
    ci = copy.deepcopy(rendered("ci"))
    dev = _named(ci, "Pod", "mfr-dev")
    dev["spec"]["containers"][0]["volumeMounts"].append(
        {"name": "work", "mountPath": SESSION_DIR + "/private"}
    )
    assert _session_mounts(rendered("prod") + ci) == ["mfr-bastion/bastion", "mfr-dev/dev"]


def test_policy_checks_catch_widened_egress_and_selectors():
    prod = rendered("prod")
    allow = copy.deepcopy(_named(prod, "NetworkPolicy", "mfr-bastion-egress"))
    allow["spec"]["egress"].append({"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]})
    assert any(
        "maps to no contract class" in v
        for v in allow_policy_violations(allow, "bastion", PARAMS["prod"])
    )
    deny = copy.deepcopy(_named(prod, "NetworkPolicy", "mfr-bastion-default-deny-egress"))
    deny["spec"]["podSelector"] = {}
    assert any("default deny selects" in v for v in deny_policy_violations(deny, "prod"))
    # A JSON parameter is not validated by the template: a namespace selector
    # passed as "CIDRs" would admit every pod of every namespace.
    smuggled = json.dumps([{"namespaceSelector": {}}])
    widened = _named(
        rendered("prod", API_SERVER_CIDRS=smuggled), "NetworkPolicy", "mfr-bastion-egress"
    )
    params = {**PARAMS["prod"], "API_SERVER_CIDRS": smuggled}
    assert any(
        "is not a plain ipBlock" in v for v in allow_policy_violations(widened, "bastion", params)
    )


@pytest.mark.parametrize(("namespace_key", "role"), [("prod", "bastion"), ("ci", "runner")])
def test_policy_check_catches_an_empty_peer_list(namespace_key, role):
    # `oc process` renders `-p GITLAB_CIDRS=[]` as `to: []`, which allows
    # every destination on that port.
    params = {**PARAMS[namespace_key], "GITLAB_CIDRS": "[]"}
    policy = _named(
        render(load_template(namespace_key), params),
        "NetworkPolicy",
        CONTRACT["roles"][role]["allow_policy"],
    )
    assert "gitlab: no peers, which allows every destination" in allow_policy_violations(
        policy, role, params
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('    service_account_overwrite_allowed = ".*"', "override key"),
        ('    namespace_overwrite_allowed = "ci-.*"', "override key"),
        ('    cpu_limit_overwrite_max_allowed = "8"', "override key"),
        ("    privileged = true", "privileged"),
        ("    automount_service_account_token = true", "automount_service_account_token"),
    ],
)
def test_runner_check_catches_an_override_privilege_or_token(line, expected):
    text = _runner_text("gate")
    key = line.split("=")[0]
    if key.strip() in ("privileged", "automount_service_account_token"):
        text = re.sub(rf"(?m)^{re.escape(key)}= false$", line, text)
    else:
        text = text.replace("    privileged = false", "    privileged = false\n" + line)
    config = render_runner(text, RUNNER_PARAMS)
    assert any(expected in v for v in runner_violations(config, "gate", RUNNER_PARAMS))
