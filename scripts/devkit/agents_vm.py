#!/usr/bin/env python3
"""Gap orchestration VM for the dev kit's agents (#1172; owner doc: docs/agents-vm.md).

Runs on the VM's own python3 (3.9 or later), standard library only. Every value
comes from the private site env file (default /etc/mfr-agents/site.env), which stays
outside git: the GitLab, LiteLLM and DNS addresses are site inputs, never repository
content. The file is read as KEY=VALUE lines and never executed.

  render-fence  --site-env F --out DIR [--uid N]
        Render the egress fence for the agents user: an nftables table whose output
        chain jumps to its fence chain on `meta skuid <agents>` only (primary layer: it
        also catches the rootless-Podman network helper, pasta or slirp4netns, which runs
        as that user; it has no loopback accept, since pasta reaches the VM's own
        services as that user) and a systemd drop-in for user-<uid>.slice with
        IPAddressDeny=any plus IPAddressAllow of localhost and the same addresses
        (second layer). Writes DIR/mfr-agents.nft and
        DIR/user-<uid>.slice.d/50-mfr-agents-egress.conf.
  install-fence --site-env F [--uid N] [--root DIR] [--remove]
        As root: render, check with `nft -c`, load the table, read the live table back,
        install the drop-in and reload systemd. --remove deletes both (the proof's
        fence-off negative control) and refuses while a container of the agents user
        runs. Both converge when repeated.
  proof         --site-env F [--fence-off] [--where host|container|both] [--output FILE]
        As the agents user: probe GitLab, LiteLLM and DNS (must connect) and every
        MFR_VM_BLOCKED target (production Qdrant, the agent, the OpenShift API, the
        internet; must fail) from the host shell and from inside a kit container. With
        --fence-off (after install-fence --remove) the blocked targets must CONNECT,
        which proves the probes can see a working path. Exit 0 only on a pass.
  preflight     --site-env F [--uid N] [--root DIR]
        Fail clearly unless the agents user has a subuid/subgid range, cgroups v2 with
        cpu, memory and pids delegated to user@<uid>.service, and linger. start and the
        container proof run it first.
  start         --site-env F
        As the agents user, and only while every MFR_VM_BLOCKED target is refused to
        this uid: run the kit image as the long-lived container mfr-agents under
        rootless Podman with --init, keep-id onto the kit's uid 1000770000:0, and the
        resource figures of #1172.
  observe
        The forced command of a view-only SSH key: `hc attach --observe <terminal>`
        inside mfr-agents for the terminal named in SSH_ORIGINAL_COMMAND, or `hc ls`.

Messages are fixed text plus labels and states; addresses appear only in the rendered
files and the proof's --output record, which belong in the private install record.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import ipaddress
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_SITE_ENV = "/etc/mfr-agents/site.env"
CONTAINER = "mfr-agents"
NFT_TABLE = "mfr_agents"
NFT_FENCE_CHAIN = "agents_egress"
NFT_FILE = "mfr-agents.nft"
DROPIN = "50-mfr-agents-egress.conf"
REQUIRED = ("MFR_VM_AGENTS_USER", "MFR_VM_DNS", "MFR_VM_GITLAB", "MFR_VM_GITLAB_PORTS", "MFR_VM_GITLAB_HOST",
            "MFR_VM_LITELLM", "MFR_VM_LITELLM_PORTS", "MFR_VM_LITELLM_BASE_URL", "MFR_VM_LLM_MODEL",
            "MFR_VM_BLOCKED", "MFR_VM_KIT_IMAGE", "MFR_VM_KIT_DIGEST", "MFR_VM_WORK")
# The negative controls the #1172 design names: each must be refused by the fence. The
# Podman trial (issuecomment-6548398452) added the host itself: pasta carries container
# traffic as the agents uid, so a container reaches the host's loopback services and
# its own addresses unless the fence refuses them too.
BLOCKED_LABELS = ("qdrant", "agent", "openshift-api", "internet", "host-loopback", "host-address")
# The kit image runs as an OpenShift-style arbitrary uid in group 0. Rootless Podman's
# default user namespace cannot map it (it lies outside the subuid range); keep-id maps
# the agents user to it, so the user's own files are the container user's without :U.
KIT_UID = 1000770000
USERNS = f"--userns=keep-id:uid={KIT_UID},gid=0"
RUN_USER = f"--user={KIT_UID}:0"
# Controllers the agents user's systemd service must delegate, or --memory, --cpus and
# the pids limit are silently unenforced.
DELEGATED = ("cpu", "memory", "pids")
VOLUME_LABELS = {"Z": ":Z", "z": ":z", "none": ""}
# Agent overhead for 1 controller plus 3 workers (#1172 trial, issuecomment-6548130709):
# requests 2 CPU / 2 GiB, limits 4 CPU / 4 GiB. The agents' own tools (pytest, task)
# come on top of the limits: MFR_VM_TOOL_CPUS and MFR_VM_TOOL_MEMORY_MIB.
REQUEST_CPUS, REQUEST_MIB, LIMIT_CPUS, LIMIT_MIB = 2, 2048, 4, 4096
DEFAULT_TOOL_CPUS, DEFAULT_TOOL_MIB = 2, 4096
USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
LABEL = re.compile(r"[a-z][a-z0-9-]{0,31}")
HOSTNAME = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*")
TERMINAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}")
IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9._/:-]{0,254}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SECRETS = ("mfr-dev-litellm",)  # the separate dev LiteLLM key (CI namespace decision)
OPTIONAL_SECRETS = ("mfr-gitlab-credentials",)  # git-credential-store line for the project token


class Refused(Exception):
    """A fixed, operator-facing refusal."""


class Site:
    """The validated site inputs."""

    def __init__(self, values: dict[str, str], source: Path) -> None:
        self.source = source
        self.digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.user = values["MFR_VM_AGENTS_USER"]
        if not USER.fullmatch(self.user) or self.user == "root":
            raise Refused("MFR_VM_AGENTS_USER is not a plain unprivileged user name")
        self.dns = networks(values, "MFR_VM_DNS")
        self.gitlab = networks(values, "MFR_VM_GITLAB")
        self.litellm = networks(values, "MFR_VM_LITELLM")
        self.gitlab_ports = ports(values, "MFR_VM_GITLAB_PORTS")
        self.litellm_ports = ports(values, "MFR_VM_LITELLM_PORTS")
        self.gitlab_host = values["MFR_VM_GITLAB_HOST"]
        if not HOSTNAME.fullmatch(self.gitlab_host):
            raise Refused("MFR_VM_GITLAB_HOST is not a host name")
        self.base_url = values["MFR_VM_LITELLM_BASE_URL"]
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?", self.base_url):
            raise Refused("MFR_VM_LITELLM_BASE_URL is not an https URL")
        self.model = values["MFR_VM_LLM_MODEL"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", self.model):
            raise Refused("MFR_VM_LLM_MODEL is not a plain model name")
        self.blocked = blocked(values["MFR_VM_BLOCKED"])
        self.image = values["MFR_VM_KIT_IMAGE"]
        if not IMAGE_REF.fullmatch(self.image) or "@" in self.image:
            raise Refused("MFR_VM_KIT_IMAGE is not a local image reference (name:tag)")
        self.image_digest = values["MFR_VM_KIT_DIGEST"]
        if not DIGEST.fullmatch(self.image_digest):
            raise Refused("MFR_VM_KIT_DIGEST is not sha256:<64 hex>")
        self.work = Path(values["MFR_VM_WORK"])
        if not self.work.is_absolute():
            raise Refused("MFR_VM_WORK is not an absolute path")
        self.tool_cpus = count(values, "MFR_VM_TOOL_CPUS", DEFAULT_TOOL_CPUS, 64)
        self.tool_mib = count(values, "MFR_VM_TOOL_MEMORY_MIB", DEFAULT_TOOL_MIB, 262144)
        self.ssh_clients = networks(values, "MFR_VM_SSH_CLIENTS", optional=True)
        label = values.get("MFR_VM_VOLUME_LABEL") or "Z"
        if label not in VOLUME_LABELS:
            raise Refused("MFR_VM_VOLUME_LABEL is Z (SELinux private, the default), z (shared) or none")
        self.volume_suffix = VOLUME_LABELS[label]
        loopback = self.blocked["host-loopback"][0]
        if not loopback.is_loopback:
            raise Refused("the host-loopback negative control must be a loopback address (127.0.0.1)")
        allowed = self.dns + self.gitlab + self.litellm
        for label, (address, _) in self.blocked.items():
            if any(address in net for net in allowed):
                raise Refused(f"blocked target {label} lies inside an allowed address: "
                              "its negative control could never fail; fix MFR_VM_BLOCKED or the allow lists")


def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines, optional matching quotes; never executed by a shell."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise Refused("the site env file is missing or unreadable") from None
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"MFR_VM_[A-Z0-9_]+", key) or key in values:
            raise Refused(f"site env line {number} is not a single MFR_VM_*=value assignment")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if any(c in value for c in "$`\\;&|<>"):
            raise Refused(f"site env line {number} holds shell syntax; values are literal")
        values[key] = value
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise Refused("the site env file has no value for " + ", ".join(missing))
    return values


def load_site(path: Path) -> Site:
    # The site addresses must never be committed: refuse a file inside a git work tree.
    probe = path.resolve().parent
    for parent in (probe, *probe.parents):
        if (parent / ".git").exists():
            raise Refused("the site env file is inside a git work tree; keep it outside git "
                          f"(for example {DEFAULT_SITE_ENV})")
    return Site(read_env(path), path)


def networks(values: dict[str, str], key: str, optional: bool = False) -> list:
    items = values.get(key, "").split()
    if not items and optional:
        return []
    if not items:
        raise Refused(f"{key} names no address")
    out = []
    for item in items:
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            raise Refused(f"{key} holds a value that is not an IP address or CIDR "
                          "(the fence filters by address; resolve names at the site)") from None
        if net.prefixlen < (16 if net.version == 4 else 48):
            raise Refused(f"{key} holds a range broader than /16 (IPv4) or /48 (IPv6)")
        out.append(net)
    return out


def ports(values: dict[str, str], key: str) -> list[int]:
    out = []
    for item in values[key].split():
        if not item.isdigit() or not 1 <= int(item) <= 65535:
            raise Refused(f"{key} holds a value that is not a TCP port")
        out.append(int(item))
    return sorted(set(out))


def count(values: dict[str, str], key: str, default: int, limit: int) -> int:
    raw = values.get(key)
    if raw is None or raw == "":
        return default
    if not raw.isdigit() or int(raw) > limit:
        raise Refused(f"{key} is not a whole number up to {limit}")
    return int(raw)


def blocked(raw: str) -> dict[str, tuple]:
    out: dict[str, tuple] = {}
    for item in raw.split():
        label, sep, target = item.partition("=")
        host, colon, port = target.rpartition(":")
        if not sep or not colon or not LABEL.fullmatch(label) or label in out or not port.isdigit():
            raise Refused("MFR_VM_BLOCKED entries are label=address:port, one label each")
        try:
            address = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            raise Refused("MFR_VM_BLOCKED addresses are IP literals") from None
        if not 1 <= int(port) <= 65535:
            raise Refused("MFR_VM_BLOCKED holds a port outside 1-65535")
        out[label] = (address, int(port))
    missing = [label for label in BLOCKED_LABELS if label not in out]
    if missing:
        raise Refused("MFR_VM_BLOCKED has no negative control for " + ", ".join(missing))
    return out


# --- fence -------------------------------------------------------------------


def agents_uid(site: Site, uid: int | None) -> int:
    if uid is not None:
        return uid
    try:
        return pwd.getpwnam(site.user).pw_uid
    except KeyError:
        raise Refused("the agents user does not exist on this host; create it first") from None


def render_nft(site: Site, uid: int) -> str:
    if uid == 0:
        raise Refused("the agents user must not be root")
    lines = [f"# Egress fence for the agents user (uid {uid}), #1172 primary layer.",
             f"# Rendered by scripts/devkit/agents_vm.py from a site env file (sha256 {site.digest}).",
             "# Re-render instead of editing. Loading it replaces the whole table.",
             f"table inet {NFT_TABLE}", f"delete table inet {NFT_TABLE}", f"table inet {NFT_TABLE} {{"]
    sets = (("dns", site.dns), ("gitlab", site.gitlab), ("litellm", site.litellm))
    for name, nets in sets:
        for family, kind in ((4, "ipv4_addr"), (6, "ipv6_addr")):
            members = [str(n) for n in nets if n.version == family]
            if members:
                lines.append(f"    set {name}{family} {{ type {kind}; flags interval; "
                             f"elements = {{ {', '.join(members)} }}; }}")
    # Dispatch on a positive uid match only. A packet without a socket owner (IPv6
    # neighbour discovery, kernel NFS, iSCSI or WireGuard sockets) cannot match
    # `meta skuid` at all: with `skuid != uid accept` it fell through to the drop for
    # every user (#1181 B1); here it stays in the base chain and its accept policy.
    lines += ["    chain output {", "        type filter hook output priority filter; policy accept;",
              f"        meta skuid {uid} jump {NFT_FENCE_CHAIN}", "    }",
              f"    chain {NFT_FENCE_CHAIN} {{",
              # No blanket loopback accept: pasta reaches the host's local services as
              # this uid. A local DNS stub (127.0.0.53) passes only through the DNS set.
              "        ct state established,related accept"]

    def rule(name: str, nets: list, proto: str, port_list: list[int]) -> None:
        for family, match in ((4, "ip"), (6, "ip6")):
            if any(n.version == family for n in nets):
                lines.append(f"        {match} daddr @{name}{family} {proto} dport "
                             f"{{ {', '.join(str(p) for p in port_list)} }} accept")

    rule("dns", site.dns, "udp", [53])
    rule("dns", site.dns, "tcp", [53])
    rule("gitlab", site.gitlab, "tcp", site.gitlab_ports)
    rule("litellm", site.litellm, "tcp", site.litellm_ports)
    lines += ['        counter drop comment "mfr-agents egress fence: not GitLab, LiteLLM or DNS"', "    }", "}", ""]
    return "\n".join(lines)


def render_dropin(site: Site, uid: int) -> str:
    allowed = " ".join(str(n) for n in site.dns + site.gitlab + site.litellm)
    lines = [f"# Egress fence for the agents user (uid {uid}), #1172 second layer: cgroup/eBPF",
             "# filtering for every process in the user's slice, Podman's libpod scopes included.",
             f"# Rendered by scripts/devkit/agents_vm.py from a site env file (sha256 {site.digest}).",
             "# localhost: this filter applies in every network namespace of the slice, so without it",
             "# the container's own loopback is cut, and OpenCode's --standalone run reaches its child",
             "# server over 127.0.0.1 there. The host's loopback stays shut by the nftables layer, which",
             "# matches this uid in the host's namespace, where pasta's host-side sockets live.",
             "[Slice]", "IPAddressDeny=any", "IPAddressAllow=localhost", f"IPAddressAllow={allowed}"]
    if site.ssh_clients:
        # The filter applies in both directions: let the viewers' SSH sessions in. The
        # nftables layer still refuses agents-owned connections to these addresses.
        lines.append("IPAddressAllow=" + " ".join(str(n) for n in site.ssh_clients))
    return "\n".join(lines) + "\n"


def render_fence(site: Site, uid: int, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    nft = out / NFT_FILE
    nft.write_text(render_nft(site, uid), encoding="utf-8")
    dropin = out / f"user-{uid}.slice.d" / DROPIN
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text(render_dropin(site, uid), encoding="utf-8")
    return [nft, dropin]


def admin_run(argv: list[str], after: str = "nothing was changed") -> subprocess.CompletedProcess:
    """One admin tool call; a missing or unrunnable tool is a fixed refusal, never a traceback."""
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
    except OSError:
        raise Refused(f"{argv[0]} is not installed or cannot run; {after}") from None


def live_fence_problems(listing: str, uid: int) -> list[str]:
    """Read-back of `nft -j list table`: the output chain only jumps the agents uid into
    the fence chain, and the fence chain ends in a drop."""
    try:
        items = json.loads(listing)["nftables"]
        chains = {i["chain"]["name"]: i["chain"] for i in items if "chain" in i}
        rules: dict[str, list] = {}
        for item in items:
            if "rule" in item:
                rules.setdefault(item["rule"]["chain"], []).append(item["rule"]["expr"])
    except (ValueError, TypeError, KeyError):
        return ["the listing is not nftables JSON"]
    problems = []
    base = chains.get("output") or {}
    if (base.get("type"), base.get("hook"), base.get("policy")) != ("filter", "output", "accept"):
        problems.append("no output hook chain with policy accept")
    dispatch = [{"match": {"op": "==", "left": {"meta": {"key": "skuid"}}, "right": uid}},
                {"jump": {"target": NFT_FENCE_CHAIN}}]
    if rules.get("output") != [dispatch]:
        problems.append(f"the output chain does not only jump uid {uid} into {NFT_FENCE_CHAIN}")
    fence = rules.get(NFT_FENCE_CHAIN) or [[]]
    if NFT_FENCE_CHAIN not in chains or not any("drop" in expr for expr in fence[-1]):
        problems.append(f"{NFT_FENCE_CHAIN} does not end in a drop")
    return problems


def agents_containers(root: Path, uid: int) -> bool:
    """Whether a Podman container of the agents user runs: each has a conmon monitor
    process of that uid (readable from /proc without privilege)."""
    proc = root / "proc"
    for status in proc.glob("[0-9]*/status") if proc.is_dir() else []:
        try:
            fields = dict(line.split(":", 1) for line in status.read_text(encoding="utf-8").splitlines()
                          if ":" in line)
        except OSError:
            continue  # the process ended meanwhile
        name = fields.get("Name", "").strip()
        real_uid = (fields.get("Uid", "").split() or [""])[0]
        if name in ("conmon", "conmonrs") and real_uid == str(uid):
            return True
    return False


def install_fence(site: Site, uid: int, root: Path, remove: bool) -> None:
    """Converges: installing again replaces the table and rewrites the same files;
    removing again finds no table and no files, which is the state it wants."""
    nft_dir = root / "etc/nftables"
    dropin_dir = root / f"etc/systemd/system/user-{uid}.slice.d"
    if remove:
        # A running container would keep running unfenced (#1181 B3): refuse instead.
        if agents_containers(root, uid):
            raise Refused("a Podman container of the agents user is running; stop it first (as agents: "
                          f"podman stop {CONTAINER}), then remove the fence")
        if admin_run(["nft", "delete", "table", "inet", NFT_TABLE]).returncode != 0 and \
                admin_run(["nft", "list", "table", "inet", NFT_TABLE]).returncode == 0:
            raise Refused("nft could not delete the loaded table; nothing else was changed")
        (nft_dir / NFT_FILE).unlink(missing_ok=True)
        (dropin_dir / DROPIN).unlink(missing_ok=True)
    else:
        nft_dir.mkdir(parents=True, exist_ok=True)
        staged = nft_dir / f".{NFT_FILE}.new"
        staged.write_text(render_nft(site, uid), encoding="utf-8")
        try:
            for argv in (["nft", "-c", "-f", str(staged)], ["nft", "-f", str(staged)]):
                if admin_run(argv).returncode != 0:
                    raise Refused("nft refused the rendered table; nothing was changed")
            listed = admin_run(["nft", "-j", "list", "table", "inet", NFT_TABLE])
            problems = live_fence_problems(listed.stdout, uid) if listed.returncode == 0 else ["it is not loaded"]
            if problems:
                raise Refused("the loaded nftables table is not the rendered fence (" + "; ".join(problems) +
                              "); the drop-in was not installed; run install-fence again")
            staged.replace(nft_dir / NFT_FILE)
        finally:
            staged.unlink(missing_ok=True)
        dropin_dir.mkdir(parents=True, exist_ok=True)
        (dropin_dir / DROPIN).write_text(render_dropin(site, uid), encoding="utf-8")
    if admin_run(["systemctl", "daemon-reload"], "check the drop-in by hand").returncode != 0:
        raise Refused("systemctl daemon-reload failed; check the drop-in by hand")


# --- proof -------------------------------------------------------------------

# Runs unchanged on the host's python3 and inside the kit image (python3 -I -c).
# argv[1] is JSON: {"connect": [[label, host, port], ...], "resolve": [label, name] or
# null, "loopback": label or null}. The connects run in parallel (a dropped one waits
# out its 5 s timeout). "loopback" is a listener on the probe's own 127.0.0.1 and one
# byte each way: in a container, the path OpenCode's --standalone client takes to its
# child server (#1181 B2).
PROBE = r"""
import json, socket, sys, threading
spec = json.loads(sys.argv[1])
out = {}
def connect(label, host, port):
    try:
        socket.create_connection((host, port), timeout=5).close()
        out[label] = "connected"
    except OSError as error:
        out[label] = "failed:" + type(error).__name__
