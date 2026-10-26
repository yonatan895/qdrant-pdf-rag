"""Add the site's IdP CA to the Open WebUI image's public trust stores (#1074).

Runs as the `ca-trust` init container of the Open WebUI pod, with the image's
own interpreter, only when openwebui.oidc.caBundle is set:

  python3 -I /etc/mainframe-rag/idp-ca/ca_trust.py

The bundle (ConfigMap rag-openwebui-idp-ca, key ca.crt) must be one or more PEM
certificates and nothing else, and OpenSSL must load every one of them. Then
OpenSSL's system bundle, the certifi bundle and the IdP certificates are
written to one file on an emptyDir; the app reads it through SSL_CERT_FILE and
REQUESTS_CA_BUNDLE. Anything else exits 1 with one fixed message that never
echoes the content, so a bad bundle leaves the pod not Ready instead of a Ready
pod whose every sign-in fails. Stdlib only (certifi is imported from the image).
"""
from __future__ import annotations

import re
import ssl
import sys
from pathlib import Path

IDP = Path("/etc/mainframe-rag/idp-ca/ca.crt")
OUT = Path("/etc/mainframe-rag/ca-trust/ca-bundle.crt")
REFUSAL = ("FAIL: ca-trust: openwebui.oidc.caBundle (OPENWEBUI_IDP_CA_FILE) must hold only PEM "
           "certificates that load; nothing was written (content not shown)")
_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----\r?\n(.*?)-----END \1-----", re.DOTALL)


def certificates(text: str) -> list[str]:
    """The PEM certificates of text, each re-emitted as one block. ValueError
    unless text is one or more CERTIFICATE blocks with only whitespace between
    them and OpenSSL loads each one (a key, stray text or a corrupted body fails)."""
    blocks = list(_BLOCK.finditer(text))
    if not blocks or _BLOCK.sub("", text).strip():
        raise ValueError("not only PEM blocks")
    certs = []
    for block in blocks:
        if block.group(1) != "CERTIFICATE":
            raise ValueError("not a certificate block")
        pem = block.group(0) + "\n"
        try:
            der = ssl.PEM_cert_to_DER_cert(pem)
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=der)
        except (ValueError, ssl.SSLError):
            raise ValueError("certificate does not load") from None
        certs.append(pem)
    return certs


def public_stores() -> list[Path]:
    """The stores the image trusts without a bundle: OpenSSL's default file
    (aiohttp, ssl.create_default_context) and certifi's (httpx's default)."""
    import certifi

    return [Path(ssl.get_default_verify_paths().openssl_cafile), Path(certifi.where())]


def build(idp: Path, public: list[Path], out: Path) -> int:
    """Write the public stores plus the IdP certificates to out; return the
    number of IdP certificates. ValueError/OSError leave out unwritten."""
    certs = certificates(idp.read_text(encoding="utf-8"))
    parts = [path.read_text(encoding="utf-8").strip() + "\n" for path in public]
    staged = out.with_name(out.name + ".tmp")
    staged.write_text("".join(parts + certs), encoding="utf-8")
    try:
        # The file must load as a whole, exactly as httpx and aiohttp load it.
        ssl.create_default_context(cafile=str(staged))
    except ssl.SSLError:
        staged.unlink()
        raise ValueError("combined store does not load") from None
    staged.replace(out)
    return len(certs)


def main() -> int:
    try:
        count = build(IDP, public_stores(), OUT)
    except (OSError, UnicodeDecodeError, ValueError, ImportError):
        print(REFUSAL, file=sys.stderr)
        return 1
    print(f"ca-trust: {count} IdP certificate(s) added to the public trust stores")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
