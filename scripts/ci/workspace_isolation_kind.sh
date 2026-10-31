#!/bin/sh
# Kind lane for the F9 workspace isolation proof (#1118 PR-3c, ADR-0009 section 2).
#
# Runs scripts/devkit/isolation_proof.py --probe against real NetworkPolicies on a
# throwaway Kind cluster, then proves the proof fails when isolation is broken.
# Kind's default CNI (kindnetd, kind v0.32.0) enforces NetworkPolicy.
#
#   1. a two-node cluster (workspace pods land on the worker, so traffic to the
#      control-plane API server is forwarded, as it is on a site cluster);
#   2. site stand-ins outside the cluster: registry, GitLab and LiteLLM listeners as
#      containers on the Kind network, each at its own /32 address;
#   3. CoreDNS moved to an `openshift-dns` namespace behind the kube-dns Service,
#      because the templates' DNS peer is that namespace by name;
#   4. the production and CI namespaces: a real Qdrant and an agent stand-in (with the
#      chart's default agent ingress policy) in production, the Secrets the templates
#      name, the templates rendered by the real `oc process --local` and applied,
#      a runner-manager stand-in pod and a runner job stand-in pod;
#   5. the proof, which must say "proven"; two negative controls (one CI egress
#      policy deleted, one extra CI allow policy added), each of which must fail
#      with the expected static finding AND the expected live probe finding; then
#      the proof again on the restored namespaces, which must say "proven".
#
# Images come by digest from images.txt (the UBI Python base stands in for the dev
# kit image; Qdrant is the pinned release image). Generated Secret values are random
# and never printed. Records and logs go to ISO_STATE.
#
# Environment: ISO_STATE (required, an empty or absent directory); KIND_BIN, KUBECTL,
# OC, PYTHON (default: kind, kubectl, oc, python3 on PATH); ISO_CLUSTER (default
# mfr-isolation); KIND_NODE_IMAGE (default: the pinned node image); ISO_KEEP=1 keeps
# the cluster and stand-in containers. Only objects this script created are removed.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
: "${ISO_STATE:?ISO_STATE must name the lane's state directory}"
KIND_BIN=${KIND_BIN:-kind}
KUBECTL=${KUBECTL:-kubectl}
OC=${OC:-oc}
PYTHON=${PYTHON:-python3}
CLUSTER=${ISO_CLUSTER:-mfr-isolation}
NODE_IMAGE=${KIND_NODE_IMAGE:-kindest/node:v1.33.12@sha256:3f5c8443c620245e4d355cfe09e96a91ead32ceaa569d3f1ca9edf0cb2fe2ff4}
KEEP=${ISO_KEEP:-0}

PROD=mfr-iso-prod
CI=mfr-iso-ci
POD_NET=10.244.0.0/16
SVC_NET=10.96.0.0/16
PULL=mfr-iso-pull
RUNNER_TOKEN=mfr-iso-runner-token
MGR_KEY=app.kubernetes.io/component
MGR_VALUE=gitlab-runner-manager
LITELLM_PORT=4000
RUNNER_JOB=runner-isolane-project-1-concurrent-0
LABEL="mfr.isolation-lane=$CLUSTER"

say() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

for tool in "$KIND_BIN" "$KUBECTL" "$OC" "$PYTHON" docker; do
    command -v "$tool" >/dev/null 2>&1 || die "missing tool: $tool"
done
case "$CLUSTER" in *[!a-z0-9-]*|'') die "ISO_CLUSTER must be lowercase letters, digits and dashes" ;; esac
if [ -e "$ISO_STATE" ] && [ -n "$(ls -A "$ISO_STATE" 2>/dev/null)" ]; then
    die "ISO_STATE $ISO_STATE is not empty"
fi
if "$KIND_BIN" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    die "a Kind cluster named $CLUSTER already exists; this lane never reuses one"
fi
mkdir -p "$ISO_STATE/render" "$ISO_STATE/records" "$ISO_STATE/logs"
STATE=$(CDPATH= cd -- "$ISO_STATE" && pwd)
export KUBECONFIG="$STATE/kubeconfig"
umask 077

