# Shared steps of the Open WebUI dev environment (dev.sh) and the acceptance
# lane (acceptance.sh). Sourced, never run. Owner: docs/openwebui-dev.md.
#
# The caller sets HERE REPO STATE KUBECTL HELM KIND_BIN KIND_NAME NODE APP_NS
# OWUI_HOST DEX_HOST OWUI_PORT DEX_PORT OWUI_URL DEX_ISSUER POD_CIDR OWUI_IMAGE
# DEX_IMAGE NGINX_IMAGE DRYRUN PY and the helpers say/plan/die/k.
#
# Two sources are read from the working tree, never from a git ref: the chart
# (charts/mainframe-rag) and the Docs Pipe (integrations/openwebui/docs_pipe.py).

pin() {  # pin KEY -> value from pins.env
    sed -n "s/^$1=//p" "$HERE/pins.env" | head -1
}

nginx_pin() {  # the allow-list proxy image digest, from the working tree's images.txt
    awk '$1 ~ /^docker\.io\/nginxinc\/nginx-unprivileged:/ {print $2}' "$REPO/images.txt"
}

nginx_ref() {  # repository without tag, as images.txt names it
    awk '$1 ~ /^docker\.io\/nginxinc\/nginx-unprivileged:/ {sub(/:.*/, "", $1); print $1}' "$REPO/images.txt"
}

# ------------------------------------------------------------------ images
build_images() {  # pull the pinned images, tag the local names, build the derived Open WebUI image
    OWUI_BASE=$(pin OWUI_BASE); DEX_PIN=$(pin DEX_PIN)
    NGINX_PIN=$(nginx_pin)
    [ -n "$NGINX_PIN" ] || die "no nginx-unprivileged pin in $REPO/images.txt"
    nginx_src="$(nginx_ref)@$NGINX_PIN"
    if [ "$DRYRUN" = 1 ]; then
        plan "images: inspect or pull $OWUI_BASE, $DEX_PIN, $nginx_src; tag $DEX_IMAGE, $NGINX_IMAGE"
        plan "images: docker build -f $REPO/images/openwebui/Containerfile -t $OWUI_IMAGE (cached layers)"
        return 0
    fi
    for ref in "$OWUI_BASE" "$DEX_PIN" "$nginx_src"; do
        docker image inspect "$ref" >/dev/null 2>&1 || docker pull -q "$ref" >/dev/null
    done
    docker tag "$DEX_PIN" "$DEX_IMAGE"
    docker tag "$nginx_src" "$NGINX_IMAGE"
    docker build -q -f "$REPO/images/openwebui/Containerfile" -t "$OWUI_IMAGE" "$REPO/images/openwebui" >/dev/null
    for ref in "$OWUI_IMAGE" "$DEX_IMAGE" "$NGINX_IMAGE"; do
        docker image inspect "$ref" --format "$ref {{.Id}} {{.Config.User}}"
    done | tee "$STATE/images.txt"
}

load_images() {  # into the Kind cluster $KIND_NAME, node container $NODE
    DEX_PIN=$(pin DEX_PIN); SKOPEO_IMAGE=$(pin SKOPEO_IMAGE)
    nginx_src="$(nginx_ref)@$(nginx_pin)"
    "$KIND_BIN" load docker-image --name "$KIND_NAME" "$OWUI_IMAGE"
    # Pulled images go in by their pinned digest with the pinned skopeo image
    # (docker save of a pulled index omits blobs on some hosts).
    for pair in "$DEX_PIN=$DEX_IMAGE=dex" "$nginx_src=$NGINX_IMAGE=nginx"; do
        src=${pair%%=*}; rest=${pair#*=}; name=${rest%%=*}; file=${rest#*=}
        docker run --rm --user "$(id -u):$(id -g)" -v "$STATE:$STATE" "$SKOPEO_IMAGE" \
            copy -q "docker://$src" "docker-archive:$STATE/$file.tar:$name"
        docker exec -i "$NODE" ctr -n k8s.io images import --snapshotter overlayfs - < "$STATE/$file.tar"
        rm -f "$STATE/$file.tar"
    done
    docker exec "$NODE" crictl images > "$STATE/node-images.txt"
}

coredns_rewrite() {
    # Pods resolve the IdP by the same name the browser uses (dex.localhost), so the
    # issuer is identical on both paths. CoreDNS answers it with the Dex Service and
    # logs every query (the acceptance DNS-egress check reads the log).
    k -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}' > "$STATE/Corefile.orig"
    if grep -q "rewrite name exact $DEX_HOST" "$STATE/Corefile.orig"; then return 0; fi
    awk -v rule="    rewrite name exact $DEX_HOST dex.owui-idp.svc.cluster.local" \
        '{print} /^\.:53 [{]/ {print "    log"; print rule}' "$STATE/Corefile.orig" > "$STATE/Corefile"
    k -n kube-system create configmap coredns --from-file=Corefile="$STATE/Corefile" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n kube-system rollout restart deploy/coredns >/dev/null
    k -n kube-system rollout status deploy/coredns --timeout=120s
}

