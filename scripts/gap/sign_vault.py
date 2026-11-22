#!/usr/bin/env python3
"""Gap release signing helpers (GH#1207 Rev 2 §3b, D3).

Signing NEVER runs in the bastion. These helpers run either:
  1. Primary (D3 option i/ii): on the maintainer's workstation (or, if the
     workstation cannot reach Vault, inside an ephemeral F9 ``sign`` pod with
     egress to Vault + DNS only), calling Vault transit signing with
     ``signature_algorithm=pkcs1v15``, ``hash_algorithm=sha2-256``,
     ``prehashed=true`` on an RSA-3072 key, converting ``vault:v<n>:<base64>``
     to raw RSASSA-PKCS1-v1_5 signature bytes identical to
     ``openssl dgst -sha256 -sign`` and accepted by the unchanged
     ``bootstrap.sh`` / ``common.sh`` verifier.
  2. Fallback (D3 option iii): inside an ephemeral zero-egress F9 ``sign`` pod
     with a passphrase-encrypted RSA-3072 PEM private key piped in for one
     signing, never writing an unencrypted key to disk.

``vault-probe`` (#1343) checks, from where signing runs, that the transit key
exists, is a non-exportable ``rsa-3072`` key, signs a fixed probe message
(never a SHA256SUMS) with PKCS#1 v1.5 + SHA-256 that verifies under its
exported public key, and prints a text-free JSON-lines receipt (the rules of
``mainframe_rag.probe_receipt``, copied here because this file runs alone).

Standard library only (runs under ``python3 -I``).
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

RSA_3072_MODULUS_BYTES = 384
RSA_3072_BITS = 3072
_VAULT_SIG_RE = re.compile(r"^vault:v([1-9][0-9]*):([A-Za-z0-9+/=]+)$")
_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_MOUNT_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class SignError(ValueError):
    """Fixed user-facing refusal; never embeds tokens or key bytes."""


def release_identity_of(sums_bytes: bytes) -> str:
    """Release identity = lowercase hex SHA-256 of SHA256SUMS bytes (Rev 2 §3e)."""
    return hashlib.sha256(sums_bytes).hexdigest()


def sha256_digest_b64(data: bytes) -> str:
    """Base64-encoded raw SHA-256 digest for Vault transit ``prehashed=true``."""
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def vault_sig_to_raw(vault_signature: str, *, expected_bytes: int | None = RSA_3072_MODULUS_BYTES) -> bytes:
    """Convert Vault transit ``vault:v<n>:<base64>`` to raw signature bytes.

    Refuses malformed prefixes, invalid base64, or unexpected modulus length.
    """
    match = _VAULT_SIG_RE.fullmatch(vault_signature.strip())
    if not match:
        raise SignError("STOP: Vault signature is malformed (expected vault:v<n>:<base64>)")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise SignError("STOP: Vault signature contains invalid base64") from None
    if not raw:
        raise SignError("STOP: Vault signature decoded to empty bytes")
    if expected_bytes is not None and len(raw) != expected_bytes:
        raise SignError(
            f"STOP: Vault signature length ({len(raw)} bytes) does not match RSA-3072 ({expected_bytes} bytes)"
        )
    return raw


def raw_sig_to_vault(raw_signature: bytes, *, version: int = 1) -> str:
    """Wrap raw signature bytes in Vault transit ``vault:v<n>:<base64>`` form."""
    if version < 1:
        raise SignError("STOP: Vault key version must be a positive integer")
    if not raw_signature:
        raise SignError("STOP: raw signature bytes must not be empty")
    encoded = base64.b64encode(raw_signature).decode("ascii")
    return f"vault:v{version}:{encoded}"


def normalize_public_key_pem(pem_bytes: bytes, *, require_rsa_3072: bool = True) -> bytes:
    """Normalize a PEM PUBLIC KEY via ``openssl pkey -pubin -pubout`` and validate RSA-3072."""
    proc = subprocess.run(
        ["openssl", "pkey", "-pubin", "-pubout"],
        input=pem_bytes,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0 or not proc.stdout.startswith(b"-----BEGIN PUBLIC KEY-----"):
        raise SignError("STOP: public key is not a valid PEM PUBLIC KEY")
    if require_rsa_3072:
        inspect = subprocess.run(
            ["openssl", "pkey", "-pubin", "-text", "-noout"],
            input=proc.stdout,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if inspect.returncode != 0:
            raise SignError("STOP: public key could not be inspected by openssl")
        inspect_text = inspect.stdout.decode("utf-8", "replace")
        first_line = inspect_text.splitlines()[0] if inspect_text else ""
        if f"Public-Key: ({RSA_3072_BITS} bit)" not in first_line or "Modulus:" not in inspect_text:
            raise SignError("STOP: gap signing key must be RSA 3072-bit (Rev 2 D3)")
    return proc.stdout


def pubkey_fingerprint_sha256(pubkey_path: Path) -> str:
    """SHA-256 fingerprint of a public key's DER SubjectPublicKeyInfo (matches common.sh)."""
    proc = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", str(pubkey_path), "-outform", "DER"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise SignError("STOP: cannot compute public key fingerprint (invalid PEM public key)")
    return hashlib.sha256(proc.stdout).hexdigest()