threads = [threading.Thread(target=connect, args=item) for item in spec["connect"]]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
if spec.get("loopback"):
    try:
        with socket.socket() as server:
            server.settimeout(5)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=5) as client:
                peer = server.accept()[0]
                with peer:
                    peer.settimeout(5)
                    client.sendall(b"q")
                    peer.sendall(peer.recv(1))
                    ok = client.recv(1) == b"q"
        out[spec["loopback"]] = "connected" if ok else "failed:NoEcho"
    except OSError as error:
        out[spec["loopback"]] = "failed:" + type(error).__name__
if spec.get("resolve"):
    label, name = spec["resolve"]
    try:
        socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
        out[label] = "connected"
    except OSError as error:
        out[label] = "failed:" + type(error).__name__
print(json.dumps(out, sort_keys=True))
"""
LOOPBACK_LABEL = "container:loopback"


def probe_spec(site: Site, place: str = "host") -> tuple[dict, list[str], list[str]]:
    """(spec, labels that must connect, labels that must fail with the fence on).

    In a container, 127.0.0.1 is the container's own loopback; the host's loopback is
    reached through pasta as host.containers.internal, so that is the target there.
    The container's own loopback must work, fenced or not (OpenCode's workers use it);
    on the host the agents uid's loopback is the host-loopback negative control."""
    connect = []
    allowed = []
    for name, nets, port_list in (("gitlab", site.gitlab, site.gitlab_ports),
                                  ("litellm", site.litellm, site.litellm_ports)):
        for index, net in enumerate(nets, 1):
            for port in port_list:
                # Labels carry no address: they are printed, the addresses are not.
                label = f"{name}{index}:{port}"
                connect.append([label, str(net.network_address), port])
                allowed.append(label)
    blocked_labels = []
    for label, (address, port) in sorted(site.blocked.items()):
        host = "host.containers.internal" if place == "container" and label == "host-loopback" else str(address)
        connect.append([f"blocked:{label}", host, port])
        blocked_labels.append(f"blocked:{label}")
    allowed.append("dns:resolve")
    spec = {"connect": connect, "resolve": ["dns:resolve", site.gitlab_host], "loopback": None}
    if place == "container":
        spec["loopback"] = LOOPBACK_LABEL
        allowed.append(LOOPBACK_LABEL)
    return spec, allowed, blocked_labels


