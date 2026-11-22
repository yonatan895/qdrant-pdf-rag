"""Tests for scripts/gap/sign_vault.py (GH#1207 Rev 2 §3b, D3).

Proves:
- Vault transit format conversion (vault:v1:<base64> -> raw 384-byte RSASSA-PKCS1-v1_5)
  is byte-identical to `openssl dgst -sha256 -sign` and passes the exact
  `openssl dgst -sha256 -verify` command `bootstrap.sh` runs.
- RSA-PSS padding is refused by the verifier (and never passes conversion + verify).
- Full HTTP round-trip against a local fake Vault transit server (both sign and
  pubkey export), verifying request parameters (`signature_algorithm=pkcs1v15`,
  `hash_algorithm=sha2-256`, `prehashed=true`), token header, and expected-identity
  enforcement.
- Fallback encrypted-key signing (`key-sign`) signs with a passphrase-encrypted
  RSA-3072 PEM key without leaving plaintext keys behind, and refuses wrong
  passphrases or mismatched expected identity.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from scripts.gap import sign_vault

from mainframe_rag import probe_receipt

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def rsa3072_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("rsa3072")
    priv = d / "gap.pem"
    pub = d / "gap.pub"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072", "-out", str(priv)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)], check=True, capture_output=True)
    return priv, pub


@pytest.fixture(scope="module")
def rsa2048_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("rsa2048")
    priv = d / "small.pem"
    pub = d / "small.pub"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(priv)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)], check=True, capture_output=True)
    return priv, pub


def _sign_prehashed_pkcs1v15(priv: Path, digest_bytes: bytes) -> bytes:
    """Reproduce Vault transit signing of a prehashed SHA-256 digest with PKCS#1 v1.5."""
    proc = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-inkey",
            str(priv),
            "-pkeyopt",
            "digest:sha256",
            "-pkeyopt",
            "rsa_padding_mode:pkcs1",
        ],
        input=digest_bytes,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _sign_prehashed_pss(priv: Path, digest_bytes: bytes) -> bytes:
    proc = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-inkey",
            str(priv),
            "-pkeyopt",
            "digest:sha256",
            "-pkeyopt",
            "rsa_padding_mode:pss",
            "-pkeyopt",
            "rsa_pss_saltlen:digest",
        ],
        input=digest_bytes,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def test_vault_format_conversion_is_byte_identical_to_openssl_dgst_and_passes_bootstrap_verify(
    tmp_path: Path, rsa3072_pair: tuple[Path, Path]
):
    priv, pub = rsa3072_pair
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("0" * 64 + "  MANIFEST.txt\n", encoding="utf-8")

    # Direct openssl dgst -sha256 -sign output (what pack.sh produces)
    direct_sig = tmp_path / "direct.sig"
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(priv), "-out", str(direct_sig), str(sums)],
        check=True,
        capture_output=True,
    )

    # Prehashed PKCS#1 v1.5 wrapped as vault:v1:<base64> and converted
    digest = hashlib.sha256(sums.read_bytes()).digest()
    vault_str = sign_vault.raw_sig_to_vault(_sign_prehashed_pkcs1v15(priv, digest), version=1)
    converted = sign_vault.vault_sig_to_raw(vault_str)
    assert len(converted) == 384
    assert converted == direct_sig.read_bytes()

    conv_sig = tmp_path / "SHA256SUMS.sig"
    conv_sig.write_bytes(converted)
    sign_vault.verify_signature_openssl(pub, sums, conv_sig)


def test_pss_signature_is_refused_by_bootstrap_verifier(tmp_path: Path, rsa3072_pair: tuple[Path, Path]):
    priv, pub = rsa3072_pair
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("a" * 64 + "  MANIFEST.txt\n", encoding="utf-8")
    digest = hashlib.sha256(sums.read_bytes()).digest()
    pss_sig = tmp_path / "SHA256SUMS.sig"
    pss_sig.write_bytes(_sign_prehashed_pss(priv, digest))
    assert len(pss_sig.read_bytes()) == 384
    with pytest.raises(sign_vault.SignError, match="PSS padding"):
        sign_vault.verify_signature_openssl(pub, sums, pss_sig)


@pytest.mark.parametrize("bad", [
    "",
    "not-vault-prefix",
    "vault:v0:AAAA",
    "vault:v1:!!!not-base64!!!",
    "vault:v1:" + base64.b64encode(b"too-short").decode(),
])
def test_vault_sig_to_raw_refuses_malformed_inputs(bad: str):
    with pytest.raises(sign_vault.SignError):
        sign_vault.vault_sig_to_raw(bad)


