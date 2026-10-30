"""Bastion session login/logout helpers (ADR-0009 section 8): real subprocess runs."""

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "images" / "devkit" / "bin"
LOGIN = BIN / "mfr-session-login"
LOGOUT = BIN / "mfr-session-logout"
TOKEN = "sha256~Ab3_x.y-Z=q/w+9~tail"
PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
DOCKERCFG = {"auths": {"registry.example:5000": {"auth": "dTpw"}}}

needs_shm = pytest.mark.skipif(not os.path.isdir("/dev/shm"), reason="needs /dev/shm tmpfs")


@pytest.fixture
def sess(tmp_path):
    d = Path(tempfile.mkdtemp(dir="/dev/shm", prefix="mfr-sess-test-"))
    d.chmod(0o700)
    ca = tmp_path / "ca.crt"
    ca.write_text(PEM)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "oc.log"
    stub = bindir / "oc"
    stub.write_text('#!/bin/sh\necho "$@" >> "$OC_LOG"\nexit "${OC_RC:-0}"\n')
    stub.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "MFR_SESSION_DIR": str(d),
        "MFR_KUBE_CA": str(ca),
        "MFR_NAMESPACE": "mfr-prod",
        "OC_LOG": str(log),
    }
    yield type("S", (), {"dir": d, "env": env, "ca": ca, "log": log, "tmp": tmp_path})
    shutil.rmtree(d, ignore_errors=True)


def run(script, s, data=b"", args=(), **env):
    e = {**s.env, **env}
    e = {k: v for k, v in e.items() if v is not None}
    return subprocess.run(
        [sys.executable, str(script), *args],
        input=data,
        env=e,
        capture_output=True,
        timeout=30,
        check=False,
    )


def login(s, data, **env):
    return run(LOGIN, s, data, **env)


def no_leak(r, *secrets):
    out = r.stdout + r.stderr
    for sec in secrets:
        assert sec.encode() not in out


def kubeconfig(s):
    return json.loads((s.dir / "kubeconfig").read_text())


@needs_shm
@pytest.mark.parametrize(
    "payload",
    [
        TOKEN.encode(),
        TOKEN.encode() + b"\n",
        TOKEN.encode() + b"\r\n",
        b"\xef\xbb\xbf" + TOKEN.encode() + b"\r\n",
        b"\xef\xbb\xbf" + TOKEN.encode(),
    ],
    ids=["bare", "lf", "crlf", "bom-crlf", "bom"],
)
def test_transport_variants_round_trip_exact_token(sess, payload):
    r = login(sess, payload)
    assert r.returncode == 0, r.stderr
    user = kubeconfig(sess)["users"][0]["user"]
    assert user["token"] == TOKEN
    no_leak(r, TOKEN)


@needs_shm
@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\r\n",
        b"abc def",
        b"abc\ndef",
        b"abc\r\ndef\r\n",
        "tokén".encode(),
        b"\xff\xfe",
        b'tok"en',
        b"A" * (16 * 1024 + 1),
    ],
    ids=[
        "empty",
        "only-newline",
        "space",
        "two-lines",
        "two-lines-crlf",
        "non-ascii",
        "bad-utf8",
        "quote",
        "oversize",
    ],
)
def test_malformed_token_refused_without_echo_or_files(sess, payload):
    r = login(sess, payload)
    assert r.returncode == 2
    assert r.stderr.startswith(b"mfr-session:")
    if len(payload) > 1:
        assert payload not in r.stdout + r.stderr
    assert list(sess.dir.iterdir()) == []


@needs_shm
def test_oversize_boundary_accepts_exactly_limit(sess):
    r = login(sess, b"A" * (16 * 1024))
    assert r.returncode == 0


@needs_shm
def test_kubeconfig_shape_and_env_file(sess):
    assert login(sess, TOKEN.encode()).returncode == 0
    kc = kubeconfig(sess)
    cluster = kc["clusters"][0]["cluster"]
    assert cluster["server"] == "https://kubernetes.default.svc"
    assert cluster["certificate-authority"] == str(sess.ca)
    assert len(kc["clusters"]) == len(kc["users"]) == len(kc["contexts"]) == 1
    assert kc["contexts"][0]["context"]["namespace"] == "mfr-prod"
    assert kc["current-context"] == kc["contexts"][0]["name"]
    envtxt = (sess.dir / "env").read_text()
    assert f"export KUBECONFIG={sess.dir}/kubeconfig\n" in envtxt
    assert f"export REGISTRY_AUTH_FILE={sess.dir}/auth.json\n" in envtxt
    assert TOKEN not in envtxt
    assert sorted(p.name for p in sess.dir.iterdir()) == ["env", "kubeconfig"]


