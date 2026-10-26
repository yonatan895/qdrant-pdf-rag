#!/bin/sh
# Open WebUI dev environment (#1070; grew out of the #1073 hands-on setup).
# Supported way to run the main docs platform locally: a long-lived Kind
# cluster that renders the chart (charts/mainframe-rag) and the Rosetta Pipe
# (integrations/openwebui/docs_pipe.py) from this working tree, with a test IdP,
# a router stand-in and the local agent lane. Connected dev host only.
#
#   sh scripts/openwebui/dev.sh [--dry-run] COMMAND
#
# up          images, agent-up, cluster, deploy, relay, provision, urls (heavy lock held)
# images      build the derived Open WebUI image (#1085 Containerfile); check the
#             pinned Dex, nginx-unprivileged and Kind node images
# agent-up    the maintainer's local lane: gemma-4-E4B (8000) + Qwen3-Embedding-0.6B
#             (8001) vLLM, a dedicated persistent Qdrant (6342) with a real-manual
#             subset, scripts/run_local_stack.sh (gateway 4000, agent 127.0.0.1:8080),
#             reranker off; each as a systemd --user unit named owui-hands-on-*
# cluster     create Kind cluster $KIND_NAME, host ports 127.0.0.1:443 and :5556,
#             node memory cap, image load, CoreDNS rewrite dex.localhost -> Dex
# deploy      local CA and certificates, Dex users, render the working tree's chart
#             (Open WebUI objects) and Pipe plus the Kind stand-ins, apply
# relay       host relay (Kind gateway -> 127.0.0.1:$LOCAL_AGENT_PORT), no idle timeout
# provision   admin API key, the chart's Pipe installer, groups, Rosetta model grants
# smoke       both users sign in, Rosetta-only models, one question with source chips,
#             the allow-list's blocked paths answer 403 from the proxy sidecar
# ready       run the startup checks (health, smoke) and write STATE/READY.md (mode 600):
#             URLs, which file holds which password, check results and exit codes,
#             status/down commands, RAM in use
# status      cluster, pods, units, agent, URLs, memory
# urls        the URL and the login steps
# down        delete only the cluster this script created, and stop the relay
# agent-down  stop only the owui-hands-on-* units and the dedicated Qdrant (volume kept)
#
# --dry-run validates every input without docker, Kind or a cluster: tools, git
# refs, pins, the chart render and patches, the stand-in manifests, ports,
# memory, the lock. State lives in $OWUI_STATE (mode 700, outside git):
# passwords, keys, certificates, kubeconfig, logs. Owner doc: docs/openwebui-dev.md;
# design record: docs/records/2026-10-24-openwebui-hands-on.md.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$HERE/../.." && pwd)
MAIN_REPO=${MAIN_REPO:-$HOME/projects/mainframe-rag}
# The state directory of the #1073 hands-on setup is reused while it is the only one.
if [ -n "${OWUI_STATE:-}" ]; then STATE=$OWUI_STATE
elif [ ! -d "$HOME/.local/share/mainframe-rag/openwebui-dev" ] && [ -d "$HOME/.local/share/mainframe-rag/openwebui-trial" ]; then
    STATE=$HOME/.local/share/mainframe-rag/openwebui-trial
else STATE=$HOME/.local/share/mainframe-rag/openwebui-dev; fi
TOOLS=${KIND_TOOLS:-$HOME/.config/mainframe-rag/kind-live/bin}
KIND_BIN=${KIND_BIN:-$TOOLS/kind}
KUBECTL=${KUBECTL:-$TOOLS/kubectl}
HELM=${HELM:-$TOOLS/helm}
KIND_NAME=${KIND_NAME:-owui-hands-on}
NODE="$KIND_NAME-control-plane"
KUBECONFIG="$STATE/kubeconfig"
export KUBECONFIG
LOCK=${HEAVY_LOCK:-$HOME/.cache/agent-tmp/mainframe-rag-heavy.lock}
APP_NS=owui
OWUI_HOST=owui.localhost
DEX_HOST=dex.localhost
OWUI_PORT=${OWUI_PORT:-443}
DEX_PORT=${DEX_PORT:-5556}
KIND_NODE_MEM_MB=${KIND_NODE_MEM_MB:-4096}
HEADROOM_MB=${HEADROOM_MB:-1024}
# Whole-plan host RAM, from the 2026-10-25 bring-up (reasoning vLLM 5.35 GiB host RSS,
# embed about 2 GB, gateway + OpenObserve + agent + Qdrant about 2 GB). Each server's
# own guard checks only itself, so `up` checks the sum before it starts anything.
REASONING_EST_MB=${REASONING_EST_MB:-5500}
EMBED_EST_MB=${EMBED_EST_MB:-2000}
STACK_EST_MB=${STACK_EST_MB:-2048}
LOCAL_AGENT_PORT=${LOCAL_AGENT_PORT:-8080}
GATEWAY_PORT=${GATEWAY_PORT:-4000}
HOST_RELAY_PORT=${HOST_RELAY_PORT:-18080}
# The local agent lane runs from this working tree; AGENT_PY needs the project's dependencies.
AGENT_SRC=${AGENT_SRC:-$REPO}
AGENT_PY=${AGENT_PY:-$MAIN_REPO/.venv/bin/python}
PY=$AGENT_PY
POD_CIDR=10.244.0.0/16
AGENT_REUSE=${AGENT_REUSE:-0}
AGENT_WAIT_S=${AGENT_WAIT_S:-7200}
QDRANT_PORT=${OWUI_QDRANT_PORT:-6342}
QDRANT_CONTAINER=owui-hands-on-qdrant
QDRANT_VOLUME=owui-hands-on-qdrant
REASONING_MODEL=google/gemma-4-E4B-it-qat-mobile-ct
EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B
VLLM_IMAGE=${VLLM_IMAGE:-vllm/vllm-openai@sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14}
CORPUS_SRC=${CORPUS_SRC:-$HOME/.config/mainframe-rag/kind-live/corpus}
CORPUS_COUNT=${CORPUS_COUNT:-12}
INGEST=${INGEST:-auto}
BM25_DIR=${BM25_CACHE_DIR:-$MAIN_REPO/bundles/bm25-weights}
UNIT=owui-hands-on
OWUI_IMAGE=${OWUI_IMAGE:-ghcr.io/yonatan895/qdrant-pdf-rag-openwebui:v0.12.3-slim}
DEX_IMAGE=trial.local/dex:v2.46.0-distroless
NGINX_IMAGE=trial.local/nginx-unprivileged:1.30.6-alpine
OWUI_URL="https://$OWUI_HOST"
[ "$OWUI_PORT" = 443 ] || OWUI_URL="https://$OWUI_HOST:$OWUI_PORT"
DEX_ISSUER="https://$DEX_HOST:$DEX_PORT/dex"
export OWUI_URL DEX_ISSUER
PYH="python3 -I $HERE/client.py"

