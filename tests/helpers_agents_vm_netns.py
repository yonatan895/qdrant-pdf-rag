"""The gap VM egress fence, run for real in a throwaway rootless user+net namespace.

Run by tests/test_devkit_kit.py as root of `unshare --map-auto --map-root-user --net
--mount --fork` (no real privilege): argv is <agents_vm.py> <agents uid>. It builds a
veth pair to a peer namespace that answers on documentation addresses (RFC 5737,
RFC 3849), loads the fence with the real `install-fence` code path, and probes as the
agents uid, as another uid and as root. It prints one JSON report on stdout; the test
holds the expectations. #1181 B1 (socket-less packets) and B3 (fail closed).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

VM_SOURCE, AGENTS = Path(sys.argv[1]), int(sys.argv[2])
OTHER = AGENTS + 1000
PY = "/usr/bin/python3"
# A tmpfs the namespace's other uids can traverse (the caller's home may be 0750).
BASE = next(Path(p) for p in ("/srv", "/mnt", "/opt") if Path(p).is_dir())
PEER_PORTS = (22, 53, 443, 6333, 6443, 8000)
LOCAL_PORT = 2222  # host-loopback and host-address controls, on this namespace

SERVER = r"""
import socket, sys, threading
def tcp(family, address, port):
    s = socket.socket(family)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    s.bind((address, port))
    s.listen(64)
    while True:
        s.accept()[0].close()