def verify_signature_openssl(pubkey_path: Path, data_path: Path, sig_path: Path) -> None:
    """Verify raw RSASSA-PKCS1-v1_5 SHA-256 signature with the exact command bootstrap.sh uses."""
    proc = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(pubkey_path),
            "-signature",
            str(sig_path),
            str(data_path),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        raise SignError(
            "STOP: signature verification failed under openssl dgst -sha256 -verify "
            "(wrong key, PSS padding, or tampered SHA256SUMS)"
        )


def _check_expected_identity(sums_bytes: bytes, expected_identity: str | None) -> str:
    actual = release_identity_of(sums_bytes)
    if expected_identity is not None:
        want = expected_identity.strip().lower()
        if not _HEX64_RE.fullmatch(want):
            raise SignError("STOP: --expected-identity must be a 64-character lowercase hex SHA-256")
        if actual != want:
            raise SignError(
                "STOP: SHA256SUMS release identity does not match --expected-identity — refusing to sign"
            )
    return actual


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.partial.{os.getpid()}")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


_LOOPBACK_HTTP = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?(/.*)?$")


def _vault_ssl_context(vault_addr: str, ca_file: Path | None) -> ssl.SSLContext | None:
    if not vault_addr.startswith(("https://", "http://")):
        raise SignError("STOP: Vault address must start with https://")
    if vault_addr.startswith("http://"):
        # The token travels in a header: plain HTTP only to a loopback listener
        # (a local Vault agent or a test stub), never across a network.
        if not _LOOPBACK_HTTP.fullmatch(vault_addr):
            raise SignError("STOP: Vault address must use https:// (plain http:// only to a loopback address)")
        return None
    if ca_file is not None:
        return ssl.create_default_context(cafile=str(ca_file))
    return ssl.create_default_context()


def _validate_vault_path(transit_mount: str, key_name: str) -> tuple[str, str]:
    mount = transit_mount.strip("/")
    if not _MOUNT_RE.fullmatch(mount):
        raise SignError("STOP: invalid Vault transit mount name")
    if not _KEY_NAME_RE.fullmatch(key_name):
        raise SignError("STOP: invalid Vault transit key name")
    return mount, key_name