kind_net() {  # "HOST_GW NODE_IP" of the Kind docker network and our node
    gw=$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Gateway}} {{end}}' | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
    ip=$(docker inspect -f '{{with index .NetworkSettings.Networks "kind"}}{{.IPAddress}}{{end}}' "$NODE")
    [ -n "$gw" ] && [ -n "$ip" ] || die "cannot read the Kind network gateway or node address"
    echo "$gw $ip"
}

# ------------------------------------------------------------------ certificates
PKI_REGENERATED=0
pki() {
    d="$STATE/pki"
    if [ -f "$d/ca.crt" ] && [ -f "$d/owui-svc.crt" ]; then
        # A CA from before the name constraints (N2 of #1095) is replaced: it could
        # vouch for any name, and the dev docs advise importing it into a browser.
        if openssl x509 -in "$d/ca.crt" -noout -text | grep -q "X509v3 Name Constraints"; then return 0; fi
        say "local CA has no name constraints; creating a constrained CA and new certificates"
        rm -f "$d"/*
        PKI_REGENERATED=1
    fi
    mkdir -p "$d"; chmod 700 "$d"
    # The CA may only vouch for loopback and in-cluster names, so importing it
    # into a browser trust store cannot be abused for other sites.
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 90 \
        -subj "/CN=mainframe-rag openwebui dev local CA" -keyout "$d/ca.key" -out "$d/ca.crt" \
        -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "nameConstraints=critical,permitted;DNS:localhost,permitted;DNS:.localhost,permitted;DNS:.svc,permitted;DNS:.svc.cluster.local" 2>/dev/null
    for spec in "router:$OWUI_HOST" "dex:$DEX_HOST" \
        "owui-svc:rag-openwebui.$APP_NS.svc,rag-openwebui.$APP_NS.svc.cluster.local"; do
        name=${spec%%:*}; sans=${spec#*:}
        san=$(printf '%s' "$sans" | sed 's/^/DNS:/; s/,/,DNS:/g')
        printf 'subjectAltName=%s\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n' \
            "$san" > "$d/$name.ext"
        openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=${sans%%,*}" \
            -keyout "$d/$name.key" -out "$d/$name.csr" 2>/dev/null
        openssl x509 -req -in "$d/$name.csr" -CA "$d/ca.crt" -CAkey "$d/ca.key" -CAcreateserial \
            -days 90 -extfile "$d/$name.ext" -out "$d/$name.crt" 2>/dev/null
        openssl verify -CAfile "$d/ca.crt" -verify_hostname "${sans%%,*}" "$d/$name.crt" >/dev/null \
            || die "certificate $name does not verify against the constrained CA"
    done
    chmod 600 "$d"/*.key
}

uid_for() {  # a stable random UID per role, like restricted-v2's namespace range
    f="$STATE/uid-$1.txt"
    [ -f "$f" ] || python3 -I -c 'import secrets;print(1000700000 + secrets.randbelow(100000))' > "$f"
    cat "$f"
}

# ------------------------------------------------------------------ render
render_chart() {  # render_chart OUTDIR VALUES_TEMPLATE
    out=$1
    src="$out/src"
    rm -rf "$src"
    mkdir -p "$src" "$out/kustomize"
    cp -R "$REPO/charts/mainframe-rag" "$src/mainframe-rag"
    chart="$src/mainframe-rag"
    # The Pipe is the working tree's source file, whatever the chart's copy holds.
    cp "$REPO/integrations/openwebui/docs_pipe.py" "$chart/files/openwebui/docs_pipe.py"
    sha256sum "$chart/files/openwebui/docs_pipe.py" | cut -d' ' -f1 > "$out/pipe.sha256"
    sed -e "s#@OWUI_REPO@#docker.io/${OWUI_IMAGE%:*}#" -e "s#@OWUI_TAG@#${OWUI_IMAGE##*:}#" \
        -e "s#@NGINX_REPO@#${NGINX_IMAGE%:*}#" -e "s#@NGINX_TAG@#${NGINX_IMAGE##*:}#" \
        -e "s#@OWUI_HOST@#$OWUI_HOST#" -e "s#@POD_CIDR@#$POD_CIDR#" \
        -e "s#@OIDC_PROVIDER_URL@#$DEX_ISSUER/.well-known/openid-configuration#" \
        "$HERE/values.yaml" > "$out/values.yaml"
    set --
    for t in deployment networkpolicy pipe-configmap proxy-configmap service pvc; do
        set -- "$@" --show-only "templates/openwebui-$t.yaml"
    done
    "$HELM" template rag "$chart" --namespace "$APP_NS" -f "$out/values.yaml" "$@" > "$out/kustomize/chart.yaml"
    uid=$(uid_for openwebui)
    {
        cat <<EOF
# Kind stand-ins on the chart render (docs/openwebui-dev.md): restricted-v2's
# assigned UID and group 0, and trust for the test IdP's CA.
apiVersion: apps/v1
kind: Deployment
metadata: {name: rag-openwebui, namespace: $APP_NS}
spec:
  template:
    spec:
      securityContext: {runAsUser: $uid, runAsGroup: 0, fsGroup: $uid}
      containers:
        - name: openwebui
          env:
            - {name: SSL_CERT_FILE, value: /etc/mainframe-rag/trial-ca/ca.crt}
            - {name: REQUESTS_CA_BUNDLE, value: /etc/mainframe-rag/trial-ca/ca.crt}
EOF
        if [ "$OWUI_PORT" != 443 ]; then
            cat <<EOF
            - {name: WEBUI_URL, value: "$OWUI_URL"}
            - {name: CORS_ALLOW_ORIGIN, value: "$OWUI_URL"}
            - {name: OPENID_REDIRECT_URI, value: "$OWUI_URL/oauth/oidc/callback"}
EOF
        fi
        cat <<EOF
          volumeMounts:
            - {name: trial-ca, mountPath: /etc/mainframe-rag/trial-ca, readOnly: true}
      volumes:
        - {name: trial-ca, configMap: {name: trial-ca}}
EOF
    } > "$out/kustomize/patch-openwebui.yaml"
    printf 'resources: [chart.yaml]\npatches:\n  - path: patch-openwebui.yaml\n' > "$out/kustomize/kustomization.yaml"
    "$KUBECTL" kustomize "$out/kustomize" > "$out/openwebui.yaml"
}

route_timeout() {
    sed -n 's/^route: .*timeoutSeconds: \([0-9]*\).*/\1/p' "$HERE/values.yaml"
}