image_of() {  # name in images.txt -> name-without-tag@digest
    awk -v n="$1" '$1 == n { sub(/:[^:\/]*$/, "", $1); print $1 "@" $2; found = 1 } END { exit !found }' \
        "$ROOT/images.txt" || die "images.txt has no pin for $1"
}
DEVKIT_IMAGE=$(image_of registry.access.redhat.com/ubi9/python-314-minimal:9.8)
QDRANT_IMAGE=$(image_of docker.io/qdrant/qdrant:v1.19.2-unprivileged)
DEVKIT_REPOSITORY=${DEVKIT_IMAGE%@*}
DEVKIT_DIGEST=${DEVKIT_IMAGE#*@}
REGISTRY_PATH=${DEVKIT_REPOSITORY%/*}

STANDINS="registry gitlab litellm"
cleanup() {
    status=$?
    if [ "$KEEP" = 1 ]; then
        say "ISO_KEEP=1: keeping cluster $CLUSTER and its stand-in containers"
        exit "$status"
    fi
    for name in $STANDINS; do
        # Only a container this lane labelled is removed.
        if [ "$(docker inspect -f '{{index .Config.Labels "mfr.isolation-lane"}}' "$CLUSTER-$name" 2>/dev/null)" = "$CLUSTER" ]; then
            docker rm -f "$CLUSTER-$name" >/dev/null 2>&1 || true
        fi
    done
    if [ "${CREATED:-0}" = 1 ]; then
        "$KIND_BIN" delete cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG" >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

k() { "$KUBECTL" "$@"; }

# --------------------------------------------------------------------- 1. cluster
say "Kind cluster $CLUSTER (control plane + worker, pod $POD_NET, service $SVC_NET)"
cat > "$STATE/kind-config.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  podSubnet: "$POD_NET"
  serviceSubnet: "$SVC_NET"
nodes:
  - role: control-plane
  - role: worker
EOF
CREATED=1
"$KIND_BIN" create cluster --name "$CLUSTER" --image "$NODE_IMAGE" \
    --config "$STATE/kind-config.yaml" --kubeconfig "$KUBECONFIG" --wait 180s
k wait --for=condition=Ready nodes --all --timeout=180s
k get nodes -o wide
CNI_IMAGE=$(k -n kube-system get daemonset kindnet -o jsonpath='{.spec.template.spec.containers[0].image}')
say "CNI: $CNI_IMAGE"
API_IP=$(docker inspect -f '{{(index .NetworkSettings.Networks "kind").IPAddress}}' "$CLUSTER-control-plane")
[ -n "$API_IP" ] || die "no Kind network address for the control plane"

# --------------------------------------------------------------------- 2. site stand-ins
say "registry, GitLab and LiteLLM stand-ins on the Kind network"
for name in $STANDINS; do
    port=443
    [ "$name" = litellm ] && port=$LITELLM_PORT
    docker run -d --name "$CLUSTER-$name" --label "$LABEL" --network kind \
        --sysctl net.ipv4.ip_unprivileged_port_start=0 \
        "$DEVKIT_IMAGE" python3 -I -m http.server "$port" >/dev/null
done
standin_ip() { docker inspect -f '{{(index .NetworkSettings.Networks "kind").IPAddress}}' "$CLUSTER-$1"; }
REGISTRY_IP=$(standin_ip registry)
GITLAB_IP=$(standin_ip gitlab)
LITELLM_IP=$(standin_ip litellm)
for ip in "$REGISTRY_IP" "$GITLAB_IP" "$LITELLM_IP"; do [ -n "$ip" ] || die "a stand-in has no address"; done
peers() { printf '[{"ipBlock":{"cidr":"%s/32"}}]' "$1"; }

# --------------------------------------------------------------------- 3. DNS
# The templates allow DNS only to the namespace named openshift-dns. CoreDNS moves
# there, and the kube-dns Service (the kubelet's cluster DNS address) points at it.
say "CoreDNS behind kube-dns, in namespace openshift-dns"
k create namespace openshift-dns
k -n kube-system get configmap coredns -o json > "$STATE/render/coredns-configmap.json"
k -n kube-system get deployment coredns -o json > "$STATE/render/coredns-deployment.json"
"$PYTHON" -I - "$STATE/render" <<'PY'
import json, sys
from pathlib import Path
d = Path(sys.argv[1])
def moved(obj):
    meta = obj["metadata"]
    obj["metadata"] = {"name": meta["name"], "namespace": "openshift-dns",
                       "labels": meta.get("labels", {})}
    obj.pop("status", None)
    return obj
cm = moved(json.loads((d / "coredns-configmap.json").read_text()))
dep = moved(json.loads((d / "coredns-deployment.json").read_text()))
dep["spec"]["template"]["spec"].pop("priorityClassName", None)
objs = [cm, dep,
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": "coredns", "namespace": "openshift-dns"}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": "mfr-iso-openshift-dns-coredns"},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                     "name": "system:coredns"},
         "subjects": [{"kind": "ServiceAccount", "name": "coredns",
                       "namespace": "openshift-dns"}]}]
(d / "openshift-dns.json").write_text(json.dumps({"apiVersion": "v1", "kind": "List",
                                                  "items": objs}))
PY
k apply -f "$STATE/render/openshift-dns.json"
k -n openshift-dns rollout status deployment/coredns --timeout=180s
k -n kube-system scale deployment coredns --replicas=0
k -n kube-system patch service kube-dns --type=json -p '[{"op":"remove","path":"/spec/selector"}]'
DNS_IPS=$(k -n openshift-dns get pods -l k8s-app=kube-dns -o jsonpath='{range .items[*]}{.status.podIP}{"\n"}{end}')
"$PYTHON" -I - "$STATE/render/kube-dns-endpoints.json" $DNS_IPS <<'PY'
import json, sys
from pathlib import Path
slice_ = {"apiVersion": "discovery.k8s.io/v1", "kind": "EndpointSlice",
          "metadata": {"name": "kube-dns-openshift-dns", "namespace": "kube-system",
                       "labels": {"kubernetes.io/service-name": "kube-dns",
                                  "endpointslice.kubernetes.io/managed-by": "mfr-isolation-lane"}},
          "addressType": "IPv4",
          "ports": [{"name": "dns", "protocol": "UDP", "port": 53},
                    {"name": "dns-tcp", "protocol": "TCP", "port": 53},
                    {"name": "metrics", "protocol": "TCP", "port": 9153}],
          "endpoints": [{"addresses": [ip], "conditions": {"ready": True}}
                        for ip in sys.argv[2:]]}
assert slice_["endpoints"], "no CoreDNS pod address in openshift-dns"
Path(sys.argv[1]).write_text(json.dumps(slice_))
PY
k apply -f "$STATE/render/kube-dns-endpoints.json"

# --------------------------------------------------------------------- 4. namespaces
say "namespaces $PROD and $CI, Secrets, production stand-ins"
k create namespace "$PROD"
k create namespace "$CI"
secret_value() { "$PYTHON" -I -c 'import secrets; print(secrets.token_hex(16))'; }
for ns in "$PROD" "$CI"; do
    # The proof checks this Secret by name only. Its registry host matches no image
    # here, so the kubelet never presents these credentials to a public registry.
    printf '{"auths":{"registry.site.invalid":{"auth":"%s"}}}' \
        "$(printf 'mfr-iso:%s' "$(secret_value)" | base64 | tr -d '\n')" > "$STATE/render/pull.json"
    k -n "$ns" create secret generic "$PULL" --type=kubernetes.io/dockerconfigjson \
        --from-file=.dockerconfigjson="$STATE/render/pull.json" >/dev/null
done
rm -f "$STATE/render/pull.json"
for name in mfr-dev-gitlab mfr-dev-litellm "$RUNNER_TOKEN"; do
    secret_value > "$STATE/render/value"
    k -n "$CI" create secret generic "$name" --from-file=token="$STATE/render/value" >/dev/null
done
rm -f "$STATE/render/value"

# A real Qdrant (6333 HTTP, 6334 gRPC) and an agent HTTP stand-in on 8080, with the
# chart's default agent ingress policy (charts/mainframe-rag/templates/networkpolicy.yaml:
# same-namespace pods only).
cat > "$STATE/render/prod-release.yaml" <<EOF
apiVersion: v1
kind: List
items:
  - apiVersion: apps/v1
    kind: Deployment
    metadata: {name: rag-qdrant, namespace: $PROD, labels: {app: rag-qdrant}}
    spec:
      replicas: 1
      selector: {matchLabels: {app: rag-qdrant}}
      template:
        metadata: {labels: {app: rag-qdrant}}
        spec:
          automountServiceAccountToken: false
          securityContext: {runAsNonRoot: true, seccompProfile: {type: RuntimeDefault}}
          containers:
            - name: qdrant
              image: $QDRANT_IMAGE
              env:
                - {name: QDRANT__TELEMETRY_DISABLED, value: "true"}
                - {name: QDRANT_INIT_FILE_PATH, value: /qdrant/init/.qdrant-initialized}
              ports: [{containerPort: 6333}, {containerPort: 6334}]
              readinessProbe: {tcpSocket: {port: 6333}, periodSeconds: 2}
              securityContext:
                allowPrivilegeEscalation: false
                capabilities: {drop: [ALL]}
              volumeMounts:
                - {name: storage, mountPath: /qdrant/storage}
                - {name: snapshots, mountPath: /qdrant/snapshots}
                - {name: init, mountPath: /qdrant/init}
          volumes:
            - {name: storage, emptyDir: {}}
            - {name: snapshots, emptyDir: {}}
            - {name: init, emptyDir: {}}
  - apiVersion: v1
    kind: Service
    metadata: {name: rag-qdrant, namespace: $PROD}
    spec:
      selector: {app: rag-qdrant}
      ports:
        - {name: http, port: 6333, targetPort: 6333}
        - {name: grpc, port: 6334, targetPort: 6334}
  - apiVersion: apps/v1
    kind: Deployment
    metadata: {name: rag-agent, namespace: $PROD, labels: {app: rag-agent}}
    spec:
      replicas: 1
      selector: {matchLabels: {app: rag-agent}}
      template:
        metadata: {labels: {app: rag-agent}}
        spec:
          automountServiceAccountToken: false
          securityContext: {runAsNonRoot: true, seccompProfile: {type: RuntimeDefault}}
          containers:
            - name: agent
              image: $DEVKIT_IMAGE
              command: ["python3", "-I", "-m", "http.server", "8080"]
              ports: [{containerPort: 8080}]
              readinessProbe: {tcpSocket: {port: 8080}, periodSeconds: 2}
              securityContext:
                allowPrivilegeEscalation: false
                capabilities: {drop: [ALL]}
  - apiVersion: v1
    kind: Service
    metadata: {name: rag-agent, namespace: $PROD}
    spec:
      selector: {app: rag-agent}
      ports: [{name: http, port: 8080, targetPort: 8080}]
  - apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata: {name: rag-agent-ingress, namespace: $PROD, labels: {app: rag-agent}}
    spec:
      podSelector: {matchLabels: {app: rag-agent}}
      policyTypes: [Ingress]
      ingress:
        - from: [{podSelector: {}}]
          ports: [{protocol: TCP, port: 8080}]
EOF
k apply -f "$STATE/render/prod-release.yaml"

# --------------------------------------------------------------------- 4b. templates
say "rendering deploy/workspace templates with $("$OC" version --client 2>/dev/null | head -1)"
cat > "$STATE/render/bastion.env" <<EOF
PROD_NAMESPACE=$PROD
DEVKIT_REPOSITORY=$DEVKIT_REPOSITORY
DEVKIT_DIGEST=$DEVKIT_DIGEST
PULL_SECRET=$PULL
STORAGE_CLASS=standard
API_SERVER_CIDRS=$(peers "$API_IP")
REGISTRY_CIDRS=$(peers "$REGISTRY_IP")
GITLAB_CIDRS=$(peers "$GITLAB_IP")
BASTION_CPU_REQUEST=50m
BASTION_MEMORY_REQUEST=64Mi
BASTION_MEMORY_LIMIT=256Mi
BASTION_EPHEMERAL_STORAGE_REQUEST=64Mi
EOF
cat > "$STATE/render/ci.env" <<EOF
CI_NAMESPACE=$CI
DEVKIT_REPOSITORY=$DEVKIT_REPOSITORY
DEVKIT_DIGEST=$DEVKIT_DIGEST
QDRANT_REPOSITORY=${QDRANT_IMAGE%@*}
QDRANT_DIGEST=${QDRANT_IMAGE#*@}
PULL_SECRET=$PULL
STORAGE_CLASS=standard
API_SERVER_CIDRS=$(peers "$API_IP")
GITLAB_CIDRS=$(peers "$GITLAB_IP")
LITELLM_CIDRS=$(peers "$LITELLM_IP")
LITELLM_PORT=$LITELLM_PORT
MANAGER_POD_LABEL_KEY=$MGR_KEY
MANAGER_POD_LABEL_VALUE=$MGR_VALUE
DEV_CPU_REQUEST=50m
DEV_MEMORY_REQUEST=64Mi
DEV_MEMORY_LIMIT=256Mi
DEV_EPHEMERAL_STORAGE_REQUEST=64Mi
QDRANT_CPU_REQUEST=50m
QDRANT_MEMORY_REQUEST=64Mi
QDRANT_MEMORY_LIMIT=512Mi
QDRANT_EPHEMERAL_STORAGE_REQUEST=64Mi
EOF
"$OC" process --local -f "$ROOT/deploy/workspace/bastion.template.yaml" \
    --param-file="$STATE/render/bastion.env" -o json > "$STATE/render/bastion.json"
"$OC" process --local -f "$ROOT/deploy/workspace/ci-namespace.template.yaml" \
    --param-file="$STATE/render/ci.env" -o json > "$STATE/render/ci.json"
k apply -f "$STATE/render/bastion.json"
k apply -f "$STATE/render/ci.json"

# The runner manager stand-in: a pod with the manager selector label, its own
# ServiceAccount (the operator's, never borrowed) and the runner token Secret.
cat > "$STATE/render/manager.yaml" <<EOF
apiVersion: v1
kind: List
items:
  - apiVersion: v1
    kind: ServiceAccount
    metadata: {name: mfr-iso-runner-manager, namespace: $CI}
  - apiVersion: v1
    kind: Pod
    metadata:
      name: mfr-iso-runner-manager
      namespace: $CI
      labels: {"$MGR_KEY": "$MGR_VALUE"}
    spec:
      serviceAccountName: mfr-iso-runner-manager
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: manager
          image: $DEVKIT_IMAGE
          command: ["/bin/sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait \$!; done"]
          env:
            - name: RUNNER_TOKEN
              valueFrom: {secretKeyRef: {name: $RUNNER_TOKEN, key: token}}
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
  # A runner job stand-in, as GitLab Runner's Kubernetes executor names and labels a
  # job pod (runner-<token>-project-<id>-concurrent-<n>, its own labels plus the
  # configuration's pod_labels), so the proof classifies a live runner pod and its
  # runner probe copies that pod's labels.
  - apiVersion: v1
    kind: Pod
    metadata:
      name: $RUNNER_JOB
      namespace: $CI
      labels:
        workspace.mainframe-rag.io/role: runner
        pod: $RUNNER_JOB
        manager.runner.gitlab.com/name: mfr-gate
        project.runner.gitlab.com/id: "1"
        job.runner.gitlab.com/id: "1"
    spec:
      serviceAccountName: mfr-runner-job
      automountServiceAccountToken: false
      enableServiceLinks: false
      imagePullSecrets: [{name: $PULL}]
      restartPolicy: Never
      securityContext: {runAsNonRoot: true, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: build
          image: $DEVKIT_IMAGE
          command: ["/bin/sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait \$!; done"]
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
EOF
k apply -f "$STATE/render/manager.yaml"

say "waiting for the production, workspace and manager pods"
k -n "$PROD" rollout status deployment/rag-qdrant --timeout=300s
k -n "$PROD" rollout status deployment/rag-agent --timeout=300s
k -n "$PROD" wait --for=condition=Ready pod/mfr-bastion --timeout=300s
k -n "$CI" wait --for=condition=Ready pod/mfr-dev pod/mfr-iso-runner-manager "pod/$RUNNER_JOB" --timeout=300s
k get pods -n "$PROD" -o wide
k get pods -n "$CI" -o wide

# --------------------------------------------------------------------- 4c. runner + params
# The runner configurations as the operator renders them: every placeholder replaced.
# The helper image is never pulled (no runner job runs here); the proof reads the file.
"$PYTHON" -I - "$ROOT/deploy/workspace" "$STATE/render" \
    "CI_NAMESPACE=$CI" "PULL_SECRET=$PULL" "REGISTRY_PATH=$REGISTRY_PATH" \
    "DEVKIT_REPOSITORY=$DEVKIT_REPOSITORY" "DEVKIT_DIGEST=$DEVKIT_DIGEST" \
    "RUNNER_HELPER_REPOSITORY=$REGISTRY_PATH/gitlab-runner-helper" <<'PY'
import re, sys
from pathlib import Path
src, out = Path(sys.argv[1]), Path(sys.argv[2])
values = dict(item.split("=", 1) for item in sys.argv[3:])
values["RUNNER_HELPER_DIGEST"] = "sha256:" + "0" * 64
values.update({f"{part}_{res}_{bound}": ("100m" if res == "CPU" else "256Mi")
               for part in ("JOB", "HELPER", "SERVICE")
               for res in ("CPU", "MEMORY", "EPHEMERAL_STORAGE")
               for bound in ("REQUEST", "LIMIT")})
for name in ("gate", "wip"):
    text = (src / f"runner-{name}.example.toml").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    (out / f"runner-{name}.toml").write_text(
        re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: values[m.group(1)], body) + "\n",
        encoding="utf-8")
PY
cat "$STATE/render/bastion.env" "$STATE/render/ci.env" > "$STATE/render/proof.params"
cat >> "$STATE/render/proof.params" <<EOF
REGISTRY_PATH=$REGISTRY_PATH
RUNNER_TOKEN_SECRET=$RUNNER_TOKEN
EOF

# --------------------------------------------------------------------- 5. the proof
proof() {  # proof <name>: one full proof with live probes; prints its exit code
    set +e
    "$PYTHON" -I "$ROOT/scripts/devkit/isolation_proof.py" \
        --contract "$ROOT/deploy/workspace/contract.json" \
        --param-file "$STATE/render/proof.params" \
        --runner-config "$STATE/render/runner-gate.toml" \
        --runner-config "$STATE/render/runner-wip.toml" \
        --kubectl "$OC" --cluster-network "$POD_NET" --cluster-network "$SVC_NET" \
        --probe --output "$STATE/records/$1.json" > "$STATE/logs/proof-$1.log" 2>&1
    rc=$?
    set -e
    cat "$STATE/logs/proof-$1.log" >&2
    echo "$rc"
}

expect() {  # expect <record> <rc> <verdict> [check|subject-prefix|reason-prefix ...]
    "$PYTHON" -I - "$@" <<'PY'
import json, sys
path, rc, rc_want, verdict_want, *wanted = sys.argv[1:]
record = json.loads(open(path, encoding="utf-8").read())
findings = record.get("findings", [])
errors = []
if rc != rc_want:
    errors.append(f"exit {rc}, expected {rc_want}")
if record.get("verdict") != verdict_want:
    errors.append(f"verdict {record.get('verdict')}, expected {verdict_want}")
probes = record.get("checks", {}).get("probes", {})
if probes.get("status") != "ran":
    errors.append("the live probes did not run")
for item in wanted:
    check, subject, reason = item.split("|")
    if not any(f["kind"] == "violation" and f["check"] == check
               and f["subject"].startswith(subject) and f["reason"].startswith(reason)
               for f in findings):
        errors.append(f"missing violation: {item}")
summary = {"verdict": record.get("verdict"), "findings": len(findings),
           "probe_results": len(probes.get("results", []))}
print(f"{path}: {json.dumps(summary)}")
for f in findings:
    print(f"  {f['kind']}: {f['check']} {f['subject']}: {f['reason']}")
if errors:
    print("\n".join(f"FAIL: {e}" for e in errors), file=sys.stderr)
    sys.exit(1)
PY
}

say "proof on the rendered workspace (must be proven)"
rc=$(proof baseline)
expect "$STATE/records/baseline.json" "$rc" 0 proven

say "negative control 1: CI policy mfr-dev-egress deleted (must fail)"
k -n "$CI" delete networkpolicy mfr-dev-egress
rc=$(proof deleted-dev-egress)
expect "$STATE/records/deleted-dev-egress.json" "$rc" 1 violation \
    "egress-policies|$CI/mfr-dev-egress|missing" \
    "probes|dev -> gitlab $GITLAB_IP:443|unexpected-block"
k apply -f "$STATE/render/ci.json"

say "negative control 2: an extra CI allow policy to production Qdrant (must fail)"
cat > "$STATE/render/extra-allow.yaml" <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: mfr-iso-extra-allow, namespace: $CI}
spec:
  podSelector: {matchLabels: {workspace.mainframe-rag.io/role: dev}}
  policyTypes: [Egress]
  egress:
    - to: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: $PROD}}}]
      ports: [{protocol: TCP, port: 6333}]
EOF
k apply -f "$STATE/render/extra-allow.yaml"
rc=$(proof extra-allow)
expect "$STATE/records/extra-allow.json" "$rc" 1 violation \
    "egress-policies|$CI/mfr-iso-extra-allow|policy-outside-contract" \
    "probes|dev -> prod_qdrant|unexpected-reach"
k -n "$CI" delete networkpolicy mfr-iso-extra-allow

say "proof on the restored workspace (must be proven again)"
rc=$(proof restored)
expect "$STATE/records/restored.json" "$rc" 0 proven

say "workspace isolation lane PASSED (records in $STATE/records)"