def sign_with_vault(
    sums_path: Path,
    *,
    vault_addr: str,
    vault_token: str,
    transit_mount: str,
    key_name: str,
    out_sig: Path,
    ca_file: Path | None = None,
    expected_identity: str | None = None,
    verify_pub: Path | None = None,
    timeout_s: float = 30.0,
) -> dict[str, str]:
    """Sign SHA256SUMS via Vault transit RSA-3072 PKCS#1 v1.5 SHA-256 (prehashed).

    Returns metadata (release_identity, vault_signature_version, sig_bytes_len).
    Never prints or returns the Vault token.
    """
    if not vault_token:
        raise SignError("STOP: Vault token is empty")
    mount, key = _validate_vault_path(transit_mount, key_name)
    sums_bytes = sums_path.read_bytes()
    identity = _check_expected_identity(sums_bytes, expected_identity)

    payload = json.dumps({
        "input": sha256_digest_b64(sums_bytes),
        "hash_algorithm": "sha2-256",
        "signature_algorithm": "pkcs1v15",
        "prehashed": True,
    }).encode("utf-8")
    url = f"{vault_addr.rstrip('/')}/v1/{mount}/sign/{key}"
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Vault-Token": vault_token,
        },
    )
    ctx = _vault_ssl_context(vault_addr, ca_file)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SignError(f"STOP: Vault transit sign request failed (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise SignError("STOP: Vault transit sign request could not be completed") from None

    vault_sig = (body.get("data") or {}).get("signature")
    if not isinstance(vault_sig, str):
        raise SignError("STOP: Vault transit response did not include data.signature")

    raw_sig = vault_sig_to_raw(vault_sig)
    _write_atomic(out_sig, raw_sig)
    if verify_pub is not None:
        try:
            verify_signature_openssl(verify_pub, sums_path, out_sig)
        except SignError:
            out_sig.unlink(missing_ok=True)
            raise
    return {
        "release_identity": identity,
        "signature_sha256": hashlib.sha256(raw_sig).hexdigest(),
        "signature_bytes": str(len(raw_sig)),
    }


def export_vault_pubkey(
    *,
    vault_addr: str,
    vault_token: str,
    transit_mount: str,
    key_name: str,
    out_pub: Path,
    ca_file: Path | None = None,
    timeout_s: float = 30.0,
) -> dict[str, str]:
    """Fetch the latest public key from Vault transit, normalize with openssl, and validate RSA-3072."""
    if not vault_token:
        raise SignError("STOP: Vault token is empty")
    mount, key = _validate_vault_path(transit_mount, key_name)
    url = f"{vault_addr.rstrip('/')}/v1/{mount}/keys/{key}"
    req = urllib.request.Request(url, method="GET", headers={"X-Vault-Token": vault_token})
    ctx = _vault_ssl_context(vault_addr, ca_file)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SignError(f"STOP: Vault transit key read failed (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise SignError("STOP: Vault transit key read could not be completed") from None

    data = body.get("data") or {}
    latest = str(data.get("latest_version", ""))
    keys_map = data.get("keys") or {}
    entry = keys_map.get(latest) if isinstance(keys_map, dict) else None
    public_pem = entry.get("public_key") if isinstance(entry, dict) else None
    if not isinstance(public_pem, str) or not public_pem.strip():
        raise SignError("STOP: Vault transit key response did not include an exportable public_key")

    normalized = normalize_public_key_pem(public_pem.encode("utf-8"), require_rsa_3072=True)
    _write_atomic(out_pub, normalized)
    fp = pubkey_fingerprint_sha256(out_pub)
    return {"version": latest, "fingerprint_sha256": fp}


def sign_with_encrypted_key(
    sums_path: Path,
    *,
    key_path: Path,
    out_sig: Path,
    passin: str | None = None,
    expected_identity: str | None = None,
    verify_pub: Path | None = None,
) -> dict[str, str]:
    """Fallback D3 option (iii): sign SHA256SUMS with a passphrase-encrypted RSA-3072 PEM key."""
    if not key_path.is_file() or key_path.is_symlink():
        raise SignError("STOP: private key path must be a readable plain file (not a link)")
    sums_bytes = sums_path.read_bytes()
    identity = _check_expected_identity(sums_bytes, expected_identity)

    out_sig.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_sig.with_name(f".{out_sig.name}.partial.{os.getpid()}")
    cmd = ["openssl", "dgst", "-sha256", "-sign", str(key_path)]
    if passin is not None:
        cmd.extend(["-passin", passin])
    cmd.extend(["-out", str(tmp), str(sums_path)])
    try:
        # openssl prompts on the terminal when no -passin is given: no timeout then.
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=60 if passin is not None else None)
        if proc.returncode != 0 or not tmp.is_file():
            raise SignError("STOP: openssl signing failed (bad passphrase or invalid private key)")
        raw_sig = tmp.read_bytes()
        if len(raw_sig) != RSA_3072_MODULUS_BYTES:
            raise SignError(
                f"STOP: signature length ({len(raw_sig)} bytes) does not match RSA-3072 ({RSA_3072_MODULUS_BYTES} bytes)"
            )
        tmp.replace(out_sig)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    if verify_pub is not None:
        try:
            verify_signature_openssl(verify_pub, sums_path, out_sig)
        except SignError:
            out_sig.unlink(missing_ok=True)
            raise
    return {
        "release_identity": identity,
        "signature_sha256": hashlib.sha256(raw_sig).hexdigest(),
        "signature_bytes": str(len(raw_sig)),
    }


# --- vault-probe (#1343) -------------------------------------------------------------
# A copy of mainframe_rag.probe_receipt's rules (this file runs as one stdlib
# script under python3 -I); tests/test_gap_sign_vault.py pins the two equal.
PROBE_SCHEMA_VERSION = 1
PROBE_STATUSES = ("pass", "fail", "inconclusive")
_PROBE_CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}\.[a-z][a-z0-9_]{0,47}$")
_PROBE_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_PROBE_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.+=-]{0,64}$")
_PROBE_RESERVED = frozenset({"probe", "check", "status"})
# Signed by the probe: a fixed message that is not a SHA256SUMS file, so its
# signature can never stand in for a release's.
PROBE_MESSAGE = b"mainframe-rag vault-probe v1: test digest, not a release\n"
VAULT_PROBE_CHECKS = (
    "vault.key_read",
    "vault.key_type",
    "vault.public_key_export",
    "vault.sign_pkcs1v15_sha256",
)