render_support() {  # render_support OUT NODE_IP DEX_CONFIG_SHA  (Dex, router stand-in, kubelet-probe policy)
    sed -e "s#@NODE_IP@#$2#g" -e "s#@APP_NS@#$APP_NS#g" \
        -e "s#@DEX_UID@#$(uid_for dex)#g" -e "s#@ROUTER_UID@#$(uid_for router)#g" \
        -e "s#@NGINX_IMAGE@#$NGINX_IMAGE#g" -e "s#@DEX_IMAGE@#$DEX_IMAGE#g" \
        -e "s#@DEX_CONFIG_SHA@#$3#g" -e "s#@DEX_PORT@#$DEX_PORT#g" \
        -e "s#@OWUI_HOST@#$OWUI_HOST#g" -e "s#@ROUTE_TIMEOUT@#$(route_timeout)#g" \
        "$HERE/kind-support.yaml" > "$1"
}

check_render() {  # check_render DIR SUPPORT_OBJECTS: parse the files, assert the objects and the Pipe hash
    "$PY" -I - "$1" <<'PY'
import hashlib, sys, yaml
d = sys.argv[1]
chart = [x for x in yaml.safe_load_all(open(f"{d}/openwebui.yaml")) if x]
support = [x for x in yaml.safe_load_all(open(f"{d}/support.yaml")) if x]
kinds = sorted(f"{x['kind']}/{x['metadata']['name']}" for x in chart)
want = ["ConfigMap/rag-openwebui-pipe", "ConfigMap/rag-openwebui-proxy", "Deployment/rag-openwebui",
        "NetworkPolicy/rag-openwebui", "PersistentVolumeClaim/rag-openwebui-data", "Service/rag-openwebui"]
assert kinds == want, kinds
dep = next(x for x in chart if x["kind"] == "Deployment")
pod = dep["spec"]["template"]["spec"]
assert [c["name"] for c in pod["containers"]] == ["openwebui", "proxy", "chat-purge"]
assert pod["securityContext"]["runAsNonRoot"] is True and pod["securityContext"]["runAsUser"] >= 1000700000
env = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
assert env["SSL_CERT_FILE"] and env["OPENID_PROVIDER_URL"].startswith("https://dex.localhost:")
pipe = next(x for x in chart if x["metadata"]["name"] == "rag-openwebui-pipe")
sha = open(f"{d}/pipe.sha256").read().strip()
assert hashlib.sha256(pipe["data"]["docs_pipe.py"].encode()).hexdigest() == sha
assert pipe["metadata"]["annotations"]["mainframe-rag/docs-pipe-sha256"] == sha
np = next(x for x in chart if x["kind"] == "NetworkPolicy")
print(f"render: chart objects {len(chart)} ({', '.join(kinds)}); support objects {len(support)}")
print(f"render: Pipe sha256 {sha} in the ConfigMap; NetworkPolicy egress rules {len(np['spec']['egress'])}; "
      f"image {pod['containers'][0]['image']}; Route not rendered (router stand-in)")
PY
}