def udp(address, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((address, port))
    while True:
        data, peer = s.recvfrom(512)
        s.sendto(data, peer)
threads = []
for spec in sys.argv[1:]:
    kind, address, port = spec.split(",")
    if kind == "udp":
        threads.append(threading.Thread(target=udp, args=(address, int(port)), daemon=True))
    else:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        threads.append(threading.Thread(target=tcp, args=(family, address, int(port)), daemon=True))
for t in threads:
    t.start()
print("ready", flush=True)
threading.Event().wait()
"""

# One process per uid: TCP connects to [label, host, port], UDP round-trips to
# [label, host, port] ("udp:" labels), five-second timeouts, in parallel.
PROBE = r"""
import json, socket, sys, threading
out = {}
def run(label, host, port):
    try:
        if label.startswith("udp:"):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(5)
                s.sendto(b"q", (host, port))
                s.recvfrom(16)
        else:
            socket.create_connection((host, port), timeout=5).close()
        out[label] = "connected"
    except OSError as error:
        out[label] = "failed:" + type(error).__name__
threads = [threading.Thread(target=run, args=item) for item in json.loads(sys.argv[1])]
for t in threads:
    t.start()
for t in threads:
    t.join()
print(json.dumps(out, sort_keys=True))
"""

TARGETS = [
    ["gitlab", "192.0.2.10", 443], ["litellm", "198.51.100.7", 443], ["dns-tcp", "192.0.2.53", 53],
    ["udp:dns", "192.0.2.53", 53], ["udp:other-resolver", "203.0.113.9", 53],
    ["qdrant", "203.0.113.6", 6333], ["agent", "203.0.113.7", 8000], ["openshift-api", "203.0.113.8", 6443],
    ["internet", "203.0.113.9", 443], ["host-loopback", "127.0.0.1", LOCAL_PORT],
    ["host-address", "192.0.2.1", LOCAL_PORT], ["ipv6-gitlab-fresh-neighbour", "2001:db8::10", 443],
]


def sh(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=check, timeout=120)


def as_uid(uid: int, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    if uid == 0:
        prefix: list[str] = []
    else:
        prefix = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", "--inh-caps=-all"]
    return subprocess.run([*prefix, *argv], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          check=False, timeout=120, env=env, cwd="/")


def probe(uid: int) -> dict:
    # A fresh IPv6 neighbour each time: its solicitation is a packet with no socket owner.
    sh("ip", "-6", "neigh", "flush", "dev", "v0")
    done = as_uid(uid, PY, "-I", "-c", PROBE, json.dumps(TARGETS))
    return json.loads(done.stdout) if done.returncode == 0 else {"error": done.stderr[-300:]}


def main() -> int:
    sh("mount", "-t", "tmpfs", "-o", "mode=0755,size=64m", "fence-test", str(BASE))
    work = BASE / "t"
    for sub in ("bin", "admin-bin", "root", "home", "work"):
        (work / sub).mkdir(parents=True)
    # install-fence --root: its /proc stays the real one, where --remove looks for conmon.
    (work / "root/proc").symlink_to("/proc")
    os.chmod(work / "home", 0o777)
    os.chmod(work / "work", 0o777)
    shutil.copy(VM_SOURCE, work / "agents_vm.py")
    hosts = work / "hosts"
    hosts.write_text("127.0.0.1 localhost\n192.0.2.10 gitlab.site.example\n")
    sh("mount", "--bind", str(hosts), "/etc/hosts")
    # The admin's systemctl and the agents' podman are fakes; nft is the real one.
    (work / "admin-bin/systemctl").write_text("#!/bin/sh\nexit 0\n")
    (work / "bin/podman").write_text(f"""#!/bin/sh
echo "podman $*" >> {work}/home/podman.log
case "$1 $2" in "image inspect") echo "sha256:{'ab' * 32}" ;; esac
""")
    for tool in ("admin-bin/systemctl", "bin/podman"):
        os.chmod(work / tool, 0o755)

    sh("ip", "link", "set", "lo", "up")
    peer = subprocess.Popen(["unshare", "--net", "sleep", "600"], stdin=subprocess.DEVNULL)
    servers: list[subprocess.Popen] = []
    try:
        own = os.readlink("/proc/self/ns/net")
        for _ in range(100):
            if os.readlink(f"/proc/{peer.pid}/ns/net") != own:
                break
            time.sleep(0.05)
        sh("ip", "link", "add", "v0", "type", "veth", "peer", "name", "v1", "netns", str(peer.pid))
        for address in ("192.0.2.1/24", "198.51.100.1/24", "203.0.113.1/24"):
            sh("ip", "addr", "add", address, "dev", "v0")
        sh("ip", "-6", "addr", "add", "2001:db8::1/64", "dev", "v0", "nodad")
        sh("ip", "link", "set", "v0", "up")
        inside = ["nsenter", "-t", str(peer.pid), "-n"]
        sh(*inside, "ip", "link", "set", "lo", "up")
        for address in ("192.0.2.10/24", "192.0.2.53/24", "198.51.100.7/24", "203.0.113.6/24", "203.0.113.7/24",
                        "203.0.113.8/24", "203.0.113.9/24"):
            sh(*inside, "ip", "addr", "add", address, "dev", "v1")
        sh(*inside, "ip", "-6", "addr", "add", "2001:db8::10/64", "dev", "v1", "nodad")
        sh(*inside, "ip", "link", "set", "v1", "up")
        specs = [f"tcp,0.0.0.0,{p}" for p in PEER_PORTS] + [f"tcp,::,{p}" for p in PEER_PORTS] + \
            ["udp,0.0.0.0,53"]
        for argv in ([*inside, PY, "-I", "-c", SERVER, *specs],
                     [PY, "-I", "-c", SERVER, f"tcp,127.0.0.1,{LOCAL_PORT}", f"tcp,192.0.2.1,{LOCAL_PORT}"]):
            server = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
            servers.append(server)
            assert server.stdout is not None and server.stdout.readline().strip() == "ready"

        name = __import__("pwd").getpwuid(AGENTS).pw_name
        site = work / "site.env"
        site.write_text(f"""MFR_VM_AGENTS_USER={name}