class ProbeReceipt:
    """Text-free probe records (see mainframe_rag.probe_receipt.Receipt)."""

    def __init__(self, probe: str) -> None:
        if not _PROBE_FIELD_RE.fullmatch(probe):
            raise SignError("invalid probe name")
        self.probe = probe
        self.records: list[dict[str, object]] = []

    def add(self, check: str, status: str, **fields: object) -> None:
        if not _PROBE_CHECK_ID_RE.fullmatch(check) or not check.startswith(self.probe + "."):
            raise SignError("invalid check id")
        if status not in PROBE_STATUSES:
            raise SignError("invalid status")
        record: dict[str, object] = {"probe": self.probe, "check": check, "status": status}
        for name, value in fields.items():
            if not _PROBE_FIELD_RE.fullmatch(name) or name in _PROBE_RESERVED:
                raise SignError("invalid receipt field name")
            safe = value is None or isinstance(value, (bool, int)) or (
                isinstance(value, str) and _PROBE_SAFE_VALUE_RE.fullmatch(value) is not None
            )
            if not safe:
                raise SignError(f"receipt field {name} is not a boolean, integer or safe token")
            record[name] = value
        self.records.append(record)

    def summary(self) -> dict[str, object]:
        counts = {status: 0 for status in PROBE_STATUSES}
        for record in self.records:
            counts[str(record["status"])] += 1
        overall = "fail" if counts["fail"] else "inconclusive" if counts["inconclusive"] else "pass"
        return {
            "probe": self.probe,
            "check": self.probe + ".summary",
            "status": overall,
            "schema": PROBE_SCHEMA_VERSION,
            **counts,
        }

    def exit_code(self) -> int:
        return 1 if any(record["status"] == "fail" for record in self.records) else 0

    def lines(self) -> list[str]:
        return [json.dumps(r, separators=(",", ":")) for r in [*self.records, self.summary()]]


