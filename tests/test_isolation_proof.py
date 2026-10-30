"""Workspace isolation proof (#1118, ADR-0009 section 2): a stub `oc` serves recorded JSON.

The stub keeps cluster objects the proof creates in a state file, so cleanup and the
next run are observed on the same fake cluster. No network, no cluster.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/devkit/isolation_proof.py"
CONTRACT = REPO / "deploy/workspace/contract.json"
RUNNER_EXAMPLES = {"gate": REPO / "deploy/workspace/runner-gate.example.toml",
                   "wip": REPO / "deploy/workspace/runner-wip.example.toml"}

_spec = importlib.util.spec_from_file_location("isolation_proof", SCRIPT)
assert _spec and _spec.loader
proof = importlib.util.module_from_spec(_spec)
sys.modules["isolation_proof"] = proof
_spec.loader.exec_module(proof)

PROD, CI = "mfr-prod", "mfr-ci"
ROLE = "workspace.mainframe-rag.io/role"
MGR_KEY, MGR_VALUE = "app.kubernetes.io/component", "gitlab-runner-manager"
PULL, TOKEN = "mfr-ci-pull-robot", "mfr-runner-token"
CANARY = "CANARY-7d1f0c-value"
DIGEST = "0" * 64
PARAMS = {
    "PROD_NAMESPACE": PROD,
    "CI_NAMESPACE": CI,
    "DEVKIT_REPOSITORY": "registry.site.example/mfr/devkit",
    "DEVKIT_DIGEST": f"sha256:{DIGEST}",
    "PULL_SECRET": PULL,
    "RUNNER_TOKEN_SECRET": TOKEN,
    "MANAGER_POD_LABEL_KEY": MGR_KEY,
    "MANAGER_POD_LABEL_VALUE": MGR_VALUE,
    "API_SERVER_CIDRS": '[{"ipBlock":{"cidr":"10.0.0.5/32"}},{"ipBlock":{"cidr":"10.0.0.6/32"}}]',
    "REGISTRY_CIDRS": '[{"ipBlock":{"cidr":"10.0.1.10/32"}}]',
    "GITLAB_CIDRS": '[{"ipBlock":{"cidr":"10.0.2.20/32"}}]',
    "LITELLM_CIDRS": '[{"ipBlock":{"cidr":"10.0.3.30/32"}}]',
    "REGISTRY_PATH": "registry.site.example/mfr",
    "LITELLM_PORT": "4000",
}
# The contract's egress allow-lists, per probe identity (the control reaches all).
ALLOWED = {
    "bastion": {"api_server", "registry", "gitlab", "dns"},
    "dev": {"gitlab", "litellm", "dns"},
    "runner": {"gitlab", "dns"},
    "manager": {"api_server", "gitlab", "dns"},
}

BASELINE_TABLE = """\
Resources                                       Non-Resource URLs   Resource Names   Verbs
selfsubjectreviews.authentication.k8s.io        []                  []               [create]
selfsubjectaccessreviews.authorization.k8s.io   []                  []               [create]
selfsubjectrulesreviews.authorization.k8s.io    []                  []               [create]
imagestreams.image.openshift.io/layers          []                  []               [get]
                                                [/api/*]            []               [get]
                                                [/healthz /version] []               [get]
"""
MANAGER_TABLE = BASELINE_TABLE + """\
secrets                                         []                  []               [get list watch]
pods/exec                                       []                  []               [create get]
pods                                            []                  []               [create delete get list]
"""

STUB = r'''
import json, os, sys
args = sys.argv[1:]
scen = json.load(open(os.environ["STUB_SCENARIO"]))
state_path = os.environ["STUB_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {"objects": []}
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")

def opt(flag):
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None

def save():
    json.dump(state, open(state_path, "w"))

def out(obj):
    print(json.dumps(obj)); sys.exit(0)

def fail(msg="Error from server (NotFound): " + scen.get("canary", "")):
    print(msg, file=sys.stderr); sys.exit(1)

def matches(obj, sel):
    labels = obj["metadata"].get("labels") or {}
    key, _, value = sel.partition("=")
    return key in labels and (not value or labels[key] == value)

def kinds(spec):
    return {"pods": "Pod", "serviceaccounts": "ServiceAccount", "pod": "Pod"}.get(spec)

ns = opt("-n")
user = opt("--as")
if args[:2] == ["auth", "can-i"]:
    sa = user.rsplit(":", 1)[1] if user else None
    if "--list" in args:
        key = "baseline" if sa.startswith("mfr-proof-baseline-") else sa
        table = scen["can_i_list"].get(f"{key}|{ns}", scen["can_i_list"].get(key))
        if table is None:
            fail()
        print(table, end=""); sys.exit(0)
    verb, res = args[2], args[3]
    sub = opt("--subresource")
    target = f"{res}/{sub}" if sub else res
    if sa is None:
        yes = scen.get("operator_can", True)
    else:
        yes = f"{sa}|{ns}|{verb}|{target}" in scen.get("allow", [])
    print("yes" if yes else "no"); sys.exit(0 if yes else 1)
if args[0] == "create":
    obj = json.loads(sys.stdin.read())
    if obj["metadata"]["name"].startswith(tuple(scen.get("fail_create", []))):
        fail()
    obj["metadata"]["namespace"] = ns
    state["objects"].append(obj)
    state.setdefault("created", []).append(obj); save()
    print(f"{obj['kind'].lower()}/{obj['metadata']['name']}"); sys.exit(0)
if args[0] == "delete":
    if scen.get("fail_delete"):
        fail()
    wanted = {kinds(k) for k in args[1].split(",")}
    sel = opt("-l")
    keep, gone = [], []
    for obj in state["objects"]:
        hit = obj["kind"] in wanted and obj["metadata"]["namespace"] == ns and matches(obj, sel)
        (gone if hit else keep).append(obj)
    state["objects"] = keep; save()
    for obj in gone:
        print(f"{obj['kind'].lower()}/{obj['metadata']['name']}")
    sys.exit(0)
if args[0] == "logs":
    pod = next(o for o in state["objects"] if o["metadata"]["name"] == args[1])
    labels = pod["metadata"]["labels"]
    ident = labels.get(scen["role_key"]) or (
        "manager" if labels.get(scen["mgr_key"]) == scen["mgr_value"] else "control")
    if ident in scen.get("logs_fail", []):
        fail()
    targets = json.loads(pod["spec"]["containers"][0]["command"][-1])
    net = scen["network"].get(ident, {})
    res = [{"id": t["id"], "status": net.get(t["class"], "timeout")} for t in targets]
    print("probe starting " + scen.get("canary", ""))
    print("MFR-PROBE " + json.dumps(res)); sys.exit(0)
if args[0] == "get":
    what, rest = args[1], args[2:]
    sel = opt("-l")
    if sel is not None:
        wanted = {kinds(k) for k in what.split(",")}
        out({"items": [o for o in state["objects"] if o["kind"] in wanted
                       and o["metadata"]["namespace"] == ns and matches(o, sel)]})
    if what == "pods":
        live = [o for o in state["objects"] if o["kind"] == "Pod"
                and o["metadata"]["namespace"] == ns]
        out({"items": scen["pods"].get(ns, []) + live})
    if what == "pod":
        pod = next((o for o in state["objects"] if o["metadata"]["name"] == rest[0]), None)
        if pod is None:
            fail()
        out(dict(pod, status={"phase": scen.get("phase", "Succeeded")}))
    if what == "services":
        out({"items": scen["services"]})
    if what == "service":
        svc = next((s for s in scen["services"] if s["metadata"]["name"] == rest[0]), None)
        out(svc) if svc else fail()
    if what in ("namespace", "project"):
        labels = scen.get("namespace_labels")
        out({"metadata": {"name": rest[0], "labels": labels}}) if labels is not None else fail()
    if what == "networkpolicies":
        out({"items": scen["policies"][ns]})
fail("unexpected call")
'''


def pod(ns: str, name: str, labels: dict[str, str], sa: str, *, volumes=(), env=(),
        pull=(), env_from=()) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": ns, "labels": labels,
                     "annotations": {"note": CANARY}},
        "spec": {
            "serviceAccountName": sa,
            "volumes": list(volumes),
            "imagePullSecrets": [{"name": p} for p in pull],
            "containers": [{"name": "c", "env": [{"name": "SAFE", "value": CANARY}, *env],
                            "envFrom": list(env_from)}],
        },
    }


def secret_volume(name: str) -> dict[str, Any]:
    return {"name": f"v-{name}", "secret": {"secretName": name}}


def clean_scenario() -> dict[str, Any]:
    pods = {
        PROD: [
            pod(PROD, "mfr-bastion-0", {ROLE: "bastion"}, "mfr-bastion",
                pull=[PULL, "mfr-bastion-dockercfg-k2x9q"]),
            pod(PROD, "rag-agent-1", {"app": "rag-agent"}, "rag-agent",
                volumes=[secret_volume("rag-qdrant-keys")]),
        ],
        CI: [
            pod(CI, "mfr-dev-0", {ROLE: "dev"}, "mfr-dev",
                volumes=[secret_volume("mfr-dev-gitlab"), secret_volume("mfr-dev-litellm")],
                pull=[PULL, "mfr-dev-dockercfg-7h2mz"]),
            pod(CI, "runner-abc-project-1", {ROLE: "runner", "pod": "runner-abc"},
                "mfr-runner-job", pull=[PULL, "mfr-runner-job-dockercfg-a1b2c"]),
            pod(CI, "gitlab-runner-mgr-0", {MGR_KEY: MGR_VALUE}, "gitlab-runner-sa",
                volumes=[secret_volume(TOKEN), secret_volume("gitlab-runner-operator-cfg")],
                pull=[PULL]),
        ],
    }
    can_i_list = {"baseline": BASELINE_TABLE, "gitlab-runner-sa": MANAGER_TABLE}
    for sa in ("mfr-bastion", "mfr-dev", "mfr-runner-job"):
        can_i_list[sa] = BASELINE_TABLE
    network: dict[str, dict[str, str]] = {
        "control": {c: ("resolved" if c == "dns" else "connected")
                    for c in ("prod_qdrant", "prod_agent", "api_server", "registry", "gitlab",
                              "litellm", "dns")},
    }
    for ident, allowed in ALLOWED.items():
        network[ident] = {c: ("resolved" if c == "dns" else "connected") for c in allowed}
        network[ident].setdefault("dns", "unresolved")
    return {
        "canary": CANARY,
        "role_key": ROLE, "mgr_key": MGR_KEY, "mgr_value": MGR_VALUE,
        "pods": pods,
        "can_i_list": can_i_list,
        "allow": [],
        "services": [
            {"metadata": {"name": "rag-agent", "annotations": {"x": CANARY}},
             "spec": {"clusterIP": "172.30.5.5", "selector": {"app": "rag-agent"},
                      "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}},
            {"metadata": {"name": "rag-qdrant"},
             "spec": {"clusterIP": "172.30.6.6", "selector": {"app": "qdrant"},
                      "ports": [{"port": 6333}, {"port": 6334}]}},
            {"metadata": {"name": "rag-qdrant-headless"},
             "spec": {"clusterIP": "None", "ports": [{"port": 6333}]}},
        ],
        "namespace_labels": {"kubernetes.io/metadata.name": CI},
        "policies": {PROD: [agent_policy([]), *workspace_policies(PROD)],
                     CI: workspace_policies(CI)},
        "network": network,
    }


def agent_policy(allowed_namespaces: list[str]) -> dict[str, Any]:
    ingress: list[dict[str, Any]] = [
        {"from": [{"podSelector": {}}], "ports": [{"protocol": "TCP", "port": 8080}]},
        {"from": [{"namespaceSelector": {"matchLabels": {
            "network.openshift.io/policy-group": "monitoring"}}}],
         "ports": [{"protocol": "TCP", "port": 8080}]},
    ]
    if allowed_namespaces:
        ingress.append({
            "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": n}}}
                     for n in allowed_namespaces],
            "ports": [{"protocol": "TCP", "port": 8080}],
        })
    return {"metadata": {"name": "rag-agent-ingress", "annotations": {"x": CANARY}},
            "spec": {"podSelector": {"matchLabels": {"app": "rag-agent"}},
                     "policyTypes": ["Ingress"], "ingress": ingress}}


def workspace_policies(ns: str) -> list[dict[str, Any]]:
    """The F9 egress policies as the contract names them (bodies minimal)."""
    contract = json.loads(CONTRACT.read_text())
    key = "prod" if ns == PROD else "ci"
    spec = contract["policies"][key]
    deny = spec["default_deny"]
    out = [{"metadata": {"name": deny["name"]},
            "spec": {"podSelector": deny["pod_selector"], "policyTypes": deny["policy_types"]}}]
    for name in spec["allow"]:
        out.append({"metadata": {"name": name},
                    "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]}})
    return out


RENDER = {"CI_NAMESPACE": CI, "PULL_SECRET": PULL, "REGISTRY_PATH": PARAMS["REGISTRY_PATH"],
          "DEVKIT_REPOSITORY": PARAMS["DEVKIT_REPOSITORY"], "DEVKIT_DIGEST": PARAMS["DEVKIT_DIGEST"],
          "RUNNER_HELPER_REPOSITORY": "registry.site.example/mfr/helper",
          "RUNNER_HELPER_DIGEST": f"sha256:{DIGEST}"}


def runner_toml(name: str = "gate", extra: str = "", drop: str = "") -> str:
    """The PR-3a example configuration, rendered as the operator renders it."""
    text = RUNNER_EXAMPLES[name].read_text()
    text = re.sub(r"\$\{([A-Z_]+)\}", lambda m: RENDER.get(m[1], "1"), text)
    text = text.replace("  [runners.kubernetes]\n", f"  token = \"{CANARY}\"\n"
                        f"  [runners.kubernetes]\n{extra}\n", 1)
    if drop:
        text = "\n".join(line for line in text.splitlines() if drop not in line) + "\n"
    return text


class Cluster:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        stub = self.bin / "oc"
        stub.write_text(f"#!{sys.executable} -IS\n{STUB}")
        stub.chmod(0o755)
        self.state = root / "state.json"
        self.log = root / "calls.log"
        self.scenario_path = root / "scenario.json"
        self.scenario = clean_scenario()
        self.params = root / "params.env"
        self.params.write_text("".join(f"{k}={v}\n" for k, v in PARAMS.items()))
        self.runner = [root / "gate.toml", root / "wip.toml"]
        self.runner[0].write_text(runner_toml("gate"))
        self.runner[1].write_text(runner_toml("wip"))
        self.output = root / "record.json"
        monkeypatch.setenv("PATH", f"{self.bin}:/usr/bin:/bin")
        monkeypatch.setenv("STUB_SCENARIO", str(self.scenario_path))
        monkeypatch.setenv("STUB_STATE", str(self.state))
        monkeypatch.setenv("STUB_LOG", str(self.log))

    def argv(self, *extra: str) -> list[str]:
        argv = ["--contract", str(CONTRACT), "--param-file", str(self.params),
                "--output", str(self.output)]
        for path in self.runner:
            argv += ["--runner-config", str(path)]
        return argv + list(extra)

    def run(self, *extra: str) -> tuple[int, dict[str, Any]]:
        self.scenario_path.write_text(json.dumps(self.scenario))
        code = proof.main(self.argv(*extra))
        return code, json.loads(self.output.read_text())

    def objects(self) -> list[dict[str, Any]]:
        if not self.state.exists():
            return []
        return json.loads(self.state.read_text())["objects"]

    def created(self) -> list[dict[str, Any]]:
        return json.loads(self.state.read_text()).get("created", [])

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Cluster:
    return Cluster(tmp_path, monkeypatch)


def reasons(record: dict[str, Any], check: str, kind: str = "violation") -> list[str]:
    return [f"{f['subject']}: {f['reason']}" for f in record["findings"]
            if f["check"] == check and f["kind"] == kind]


# --------------------------------------------------------------------------- outcomes


def test_clean_workspace_is_proven_with_probes_and_leaves_nothing(cluster: Cluster) -> None:
    code, record = cluster.run("--probe")
    assert (code, record["verdict"], record["findings"]) == (0, "proven", [])
    assert cluster.objects() == []
    assert record["cleanup"]["remaining"] == {PROD: 0, CI: 0}
    # Baseline ServiceAccounts were really created (negative control) and removed.
    created = [c for c in cluster.calls() if c[0] == "create"]
    assert len(created) == 2 + 5  # two baseline SAs, four role probes and the control
    classes = {r["name"]: r["class"] for r in record["checks"]["pods"]}
    assert classes == {"mfr-bastion-0": "bastion", "rag-agent-1": "release",
                       "mfr-dev-0": "dev", "runner-abc-project-1": "runner",
                       "gitlab-runner-mgr-0": "manager"}
    probes = record["checks"]["probes"]
    assert {r["verdict"] for r in probes["results"]} == {"ok"}
    blocked = {(r["identity"], r["class"]) for r in probes["results"] if r["expect"] == "block"}
    assert ("dev", "prod_qdrant") in blocked and ("runner", "prod_agent") in blocked
    assert ("bastion", "prod_qdrant") in blocked and ("manager", "litellm") in blocked


def test_static_run_is_not_reported_as_the_full_proof(cluster: Cluster) -> None:
    code, record = cluster.run()
    assert (code, record["verdict"]) == (0, "proven-static-only")
    assert record["checks"]["probes"] == {"status": "not-run"}
    assert not [c for c in cluster.calls() if c[0] == "logs"]


def test_manager_rules_and_extra_secrets_are_listed_not_judged(cluster: Cluster) -> None:
    code, record = cluster.run()
    assert code == 0
    (manager,) = record["checks"]["rbac"]["manager"]
    assert manager["service_account"] == "gitlab-runner-sa" and manager["judged"] is False
    assert "get secrets" in manager["rules"][CI]
    row = next(r for r in record["checks"]["pods"] if r["class"] == "manager")
    assert {(s["name"], s["expected"]) for s in row["secret_refs"]} == {
        (TOKEN, True), (PULL, True), ("gitlab-runner-operator-cfg", False)}


# --------------------------------------------------------------------------- step 1, 2


def test_f9_sa_that_gained_get_secrets_is_a_violation(cluster: Cluster) -> None:
    cluster.scenario["can_i_list"]["mfr-dev"] = (
        BASELINE_TABLE + "secrets   []   []   [get]\n")
    cluster.scenario["allow"] = [f"mfr-dev|{CI}|get|secrets"]
    code, record = cluster.run()
    assert (code, record["verdict"]) == (1, "violation")
    assert f"dev in {CI}: beyond-baseline: get secrets" in reasons(record, "rbac")
    assert reasons(record, "negative-matrix") == [f"dev in {CI}: allowed: get secrets"]
    assert cluster.objects() == []


def test_exec_subresource_refusal_is_asked_in_both_namespaces(cluster: Cluster) -> None:
    cluster.scenario["allow"] = [f"mfr-runner-job|{PROD}|create|pods/exec"]
    code, record = cluster.run()
    assert code == 1
    assert reasons(record, "negative-matrix") == [f"runner in {PROD}: allowed: create pods/exec"]
    asked = {(r["role"], r["namespace"]) for r in record["checks"]["negative_matrix"]["results"]}
    assert asked == {(r, ns) for r in ("bastion", "dev", "runner") for ns in (PROD, CI)}


def test_empty_listing_is_inconclusive_never_a_pass(cluster: Cluster) -> None:
    cluster.scenario["can_i_list"]["mfr-bastion"] = "Resources  Non-Resource URLs\n"
    code, record = cluster.run()
    assert (code, record["verdict"]) == (2, "inconclusive")
    assert f"bastion in {PROD}: empty-listing" in reasons(record, "rbac", "inconclusive")


def test_refused_operator_control_makes_the_matrix_inconclusive(cluster: Cluster) -> None:
    cluster.scenario["operator_can"] = False
    code, record = cluster.run()
    assert (code, record["verdict"]) == (2, "inconclusive")
    assert reasons(record, "negative-matrix", "inconclusive") == [
        f"operator in {PROD}: positive-control-refused",
        f"operator in {CI}: positive-control-refused"]


# --------------------------------------------------------------------------- step 3


def test_bastion_pull_secret_and_own_dockercfg_pass(cluster: Cluster) -> None:
    code, record = cluster.run()
    assert code == 0
    row = next(r for r in record["checks"]["pods"] if r["class"] == "bastion")
    assert row["secret_refs"] == [
        {"name": "mfr-bastion-dockercfg-k2x9q", "via": "imagePullSecrets", "allowed": True},
        {"name": PULL, "via": "imagePullSecrets", "allowed": True}]


@pytest.mark.parametrize("index, mutate, reason", [
    # A foreign ServiceAccount's injected pull Secret.
    (1, lambda p: p["spec"]["imagePullSecrets"].append({"name": "mfr-dev-dockercfg-7h2mz"}),
     "secret-not-allowed: mfr-dev-dockercfg-7h2mz via imagePullSecrets"),
    # The pod's own injected pull Secret, mounted where a container can read it.
    (1, lambda p: p["spec"]["volumes"].append(secret_volume("mfr-runner-job-dockercfg-a1b2c")),
     "secret-not-allowed: mfr-runner-job-dockercfg-a1b2c via volume"),
    # The pull-only robot as a mount on the dev pod.
    (0, lambda p: p["spec"]["volumes"].append(secret_volume(PULL)),
     f"secret-not-allowed: {PULL} via volume"),
    # A mounted Secret offered as a pull credential is not a pull credential.
    (0, lambda p: p["spec"]["imagePullSecrets"].append({"name": "mfr-dev-gitlab"}),
     "secret-not-allowed: mfr-dev-gitlab via imagePullSecrets"),
])
def test_image_pull_and_mounted_secrets_are_checked_separately(cluster: Cluster, index, mutate,
                                                                reason) -> None:
    target = cluster.scenario["pods"][CI][index]
    mutate(target)
    code, record = cluster.run()
    assert code == 1
    assert reasons(record, "pods") == [f"{CI}/{target['metadata']['name']}: {reason}"]


def test_injected_pull_secret_is_bound_to_the_pod_service_account(cluster: Cluster) -> None:
    # A runner pod under the dev SA: its own SA's dockercfg would match the injected
    # pattern, but not the runner role's; the SA itself is also refused.
    runner = cluster.scenario["pods"][CI][1]
    runner["spec"]["serviceAccountName"] = "mfr-dev"
    runner["spec"]["imagePullSecrets"] = [{"name": PULL}, {"name": "mfr-dev-dockercfg-7h2mz"}]
    code, record = cluster.run()
    assert code == 1
    assert sorted(reasons(record, "pods")) == [
        f"{CI}/runner-abc-project-1: secret-not-allowed: mfr-dev-dockercfg-7h2mz via imagePullSecrets",
        f"{CI}/runner-abc-project-1: service-account: mfr-dev"]


def test_dev_pod_with_a_third_secret_is_a_violation(cluster: Cluster) -> None:
    dev = cluster.scenario["pods"][CI][0]
    dev["spec"]["containers"][0]["env"].append(
        {"name": "K", "valueFrom": {"secretKeyRef": {"name": "rag-qdrant-keys", "key": "k"}}})
    code, record = cluster.run()
    assert code == 1
    assert reasons(record, "pods") == [f"{CI}/mfr-dev-0: secret-not-allowed: rag-qdrant-keys via env"]


@pytest.mark.parametrize("mutate, reason", [
    (lambda p: p["spec"]["volumes"].append(secret_volume(PULL)),
     f"secret-not-allowed: {PULL} via volume"),
    (lambda p: p["spec"]["containers"][0]["envFrom"].append({"secretRef": {"name": TOKEN}}),
     f"secret-not-allowed: {TOKEN} via env"),
    (lambda p: p["spec"]["volumes"].append({"name": "x", "projected": {"sources": [
        {"secret": {"name": "mfr-dev-litellm"}}]}}),
     "secret-not-allowed: mfr-dev-litellm via volume"),
    (lambda p: p["spec"]["volumes"].append({"name": "t", "projected": {"sources": [
        {"serviceAccountToken": {"path": "token"}}]}}),
     "service-account-token-mounted"),
    (lambda p: p["spec"]["volumes"].append({"name": "c", "csi": {
        "driver": "x", "nodePublishSecretRef": {"name": "csi-creds"}}}),
     "secret-not-allowed: csi-creds via volume"),
    (lambda p: p["spec"].update(serviceAccountName="default", imagePullSecrets=[{"name": PULL}]),
     "service-account: default"),
])
def test_runner_pod_secret_references_are_checked_exactly(cluster: Cluster, mutate, reason) -> None:
    mutate(cluster.scenario["pods"][CI][1])
    code, record = cluster.run()
    assert code == 1
    assert reasons(record, "pods") == [f"{CI}/runner-abc-project-1: {reason}"]


@pytest.mark.parametrize("label", ["app", "app.kubernetes.io/name"])
def test_app_label_on_a_runner_pod_is_a_violation(cluster: Cluster, label: str) -> None:
    cluster.scenario["pods"][CI][1]["metadata"]["labels"][label] = "rag-agent"
    code, record = cluster.run()
    assert code == 1
    assert reasons(record, "pods") == [f"{CI}/runner-abc-project-1: forbidden-label: {label}"]


def test_pods_outside_the_classes_are_violations(cluster: Cluster) -> None:
    cluster.scenario["pods"][CI].append(pod(CI, "debug-shell", {}, "default"))
    cluster.scenario["pods"][PROD].append(pod(PROD, "stray-dev", {ROLE: "dev"}, "mfr-dev"))
    cluster.scenario["pods"][CI].append(
        pod(CI, "both", {ROLE: "runner", MGR_KEY: MGR_VALUE}, "mfr-runner-job"))
    code, record = cluster.run()
    assert code == 1
    assert sorted(reasons(record, "pods")) == [
        f"{CI}/both: unexpected-role-label",
        f"{CI}/debug-shell: unclassified-pod",
        f"{PROD}/stray-dev: unexpected-role-label"]


def test_missing_runner_manager_is_inconclusive(cluster: Cluster) -> None:
    cluster.scenario["pods"][CI].pop()
    code, record = cluster.run()
    assert (code, record["verdict"]) == (2, "inconclusive")
    assert reasons(record, "pods", "inconclusive") == [f"{CI}: no-runner-manager-pod"]


# --------------------------------------------------------------------------- step 4


@pytest.mark.parametrize("extra, drop, failed", [
    ("    service_account_overwrite_allowed = \".*\"", "", "no_overwrite_allowed_key"),
    ("", "privileged = false", "privileged_false"),
    ("", "helper_memory_limit", "has_helper_memory_limit"),
    ("", "service_cpu_request", "has_service_cpu_request"),
    ("", "namespace =", "namespace"),
    ("", "service_account =", "service_account"),
    ("", "allowed_services", "allowed_services_site_registry"),
])
def test_runner_config_must_allow_no_override(cluster: Cluster, extra, drop, failed) -> None:
    cluster.runner[1].write_text(runner_toml("wip", extra=extra, drop=drop))
    code, record = cluster.run()
    assert code == 1
    assert reasons(record, "runner-config") == [f"wip.toml#0: failed: {failed}"]


def test_overwrite_key_anywhere_in_the_document_is_found(cluster: Cluster) -> None:
    cluster.runner[0].write_text(
        runner_toml() + "[runners.kubernetes.pod_annotations]\n  x = 1\n"
        "[runners.kubernetes.extra]\n  helper_image_overwrite_allowed = \"*\"\n")
    code, record = cluster.run()
    assert code == 1
    row = record["checks"]["runner_config"][0]
    assert row["overwrite_keys"] == ["runners[0].kubernetes.extra.helper_image_overwrite_allowed"]


def test_runner_config_from_processed_template_json(cluster: Cluster) -> None:
    listing = {"kind": "List", "items": [
        {"kind": "ConfigMap", "metadata": {"name": "gate"}, "data": {"config.toml": runner_toml()}},
        {"kind": "Secret", "metadata": {"name": "s"}, "stringData": {"t": CANARY}},
    ]}
    cluster.runner[0].write_text(json.dumps(listing))
    code, record = cluster.run()
    assert code == 0
    assert [r["file"] for r in record["checks"]["runner_config"]] == ["gate.toml", "wip.toml"]


def test_unparseable_runner_config_is_an_environment_error(cluster: Cluster) -> None:
    cluster.runner[0].write_text("[[runners]\n")
    code, record = cluster.run()
    assert (code, record["error"]["code"]) == (2, "runner-config-unreadable")


def test_unrendered_example_is_inconclusive(cluster: Cluster) -> None:
    cluster.runner[0].write_text(RUNNER_EXAMPLES["gate"].read_text())
    code, record = cluster.run()
    assert code == 1  # the literal ${CI_NAMESPACE} also fails the namespace check
    assert "gate.toml: unrendered-placeholder" in reasons(record, "runner-config", "inconclusive")


def test_runner_jobs_labelled_as_another_role_are_a_violation(cluster: Cluster) -> None:
    cluster.runner[0].write_text(runner_toml().replace(f'"{ROLE}" = "runner"', f'"{ROLE}" = "dev"'))
    code, record = cluster.run()
    assert code == 1
    assert reasons(record, "runner-config") == ["gate.toml#0: failed: pod_labels_role_only"]


def test_both_rendered_examples_pass_unchanged(cluster: Cluster) -> None:
    code, record = cluster.run()
    assert code == 0
    rows = record["checks"]["runner_config"]
    assert [(r["file"], r["name"]) for r in rows] == [("gate.toml", "mfr-gate"),
                                                       ("wip.toml", "mfr-wip")]
    assert all(all(r["checks"].values()) for r in rows)
    assert len(rows[0]["checks"]) == 5 + 2 + 18 + 2


@pytest.mark.parametrize("ns, index, change, reason", [
    (CI, 0, lambda p: p["spec"].update(podSelector={"matchLabels": {ROLE: "dev"}}),
     "default-deny-altered"),
    (CI, 0, lambda p: p["spec"].update(egress=[{}]), "default-deny-altered"),
    (PROD, 1, lambda p: p["metadata"].update(name="renamed"), "missing"),
    (CI, 2, lambda p: p["metadata"].update(name="renamed"), "missing"),
])
def test_contract_egress_policies_must_exist(cluster: Cluster, ns, index, change, reason) -> None:
    policy = cluster.scenario["policies"][ns][index]
    name = policy["metadata"]["name"]
    change(policy)
    code, record = cluster.run()
    assert code == 1
    assert reasons(record, "egress-policies") == [f"{ns}/{name}: {reason}"]


# --------------------------------------------------------------------------- step 5


def test_allowed_namespaces_listing_ci_is_a_violation(cluster: Cluster) -> None:
    cluster.scenario["policies"][PROD][0] = agent_policy(["mfr-tools", CI])
    code, record = cluster.run()
    assert code == 1
    assert reasons(record, "agent-policy") == ["rag-agent-ingress: admits-ci-namespace"]
    (row,) = record["checks"]["agent_policy"]["policies"]
    assert row["name"] == "rag-agent-ingress"
    assert row["allowed_namespaces"] == sorted(["mfr-tools", CI])


def test_other_allowed_namespaces_are_listed_and_pass(cluster: Cluster) -> None:
    cluster.scenario["policies"][PROD][0] = agent_policy(["mfr-tools"])
    code, _ = cluster.run()
    assert code == 0


@pytest.mark.parametrize("policies, reason", [
    ([], "rag-agent: no-ingress-policy-selects-agent"),
    ([{"metadata": {"name": "open"}, "spec": {"podSelector": {}, "ingress": [{}]}}],
     "open: rule-admits-every-source"),
    ([{"metadata": {"name": "all-ns"}, "spec": {"podSelector": {}, "ingress": [
        {"from": [{"namespaceSelector": {}}]}]}}], "all-ns: admits-ci-namespace"),
])
def test_agent_ingress_that_admits_ci_is_a_violation(cluster: Cluster, policies, reason) -> None:
    cluster.scenario["policies"][PROD] = policies + workspace_policies(PROD)
    code, record = cluster.run()
    assert code == 1
    assert reasons(record, "agent-policy") == [reason]


def test_unreadable_ci_labels_leave_label_selectors_inconclusive(cluster: Cluster) -> None:
    cluster.scenario["namespace_labels"] = None
    code, record = cluster.run()
    assert (code, record["verdict"]) == (2, "inconclusive")
    assert reasons(record, "agent-policy", "inconclusive") == [
        "rag-agent-ingress: ci-namespace-labels-unreadable"]


# --------------------------------------------------------------------------- step 6


def test_probe_reaching_production_qdrant_is_a_violation(cluster: Cluster) -> None:
    cluster.scenario["network"]["dev"]["prod_qdrant"] = "connected"
    code, record = cluster.run("--probe")
    assert (code, record["verdict"]) == (1, "violation")
    assert reasons(record, "probes") == [
        "dev -> prod_qdrant 172.30.6.6:6333: unexpected-reach",
        "dev -> prod_qdrant 172.30.6.6:6334: unexpected-reach"]
    assert cluster.objects() == []


def test_manager_probe_reaching_litellm_is_a_violation(cluster: Cluster) -> None:
    cluster.scenario["network"]["manager"]["litellm"] = "connected"
    code, record = cluster.run("--probe")
    assert code == 1
    assert reasons(record, "probes") == ["manager -> litellm 10.0.3.30:4000: unexpected-reach"]


def test_failed_negative_control_is_inconclusive_never_proven(cluster: Cluster) -> None:
    cluster.scenario["network"]["control"] = {}  # the control reached nothing
    code, record = cluster.run("--probe")
    assert (code, record["verdict"]) == (2, "inconclusive")
    assert not reasons(record, "probes")
    assert "dev -> prod_qdrant 172.30.6.6:6333: control-did-not-reach" in reasons(
        record, "probes", "inconclusive")
    assert cluster.objects() == []


def test_allowed_destination_that_is_blocked_is_a_violation(cluster: Cluster) -> None:
    cluster.scenario["network"]["runner"]["gitlab"] = "timeout"
    code, record = cluster.run("--probe")
    assert code == 1
    assert reasons(record, "probes") == ["runner -> gitlab 10.0.2.20:443: unexpected-block: timeout"]


def test_refused_connection_on_a_denied_target_is_inconclusive(cluster: Cluster) -> None:
    cluster.scenario["network"]["dev"]["prod_agent"] = "refused"
    code, record = cluster.run("--probe")
    assert (code, record["verdict"]) == (2, "inconclusive")
    assert reasons(record, "probes", "inconclusive") == [
        "dev -> prod_agent 172.30.5.5:8080: ambiguous-outcome: refused"]


def test_wide_cidr_needs_an_explicit_probe_host(cluster: Cluster) -> None:
    cluster.params.write_text(cluster.params.read_text().replace(
        "10.0.2.20/32", "10.0.2.0/28"))
    code, record = cluster.run("--probe")
    assert (code, record["verdict"]) == (2, "inconclusive")
    assert reasons(record, "probes", "inconclusive") == ["gitlab: no-probe-target"]
    assert record["checks"]["probes"]["cidrs_not_probed"] == ["gitlab 10.0.2.0/28"]
    code, record = cluster.run("--probe", "--probe-host", "gitlab=gitlab.site.example")
    assert (code, record["verdict"]) == (0, "proven")
    targets = {t["host"] for t in record["checks"]["probes"]["targets"] if t["class"] == "gitlab"}
    assert targets == {"gitlab.site.example"}


def test_probe_pods_carry_role_labels_and_never_the_manager_sa(cluster: Cluster) -> None:
    code, record = cluster.run("--probe")
    assert code == 0
    pods = {o["metadata"]["name"].split("-")[2]: o for o in cluster.created() if o["kind"] == "Pod"}
    run = {proof.PROOF_LABEL: record["run_id"]}
    assert {k: (p["metadata"]["namespace"], p["metadata"]["labels"],
                p["spec"].get("serviceAccountName")) for k, p in pods.items()} == {
        "bastion": (PROD, {ROLE: "bastion", **run}, "mfr-bastion"),
        "dev": (CI, {ROLE: "dev", **run}, "mfr-dev"),
        "runner": (CI, {ROLE: "runner", **run}, "mfr-runner-job"),
        "manager": (CI, {MGR_KEY: MGR_VALUE, **run}, None),
        "control": (PROD, run, None),
    }
    for body in pods.values():
        spec = body["spec"]
        (container,) = spec["containers"]
        assert spec["automountServiceAccountToken"] is False
        assert container["image"] == f"{PARAMS['DEVKIT_REPOSITORY']}@{PARAMS['DEVKIT_DIGEST']}"
        assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
        assert set(container["resources"]["limits"]) == {"cpu", "memory", "ephemeral-storage"}
        assert container["resources"]["requests"] == container["resources"]["limits"]
        assert "volumes" not in spec and "imagePullSecrets" not in spec


def test_probe_program_classifies_real_sockets(tmp_path: Path) -> None:
    import socket

    listener = socket.create_server(("127.0.0.1", 0))
    closed = socket.create_server(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    targets = [
        {"id": "a", "kind": "tcp", "host": "127.0.0.1", "port": listener.getsockname()[1],
         "timeout": 2},
        {"id": "b", "kind": "tcp", "host": "127.0.0.1", "port": closed_port, "timeout": 2},
        {"id": "c", "kind": "dns", "host": "localhost", "port": None, "timeout": 2},
        {"id": "d", "kind": "kube_service", "host": None, "port": None, "timeout": 2},
    ]
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", proof.PROBE_PROGRAM, json.dumps(targets)],
            capture_output=True, text=True, check=True, timeout=30,
            env={"PATH": "/usr/bin:/bin"})
    finally:
        listener.close()
    (line,) = proc.stdout.splitlines()
    assert line.startswith(proof.PROBE_MARKER)
    assert json.loads(line[len(proof.PROBE_MARKER):]) == [
        {"id": "a", "status": "connected"}, {"id": "b", "status": "refused"},
        {"id": "c", "status": "resolved"}, {"id": "d", "status": "no-address"}]


# --------------------------------------------------------------------------- lifecycle


def test_cleanup_after_an_error_then_the_next_run_succeeds(cluster: Cluster) -> None:
    cluster.scenario["logs_fail"] = ["runner"]
    code, record = cluster.run("--probe")
    assert (code, record["verdict"]) == (2, "error")
    assert record["error"] == {"code": "kube-call-failed", "detail": "probe logs",
                               "message": proof.MESSAGES["kube-call-failed"]}
    assert cluster.objects() == []
    assert record["cleanup"]["deleted"][CI] >= 3  # baseline SA, dev, runner, manager probes
    cluster.scenario["logs_fail"] = []
    code, record = cluster.run("--probe")
    assert (code, record["verdict"]) == (0, "proven")
    assert record["leftovers_removed"] == {PROD: 0, CI: 0}
    assert cluster.objects() == []


def test_leftovers_of_an_interrupted_run_are_removed_first(cluster: Cluster) -> None:
    stale = {"apiVersion": "v1", "kind": "Pod",
             "metadata": {"name": "mfr-proof-dev-dead", "namespace": CI,
                          "labels": {ROLE: "dev", proof.PROOF_LABEL: "dead"}},
             "spec": {"containers": [{"name": "probe"}]}}
    stale_sa = {"apiVersion": "v1", "kind": "ServiceAccount",
                "metadata": {"name": "mfr-proof-baseline-dead", "namespace": PROD,
                             "labels": {proof.PROOF_LABEL: "dead"}}}
    unrelated = {"apiVersion": "v1", "kind": "ServiceAccount",
                 "metadata": {"name": "keep-me", "namespace": CI, "labels": {"x": "y"}}}
    cluster.state.write_text(json.dumps({"objects": [stale, stale_sa, unrelated]}))
    code, record = cluster.run("--probe")
    assert (code, record["verdict"]) == (0, "proven")
    assert record["leftovers_removed"] == {PROD: 1, CI: 1}
    assert [o["metadata"]["name"] for o in cluster.objects()] == ["keep-me"]
    assert "mfr-proof-dev-dead" not in {r["name"] for r in record["checks"]["pods"]}


def test_failed_cleanup_is_reported_not_hidden(cluster: Cluster) -> None:
    code, _ = cluster.run()
    assert code == 0
    cluster.scenario["fail_delete"] = True
    code, record = cluster.run()
    assert (code, record["error"]["code"]) == (2, "kube-call-failed")
    assert record["cleanup"] == {"status": "failed"}


# --------------------------------------------------------------------------- hygiene and CLI


def test_record_never_carries_secret_data_or_env_values(cluster: Cluster) -> None:
    cluster.scenario["pods"][CI][0]["spec"]["containers"][0]["env"].append(
        {"name": "TOK", "value": CANARY})
    cluster.scenario["network"]["dev"]["prod_qdrant"] = "connected"
    code, _ = cluster.run("--probe")
    assert code == 1
    proc = subprocess.run([sys.executable, str(SCRIPT), *cluster.argv("--probe")],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 1
    text = cluster.output.read_text() + proc.stdout + proc.stderr
    assert CANARY not in text
    assert "isolation-proof: violation" in proc.stderr


def test_cli_exit_codes_and_fixed_messages(cluster: Cluster, tmp_path: Path) -> None:
    cluster.scenario_path.write_text(json.dumps(cluster.scenario))
    ok = subprocess.run([sys.executable, str(SCRIPT), *cluster.argv("--probe")],
                        capture_output=True, text=True, check=False)
    assert (ok.returncode, ok.stdout.strip()) == (
        0, "isolation-proof: proven (0 violations, 0 inconclusive)")
    usage = subprocess.run([sys.executable, str(SCRIPT), "--bogus"],
                           capture_output=True, text=True, check=False)
    assert usage.returncode == 2
    missing = tmp_path / "missing.env"
    missing.write_text("PROD_NAMESPACE=mfr-prod\n")
    argv = cluster.argv()
    argv[argv.index(str(cluster.params))] = str(missing)
    proc = subprocess.run([sys.executable, str(SCRIPT), *argv],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 2
    assert proc.stderr.strip().endswith(
        "error param-missing: a required template parameter is missing [CI_NAMESPACE]")


def test_missing_cluster_client_is_an_environment_error(cluster: Cluster) -> None:
    code, record = cluster.run("--kubectl", "no-such-oc")
    assert (code, record["error"]["code"]) == (2, "kubectl-missing")
    assert "cleanup" not in record


@pytest.mark.parametrize("edit", [
    lambda c: c.update(schema_version=9),
    lambda c: c["roles"]["dev"].pop("service_account"),
    lambda c: c["roles"]["dev"].update(namespace="staging"),
])
def test_malformed_contract_is_refused(cluster: Cluster, tmp_path: Path, edit) -> None:
    contract = copy.deepcopy(json.loads(CONTRACT.read_text()))
    edit(contract)
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    argv = cluster.argv()
    argv[argv.index(str(CONTRACT))] = str(path)
    cluster.scenario_path.write_text(json.dumps(cluster.scenario))
    assert proof.main(argv) == 2
    assert json.loads(cluster.output.read_text())["error"]["code"] == "contract-unreadable"


def test_roles_come_from_the_contract_not_the_script(cluster: Cluster, tmp_path: Path) -> None:
    contract = json.loads(CONTRACT.read_text())
    contract["roles"]["dev"]["allowed_secret_refs"]["mounted"].append("rag-qdrant-keys")
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    dev = cluster.scenario["pods"][CI][0]
    dev["spec"]["volumes"].append(secret_volume("rag-qdrant-keys"))
    argv = cluster.argv()
    argv[argv.index(str(CONTRACT))] = str(path)
    cluster.scenario_path.write_text(json.dumps(cluster.scenario))
    assert proof.main(argv) == 0