def test_normalize_public_key_enforces_rsa_3072(rsa3072_pair: tuple[Path, Path], rsa2048_pair: tuple[Path, Path]):
    _, pub3072 = rsa3072_pair
    _, pub2048 = rsa2048_pair
    normalized = sign_vault.normalize_public_key_pem(pub3072.read_bytes())
    assert normalized == pub3072.read_bytes()
    with pytest.raises(sign_vault.SignError, match="RSA 3072-bit"):
        sign_vault.normalize_public_key_pem(pub2048.read_bytes())


def test_vault_http_sign_and_pubkey_round_trip(tmp_path: Path, rsa3072_pair: tuple[Path, Path]):
    priv, pub = rsa3072_pair
    seen_requests: list[tuple[str, str, dict[str, object] | None, str | None]] = []
    expected_token = "s.test-token-never-logged"

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            tok = self.headers.get("X-Vault-Token")
            seen_requests.append(("GET", self.path, None, tok))
            if tok != expected_token or self.path != "/v1/transit/keys/mfr-gap-release":
                self.send_response(403)
                self.end_headers()
                return
            body = json.dumps({
                "data": {
                    "latest_version": 2,
                    "keys": {"2": {"public_key": pub.read_text(encoding="utf-8")}},
                }
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            tok = self.headers.get("X-Vault-Token")
            length = int(self.headers.get("Content-Length", "0"))
            req_body = json.loads(self.rfile.read(length).decode("utf-8"))
            seen_requests.append(("POST", self.path, req_body, tok))
            if tok != expected_token or self.path != "/v1/transit/sign/mfr-gap-release":
                self.send_response(403)
                self.end_headers()
                return
            assert req_body["hash_algorithm"] == "sha2-256"
            assert req_body["signature_algorithm"] == "pkcs1v15"
            assert req_body["prehashed"] is True
            digest = base64.b64decode(req_body["input"])
            sig = _sign_prehashed_pkcs1v15(priv, digest)
            resp = json.dumps({"data": {"signature": sign_vault.raw_sig_to_vault(sig, version=2)}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        addr = f"http://127.0.0.1:{port}"
        exported_pub = tmp_path / "gap.pub"
        pub_info = sign_vault.export_vault_pubkey(
            vault_addr=addr,
            vault_token=expected_token,
            transit_mount="transit",
            key_name="mfr-gap-release",
            out_pub=exported_pub,
        )
        assert pub_info["version"] == "2"
        assert exported_pub.read_bytes() == pub.read_bytes()

        sums = tmp_path / "SHA256SUMS"
        sums.write_text("b" * 64 + "  MANIFEST.txt\n", encoding="utf-8")
        identity = sign_vault.release_identity_of(sums.read_bytes())

        # Wrong expected identity refuses before calling Vault
        before_count = len(seen_requests)
        with pytest.raises(sign_vault.SignError, match="does not match --expected-identity"):
            sign_vault.sign_with_vault(
                sums,
                vault_addr=addr,
                vault_token=expected_token,
                transit_mount="transit",
                key_name="mfr-gap-release",
                out_sig=tmp_path / "bad.sig",
                expected_identity="0" * 64,
            )
        assert len(seen_requests) == before_count
        assert not (tmp_path / "bad.sig").exists()

        # Real CLI call via python3 -I
        out_sig = tmp_path / "SHA256SUMS.sig"
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                str(ROOT / "scripts/gap/sign_vault.py"),
                "vault-sign",
                "--sums",
                str(sums),
                "--out",
                str(out_sig),
                "--vault-addr",
                addr,
                "--key-name",
                "mfr-gap-release",
                "--expected-identity",
                identity,
                "--verify-pub",
                str(exported_pub),
            ],
            env={**os.environ, "VAULT_TOKEN": expected_token},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert expected_token not in proc.stdout and expected_token not in proc.stderr
        assert f"SIGNED {identity}" in proc.stdout
        sign_vault.verify_signature_openssl(exported_pub, sums, out_sig)
    finally:
        server.shutdown()
        server.server_close()


def test_fallback_encrypted_key_sign_round_trip_and_refusals(tmp_path: Path):
    enc_key = tmp_path / "gap-encrypted.pem"
    pub = tmp_path / "gap.pub"
    passphrase = "test-passphrase-for-hermetic-unit"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:3072",
            "-aes-256-cbc",
            "-pass",
            f"pass:{passphrase}",
            "-out",
            str(enc_key),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(enc_key), "-passin", f"pass:{passphrase}", "-pubout", "-out", str(pub)],
        check=True,
        capture_output=True,
    )
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("c" * 64 + "  MANIFEST.txt\n", encoding="utf-8")
    identity = sign_vault.release_identity_of(sums.read_bytes())

    # Wrong passphrase fails cleanly
    out_sig = tmp_path / "SHA256SUMS.sig"
    with pytest.raises(sign_vault.SignError, match="openssl signing failed"):
        sign_vault.sign_with_encrypted_key(
            sums,
            key_path=enc_key,
            out_sig=out_sig,
            passin="pass:wrong-passphrase",
            expected_identity=identity,
        )
    assert not out_sig.exists()

    # Correct passphrase signs and passes bootstrap.sh verifier
    info = sign_vault.sign_with_encrypted_key(
        sums,
        key_path=enc_key,
        out_sig=out_sig,
        passin=f"pass:{passphrase}",
        expected_identity=identity,
        verify_pub=pub,
    )
    assert info["release_identity"] == identity
    sign_vault.verify_signature_openssl(pub, sums, out_sig)


# --- vault-probe (#1343) ---------------------------------------------------------------

_PROBE_TOKEN = "s.MRKTOKENQ-never-logged"
_PROBE_KEY = "mrkkeyq-gap"
_ERROR_BODY = b'{"errors":["MRKERRBODYQ permission denied for mrkkeyq-gap"]}'


@contextlib.contextmanager
def _vault_stub(priv: Path, pub: Path, *, key_type: str = "rsa-3072", exportable: bool = False,
                read_status: int = 200, sign_status: int = 200, pss: bool = False):
    calls: list[tuple[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            calls.append(("GET", self.path))
            ok = self.headers.get("X-Vault-Token") == _PROBE_TOKEN
            if read_status != 200 or not ok or self.path != f"/v1/transit/keys/{_PROBE_KEY}":
                self._reply(read_status if read_status != 200 else 403, _ERROR_BODY)
                return
            self._reply(200, json.dumps({"data": {
                "name": _PROBE_KEY, "type": key_type, "exportable": exportable,
                "supports_signing": True, "latest_version": 3,
                "keys": {"3": {"public_key": pub.read_text(encoding="utf-8"), "name": "MRKNAMEQ"}},
            }}).encode())

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            calls.append(("POST", self.path))
            ok = self.headers.get("X-Vault-Token") == _PROBE_TOKEN
            if sign_status != 200 or not ok or self.path != f"/v1/transit/sign/{_PROBE_KEY}":
                self._reply(sign_status if sign_status != 200 else 403, _ERROR_BODY)
                return
            assert (req["hash_algorithm"], req["signature_algorithm"], req["prehashed"]) == (
                "sha2-256", "pkcs1v15", True)
            digest = base64.b64decode(req["input"])
            assert digest == hashlib.sha256(sign_vault.PROBE_MESSAGE).digest()
            raw = (_sign_prehashed_pss if pss else _sign_prehashed_pkcs1v15)(priv, digest)
            self._reply(200, json.dumps(
                {"data": {"signature": sign_vault.raw_sig_to_vault(raw, version=3)}}).encode())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", calls
    finally:
        server.shutdown()
        server.server_close()


def _vault_probe(addr: str) -> dict[str, dict[str, object]]:
    receipt = sign_vault.probe_vault(vault_addr=addr, vault_token=_PROBE_TOKEN,
                                     transit_mount="transit", key_name=_PROBE_KEY)
    assert [r["check"] for r in receipt.records] == list(sign_vault.VAULT_PROBE_CHECKS)
    return {str(r["check"]): r for r in receipt.records}


def test_vault_probe_passes_and_its_receipt_is_text_free(
    tmp_path: Path, rsa3072_pair: tuple[Path, Path], monkeypatch, capsys
):
    priv, pub = rsa3072_pair
    with _vault_stub(priv, pub) as (addr, calls):
        monkeypatch.setenv("VAULT_TOKEN", _PROBE_TOKEN)
        out = tmp_path / "vault-receipt.jsonl"
        rc = sign_vault.main(["vault-probe", "--vault-addr", addr, "--key-name", _PROBE_KEY,
                              "--receipt", str(out)])
    captured = capsys.readouterr()
    assert rc == 0
    assert calls == [("GET", f"/v1/transit/keys/{_PROBE_KEY}"), ("POST", f"/v1/transit/sign/{_PROBE_KEY}")]
    written = out.read_text(encoding="utf-8")
    assert written == captured.out and out.stat().st_mode & 0o777 == 0o600
    records = {r["check"]: r for r in map(json.loads, written.splitlines())}
    assert all(records[c]["status"] == "pass" for c in sign_vault.VAULT_PROBE_CHECKS)
    assert records["vault.key_type"]["key_type"] == "rsa-3072"
    assert records["vault.key_type"]["exportable"] is False
    assert records["vault.sign_pkcs1v15_sha256"]["verified"] is True
    assert records["vault.sign_pkcs1v15_sha256"]["signature_bytes"] == 384
    assert records["vault.sign_pkcs1v15_sha256"]["key_version"] == 3
    expected_fp = sign_vault.pubkey_fingerprint_sha256(pub)
    assert records["vault.public_key_export"]["fingerprint_sha256"] == expected_fp
    assert records["vault.summary"]["status"] == "pass"
    everything = (written + captured.err).lower()
    for marker in ("mrktokenq", "mrkkeyq", "mrknameq", "127.0.0.1", "begin public key"):
        assert marker not in everything, marker


@pytest.mark.parametrize(
    ("stub", "check", "status", "reason"),
    [
        ({"key_type": "rsa-2048"}, "vault.key_type", "fail", "not_rsa_3072"),
        ({"exportable": True}, "vault.key_type", "fail", "exportable"),
        ({"pss": True}, "vault.sign_pkcs1v15_sha256", "fail", "verify_failed"),
        ({"sign_status": 403}, "vault.sign_pkcs1v15_sha256", "fail", "forbidden"),
        ({"read_status": 404}, "vault.key_read", "fail", "not_found"),
        ({"read_status": 403}, "vault.sign_pkcs1v15_sha256", "inconclusive", "no_public_key"),
    ],
)
def test_vault_probe_fail_and_inconclusive_paths(
    rsa3072_pair: tuple[Path, Path], rsa2048_pair: tuple[Path, Path],
    stub: dict[str, object], check: str, status: str, reason: str,
):
    priv, pub = rsa2048_pair if stub.get("key_type") == "rsa-2048" else rsa3072_pair
    with _vault_stub(priv, pub, **stub) as (addr, _):
        checks = _vault_probe(addr)
    assert checks[check]["status"] == status and checks[check]["reason"] == reason
    if stub.get("key_type") == "rsa-2048":
        assert checks["vault.public_key_export"]["status"] == "fail"
    if "read_status" in stub:
        assert checks["vault.key_type"]["reason"] == "not_reached"
    assert "MRKERRBODYQ" not in json.dumps(checks)


def test_vault_probe_unreachable_and_bad_config(capsys):
    checks = _vault_probe("http://127.0.0.1:9")  # discard port: connection refused
    assert checks["vault.key_read"]["reason"] == "unreachable"
    assert checks["vault.sign_pkcs1v15_sha256"]["reason"] == "unreachable"
    assert sign_vault.main(["vault-probe", "--vault-addr", "http://vault.example:8200",
                            "--key-name", _PROBE_KEY]) == 2
    assert sign_vault.main(["vault-probe", "--vault-addr", "https://127.0.0.1:9",
                            "--key-name", "bad/name"]) == 2
    assert "MRKTOKENQ" not in capsys.readouterr().err


def test_vault_probe_receipt_rules_equal_the_package_copy():
    """sign_vault.py runs as a single stdlib file, so it carries a copy of the
    receipt rules; the two must accept, refuse and print the same records."""
    assert sign_vault._PROBE_SAFE_VALUE_RE.pattern == probe_receipt.SAFE_VALUE_RE.pattern
    assert sign_vault._PROBE_CHECK_ID_RE.pattern == probe_receipt.CHECK_ID_RE.pattern
    assert sign_vault._PROBE_FIELD_RE.pattern == probe_receipt.FIELD_RE.pattern
    assert sign_vault.PROBE_STATUSES == probe_receipt.STATUSES
    assert sign_vault.PROBE_SCHEMA_VERSION == probe_receipt.SCHEMA_VERSION
    ours, theirs = sign_vault.ProbeReceipt("vault"), probe_receipt.Receipt("vault")
    for receipt in (ours, theirs):
        receipt.add("vault.a", "pass", http_status=200, verified=True, fingerprint_sha256="ab" * 32)
        receipt.add("vault.b", "inconclusive", reason="no_public_key", key_version=None)
        receipt.add("vault.c", "fail", reason="forbidden")
    assert ours.lines() == theirs.lines()
    assert ours.exit_code() == theirs.exit_code() == 1
    with pytest.raises(sign_vault.SignError):
        ours.add("vault.d", "pass", note="two words")