def write_probe_receipt(receipt: ProbeReceipt, path: Path | None = None) -> None:
    """Print the receipt; with ``path``, also write it there as a 0600 file."""
    text = "".join(line + "\n" for line in receipt.lines())
    sys.stdout.write(text)
    sys.stdout.flush()
    if path is None:
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _vault_call(
    req: urllib.request.Request, ctx: ssl.SSLContext | None, timeout_s: float
) -> tuple[int | None, dict | None]:
    """(HTTP status, JSON body) of one Vault call; never the error body. A
    transport failure is (None, None); a 2xx that is not a JSON object is
    (status, None)."""
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout_s) as resp:
            status = int(resp.status)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None, None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return status, None
    return status, body if isinstance(body, dict) else None


def _http_reason(status: int | None) -> str:
    if status is None:
        return "unreachable"
    return {403: "forbidden", 404: "not_found"}.get(status, "http_error")


def probe_vault(
    *,
    vault_addr: str,
    vault_token: str,
    transit_mount: str,
    key_name: str,
    ca_file: Path | None = None,
    timeout_s: float = 30.0,
) -> ProbeReceipt:
    """The read-only transit checks (#1343): key read, key type, public key
    export and a probe signature verified with ``openssl dgst -sha256 -verify``
    (the bootstrap command). Raises SignError only for configuration errors;
    every Vault outcome is a receipt record."""
    if not vault_token:
        raise SignError("STOP: Vault token is empty")
    mount, key = _validate_vault_path(transit_mount, key_name)
    ctx = _vault_ssl_context(vault_addr, ca_file)
    base = vault_addr.rstrip("/") + "/v1/" + mount
    receipt = ProbeReceipt("vault")
    results: dict[str, tuple[str, dict[str, object]]] = {}

    read_req = urllib.request.Request(
        base + "/keys/" + key, method="GET", headers={"X-Vault-Token": vault_token}
    )
    status, body = _vault_call(read_req, ctx, timeout_s)
    data = body.get("data") if isinstance(body, dict) else None
    public_pem: str | None = None
    with tempfile.TemporaryDirectory(prefix="vault-probe-") as tmp:
        work = Path(tmp)
        if status == 200 and isinstance(data, dict):
            results["vault.key_read"] = ("pass", {"http_status": status})
            key_type = data.get("type")
            exportable = data.get("exportable")
            latest = data.get("latest_version")
            type_facts: dict[str, object] = {
                "key_type": key_type if key_type in ("rsa-2048", "rsa-3072", "rsa-4096") else "other",
                "exportable": exportable if isinstance(exportable, bool) else None,
                "supports_signing": data.get("supports_signing") is True,
                "latest_version": latest if isinstance(latest, int) and not isinstance(latest, bool) else None,
            }
            if key_type != "rsa-3072":
                results["vault.key_type"] = ("fail", {"reason": "not_rsa_3072", **type_facts})
            elif exportable is not False:
                results["vault.key_type"] = ("fail", {"reason": "exportable", **type_facts})
            else:
                results["vault.key_type"] = ("pass", type_facts)
            keys_map = data.get("keys")
            entry = keys_map.get(str(latest)) if isinstance(keys_map, dict) else None
            candidate = entry.get("public_key") if isinstance(entry, dict) else None
            if isinstance(candidate, str) and candidate.strip():
                try:
                    normalized = normalize_public_key_pem(candidate.encode("utf-8"))
                    pub_path = work / "probe.pub"
                    pub_path.write_bytes(normalized)
                    fingerprint = pubkey_fingerprint_sha256(pub_path)
                    public_pem = str(pub_path)
                    results["vault.public_key_export"] = (
                        "pass", {"rsa_3072": True, "fingerprint_sha256": fingerprint}
                    )
                except (SignError, OSError, subprocess.SubprocessError):
                    results["vault.public_key_export"] = (
                        "fail", {"reason": "not_rsa_3072_public_key", "rsa_3072": False}
                    )
            else:
                results["vault.public_key_export"] = ("fail", {"reason": "no_public_key"})
        elif status == 200:
            results["vault.key_read"] = ("fail", {"reason": "unexpected_body", "http_status": status})
        else:
            results["vault.key_read"] = ("fail", {"reason": _http_reason(status), "http_status": status})

        payload = json.dumps({
            "input": sha256_digest_b64(PROBE_MESSAGE),
            "hash_algorithm": "sha2-256",
            "signature_algorithm": "pkcs1v15",
            "prehashed": True,
        }).encode("utf-8")
        sign_req = urllib.request.Request(
            base + "/sign/" + key,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "X-Vault-Token": vault_token},
        )
        status, body = _vault_call(sign_req, ctx, timeout_s)
        check = "vault.sign_pkcs1v15_sha256"
        signature = (body.get("data") or {}).get("signature") if isinstance(body, dict) else None
        if status != 200 or not isinstance(signature, str):
            reason = _http_reason(status) if status != 200 else "no_signature"
            results[check] = ("fail", {"reason": reason, "http_status": status})
        else:
            match = _VAULT_SIG_RE.fullmatch(signature.strip())
            try:
                raw = vault_sig_to_raw(signature)
            except SignError:
                raw = None
            if raw is None or match is None:
                results[check] = ("fail", {"reason": "malformed_signature", "http_status": status})
            else:
                facts: dict[str, object] = {
                    "http_status": status,
                    "key_version": int(match.group(1)),
                    "signature_bytes": len(raw),
                }
                if public_pem is None:
                    results[check] = ("inconclusive", {"reason": "no_public_key", **facts})
                else:
                    msg = work / "probe.msg"
                    sig = work / "probe.sig"
                    msg.write_bytes(PROBE_MESSAGE)
                    sig.write_bytes(raw)
                    try:
                        verify_signature_openssl(Path(public_pem), msg, sig)
                        results[check] = ("pass", {"verified": True, **facts})
                    except (SignError, subprocess.SubprocessError):
                        results[check] = (
                            "fail", {"reason": "verify_failed", "verified": False, **facts}
                        )

    for name in VAULT_PROBE_CHECKS:
        state, fields = results.get(name, ("inconclusive", {"reason": "not_reached"}))
        receipt.add(name, state, **fields)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gap release signing helpers (run outside the bastion, Rev 2 D3).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_vsign = sub.add_parser("vault-sign", help="sign SHA256SUMS via Vault transit (RSA-3072 PKCS#1 v1.5 SHA-256)")
    p_vsign.add_argument("--sums", type=Path, required=True)
    p_vsign.add_argument("--out", type=Path, required=True)
    p_vsign.add_argument("--vault-addr", default=os.environ.get("VAULT_ADDR", ""))
    p_vsign.add_argument("--transit-mount", default=os.environ.get("VAULT_TRANSIT_MOUNT", "transit"))
    p_vsign.add_argument("--key-name", required=True)
    p_vsign.add_argument("--ca-file", type=Path, default=None)
    p_vsign.add_argument("--expected-identity", required=True,
                         help="release identity pack printed; the signer reads it before signing (D4)")
    p_vsign.add_argument("--verify-pub", type=Path, default=None)

    p_vpub = sub.add_parser("vault-pubkey", help="export and normalize RSA-3072 public key from Vault transit")
    p_vpub.add_argument("--out", type=Path, required=True)
    p_vpub.add_argument("--vault-addr", default=os.environ.get("VAULT_ADDR", ""))
    p_vpub.add_argument("--transit-mount", default=os.environ.get("VAULT_TRANSIT_MOUNT", "transit"))
    p_vpub.add_argument("--key-name", required=True)
    p_vpub.add_argument("--ca-file", type=Path, default=None)

    p_conv = sub.add_parser("convert", help="convert vault:v<n>:<base64> signature string to raw SHA256SUMS.sig")
    p_conv.add_argument("--signature", required=True, help="vault:v<n>:<base64> string (or '-' to read stdin)")
    p_conv.add_argument("--out", type=Path, required=True)
    p_conv.add_argument("--sums", type=Path, default=None, help="optional SHA256SUMS to verify against --verify-pub")
    p_conv.add_argument("--verify-pub", type=Path, default=None)

    p_ksign = sub.add_parser("key-sign", help="fallback zero-egress sign-pod signing with encrypted RSA-3072 PEM")
    p_ksign.add_argument("--sums", type=Path, required=True)
    p_ksign.add_argument("--key", type=Path, required=True)
    p_ksign.add_argument("--out", type=Path, required=True)
    p_ksign.add_argument("--passin", default=None, help="openssl -passin source (e.g. fd:3 or env:VAR)")
    p_ksign.add_argument("--expected-identity", required=True,
                         help="release identity pack printed; the signer reads it before signing (D4)")
    p_ksign.add_argument("--verify-pub", type=Path, default=None)

    p_probe = sub.add_parser("vault-probe", help="read-only transit key checks with a text-free receipt (#1343)")
    p_probe.add_argument("--vault-addr", default=os.environ.get("VAULT_ADDR", ""))
    p_probe.add_argument("--transit-mount", default=os.environ.get("VAULT_TRANSIT_MOUNT", "transit"))
    p_probe.add_argument("--key-name", required=True)
    p_probe.add_argument("--ca-file", type=Path, default=None)
    p_probe.add_argument("--receipt", type=Path, default=None, help="also write the receipt here (mode 0600)")

    p_ver = sub.add_parser("verify", help="verify raw SHA256SUMS.sig using bootstrap.sh's openssl command")
    p_ver.add_argument("--pub", type=Path, required=True)
    p_ver.add_argument("--sums", type=Path, required=True)
    p_ver.add_argument("--sig", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "vault-probe":
        try:
            receipt = probe_vault(
                vault_addr=args.vault_addr,
                vault_token=os.environ.get("VAULT_TOKEN", ""),
                transit_mount=args.transit_mount,
                key_name=args.key_name,
                ca_file=args.ca_file,
            )
        except SignError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        try:
            write_probe_receipt(receipt, args.receipt)
        except OSError:
            print("STOP: the receipt file could not be written", file=sys.stderr)
            return 2
        return receipt.exit_code()
    try:
        if args.command == "vault-sign":
            token = os.environ.get("VAULT_TOKEN", "")
            info = sign_with_vault(
                args.sums,
                vault_addr=args.vault_addr,
                vault_token=token,
                transit_mount=args.transit_mount,
                key_name=args.key_name,
                out_sig=args.out,
                ca_file=args.ca_file,
                expected_identity=args.expected_identity,
                verify_pub=args.verify_pub,
            )
            print(f"SIGNED {info['release_identity']} {info['signature_sha256']}")
        elif args.command == "vault-pubkey":
            token = os.environ.get("VAULT_TOKEN", "")
            info = export_vault_pubkey(
                vault_addr=args.vault_addr,
                vault_token=token,
                transit_mount=args.transit_mount,
                key_name=args.key_name,
                out_pub=args.out,
                ca_file=args.ca_file,
            )
            print(f"PUBKEY v{info['version']} {info['fingerprint_sha256']}")
        elif args.command == "convert":
            sig_text = sys.stdin.read() if args.signature == "-" else args.signature
            raw = vault_sig_to_raw(sig_text)
            _write_atomic(args.out, raw)
            if args.verify_pub is not None:
                if args.sums is None:
                    raise SignError("STOP: --sums is required when --verify-pub is given")
                try:
                    verify_signature_openssl(args.verify_pub, args.sums, args.out)
                except SignError:
                    args.out.unlink(missing_ok=True)
                    raise
            print(f"CONVERTED {hashlib.sha256(raw).hexdigest()} {len(raw)}")
        elif args.command == "key-sign":
            info = sign_with_encrypted_key(
                args.sums,
                key_path=args.key,
                out_sig=args.out,
                passin=args.passin,
                expected_identity=args.expected_identity,
                verify_pub=args.verify_pub,
            )
            print(f"SIGNED {info['release_identity']} {info['signature_sha256']}")
        else:
            verify_signature_openssl(args.pub, args.sums, args.sig)
            print(f"VERIFIED {release_identity_of(args.sums.read_bytes())}")
    except SignError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("STOP: signing input or output file could not be read or written", file=sys.stderr)
        return 1
    except subprocess.SubprocessError:
        print("STOP: openssl did not finish; nothing was signed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