def verdict(results: dict[str, str], allowed: list[str], blocked_labels: list[str], fence_off: bool) -> list[str]:
    """Problems with one probe run; empty means it passed."""
    problems = []
    missing = [label for label in allowed + blocked_labels if label not in results]
    if missing:
        return ["the probe returned no result for " + ", ".join(missing)]
    for label in allowed:
        if results[label] != "connected":
            problems.append(f"{label} must connect but {results[label]} "
                            "(fence too tight, or a wrong site address)")
    for label in blocked_labels:
        if fence_off and results[label] != "connected":
            problems.append(f"{label} did not connect with the fence off ({results[label]}): this negative "
                            "control cannot prove the fence; give it a target that answers")
        if not fence_off and results[label] == "connected":
            problems.append(f"{label} connected with the fence on: the fence leaks")
    return problems


def run_probe(argv: list[str]) -> dict[str, str]:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    try:
        data = json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0 and done.stdout else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def require_agents_user(site: Site, action: str) -> str:
    """The fence matches the agents uid only, so `action` runs as that account or not at all.
    A uid with no passwd entry (the kit's arbitrary uid in a CI job) is not `agents`: fail closed."""
    try:
        me = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        me = None
    if me != site.user:
        raise Refused(f"run {action} as the agents user: the fence matches that uid only")
    return site.user