MFR_VM_DNS=192.0.2.53
MFR_VM_GITLAB=192.0.2.10 2001:db8::10
MFR_VM_GITLAB_PORTS=443 22
MFR_VM_GITLAB_HOST=gitlab.site.example
MFR_VM_LITELLM=198.51.100.7
MFR_VM_LITELLM_PORTS=443
MFR_VM_LITELLM_BASE_URL=https://litellm.site.example/v1
MFR_VM_LLM_MODEL=site-reasoning
MFR_VM_BLOCKED=qdrant=203.0.113.6:6333 agent=203.0.113.7:8000 openshift-api=203.0.113.8:6443 internet=203.0.113.9:443 host-loopback=127.0.0.1:{LOCAL_PORT} host-address=192.0.2.1:{LOCAL_PORT}
MFR_VM_KIT_IMAGE=localhost/qdrant-pdf-rag-devkit:testkey
MFR_VM_KIT_DIGEST=sha256:{'ab' * 32}
MFR_VM_WORK={work}/work
""")
        os.chmod(site, 0o644)
        vm = str(work / "agents_vm.py")
        admin_env = {"PATH": f"{work}/admin-bin:/usr/sbin:/usr/bin:/sbin:/bin"}
        agents_env = {"PATH": f"{work}/bin:/usr/bin:/bin", "HOME": f"{work}/home"}
        start_code = ("import sys; sys.path.insert(0, sys.argv[1]); import agents_vm as vm; "
                      "vm.preflight = lambda *a, **k: None; sys.exit(vm.main(['start', '--site-env', sys.argv[2]]))")
        report: dict = {}

        def fence(*extra: str) -> int:
            return as_uid(0, PY, "-I", vm, "install-fence", "--site-env", str(site), "--uid", str(AGENTS),
                          "--root", str(work / "root"), *extra, env=admin_env).returncode

        def proof(*extra: str) -> int:
            return as_uid(AGENTS, PY, "-I", vm, "proof", "--site-env", str(site), "--where", "host", *extra,
                          env=agents_env).returncode

        def start() -> dict:
            log = work / "home/podman.log"
            log.unlink(missing_ok=True)
            done = as_uid(AGENTS, PY, "-I", "-c", start_code, str(work), str(site), env=agents_env)
            runs = [line for line in log.read_text().splitlines() if line.startswith("podman run")] \
                if log.exists() else []
            return {"rc": done.returncode, "podman_run": len(runs), "err": done.stderr.strip()[:400]}

        def loaded() -> bool:
            return sh("nft", "list", "table", "inet", "mfr_agents", check=False).returncode == 0

        report["A_no_fence"] = {"proof": proof(), "proof_fence_off": proof("--fence-off"), "start": start(),
                                "agents": probe(AGENTS), "other": probe(OTHER), "root": probe(0)}
        report["B_fence"] = {"install": fence(), "install_again": fence(), "loaded": loaded(),
                             "proof": proof(), "proof_fence_off": proof("--fence-off"), "start": start(),
                             "agents": probe(AGENTS), "other": probe(OTHER), "root": probe(0)}
        # The #1181 B1 shape on the same table, as a check that these probes see it.
        sh("nft", "flush", "chain", "inet", "mfr_agents", "output")
        sh("nft", "add", "rule", "inet", "mfr_agents", "output", "meta", "skuid", "!=", str(AGENTS), "accept")
        sh("nft", "add", "rule", "inet", "mfr_agents", "output", "jump", "agents_egress")
        report["X_old_rule"] = {"other": probe(OTHER), "root": probe(0)}
        assert fence() == 0
        # A running container of the agents user: its conmon. --remove must refuse.
        conmon = work / "bin/conmon"
        shutil.copy("/usr/bin/sleep", conmon)
        monitor = subprocess.Popen(["setpriv", f"--reuid={AGENTS}", f"--regid={AGENTS}", "--clear-groups",
                                    str(conmon), "600"], stdin=subprocess.DEVNULL)
        try:
            time.sleep(0.3)
            report["C_remove_while_running"] = {"remove": fence("--remove"), "loaded": loaded()}
        finally:
            monitor.send_signal(signal.SIGTERM)
            monitor.wait(timeout=10)
        report["C_remove"] = {"remove": fence("--remove"), "remove_again": fence("--remove"), "loaded": loaded(),
                              "proof_fence_off": proof("--fence-off"), "start": start()}
        report["D_reinstall"] = {"install": fence(), "proof": proof(), "start": start()}
        print(json.dumps(report, sort_keys=True))
        return 0
    finally:
        for server in servers:
            server.kill()
            server.wait(timeout=10)
        peer.kill()
        peer.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
