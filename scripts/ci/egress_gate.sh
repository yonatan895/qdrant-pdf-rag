#!/bin/sh
# CI-only network egress gate for the Kind handoff rehearsal (issue #809).
#
# Blocks public egress with kernel packet-filter rules, not by mocking:
#
#   host    processes started through `egress_gate.sh run` run with a dedicated
#           primary group; an iptables OUTPUT owner-match accepts only loopback
#           (and any --allow CIDR, i.e. local Docker bridge networks) and
#           REJECTs everything else, DNS included. Other runner processes (the
#           GitHub runner agent, Docker daemon) keep their network: the runner
#           must keep reporting to GitHub, so a host-wide block is not possible.
#   nodes   a DOCKER-USER rule REJECTs forwarded traffic that leaves a Kind
#           network (--block-source CIDR), i.e. all node and pod egress.
#
# Subcommands (root is obtained with `sudo -n` when needed):
#   install [--allow CIDR]... [--block-source CIDR]...
#   run COMMAND [ARG...]     exec COMMAND under the gated group; refuses to run
#                            when the rules are absent (fail closed)
#   status                   print the rules and their packet counters
#   require-hits host|nodes  fail unless that rule class rejected >= 1 packet
#   remove
#
# The gate is a rehearsal boundary, not a security control: a process with
# sudo can lift it. `run` therefore sets no_new_privs on gated commands.
set -eu
set -f

TAG=airgap-egress-gate
GROUP=${EGRESS_GATE_GROUP:-airgap-egress-gate}