DRYRUN=0
CMD=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRYRUN=1 ;;
        -*) echo "unknown option $arg" >&2; exit 2 ;;
        *) [ -z "$CMD" ] || { echo "one command only" >&2; exit 2; }; CMD=$arg ;;
    esac
done

# shellcheck source=lib.sh
. "$HERE/lib.sh"

say() { echo "==> $*"; }
plan() { echo "[dry-run] $*"; }
die() {
    echo "ERROR: $*" >&2
    [ -d "$STATE/logs" ] && echo "$*" > "$STATE/logs/last-error.txt"
    exit 1
}
started() {  # record what this `up` run started, for the failure cleanup
    [ "$DRYRUN" = 1 ] || echo "$1" >> "$STATE/run-started.txt"
}
k() { "$KUBECTL" --context "kind-$KIND_NAME" "$@"; }
mem_avail_mb() { awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo; }
port_busy() { [ -n "$(ss -ltnH "sport = :$1" 2>/dev/null)" ]; }
unit_active() { systemctl --user is-active --quiet "$1.service" 2>/dev/null; }

NODE_IMAGE=$(pin NODE_IMAGE)
admit() {  # admit NEED_MB WHAT
    avail=$(mem_avail_mb)
    if [ "$avail" -ge "$1" ]; then verdict=pass; else verdict=refuse; fi
    if [ "$DRYRUN" = 1 ]; then
        if [ "$verdict" = pass ]; then plan "admission $2: need ${1} MB, MemAvailable ${avail} MB now: would pass"
        else plan "admission $2: need ${1} MB, MemAvailable ${avail} MB now: would refuse (up exits 75, starts nothing)"; fi
        return 0
    fi
    if [ "$verdict" != pass ]; then
        echo "ERROR: admission $2: MemAvailable ${avail} MB < ${1} MB" >&2
        [ -d "$STATE/logs" ] && echo "admission $2: MemAvailable ${avail} MB < ${1} MB" > "$STATE/logs/last-error.txt"
        exit 75
    fi
    say "admission $2: MemAvailable ${avail} MB >= ${1} MB"
}

budget() {  # print the whole-plan table (MiB) against MemAvailable, then admit its sum
    r=$REASONING_EST_MB; e=$EMBED_EST_MB; rn=""; en=""
    printf '%s\n' "$(served_ids 8000)" | grep -qx "$REASONING_MODEL" && { r=0; rn=", already served on 8000"; }
    printf '%s\n' "$(served_ids 8001)" | grep -qx "$EMBED_MODEL" && { e=0; en=", already served on 8001"; }
    need=$((r + e + STACK_EST_MB + KIND_NODE_MEM_MB + HEADROOM_MB))
    if [ "$DRYRUN" = 1 ]; then plan "memory budget (whole plan):"; else say "memory budget (whole plan):"; fi
    printf '  %-62s %6s\n' component MiB \
        "reasoning vLLM (REASONING_EST_MB; 5.35 GiB RSS measured$rn)" "$r" \
        "embed vLLM (EMBED_EST_MB$en)" "$e" \
        "gateway, OpenObserve, agent, Qdrant (STACK_EST_MB)" "$STACK_EST_MB" \
        "Kind node cap: Open WebUI, Dex, router (KIND_NODE_MEM_MB)" "$KIND_NODE_MEM_MB" \
        "headroom (HEADROOM_MB)" "$HEADROOM_MB" \
        "total needed" "$need" \
        "MemAvailable now" "$(mem_avail_mb)"
    admit "$need" "whole plan"
}

state_dir() {
    mkdir -p "$STATE" "$STATE/logs" "$STATE/render"
    chmod 700 "$STATE"
}

# ------------------------------------------------------------------ images
images() {
    state_dir
    build_images
}

# ------------------------------------------------------------------ local lane
served_ids() {  # served_ids PORT -> model ids, one per line (empty if nothing answers)
    curl -s -m 5 "http://127.0.0.1:$1/v1/models" 2>/dev/null | python3 -I -c '
import json, sys
try:
    print("\n".join(m.get("id", "") for m in json.load(sys.stdin).get("data", [])))
except ValueError:
    pass' || true
}

unit_env() {
    printf '%s\n' "--setenv=HOME=$HOME" \
        "--setenv=PATH=$AGENT_SRC/.tools/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
        "--setenv=TMPDIR=$HOME/.cache/agent-tmp" "--setenv=_ZO_DOCTOR=0"
}