@needs_shm
def test_files_are_0600(sess):
    assert login(sess, TOKEN.encode()).returncode == 0
    r = run(LOGIN, sess, json.dumps(DOCKERCFG).encode(), args=["--registry"])
    assert r.returncode == 0
    for name in ("kubeconfig", "env", "auth.json"):
        assert stat.S_IMODE((sess.dir / name).stat().st_mode) == 0o600


@needs_shm
def test_relogin_replaces_kubeconfig(sess):
    login(sess, b"first-token")
    login(sess, b"second-token")
    assert kubeconfig(sess)["users"][0]["user"]["token"] == "second-token"


def test_not_tmpfs_refused(sess):
    d = Path(tempfile.mkdtemp(dir=Path.home() / ".cache", prefix="mfr-sess-disk-"))
    d.chmod(0o700)
    try:
        _not_tmpfs(sess, d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _not_tmpfs(sess, d):
    r = login(sess, TOKEN.encode(), MFR_SESSION_DIR=str(d))
    assert r.returncode == 2
    assert b"memory filesystem" in r.stderr
    assert list(d.iterdir()) == []
    no_leak(r, TOKEN)


@needs_shm
@pytest.mark.parametrize("mode", [0o755, 0o770, 0o600, 0o777])
def test_wrong_mode_refused(sess, mode):
    sess.dir.chmod(mode)
    r = login(sess, TOKEN.encode())
    sess.dir.chmod(0o700)
    assert r.returncode == 2
    assert b"0700" in r.stderr


def test_missing_dir_refused(sess):
    r = login(sess, TOKEN.encode(), MFR_SESSION_DIR="/dev/shm/mfr-does-not-exist")
    assert r.returncode == 2
    assert r.stderr.startswith(b"mfr-session:")


@needs_shm
def test_symlinked_dir_refused(sess, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(sess.dir)
    r = login(sess, TOKEN.encode(), MFR_SESSION_DIR=str(link))
    assert r.returncode == 2


@pytest.mark.skipif(os.getuid() != 0 or not shutil.which("chown"), reason="needs root to chown")
def test_wrong_owner_refused(sess):
    os.chown(sess.dir, 12345, 12345)
    r = login(sess, TOKEN.encode())
    assert r.returncode == 2
    assert b"owned by this user" in r.stderr


@needs_shm
def test_missing_or_invalid_ca_refused(sess):
    r = login(sess, TOKEN.encode(), MFR_KUBE_CA=str(sess.tmp / "nope.crt"))
    assert r.returncode == 2 and b"CA" in r.stderr
    sess.ca.write_text("not a certificate")
    r = login(sess, TOKEN.encode())
    assert r.returncode == 2
    assert list(sess.dir.iterdir()) == []
    no_leak(r, TOKEN)


@needs_shm
def test_default_ca_path_is_the_configmap_mount(sess):
    r = login(sess, TOKEN.encode(), MFR_KUBE_CA=None)
    assert r.returncode == 2  # /etc/mfr/kube-root-ca/ca.crt absent on the test host
    assert list(sess.dir.iterdir()) == []


@needs_shm
@pytest.mark.parametrize(
    "ns", [None, "", "Bad_NS", "has space", "-lead", "trail-", "a" * 64, "x\nexport"]
)
def test_namespace_required_dns_label(sess, ns):
    r = login(sess, TOKEN.encode(), MFR_NAMESPACE=ns)
    assert r.returncode == 2
    assert b"MFR_NAMESPACE" in r.stderr
    assert list(sess.dir.iterdir()) == []


@needs_shm
def test_registry_raw_and_base64_round_trip(sess):
    raw = json.dumps(DOCKERCFG).encode()
    for payload in (raw, raw + b"\r\n", b"\xef\xbb\xbf" + raw, base64.b64encode(raw) + b"\r\n"):
        r = run(LOGIN, sess, payload, args=["--registry"])
        assert r.returncode == 0, r.stderr
        assert json.loads((sess.dir / "auth.json").read_text()) == DOCKERCFG
        no_leak(r, "dTpw")
        (sess.dir / "auth.json").unlink()


@needs_shm
@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"{not json",
        b"!!!notbase64!!!",
        base64.b64encode(b"plain text"),
        b"{}",
        b'{"auths": {}}',
        b'{"auths": []}',
        b'{"auths": {"h": "x"}}',
        b"[1]",
    ],
)
def test_registry_invalid_refused(sess, payload):
    r = run(LOGIN, sess, payload, args=["--registry"])
    assert r.returncode == 2
    assert r.stderr.startswith(b"mfr-session:")
    assert list(sess.dir.iterdir()) == []