# ------------------------------------------------------------------ apply
apply_stack() {  # the Secrets and objects both entry points create in the cluster; needs the app namespace
    pk="$STATE/pki"
    k -n owui-idp create secret generic dex-config --from-file=config.yaml="$STATE/dex-config.yaml" --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n owui-idp create secret tls dex-tls --cert="$pk/dex.crt" --key="$pk/dex.key" --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n owui-router create secret tls router-tls --cert="$pk/router.crt" --key="$pk/router.key" --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n "$APP_NS" create secret tls rag-openwebui-tls --cert="$pk/owui-svc.crt" --key="$pk/owui-svc.key" --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n "$APP_NS" create secret generic rag-openwebui --from-file="$STATE/owui-secret/webui-secret-key" \
        --from-file="$STATE/owui-secret/oidc-client-secret" --dry-run=client -o yaml | k apply -f - >/dev/null
    for ns in owui-router "$APP_NS"; do
        k -n "$ns" create configmap trial-ca --from-file=ca.crt="$pk/ca.crt" --dry-run=client -o yaml | k apply -f - >/dev/null
    done
    k apply -f "$STATE/render/openwebui.yaml" > "$STATE/logs/apply-openwebui.log"
    k -n owui-idp rollout restart deploy/dex >/dev/null
    k -n owui-router rollout restart deploy/router >/dev/null
    if [ "$PKI_REGENERATED" = 1 ]; then k -n "$APP_NS" rollout restart deploy/rag-openwebui >/dev/null; fi
}

bcrypt_hashes() {  # passwords of the client.py users -> bcrypt hashes in STATE/hashes.txt, from the image's own module
    $PYH users "$STATE" | while read -r u; do cat "$STATE/$u.password"; done \
        | docker run --rm -i --network none --entrypoint python3 "$OWUI_IMAGE" -c '
import bcrypt, sys
for line in sys.stdin.read().split():
    print(bcrypt.hashpw(line.encode(), bcrypt.gensalt(10)).decode())' > "$STATE/hashes.txt"
    chmod 600 "$STATE/hashes.txt"
    ( umask 077; $PYH dex-config "$STATE" "$STATE/hashes.txt" > "$STATE/dex-config.yaml" )
}

provision_live() {  # admin API key, the chart's Pipe installer, groups, Docs model grants, both users sign in
    $PYH api-key "$STATE"
    k -n "$APP_NS" exec -i deploy/rag-openwebui -c openwebui -- \
        python3 /opt/mainframe-rag/openwebui/provision_pipe.py "$(cat "$STATE/render/pipe.sha256")" < "$STATE/admin-api-key"
    $PYH access "$STATE"
    for u in team-user docs-user; do $PYH login "$STATE" "$u"; done
}