start_model() {  # start_model ROLE PORT TASK MODEL
    ids=$(served_ids "$2")
    if [ -n "$ids" ]; then
        if printf '%s\n' "$ids" | grep -qx "$4"; then
            say "$1: $4 already serving on 127.0.0.1:$2 (reused, not owned)"
            return 0
        fi
        if [ "$DRYRUN" = 1 ]; then
            plan "$1: 127.0.0.1:$2 serves another model now (expected while G4 tunnels run); up would refuse until it is free"
            return 0
        fi
        die "$1: 127.0.0.1:$2 serves another model, not $4; stop it first"
    fi
    if port_busy "$2"; then
        [ "$DRYRUN" = 1 ] && { plan "$1: port $2 is held by another process now; up would refuse"; return 0; }
        die "$1: port $2 is held by another process"
    fi
    if [ "$DRYRUN" = 1 ]; then
        plan "$1: systemd-run --user --unit $UNIT-$1 sh scripts/tools/run-task.sh $3 CONTAINER_NAME=$UNIT-$1 MODEL=$4 SERVED_NAME=$4 PORT=$2 BUDGET_PROFILE=LOCAL_RT_8GB VLLM_IMAGE=$VLLM_IMAGE; wait for /health"
        return 0
    fi
    # shellcheck disable=SC2046
    systemd-run --user --quiet --collect --unit "$UNIT-$1" -p WorkingDirectory="$AGENT_SRC" \
        -p StandardOutput="append:$STATE/logs/$1.log" -p StandardError="append:$STATE/logs/$1.log" \
        $(unit_env) sh scripts/tools/run-task.sh "$3" CONTAINER_NAME="$UNIT-$1" MODEL="$4" SERVED_NAME="$4" \
        PORT="$2" BUDGET_PROFILE=LOCAL_RT_8GB VLLM_IMAGE="$VLLM_IMAGE"
    started "unit $UNIT-$1"
    i=0
    until curl -s -m 3 -o /dev/null "http://127.0.0.1:$2/health"; do
        unit_active "$UNIT-$1" || die "$1 server exited; see $STATE/logs/$1.log"
        i=$((i + 5)); [ "$i" -lt 1200 ] || die "$1 not healthy within 1200 s; see $STATE/logs/$1.log"
        sleep 5
    done
    say "$1: $4 serving on 127.0.0.1:$2 (unit $UNIT-$1)"
}