@needs_shm
def test_registry_secret_not_leaked_on_failure(sess):
    bad = b'{"auths": {"h": "supersecretvalue"}}'
    r = run(LOGIN, sess, bad, args=["--registry"])
    assert r.returncode == 2
    no_leak(r, "supersecretvalue")


@needs_shm
def test_registry_needs_no_namespace_or_ca(sess):
    r = run(
        LOGIN,
        sess,
        json.dumps(DOCKERCFG).encode(),
        args=["--registry"],
        MFR_NAMESPACE=None,
        MFR_KUBE_CA=None,
    )
    assert r.returncode == 0


def test_unknown_argument_refused(sess):
    r = run(LOGIN, sess, b"", args=["--bogus"])
    assert r.returncode == 2


@needs_shm
def test_logout_revokes_then_removes(sess):
    login(sess, TOKEN.encode())
    run(LOGIN, sess, json.dumps(DOCKERCFG).encode(), args=["--registry"])
    r = run(LOGOUT, sess)
    assert r.returncode == 0
    assert list(sess.dir.iterdir()) == []
    assert sess.log.read_text().split() == ["logout", "--kubeconfig", f"{sess.dir}/kubeconfig"]
    no_leak(r, TOKEN)


@needs_shm
def test_logout_failure_still_removes_and_exits_1(sess):
    login(sess, TOKEN.encode())
    run(LOGIN, sess, json.dumps(DOCKERCFG).encode(), args=["--registry"])
    r = run(LOGOUT, sess, OC_RC="1")
    assert r.returncode == 1
    assert list(sess.dir.iterdir()) == []
    assert b"may not be revoked" in r.stderr and b"oc logout" in r.stderr
    no_leak(r, TOKEN)


@needs_shm
def test_logout_without_oc_binary_fails_closed(sess):
    login(sess, TOKEN.encode())
    r = run(LOGOUT, sess, PATH="/nonexistent")
    assert r.returncode == 1
    assert list(sess.dir.iterdir()) == []


@needs_shm
def test_logout_idempotent_and_skips_oc_when_nothing_present(sess):
    assert run(LOGOUT, sess).returncode == 0
    assert run(LOGOUT, sess).returncode == 0
    assert not sess.log.exists()
    run(LOGIN, sess, json.dumps(DOCKERCFG).encode(), args=["--registry"])
    assert run(LOGOUT, sess).returncode == 0  # auth only: no oc call, file removed
    assert not sess.log.exists()
    assert list(sess.dir.iterdir()) == []


def test_logout_with_missing_dir_is_ok(sess):
    assert run(LOGOUT, sess, MFR_SESSION_DIR="/dev/shm/mfr-does-not-exist").returncode == 0


@needs_shm
def test_login_after_logout_yields_new_kubeconfig(sess):
    login(sess, b"old-token")
    assert run(LOGOUT, sess).returncode == 0
    r = login(sess, b"new-token")
    assert r.returncode == 0
    assert kubeconfig(sess)["users"][0]["user"]["token"] == "new-token"
    assert stat.S_IMODE((sess.dir / "kubeconfig").stat().st_mode) == 0o600
    assert run(LOGOUT, sess).returncode == 0
    assert sess.log.read_text().count("logout") == 2


def test_scripts_are_executable_with_env_shebang():
    for p in (LOGIN, LOGOUT):
        assert os.access(p, os.X_OK)
        assert p.read_text().startswith("#!/usr/bin/env python3\n")
