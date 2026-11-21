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