agent_src() {
    if [ "$DRYRUN" = 0 ]; then
        got=$(env PYTHONPATH="$AGENT_SRC/src" "$AGENT_PY" -c 'import mainframe_rag.agent.app as a; print(a.__file__)' 2>"$STATE/logs/agent-import.log") \
            || die "the agent does not import with $AGENT_PY; see $STATE/logs/agent-import.log (set AGENT_PY)"
        case "$got" in "$AGENT_SRC"/*) ;; *) die "agent imported from $got, not $AGENT_SRC" ;; esac
        say "agent source $AGENT_SRC (rules version: any rule-module change needs INGEST=always)"
    else
        plan "agent source: $AGENT_SRC (this working tree); the agent must import from it with $AGENT_PY"
    fi
}

corpus_subset() {
    [ -d "$CORPUS_SRC" ] || die "CORPUS_SRC $CORPUS_SRC is not a directory"
    python3 -I - "$CORPUS_SRC" "$STATE/corpus-subset" "$CORPUS_COUNT" "$DRYRUN" <<'PY'
import os, shutil, sys
src, dst, count, dry = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4] == "1"
pdfs = sorted(os.path.relpath(os.path.join(d, f), src) for d, _, fs in os.walk(src)
              for f in fs if f.lower().endswith(".pdf"))
if not pdfs:
    sys.exit("no PDFs under CORPUS_SRC")
step = max(1, len(pdfs) // count)
pick = pdfs[::step][:count]
size = sum(os.path.getsize(os.path.join(src, p)) for p in pick)
manifest = dst + ".txt"
if os.path.exists(manifest):
    print(f"corpus subset: reusing {manifest}")
    sys.exit(0)
if dry:
    print(f"[dry-run] corpus subset: {len(pick)} of {len(pdfs)} PDFs, {size // 2**20} MiB, "
          f"relative layout kept, hard links under {dst}")
    sys.exit(0)
for rel in pick:
    target = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        os.link(os.path.join(src, rel), target)
    except OSError:
        shutil.copy2(os.path.join(src, rel), target)
fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as fh:
    fh.write("\n".join(pick) + "\n")
print(f"corpus subset: {len(pick)} of {len(pdfs)} PDFs, {size // 2**20} MiB")
PY
}

agent_up() {
    state_dir
    if curl -s -m 3 -o /dev/null "http://127.0.0.1:$LOCAL_AGENT_PORT/healthz"; then
        if unit_active "$UNIT-stack"; then say "agent: our local stack is already up"; return 0; fi
        if [ "$AGENT_REUSE" = 1 ]; then say "agent: reusing the agent on 127.0.0.1:$LOCAL_AGENT_PORT (AGENT_REUSE=1, not owned)"; return 0; fi
        [ "$DRYRUN" = 1 ] && { plan "agent: 127.0.0.1:$LOCAL_AGENT_PORT answers now (another stack, e.g. G4); up refuses unless AGENT_REUSE=1 or it stops"; }
        [ "$DRYRUN" = 1 ] || die "an agent not started here answers on 127.0.0.1:$LOCAL_AGENT_PORT; stop it or set AGENT_REUSE=1"
    fi
    if port_busy "$GATEWAY_PORT"; then
        [ "$DRYRUN" = 1 ] && plan "gateway: port $GATEWAY_PORT is held now (G4 gateway); up refuses until it is free"
        [ "$DRYRUN" = 1 ] || die "gateway port $GATEWAY_PORT is held by another process"
    fi
    agent_src
    budget
    start_model reasoning 8000 local:llm "$REASONING_MODEL"
    start_model embed 8001 local:embed "$EMBED_MODEL"
    if [ "$DRYRUN" = 1 ]; then
        plan "qdrant: SIM_CONTAINER=$QDRANT_CONTAINER SIM_PORT=$QDRANT_PORT SIM_QDRANT_VOLUME=$QDRANT_VOLUME sh scripts/sim_qdrant.sh up"
    else
        docker inspect "$QDRANT_CONTAINER" >/dev/null 2>&1 || started "qdrant"
        SIM_CONTAINER=$QDRANT_CONTAINER SIM_PORT=$QDRANT_PORT SIM_QDRANT_VOLUME=$QDRANT_VOLUME PY=$AGENT_PY \
            sh "$AGENT_SRC/scripts/sim_qdrant.sh" up
        i=0
        until curl -s -m 3 -o /dev/null "http://127.0.0.1:$QDRANT_PORT/readyz"; do
            i=$((i + 1)); [ "$i" -lt 60 ] || die "Qdrant did not answer on $QDRANT_PORT"; sleep 1
        done
    fi
    corpus_subset
    corpus_env=""
    case "$INGEST" in
        always) corpus_env="--setenv=CORPUS_DIR=$STATE/corpus-subset" ;;
        never) ;;
        auto)
            if curl -s -m 5 "http://127.0.0.1:$QDRANT_PORT/aliases" 2>/dev/null | grep -q '"alias_name":"mainframe_manuals"'; then
                say "ingest: alias mainframe_manuals present in $QDRANT_CONTAINER; skipped"
            else
                corpus_env="--setenv=CORPUS_DIR=$STATE/corpus-subset"
            fi ;;
        *) die "INGEST must be auto, always or never" ;;
    esac
    if [ "$DRYRUN" = 1 ]; then
        plan "stack: systemd-run --user --unit $UNIT-stack (cwd $AGENT_SRC) PYTHONPATH=$AGENT_SRC/src QDRANT_URL=http://127.0.0.1:$QDRANT_PORT RERANK_ENABLED=false BM25_CACHE_DIR=$BM25_DIR ${corpus_env:+CORPUS_DIR=$STATE/corpus-subset }sh scripts/tools/run-task.sh local:stack"
        plan "stack: wait for http://127.0.0.1:$LOCAL_AGENT_PORT/healthz (ingest first when CORPUS_DIR is set; up to $AGENT_WAIT_S s)"
        return 0
    fi
    # shellcheck disable=SC2046,SC2086
    systemd-run --user --quiet --collect --unit "$UNIT-stack" -p WorkingDirectory="$AGENT_SRC" \
        -p StandardOutput="append:$STATE/logs/stack.log" -p StandardError="append:$STATE/logs/stack.log" \
        $(unit_env) --setenv=PYTHONPATH="$AGENT_SRC/src" --setenv=PY="$AGENT_PY" \
        --setenv=QDRANT_URL="http://127.0.0.1:$QDRANT_PORT" --setenv=RERANK_ENABLED=false \
        --setenv=BM25_CACHE_DIR="$BM25_DIR" --setenv=SIM_BM25_CACHE_DIR="$BM25_DIR" \
        --setenv=LOCAL_STACK_LOG_DIR="$STATE/logs" --setenv=GATEWAY_ENV_FILE="$STATE/gateway.env" \
        --setenv=LOCAL_AGENT_PORT="$LOCAL_AGENT_PORT" --setenv=GATEWAY_PORT="$GATEWAY_PORT" \
        $corpus_env sh scripts/tools/run-task.sh local:stack
    started "unit $UNIT-stack"
    i=0
    until curl -s -m 3 -o /dev/null "http://127.0.0.1:$LOCAL_AGENT_PORT/healthz"; do
        unit_active "$UNIT-stack" || die "local stack exited; see $STATE/logs/stack.log"
        i=$((i + 10)); [ "$i" -lt "$AGENT_WAIT_S" ] || die "agent not up within $AGENT_WAIT_S s; see $STATE/logs/stack.log"
        sleep 10
    done
    say "agent: local stack up on 127.0.0.1:$LOCAL_AGENT_PORT (unit $UNIT-stack)"
}

agent_down() {
    for u in stack embed reasoning; do
        if systemctl --user list-units --all --plain --no-legend "$UNIT-$u.service" | grep -q .; then
            systemctl --user stop "$UNIT-$u.service" || true
            say "stopped unit $UNIT-$u"
        fi
    done
    if docker inspect "$QDRANT_CONTAINER" >/dev/null 2>&1; then
        SIM_CONTAINER=$QDRANT_CONTAINER SIM_PORT=$QDRANT_PORT sh "$AGENT_SRC/scripts/sim_qdrant.sh" down
        say "stopped $QDRANT_CONTAINER (volume $QDRANT_VOLUME kept)"
    fi
}

# ------------------------------------------------------------------ cluster
kind_config() {
    cat <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking: {podSubnet: $POD_CIDR}
nodes:
  - role: control-plane
    extraPortMappings:
      # Browser paths, loopback only: the router stand-in and the test IdP.
      - {containerPort: 30443, hostPort: $OWUI_PORT, listenAddress: 127.0.0.1, protocol: TCP}
      - {containerPort: 30556, hostPort: $DEX_PORT, listenAddress: 127.0.0.1, protocol: TCP}
EOF
}

ours() {  # the cluster exists and is the one this script created
    [ -f "$STATE/cluster.created" ] || return 1
    want=$(sed -n 's/^node_id=//p' "$STATE/cluster.created")
    have=$(docker inspect -f '{{.Id}}' "$NODE" 2>/dev/null || true)
    [ -n "$want" ] && [ "$want" = "$have" ]
}

cluster() {
    state_dir
    kind_config > "$STATE/kind-config.yaml"
    for p in "$OWUI_PORT" "$DEX_PORT"; do
        if port_busy "$p"; then
            [ "$DRYRUN" = 1 ] && { plan "cluster: host port $p is in use now; create would fail"; continue; }
            ours || die "host port $p is in use"
        fi
    done
    if [ "$DRYRUN" = 1 ]; then
        admit $((KIND_NODE_MEM_MB + HEADROOM_MB)) "Kind node (cap $KIND_NODE_MEM_MB MB)"
        plan "cluster: $KIND_BIN create cluster --name $KIND_NAME --image $NODE_IMAGE --config $STATE/kind-config.yaml --kubeconfig $KUBECONFIG"
        plan "cluster: record the node container id in $STATE/cluster.created; docker update --memory ${KIND_NODE_MEM_MB}m"
        plan "cluster: kind load $OWUI_IMAGE; skopeo copy $DEX_PIN and nginx@$(nginx_pin) into the node; CoreDNS rewrite $DEX_HOST -> dex.owui-idp.svc.cluster.local"
        return 0
    fi
    if "$KIND_BIN" get clusters 2>/dev/null | grep -qx "$KIND_NAME"; then
        ours || die "a cluster named $KIND_NAME exists that this script did not create; refusing"
        say "cluster $KIND_NAME already up (ours)"
        return 0
    fi
    admit $((KIND_NODE_MEM_MB + HEADROOM_MB)) "Kind node (cap $KIND_NODE_MEM_MB MB)"
    "$KIND_BIN" create cluster --name "$KIND_NAME" --image "$NODE_IMAGE" --config "$STATE/kind-config.yaml" \
        --kubeconfig "$KUBECONFIG" --wait 120s
    chmod 600 "$KUBECONFIG"
    started "cluster"
    printf 'name=%s\nnode_id=%s\ncreated=%s\n' "$KIND_NAME" "$(docker inspect -f '{{.Id}}' "$NODE")" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATE/cluster.created"
    docker update --memory "${KIND_NODE_MEM_MB}m" --memory-swap "${KIND_NODE_MEM_MB}m" "$NODE" >/dev/null
    load_images
    coredns_rewrite
}

# ------------------------------------------------------------------ deploy
render_dev_app() {  # render_dev_app OUT HOST_GW  (application namespace, default deny, agent relay)
    sed -e "s#@APP_NS@#$APP_NS#g" -e "s#@HOST_GW@#$2#g" -e "s#@HOST_RELAY_PORT@#$HOST_RELAY_PORT#g" \
        -e "s#@RELAY_UID@#$(uid_for relay)#g" -e "s#@NGINX_IMAGE@#$NGINX_IMAGE#g" \
        "$HERE/dev-app.yaml" > "$1"
}

deploy() {
    state_dir
    if [ "$DRYRUN" = 1 ]; then
        d="$STATE/dryrun"; mkdir -p "$d"
        pki
        render_chart "$d"
        for _u in $($PYH users "$STATE"); do echo '$2y$10$dryrundryrundryrundryruOdryrundryrundryrundryrundryru'; done > "$d/hashes.txt"
        printf '{"webui": "dry", "client": "dry"}' > "$d/secrets.json"
        # The Dex config is generated by the same code with dry-run inputs.
        python3 -I "$HERE/client.py" dex-config "$d" "$d/hashes.txt" > "$d/dex-config.yaml"
        "$PY" -I -c 'import sys, yaml; c = yaml.safe_load(open(sys.argv[1])); assert c["issuer"].startswith("https://"); print("dex config: issuer", c["issuer"], "users", len(c["staticPasswords"]), "redirect", c["staticClients"][0]["redirectURIs"][0])' "$d/dex-config.yaml"
        render_support "$d/support.yaml" 172.18.0.250 0000
        render_dev_app "$d/dev-app.yaml" 172.18.0.1
        "$PY" -I -c 'import sys, yaml; n = [x for f in sys.argv[1:] for x in yaml.safe_load_all(open(f)) if x]; print("dev manifests parse:", len(n), "objects")' "$d/support.yaml" "$d/dev-app.yaml"
        check_render "$d"
        plan "deploy: local CA (name-constrained) + certificates in $STATE/pki; passwords via client.py secrets; bcrypt via the Open WebUI image (network none)"
        plan "deploy: apply dev-app.yaml, support.yaml, Secrets and openwebui.yaml; wait for dex, router, agent-relay, rag-openwebui"
        return 0
    fi
    ours || die "cluster $KIND_NAME is not one this script created; run cluster first"
    pki
    $PYH secrets "$STATE"
    bcrypt_hashes
    render_chart "$STATE/render"
    set -- $(kind_net)
    render_dev_app "$STATE/render/dev-app.yaml" "$1"
    render_support "$STATE/render/support.yaml" "$2" "$(sha256sum "$STATE/dex-config.yaml" | cut -c1-16)"
    check_render "$STATE/render"
    k apply -f "$STATE/render/dev-app.yaml" > "$STATE/logs/apply-dev-app.log"
    k apply -f "$STATE/render/support.yaml" > "$STATE/logs/apply-support.log"
    apply_stack
    k -n owui-idp rollout status deploy/dex --timeout=300s
    k -n "$APP_NS" rollout status deploy/agent-relay --timeout=300s
    k -n "$APP_NS" rollout status deploy/rag-openwebui --timeout=900s
    k -n owui-router rollout status deploy/router --timeout=300s
}

# ------------------------------------------------------------------ relay
relay() {
    if [ "$DRYRUN" = 1 ]; then
        python3 -I -m py_compile "$HERE/relay.py" "$HERE/client.py"
        port_busy "$HOST_RELAY_PORT" && plan "relay: port $HOST_RELAY_PORT is in use now; relay would fail to bind"
        plan "relay: systemd-run --user --unit $UNIT-relay python3 -I relay.py --listen <kind gateway>:$HOST_RELAY_PORT --target 127.0.0.1:$LOCAL_AGENT_PORT --allow <node ip>"
        return 0
    fi
    unit_active "$UNIT-relay" && { say "relay already running (unit $UNIT-relay)"; return 0; }
    set -- $(kind_net)
    systemd-run --user --quiet --collect --unit "$UNIT-relay" -p Restart=on-failure -p MemoryMax=128M \
        -p StandardOutput="append:$STATE/logs/relay.log" -p StandardError="append:$STATE/logs/relay.log" \
        python3 -I "$HERE/relay.py" --listen "$1:$HOST_RELAY_PORT" \
        --target "127.0.0.1:$LOCAL_AGENT_PORT" --allow "$2"
    started "unit $UNIT-relay"
    say "relay $1:$HOST_RELAY_PORT -> 127.0.0.1:$LOCAL_AGENT_PORT for node $2 (unit $UNIT-relay)"
}

# ------------------------------------------------------------------ provision
provision() {
    if [ "$DRYRUN" = 1 ]; then
        plan "provision: owui-admin signs in, creates an API key (STATE/admin-api-key, mode 600)"
        plan "provision: kubectl exec -i deploy/rag-openwebui -c openwebui -- python3 /opt/mainframe-rag/openwebui/provision_pipe.py <pipe sha256> < admin-api-key"
        plan "provision: groups internal-team and docs-readers; Rosetta model (mainframe_rag_docs) read grant to both; team-user and docs-user sign in"
        return 0
    fi
    provision_live
}

# ------------------------------------------------------------------ status / urls / down
urls() {
    cat <<EOF
Open WebUI (Windows Chrome or Edge; Firefox also works): $OWUI_URL/
  1. First visit only: the pages use a local CA ($STATE/pki/ca.crt). Accept the
     certificate warning for $OWUI_HOST and, after the redirect, for $DEX_HOST:$DEX_PORT
     (or import that CA into the Windows user store and remove it when done).
  2. Click the single sign-on button, then sign in at the test IdP with
       team-user@trial.test   password in $STATE/team-user.password   (group internal-team)
       docs-user@trial.test   password in $STATE/docs-user.password   (group docs-readers)
     Admin (Pipe install, grants): owui-admin@trial.test, $STATE/owui-admin.password
  3. Pick the "Rosetta" model (the only one) and ask. Answers come from the local agent.
Test IdP: $DEX_ISSUER  (discovery $DEX_ISSUER/.well-known/openid-configuration)
Status: sh $HERE/dev.sh status    Stop: sh $HERE/dev.sh down (cluster) and sh $HERE/dev.sh agent-down (local lane)
EOF
}

status() {
    echo "cluster $KIND_NAME:"
    if ours; then echo "  present, created by this script ($(sed -n 's/^created=//p' "$STATE/cluster.created"))"
    else echo "  not present (or not ours)"; fi
    if ours; then
        k get pods -A -o wide --no-headers 2>/dev/null | awk '$1 ~ /^owui/ {print "  " $1 "/" $2, $3, $4, "restarts=" $5}'
        docker stats --no-stream --format '  node memory {{.MemUsage}}' "$NODE" 2>/dev/null || true
    fi
    echo "units:"
    for u in relay stack reasoning embed; do
        printf '  %s-%s %s\n' "$UNIT" "$u" "$(systemctl --user is-active "$UNIT-$u.service" 2>/dev/null || true)"
    done
    echo "local lane:"
    printf '  agent 127.0.0.1:%s/healthz HTTP %s\n' "$LOCAL_AGENT_PORT" \
        "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$LOCAL_AGENT_PORT/healthz" || true)"
    for p in 8000 8001; do printf '  127.0.0.1:%s serves: %s\n' "$p" "$(served_ids "$p" | tr '\n' ' ')"; done
    echo "browser paths (from WSL):"
    # The pipeline's status is sed's, so test health itself (N7 of #1095).
    if health=$($PYH health "$STATE" 2>/dev/null); then echo "  $health"
    else echo "  not reachable${health:+: $health}"; fi
    if command -v powershell.exe >/dev/null 2>&1; then
        code=$(timeout 30 powershell.exe -NoProfile -Command \
            "curl.exe -s -o NUL -w '%{http_code}' -k --resolve $OWUI_HOST:$OWUI_PORT:127.0.0.1 $OWUI_URL/health" 2>/dev/null | tr -d '\r' || true)
        echo "  from Windows: $OWUI_URL/health HTTP ${code:-unreachable}"
    fi
    echo "MemAvailable $(mem_avail_mb) MB"
    urls
}

ram_in_use() {
    awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END {printf "host: %d MB in use of %d MB (MemAvailable %d MB)\n", (t-a)/1024, t/1024, a/1024}' /proc/meminfo
    docker stats --no-stream --format '{{.Name}}: {{.MemUsage}}' "$NODE" "$UNIT-reasoning" "$UNIT-embed" \
        "$QDRANT_CONTAINER" 2>/dev/null || true
    for u in stack relay; do
        m=$(systemctl --user show -p MemoryCurrent --value "$UNIT-$u.service" 2>/dev/null || true)
        case "$m" in ''|'[not set]'|infinity) ;; *) echo "unit $UNIT-$u: $((m / 1048576)) MB (host processes)" ;; esac
    done
}

ready() {
    set +e
    $PYH health "$STATE" > "$STATE/logs/check-health.json" 2>&1; rc_health=$?
    $PYH smoke "$STATE" > "$STATE/logs/check-smoke.json" 2>&1; rc_smoke=$?
    set -e
    ( umask 077; {
        echo "# Open WebUI dev environment: ready report"
        echo
        echo "Written $(date '+%Y-%m-%d %H:%M %Z') by \`dev.sh ready\` (working tree $(git -C "$REPO" rev-parse --short HEAD)$(git -C "$REPO" diff --quiet HEAD -- charts integrations 2>/dev/null || echo " + uncommitted chart or Pipe edits"); Pipe sha256 $(cut -c1-16 "$STATE/render/pipe.sha256" 2>/dev/null))."
        echo
        echo "## URLs and sign-in"
        echo
        echo '```'
        urls
        echo '```'
        echo
        echo "## Password files (contents not shown here)"
        echo
        for u in team-user docs-user owui-admin; do echo "- \`$u@trial.test\`: \`$STATE/$u.password\`"; done
        echo
        echo "## Startup checks"
        echo
        echo "| Check | Exit code |"
        echo "|---|---|"
        echo "| health (Open WebUI /health through the router, Dex discovery) | $rc_health |"
        echo "| smoke (both sign-ins, Rosetta-only models, one question with source chips, blocked paths 403) | $rc_smoke |"
        echo
        echo "health: \`$(tr -d '\n' < "$STATE/logs/check-health.json")\`"
        echo
        echo "smoke:"
        echo
        echo '```json'
        python3 -I -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), indent=1, sort_keys=True))' \
            "$STATE/logs/check-smoke.json" 2>/dev/null || cat "$STATE/logs/check-smoke.json"
        echo '```'
        echo
        echo "## Commands"
        echo
        echo "- Status: \`sh $HERE/dev.sh status\`"
        echo "- Stop the cluster (only the one this script created) and the relay: \`sh $HERE/dev.sh down\`"
        echo "- Stop the local lane (models, local stack, dedicated Qdrant; volume kept): \`sh $HERE/dev.sh agent-down\`"
        echo
        echo "## RAM in use"
        echo
        echo '```'
        ram_in_use
        echo '```'
    } > "$STATE/READY.md" )
    chmod 600 "$STATE/READY.md"
    say "wrote $STATE/READY.md (health rc=$rc_health, smoke rc=$rc_smoke)"
    [ "$rc_health" = 0 ] && [ "$rc_smoke" = 0 ]
}

down() {
    # A kind or docker failure is not "no cluster": cluster.created is the only
    # ownership proof, so it goes only after a successful delete or after docker
    # itself confirms the node container is gone.
    kept="kept $STATE/cluster.created; retry down"
    clusters=$("$KIND_BIN" get clusters 2>/dev/null) || die "kind get clusters failed; $kept"
    if printf '%s\n' "$clusters" | grep -qx "$KIND_NAME"; then
        ours || die "cluster $KIND_NAME was not created by this script (node id mismatch); not deleting"
        "$KIND_BIN" delete cluster --name "$KIND_NAME" --kubeconfig "$KUBECONFIG" \
            || die "kind delete cluster $KIND_NAME failed; $kept"
    else
        node=$(docker ps -aq --filter "name=^/$NODE\$") || die "docker is not reachable; $kept"
        [ -z "$node" ] || die "kind lists no $KIND_NAME but container $NODE exists; $kept"
        say "no cluster $KIND_NAME"
    fi
    rm -f "$STATE/cluster.created" "$KUBECONFIG"
    if unit_active "$UNIT-relay"; then systemctl --user stop "$UNIT-relay.service"; say "relay stopped"; fi
    "$KIND_BIN" get clusters 2>/dev/null || true
    say "kept: passwords, CA and certificates, the local lane (agent-down stops it)"
}

# ------------------------------------------------------------------ dry run
preflight() {
    for t in "$KIND_BIN" "$KUBECTL" "$HELM" "$AGENT_PY"; do [ -x "$t" ] || die "missing tool $t"; done
    for t in openssl python3 systemd-run flock curl ss git; do command -v "$t" >/dev/null || die "missing $t"; done
    [ -n "$NODE_IMAGE" ] &&  [ -n "$(pin DEX_PIN)" ] && [ -n "$(pin SKOPEO_IMAGE)" ] \
        || die "pins not found in $HERE/pins.txt"
    [ -f "$REPO/charts/mainframe-rag/files/openwebui/proxy.nginx.conf" ] \
        || die "$REPO lacks the chart's Open WebUI files"
    [ -f "$REPO/integrations/openwebui/docs_pipe.py" ] || die "$REPO lacks integrations/openwebui/docs_pipe.py"
    [ -n "$(nginx_pin)" ] || die "no nginx-unprivileged pin in $REPO/images.txt"
    say "tools: $("$KIND_BIN" version | cut -d' ' -f1-2), helm $("$HELM" version --short), kubectl $("$KUBECTL" version --client -o json | python3 -I -c 'import json,sys;print(json.load(sys.stdin)["clientVersion"]["gitVersion"])')"
    say "sources: chart $REPO/charts/mainframe-rag and Pipe $REPO/integrations/openwebui/docs_pipe.py (working tree, $(git -C "$REPO" rev-parse --short HEAD))"
    say "pins: node ${NODE_IMAGE#*@}, Open WebUI image $(owui_pinned 2>/dev/null | sed "s/.*@//"), Dex $(pin DEX_PIN | sed 's/.*@//'), nginx $(nginx_pin)"
    if [ "$DRYRUN" = 1 ]; then
        if flock -n -o "$LOCK" true 2>/dev/null; then plan "heavy lock $LOCK: free now"
        else plan "heavy lock $LOCK: held by another job now; up would exit 75 and start nothing"; fi
        units=$(systemctl --user list-units --all --plain --no-legend "$UNIT-*" 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
        plan "existing $UNIT units: ${units:-none}"
    fi
}

with_lock() {  # with_lock CMD...: run this script's CMD under the heavy lock; say so when it is held (N1)
    # flock -E: a held lock exits 99, which no phase returns, so it cannot be
    # mistaken for a failing phase (admission uses 75, die uses 1).
    flock -E 99 -n -o "$LOCK" sh "$0" "$@" && return 0
    rc=$?
    if [ "$rc" = 99 ]; then
        echo "ERROR: the heavy-work lock $LOCK is held by another job; nothing was started. Retry when it is free." >&2
        exit 75
    fi
    exit "$rc"
}

locked() {  # run the rest of `up` under the heavy lock; long-lived children never hold it
    with_lock _up_locked
}

up_phases() {
    images; agent_up; cluster; deploy; relay; provision; urls
}

up_failed() {  # tear down only what this run started, then record the failure in READY.md
    rc=$1
    trap - EXIT
    set +e
    echo "up failed (rc=$rc): removing what this run started" >&2
    # A subshell, so a refusing down (its die) cannot skip the rest of the cleanup.
    if grep -qx cluster "$STATE/run-started.txt" 2>/dev/null; then
        ( down ) || echo "down refused during cleanup; the cluster and its record are kept" >&2
    fi
    for u in relay stack embed reasoning; do
        if grep -qx "unit $UNIT-$u" "$STATE/run-started.txt" 2>/dev/null; then
            systemctl --user stop "$UNIT-$u.service" 2>/dev/null
        fi
    done
    if grep -qx qdrant "$STATE/run-started.txt" 2>/dev/null; then
        SIM_CONTAINER=$QDRANT_CONTAINER SIM_PORT=$QDRANT_PORT sh "$AGENT_SRC/scripts/sim_qdrant.sh" down
    fi
    write_failed "up" "$rc" "$(cat "$STATE/logs/last-error.txt" 2>/dev/null)" "$(cat "$STATE/run-started.txt" 2>/dev/null | tr '\n' ',')"
    exit "$rc"
}

write_failed() {  # write_failed STEP RC MESSAGE STARTED
    ( umask 077; {
        echo "# Open WebUI dev environment: NOT ready"
        echo
        echo "Written $(date '+%Y-%m-%d %H:%M %Z') by \`dev.sh\` (harness $(git -C "$REPO" rev-parse --short HEAD))."
        echo
        echo "## What failed"
        echo
        echo "- Step \`$1\` exited with code **$2**: $3"
        echo "- Started by this run and removed again: ${4:-nothing}"
        echo "- Left running: nothing from this script (check with the status command)."
        echo
        echo "## Commands"
        echo
        echo "- Status: \`sh $HERE/dev.sh status\`"
        echo "- Retry: \`sh $HERE/dev.sh up\`, then \`sh $HERE/dev.sh ready\`"
        echo "- Stop: \`sh $HERE/dev.sh down\` and \`sh $HERE/dev.sh agent-down\`"
        echo
        echo "## Password files (contents not shown)"
        echo
        for u in team-user docs-user owui-admin; do echo "- \`$u@trial.test\`: \`$STATE/$u.password\`"; done
        echo
        echo "## RAM in use"
        echo
        echo '```'
        ram_in_use
        echo '```'
    } > "$STATE/READY.md" )
    chmod 600 "$STATE/READY.md"
}

case "$CMD" in
    up)
        preflight
        if [ "$DRYRUN" = 1 ]; then up_phases; plan "then: dev.sh ready (checks + READY.md)"; plan "up: all phases planned"; exit 0; fi
        state_dir
        locked ;;
    _up_locked)
        : > "$STATE/run-started.txt"
        rm -f "$STATE/logs/last-error.txt"
        trap 'up_failed $?' EXIT
        up_phases
        trap - EXIT ;;
    images|cluster|deploy|agent-up)
        preflight
        fn=$(printf '%s' "$CMD" | tr '-' '_')
        if [ "$DRYRUN" = 1 ]; then "$fn"; exit 0; fi
        with_lock "_$CMD"; exit 0 ;;
    _images) images ;;
    _cluster) cluster ;;
    _deploy) deploy ;;
    _agent-up) agent_up ;;
    relay) relay ;;
    provision) provision ;;
    smoke) $PYH smoke "$STATE" ;;
    ready) [ "$DRYRUN" = 1 ] && { plan "ready: client.py health and smoke; write $STATE/READY.md (mode 600)"; exit 0; }; ready ;;
    status) status ;;
    urls) urls ;;
    down) [ "$DRYRUN" = 1 ] && { plan "down: delete $KIND_NAME only if $STATE/cluster.created matches its node; stop $UNIT-relay"; exit 0; }; down ;;
    agent-down) [ "$DRYRUN" = 1 ] && { plan "agent-down: stop $UNIT-stack, -embed, -reasoning and $QDRANT_CONTAINER"; exit 0; }; agent_down ;;
    *) sed -n '2,/^set -eu/p' "$0" | sed '$d' >&2; exit 2 ;;
esac