die() { echo "egress-gate: $*" >&2; exit 1; }
usage() {
    echo "usage: $0 install [--allow CIDR]... [--block-source CIDR]... | run CMD [ARG...] | status | require-hits host|nodes | remove" >&2
    exit 2
}
as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo -n "$@"; fi; }
valid_cidr() { case "$1" in ''|*[!0-9./]*) return 1 ;; */*) return 0 ;; *) return 1 ;; esac; }
group_gid() { getent group "$GROUP" | cut -d: -f3; }

# One rule per line, whitespace-separated tokens without spaces inside.
host_rules() {
    echo "-m owner --gid-owner $1 -p udp --dport 53 -m comment --comment $TAG:dns -j REJECT"
    echo "-m owner --gid-owner $1 -p tcp --dport 53 -m comment --comment $TAG:dns -j REJECT"
    echo "-m owner --gid-owner $1 -o lo -m comment --comment $TAG:loopback -j ACCEPT"
    for _cidr in $ALLOW; do
        echo "-m owner --gid-owner $1 -d $_cidr -m comment --comment $TAG:allow -j ACCEPT"
    done
    echo "-m owner --gid-owner $1 -m comment --comment $TAG:reject -j REJECT"
}
host_rules6() {
    echo "-m owner --gid-owner $1 -p udp --dport 53 -m comment --comment $TAG:dns -j REJECT"
    echo "-m owner --gid-owner $1 -p tcp --dport 53 -m comment --comment $TAG:dns -j REJECT"
    echo "-m owner --gid-owner $1 -o lo -m comment --comment $TAG:loopback -j ACCEPT"
    echo "-m owner --gid-owner $1 -m comment --comment $TAG:reject -j REJECT"
}
node_rules() {
    for _cidr in $SOURCES; do
        echo "-s $_cidr ! -d $_cidr -m comment --comment $TAG:nodes -j REJECT"
    done
}

# insert_rules TOOL CHAIN: read rules on stdin and insert them in order at the top.
insert_rules() {
    _n=0
    while IFS= read -r _rule; do
        [ -n "$_rule" ] || continue
        _n=$((_n + 1))
        # shellcheck disable=SC2086  # tokens are space-free by construction; globbing is off
        as_root "$1" -w -I "$2" "$_n" $_rule </dev/null || exit 1
    done
}

remove_chain() {
    command -v "$1" >/dev/null 2>&1 || return 0
    as_root "$1" -w -S "$2" 2>/dev/null | grep -- "--comment $TAG:" | sed 's/^-A /-D /' |
        while IFS= read -r _rule; do
            # shellcheck disable=SC2086
            as_root "$1" -w $_rule </dev/null || true
        done
}

cmd_remove() {
    for _tool in iptables ip6tables; do
        remove_chain "$_tool" OUTPUT
    done
    remove_chain iptables DOCKER-USER
}

cmd_install() {
    ALLOW=""
    SOURCES=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --allow) [ "$#" -ge 2 ] || usage; valid_cidr "$2" || die "invalid CIDR: $2"; ALLOW="$ALLOW $2"; shift 2 ;;
            --block-source) [ "$#" -ge 2 ] || usage; valid_cidr "$2" || die "invalid CIDR: $2"; SOURCES="$SOURCES $2"; shift 2 ;;
            *) usage ;;
        esac
    done
    command -v iptables >/dev/null 2>&1 || die "iptables is required"
    getent group "$GROUP" >/dev/null || as_root groupadd --system "$GROUP"
    _gid=$(group_gid)
    [ -n "$_gid" ] || die "cannot resolve group $GROUP"
    cmd_remove
    host_rules "$_gid" | insert_rules iptables OUTPUT
    if [ -n "$SOURCES" ]; then
        as_root iptables -w -S DOCKER-USER >/dev/null 2>&1 || die "DOCKER-USER chain not found; is Docker running?"
        node_rules | insert_rules iptables DOCKER-USER
    fi
    if command -v ip6tables >/dev/null 2>&1; then
        host_rules6 "$_gid" | insert_rules ip6tables OUTPUT || echo "egress-gate: WARNING: IPv6 rules not installed" >&2
    else
        echo "egress-gate: WARNING: ip6tables absent; IPv6 egress is not gated" >&2
    fi
    echo "egress-gate: installed (group $GROUP gid $_gid; allowed:${ALLOW:- loopback only}; node networks:${SOURCES:- none})"
}

cmd_run() {
    [ "$#" -gt 0 ] || usage
    _gid=$(group_gid)
    [ -n "$_gid" ] || die "gate group $GROUP is missing; run install first (fail closed)"
    as_root iptables -w -C OUTPUT -m owner --gid-owner "$_gid" -m comment --comment "$TAG:reject" -j REJECT 2>/dev/null ||
        die "egress rules are not installed; refusing to run ungated (fail closed)"
    # sudo resets the environment (sudo-rs ignores -E), so carry the caller's
    # exported variables in a 0600 snapshot that the unprivileged shell sources
    # and deletes before exec.
    _env=$(mktemp "${TMPDIR:-/tmp}/egress-gate-env.XXXXXX")
    export -p > "$_env"
    exec sudo -n setpriv --reuid "$(id -u)" --regid "$_gid" --init-groups --no-new-privs \
        /usr/bin/env /bin/sh -c '. "$0" || exit 125; rm -f -- "$0"; exec "$@"' "$_env" "$@"
}

hits() {
    as_root iptables -w -n -v -x -L "$1" 2>/dev/null |
        awk -v tag="/* $TAG:$2 */" 'index($0, tag) { sum += $1 } END { print sum + 0 }'
}

cmd_status() {
    for _chain in OUTPUT DOCKER-USER; do
        echo "== iptables $_chain (rules tagged $TAG)"
        as_root iptables -w -n -v -x -L "$_chain" 2>/dev/null | grep -e '^Chain' -e 'pkts' -e "$TAG" || true
    done
    echo "host rejected packets: $(hits OUTPUT reject)"
    echo "node/pod rejected packets: $(hits DOCKER-USER nodes)"
}

cmd_require_hits() {
    [ "$#" -eq 1 ] || usage
    case "$1" in
        host) _count=$(hits OUTPUT reject) ;;
        nodes) _count=$(hits DOCKER-USER nodes) ;;
        *) usage ;;
    esac
    [ "$_count" -ge 1 ] 2>/dev/null || die "no packet was rejected by the $1 rule: the block was never exercised"
    echo "egress-gate: $1 rule rejected $_count packet(s)"
}

[ "$#" -ge 1 ] || usage
_sub=$1
shift
case "$_sub" in
    install) cmd_install "$@" ;;
    run) cmd_run "$@" ;;
    status) cmd_status ;;
    require-hits) cmd_require_hits "$@" ;;
    remove) cmd_remove ;;
    *) usage ;;
esac
