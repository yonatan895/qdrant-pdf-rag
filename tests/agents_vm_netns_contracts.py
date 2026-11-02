"""The gap VM egress fence against real nftables in a rootless namespace (#1181).

Not a unit test (the file name keeps it out of default collection, like
tests/openwebui_image_contracts.py): it needs unprivileged user and network
namespaces with a subuid range (`unshare --map-auto`), nft, ip, nsenter and
setpriv, which CI runners do not offer. No real root, VM or container is used.
Run it after any change to scripts/devkit/agents_vm.py's fence, proof or start:

  flock ~/.cache/agent-tmp/mainframe-rag-heavy.lock mfr-pytest -m 1500 <worktree> tests/agents_vm_netns_contracts.py

Without those prerequisites it skips, and a skip is not a pass.
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _netns_env() -> dict[str, str]:
    # nft and ip usually live in sbin, which an unprivileged PATH may leave out.
    return {**os.environ, "PATH": f"{os.environ.get('PATH', '')}:/usr/sbin:/sbin"}


def _rootless_netns() -> str | None:
    """Why the namespace test cannot run here, or None."""
    if not sys.platform.startswith("linux") or not 0 < os.getuid() < 65536:
        return "needs Linux and a uid that a 65536-id namespace maps"
    path = _netns_env()["PATH"]
    missing = [t for t in ("unshare", "newuidmap", "nft", "ip", "nsenter", "setpriv")
               if shutil.which(t, path=path) is None]
    if missing or not Path("/usr/bin/python3").is_file():
        return "missing " + " ".join(missing or ["/usr/bin/python3"])
    try:
        pwd.getpwuid(os.getuid())
        done = subprocess.run(["unshare", "--map-auto", "--map-root-user", "--net", "--mount", "--fork", "nft",
                               "list", "ruleset"], stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                              check=False, env=_netns_env())
    except (KeyError, OSError, subprocess.TimeoutExpired):
        return "no passwd entry or unshare failed"
    return None if done.returncode == 0 else "unavailable: " + done.stderr.decode(errors="replace")[-200:]


def test_fence_in_a_rootless_netns_limits_only_the_agents_uid_and_start_fails_closed():
    """The real install-fence, proof and start code against real nftables in a throwaway
    user+net namespace (#1181 B1, B3): fenced, the agents uid reaches only GitLab,
    LiteLLM and DNS; other uids and root are untouched, including IPv6 to a fresh
    neighbour (a solicitation has no socket owner); start runs only while fenced."""
    unavailable = _rootless_netns()
    if unavailable:
        pytest.skip(f"rootless user+net namespace: {unavailable}")
    uid = os.getuid()
    done = subprocess.run(["unshare", "--map-auto", "--map-root-user", "--net", "--mount", "--fork",
                           "/usr/bin/python3", "-I", str(REPO / "tests/helpers_agents_vm_netns.py"),
                           str(REPO / "scripts/devkit/agents_vm.py"), str(uid)],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600, check=False,
                          env=_netns_env())
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout.strip().splitlines()[-1])
    allowed = {"gitlab", "litellm", "dns-tcp", "udp:dns", "ipv6-gitlab-fresh-neighbour"}
    everything = {"gitlab", "litellm", "dns-tcp", "udp:dns", "udp:other-resolver", "qdrant", "agent",
                  "openshift-api", "internet", "host-loopback", "host-address", "ipv6-gitlab-fresh-neighbour"}

    def connected(results):
        return {label for label, state in results.items() if state == "connected"}

    a, b, x = report["A_no_fence"], report["B_fence"], report["X_old_rule"]
    # Without the fence everything answers: the negative controls can see a path.
    for who in ("agents", "other", "root"):
        assert connected(a[who]) == everything, (who, a[who])
    assert (a["proof"], a["proof_fence_off"]) == (1, 0)
    assert a["start"]["rc"] == 2 and a["start"]["podman_run"] == 0
    assert "the egress fence is not in force for this user" in a["start"]["err"]
    # Fenced: the agents uid reaches only the allow list; everyone else is unaffected.
    assert (b["install"], b["install_again"], b["loaded"]) == (0, 0, True)
    assert connected(b["agents"]) == allowed, b["agents"]
    assert b["agents"]["udp:other-resolver"] == "failed:PermissionError"
    assert connected(b["other"]) == everything, b["other"]
    assert connected(b["root"]) == everything, b["root"]
    assert (b["proof"], b["proof_fence_off"]) == (0, 1)
    assert b["start"]["rc"] == 0 and b["start"]["podman_run"] == 1
    # The old `meta skuid != uid accept` shape drops the solicitation for every uid:
    # these probes see it, so the pass above is not vacuous.
    for who in ("other", "root"):
        assert x[who]["ipv6-gitlab-fresh-neighbour"] != "connected", x[who]
        assert connected(x[who]) == everything - {"ipv6-gitlab-fresh-neighbour"}, x[who]
    # --remove refuses while a container (its conmon) runs, and the fence stays loaded;
    # then it removes, converges when repeated, and start fails closed.
    assert report["C_remove_while_running"] == {"remove": 2, "loaded": True}
    c = report["C_remove"]
    assert (c["remove"], c["remove_again"], c["loaded"], c["proof_fence_off"]) == (0, 0, False, 0)
    assert c["start"]["rc"] == 2 and c["start"]["podman_run"] == 0
    # The next ordinary operation: the fence installs again and start runs.
    d = report["D_reinstall"]
    assert (d["install"], d["proof"], d["start"]["rc"], d["start"]["podman_run"]) == (0, 0, 0, 1)