def proof(site: Site, fence_off: bool, where: str) -> tuple[int, dict]:
    me = require_agents_user(site, "the proof")
    if where in ("container", "both"):
        preflight(site)
    places = ("host", "container") if where == "both" else (where,)
    runs: dict[str, dict] = {}
    record: dict[str, object] = {
        "issue": "#1172", "mode": "fence-off" if fence_off else "fenced", "user": me,
        "site_env_sha256": site.digest, "image": site.image, "image_digest": site.image_digest,
        "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "runs": runs}
    failed = False
    for place in places:
        spec, allowed, blocked_labels = probe_spec(site, place)
        encoded = json.dumps(spec)
        if place == "host":
            argv = [sys.executable, "-I", "-c", PROBE, encoded]
        else:
            argv = ["podman", "run", "--rm", "--init", "--pull=never", USERNS, RUN_USER, site.image,
                    "python3", "-I", "-c", PROBE, encoded]
        results = run_probe(argv)
        problems = verdict(results, allowed, blocked_labels, fence_off)
        failed = failed or bool(problems)
        runs[place] = {"results": results, "problems": problems,
                                 "verdict": "fail" if problems else "pass"}
        for label in allowed + blocked_labels:
            print(f"proof: {place} {label} {results.get(label, 'no-result')}")
        for problem in problems:
            print(f"proof: {place} FAIL {problem}")
        print(f"proof: {place} {'FAIL' if problems else 'PASS'} ({'fence-off' if fence_off else 'fenced'})")
    return (1 if failed else 0), record


# --- container -----------------------------------------------------------------


def run_args(site: Site) -> list[str]:
    cpus = LIMIT_CPUS + site.tool_cpus
    mib = LIMIT_MIB + site.tool_mib
    argv = ["podman", "run", "-d", "--name", CONTAINER, "--init", "--pull=never",
            f"--cpus={cpus}", f"--memory={mib}m", f"--memory-swap={mib}m",
            f"--memory-reservation={REQUEST_MIB}m", f"--cpu-shares={REQUEST_CPUS * 1024}",
            "--pids-limit=4096", "--cap-drop=all", "--security-opt=no-new-privileges",
            USERNS, RUN_USER,
            f"--volume={site.work}:/work{site.volume_suffix}",
            "--env=HOME=/work/home",
            # No OpenShift credentials anywhere on this path: the image's oc/kubectl find none.
            "--env=KUBECONFIG=/dev/null",
            f"--env=MFR_LLM_BASE_URL={site.base_url}", f"--env=MFR_LLM_MODEL={site.model}"]
    argv += [f"--secret={name}" for name in SECRETS]
    return argv


def preflight_problems(site: Site, root: Path = Path("/"), uid: int | None = None) -> list[str]:
    """What rootless Podman needs on the VM before the agents run (Podman trial, #1172)."""
    if uid is None:
        uid = agents_uid(site, None)
    problems = []
    for name in ("subuid", "subgid"):
        ranges = []
        path = root / "etc" / name
        for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
            fields = line.strip().split(":")
            if len(fields) == 3 and fields[0] in (site.user, str(uid)) and fields[2].isdigit():
                ranges.append(int(fields[2]))
        if not ranges or max(ranges) < 65536:
            problems.append(f"/etc/{name} gives {site.user} no range of at least 65536 ids "
                            f"(admin: usermod --add-{name}s START-END {site.user})")
    if not (root / "sys/fs/cgroup/cgroup.controllers").is_file():
        problems.append("the host does not run cgroups v2; rootless Podman cannot enforce the limits")
    service = root / f"sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers"
    have = set(service.read_text(encoding="utf-8").split()) if service.is_file() else set()
    missing = [c for c in DELEGATED if c not in have]
    if missing:
        problems.append(f"user@{uid}.service does not delegate {' '.join(missing)}: --memory, --cpus and the pids "
                        "limit would be silently unenforced (admin: a Delegate=cpu memory pids drop-in for "
                        "user@.service, then log in again)")
    if not (root / "var/lib/systemd/linger" / site.user).exists():
        problems.append(f"linger is off for {site.user}: the container would stop at logout "
                        f"(admin: loginctl enable-linger {site.user})")
    return problems


def preflight(site: Site, root: Path = Path("/"), uid: int | None = None) -> None:
    problems = preflight_problems(site, root, uid)
    if problems:
        raise Refused("preflight failed: " + "; ".join(problems))


def fence_leaks(site: Site) -> list[str]:
    """Labels of the negative controls this uid can reach right now, from the host.

    The agents uid cannot read the nftables ruleset (netlink needs CAP_NET_ADMIN), so
    `start` checks the fence by its effect: every MFR_VM_BLOCKED target must be refused.
    That holds only while those targets answer without the fence, which the fence-off
    proof establishes; host-loopback (the VM's own sshd) is refused by nftables alone."""
    spec, _, blocked_labels = probe_spec(site, "host")
    spec = {"connect": [item for item in spec["connect"] if item[0] in blocked_labels]}
    results = run_probe([sys.executable, "-I", "-c", PROBE, json.dumps(spec)])
    return [label for label in blocked_labels if results.get(label, "connected") == "connected"]


def start(site: Site) -> int:
    preflight(site)
    require_agents_user(site, "start")
    if os.environ.get("KUBECONFIG") or (Path.home() / ".kube").exists():
        raise Refused("this account holds OpenShift credentials (KUBECONFIG or ~/.kube); the agents "
                      "user must have none (docs/agents-vm.md#accounts)")
    if shutil.which("podman") is None:
        raise Refused("podman is not on PATH; the VM runs the kit under rootless Podman")
    leaks = fence_leaks(site)
    if leaks:
        # Fail closed (#1181 B3): no fence installed, a removed fence, or a broken one.
        raise Refused("the egress fence is not in force for this user: " + ", ".join(leaks) +
                      " connected (or gave no result); the admin runs install-fence "
                      "(docs/agents-vm.md#egress-fence-as-the-admin)")

    def podman(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["podman", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              check=False)

    inspected = podman("image", "inspect", "--format", "{{.Digest}}", site.image)
    if inspected.returncode != 0 or inspected.stdout.strip() != site.image_digest:
        raise Refused("MFR_VM_KIT_IMAGE is not loaded at MFR_VM_KIT_DIGEST; load the kit image first "
                      "(docs/agents-vm.md#load-the-kit-image)")
    for name in SECRETS:
        if podman("secret", "exists", name).returncode != 0:
            raise Refused(f"the Podman secret {name} does not exist; create it from the dev LiteLLM key file")
    argv = run_args(site)
    for name in OPTIONAL_SECRETS:
        if podman("secret", "exists", name).returncode == 0:
            argv += [f"--secret={name}", "--env=GIT_CONFIG_COUNT=1", "--env=GIT_CONFIG_KEY_0=credential.helper",
                     f"--env=GIT_CONFIG_VALUE_0=store --file=/run/secrets/{name}"]
    (site.work / "home").mkdir(parents=True, exist_ok=True)
    done = subprocess.run([*argv, site.image, "sleep", "infinity"], stdin=subprocess.DEVNULL, check=False)
    return done.returncode


def observe() -> int:
    """Forced command of a view-only key: never a shell, never typing into a terminal."""
    requested = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    if not requested:
        argv = ["podman", "exec", CONTAINER, "hc", "ls"]
    elif TERMINAL.fullmatch(requested) and ".." not in requested:
        argv = ["podman", "exec", "-it", CONTAINER, "hc", "attach", "--observe", requested]
    else:
        print("observe: name one terminal (for example w-1) or nothing to list them", file=sys.stderr)
        return 2
    # Rootless Podman finds its containers through HOME and XDG_RUNTIME_DIR; keep the
    # account's own (a short hc HOME elsewhere makes podman exec miss the container).
    # sshd sets HOME; a uid with no passwd entry (a kit CI job) keeps whatever it has.
    env = dict(os.environ)
    if "HOME" not in env:
        with contextlib.suppress(KeyError):
            env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.execvpe(argv[0], argv, env)
    return 127  # not reached


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("render-fence", "install-fence", "proof", "start", "preflight"):
        p = sub.add_parser(name)
        p.add_argument("--site-env", type=Path, default=Path(DEFAULT_SITE_ENV))
        if name in ("render-fence", "install-fence", "preflight"):
            p.add_argument("--uid", type=int)
        if name == "preflight":
            p.add_argument("--root", type=Path, default=Path("/"))
        if name == "render-fence":
            p.add_argument("--out", type=Path, required=True)
        if name == "install-fence":
            p.add_argument("--root", type=Path, default=Path("/"))
            p.add_argument("--remove", action="store_true")
        if name == "proof":
            p.add_argument("--fence-off", action="store_true")
            p.add_argument("--where", choices=("host", "container", "both"), default="both")
            p.add_argument("--output", type=Path)
    sub.add_parser("observe")
    args = parser.parse_args(argv)
    try:
        if args.command == "observe":
            return observe()
        site = load_site(args.site_env)
        if args.command == "render-fence":
            for path in render_fence(site, agents_uid(site, args.uid), args.out):
                print(f"render-fence: wrote {path}")
            return 0
        if args.command == "install-fence":
            install_fence(site, agents_uid(site, args.uid), args.root, args.remove)
            print("install-fence: " + ("removed the fence" if args.remove else "loaded the fence"))
            return 0
        if args.command == "preflight":
            preflight(site, args.root, args.uid)
            print("preflight: subuid/subgid, cgroups v2, delegation of cpu memory pids and linger are in place")
            return 0
        if args.command == "proof":
            rc, record = proof(site, args.fence_off, args.where)
            if args.output:
                args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return rc
        return start(site)
    except Refused as error:
        print(f"agents-vm: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
