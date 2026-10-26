#!/usr/bin/env python3
"""Offline AD FS OIDC claims check for the site bastion (#1070).

Stdlib only; run as ``python3 -I scripts/openwebui/adfs_claims_check.py``.
It runs the authorization-code flow against the AD FS client registered for
Open WebUI and reports which claims the ID token carries.

Output rules: claim NAMES, a roles-claim JSON type and value COUNT, and yes/no
answers only. Tokens, the client secret, claim values, group names and user
identity are never printed. Error text from the network, TLS or the server is
never printed either; every failure is one fixed line.

The ID token signature is NOT verified: this is a diagnostic of what AD FS
issues, not an authentication step.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import http.client
import http.server
import json
import os
import secrets
import signal
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_SCOPES = "openid profile email allatclaims"
MAX_BODY = 1_000_000

# Exit codes with one fixed reason each.
(
    EXIT_USAGE,
    EXIT_TLS,
    EXIT_NET,
    EXIT_CALLBACK,
    EXIT_TOKEN,
    EXIT_VALIDATE,
    EXIT_CLAIMS,
    EXIT_INTERNAL,
) = (2, 3, 4, 5, 6, 7, 8, 9)
SOCKET_TIMEOUT = 3.0  # per accepted callback connection


class CheckError(Exception):
    """A failed expectation: ``code`` is the exit status, ``reason`` is fixed text."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


# ---------------------------------------------------------------- pure helpers


def b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """Return (verifier, S256 challenge)."""
    verifier = b64url_nopad(secrets.token_bytes(48))
    return verifier, b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())


def build_authorize_url(
    endpoint: str,
    client_id: str,
    redirect_uri: str,
    scopes: str,
    state: str,
    nonce: str,
    challenge: str,
) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    sep = "&" if "?" in endpoint else "?"
    return f"{endpoint}{sep}{query}"


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode the payload segment of a JWT without verifying the signature."""
    parts = token.strip().split(".")
    if len(parts) != 3 or not parts[1]:
        raise CheckError(EXIT_VALIDATE, "ID token is not a three-part JWT")
    try:
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")))
    except (ValueError, UnicodeError):
        raise CheckError(EXIT_VALIDATE, "ID token payload is not base64url JSON") from None
    if not isinstance(payload, dict):
        raise CheckError(EXIT_VALIDATE, "ID token payload is not a JSON object")
    return payload


def validate_claims(
    claims: dict[str, Any],
    *,
    issuer: str | None,
    client_id: str | None,
    nonce: str | None,
    now: float | None,
) -> list[str]:
    """Return fixed reasons for each failed standard check (empty when all pass).

    A check whose expected value is None is skipped (offline analysis).
    """
    failed: list[str] = []
    if issuer is not None and claims.get("iss") != issuer:
        failed.append("iss does not match the discovery issuer")
    if client_id is not None:
        aud = claims.get("aud")
        ok = aud == client_id or (isinstance(aud, list) and client_id in aud)
        if not ok:
            failed.append("aud does not match the client ID")
    if nonce is not None and claims.get("nonce") != nonce:
        failed.append("nonce does not match the request")
    if now is not None:
        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp <= now:
            failed.append("exp is missing or in the past")
    return failed


def role_values(value: Any) -> tuple[str, int, list[str]]:
    """Return (JSON type name, value count, values) for a roles claim.

    Open WebUI v0.12.3 reads the roles claim as a JSON list, or as a single string
    that it splits on ',' only (docs/deploy.md, "Register Open WebUI with AD FS").
    Anything else yields no roles; ';' is NOT a separator for roles.
    """
    if isinstance(value, list):
        items = [v for v in value if isinstance(v, str)]
        return "list", len(value), items
    if isinstance(value, str):
        items = [v for v in value.split(",") if v]
        return "string", len(items), items
    kind = {bool: "boolean", int: "number", float: "number", dict: "object"}.get(
        type(value), "null" if value is None else "other"
    )
    return kind, 0, []


def analyse(
    claims: dict[str, Any],
    *,
    roles_claim: str,
    email_claim: str,
    username_claim: str,
    baseline: str,
    expect_baseline_only: bool,
) -> tuple[list[str], list[str]]:
    """Return (report lines, failure reasons). Lines carry no claim values."""
    lines = ["claim names: " + (", ".join(sorted(claims)) or "(none)")]
    failures: list[str] = []

    if roles_claim in claims:
        kind, count, items = role_values(claims[roles_claim])
        lines.append(f"roles claim '{roles_claim}': present, type={kind}, count={count}")
        if kind not in ("list", "string") or count == 0:
            failures.append("roles claim is empty or not a list or string")
        if isinstance(claims[roles_claim], list) and len(items) != count:
            failures.append("roles list holds a value that is not a string")
        has = baseline in items
        lines.append(f"baseline role present: {'yes' if has else 'no'}")
        if not has:
            failures.append("baseline role is missing from the roles claim")
        elif expect_baseline_only:
            if count != 1:
                failures.append("roles claim has more than the baseline value")
        elif count == 1:
            failures.append("roles claim carries only the baseline value")
    else:
        lines.append(f"roles claim '{roles_claim}': absent")
        failures.append("roles claim is absent from the ID token")
        lines.append("baseline role present: no")

    for label, name in (("email claim", email_claim), ("username claim", username_claim)):
        present = isinstance(claims.get(name), str) and bool(claims.get(name))
        lines.append(f"{label} '{name}': {'present' if present else 'absent'}")
        if not present:
            failures.append(f"{label} is absent from the ID token")
    others = [n for n in ("email", "upn", "preferred_username", "unique_name") if n in claims]
    lines.append("other identity claim names present: " + (", ".join(others) or "(none)"))
    return lines, failures


# ------------------------------------------------------------------ network


def make_ssl_context(ca_path: str) -> ssl.SSLContext:
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = True
        ctx.load_verify_locations(cafile=ca_path)
    except (OSError, ssl.SSLError):
        raise CheckError(EXIT_USAGE, "CA bundle is unreadable or not PEM") from None
    return ctx


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        return None


def _http_json(
    url: str,
    ctx: ssl.SSLContext,
    *,
    data: dict[str, str] | None,
    timeout: float,
    what: str,
    fail_code: int,
) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BODY + 1)
    except urllib.error.HTTPError:
        raise CheckError(fail_code, f"{what} returned an HTTP error status") from None
    except (urllib.error.URLError, ssl.SSLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError) or isinstance(
            exc, ssl.SSLCertVerificationError
        ):
            raise CheckError(
                EXIT_TLS, "CA bundle does not validate the AD FS certificate"
            ) from None
        raise CheckError(EXIT_NET, f"{what} request failed (network or TLS)") from None
    if len(raw) > MAX_BODY:
        raise CheckError(fail_code, f"{what} response is too large")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise CheckError(fail_code, f"{what} response is not JSON") from None
    if not isinstance(doc, dict):
        raise CheckError(fail_code, f"{what} response is not a JSON object")
    return doc


def fetch_discovery(issuer: str, ctx: ssl.SSLContext, timeout: float) -> dict[str, Any]:
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    doc = _http_json(url, ctx, data=None, timeout=timeout, what="discovery", fail_code=EXIT_NET)
    for key in ("issuer", "authorization_endpoint", "token_endpoint"):
        if not isinstance(doc.get(key), str) or not doc[key].startswith("https://"):
            raise CheckError(EXIT_NET, "discovery document lacks an https endpoint it needs")
    return doc


class _Callback(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] | None = None

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        ok = parsed.path == "/callback"
        if ok:
            qs = urllib.parse.parse_qs(parsed.query)
            type(self).result = {k: v[0] for k, v in qs.items() if v}
        msg = b"You can close this tab." if ok else b"Not found."
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def log_message(self, *args: Any) -> None:
        return


def wait_for_callback(port: int, timeout: float) -> dict[str, str]:
    """Listen once on 127.0.0.1 for /callback and return its query parameters."""

    class Handler(_Callback):
        result: dict[str, str] | None = None
        timeout = SOCKET_TIMEOUT  # an idle connection must not stall the listener

    class Server(http.server.HTTPServer):
        def handle_error(self, request: Any, client_address: Any) -> None:
            return  # the default prints a traceback

    deadline = time.monotonic() + timeout
    try:
        srv = Server(("127.0.0.1", port), Handler)
    except OSError:
        raise CheckError(EXIT_CALLBACK, "cannot listen on the local callback port") from None
    with srv:
        while Handler.result is None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CheckError(EXIT_CALLBACK, "no callback arrived before the timeout")
            srv.timeout = left
            srv.handle_request()
    return Handler.result


def parse_pasted_redirect(text: str, redirect_uri: str, state: str) -> str:
    """Return the authorization code from a pasted redirect URL, or raise CheckError.

    Scheme, host and path must equal ``redirect_uri`` exactly (the host is compared
    case-insensitively); extra query parameters such as session_state are ignored.
    Nothing from ``text`` appears in any error.
    """
    try:
        got = urllib.parse.urlsplit(text.strip())
        want = urllib.parse.urlsplit(redirect_uri)
        query = urllib.parse.parse_qs(got.query, keep_blank_values=True)
    except ValueError:
        raise CheckError(EXIT_CALLBACK, "pasted text is not a URL") from None
    if (got.scheme.lower(), got.netloc.lower(), got.path) != (
        want.scheme.lower(),
        want.netloc.lower(),
        want.path,
    ):
        raise CheckError(EXIT_CALLBACK, "pasted URL does not match the redirect URI")
    if "error" in query:
        raise CheckError(EXIT_CALLBACK, "AD FS returned an authorization error")
    if len(query.get("state", [])) != 1 or not secrets.compare_digest(
        query["state"][0].encode(), state.encode()
    ):
        raise CheckError(EXIT_CALLBACK, "callback state does not match the request")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise CheckError(EXIT_CALLBACK, "pasted URL carries no single authorization code")
    return codes[0]


def read_pasted_url() -> str:
    """Read one line without echoing it (getpass on a terminal, stdin otherwise)."""
    prompt = "Paste the full redirected URL now (input hidden; the code expires in minutes): "
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    print(prompt, flush=True)
    return sys.stdin.readline()


def read_secret(path: str | None) -> str:
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            raise CheckError(EXIT_USAGE, "client secret file is unreadable") from None
    else:
        value = getpass.getpass("Client secret (input hidden): ").strip()
    if not value:
        raise CheckError(EXIT_USAGE, "client secret is empty")
    return value


# --------------------------------------------------------------------- flow


def arm_total_timeout(seconds: float) -> None:
    """Bound the whole run, including slow-drip responses that defeat socket timeouts."""

    def expired(signum: int, frame: Any) -> None:
        raise CheckError(EXIT_NET, "the run exceeded the overall time limit")

    if hasattr(signal, "setitimer"):
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)


def run_flow(args: argparse.Namespace) -> dict[str, Any]:
    ctx = make_ssl_context(args.ca_bundle)
    secret = read_secret(args.client_secret_file)
    arm_total_timeout(args.total_timeout)
    disc = fetch_discovery(args.issuer, ctx, args.http_timeout)
    print("discovery: ok")

    paste = bool(args.paste)
    if paste:
        redirect_uri = args.redirect_uri
    else:
        redirect_uri = f"http://{args.redirect_host}:{args.port}/callback"
    verifier, challenge = pkce_pair()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    url = build_authorize_url(
        disc["authorization_endpoint"],
        args.client_id,
        redirect_uri,
        args.scopes,
        state,
        nonce,
        challenge,
    )
    if paste:
        print(
            "Open this URL in a browser and sign in with the test account. The browser "
            "then lands on an error page for the registered host; that is expected. "
            "Copy the full address from the address bar.\n" + url
        )
        code = parse_pasted_redirect(read_pasted_url(), redirect_uri, state)
    else:
        print(f"Open this URL in a browser and sign in with the test account:\n{url}")
        print(
            f"Waiting up to {int(args.timeout)} s on http://127.0.0.1:{args.port}/callback ...",
            flush=True,
        )
        params = wait_for_callback(args.port, args.timeout)
        if "error" in params:
            raise CheckError(EXIT_CALLBACK, "AD FS returned an authorization error")
        if not secrets.compare_digest(params.get("state", ""), state):
            raise CheckError(EXIT_CALLBACK, "callback state does not match the request")
        if not params.get("code"):
            raise CheckError(EXIT_CALLBACK, "callback carries no authorization code")
        code = params["code"]
    print("callback: state ok")

    tok = _http_json(
        disc["token_endpoint"],
        ctx,
        timeout=args.http_timeout,
        what="token endpoint",
        fail_code=EXIT_TOKEN,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": args.client_id,
            "client_secret": secret,
            "code_verifier": verifier,
        },
    )
    id_token = tok.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise CheckError(EXIT_TOKEN, "token response carries no id_token")
    claims = decode_jwt_payload(id_token)
    bad = validate_claims(
        claims, issuer=disc["issuer"], client_id=args.client_id, nonce=nonce, now=time.time()
    )
    report_and_fail(claims, args, bad, offline=False)
    return claims


def report_and_fail(
    claims: dict[str, Any], args: argparse.Namespace, std_failures: list[str], *, offline: bool
) -> None:
    lines, failures = analyse(
        claims,
        roles_claim=args.roles_claim,
        email_claim=args.email_claim,
        username_claim=args.username_claim,
        baseline=args.baseline_role,
        expect_baseline_only=args.expect_baseline_only,
    )
    print("note: the ID token signature is NOT verified (diagnostic only)")
    if offline:
        print("standard checks: iss/aud only when given; nonce and exp not checked offline")
    for check, label in (("iss", "iss"), ("aud", "aud"), ("nonce", "nonce"), ("exp", "exp")):
        if not offline or check in ("iss", "aud"):
            failed = any(f.startswith(check + " ") for f in std_failures)
            print(f"{label} check: {'FAILED' if failed else 'ok'}")
    for line in lines:
        print(line)
    if std_failures:
        raise CheckError(EXIT_VALIDATE, std_failures[0])
    if failures:
        raise CheckError(EXIT_CLAIMS, failures[0])


def run_decode_only(args: argparse.Namespace) -> None:
    try:
        with open(args.decode_only, encoding="utf-8") as fh:
            token = fh.read()
    except OSError:
        raise CheckError(EXIT_USAGE, "ID token file is unreadable") from None
    claims = decode_jwt_payload(token)
    bad = validate_claims(
        claims, issuer=args.expect_iss, client_id=args.client_id, nonce=None, now=None
    )
    report_and_fail(claims, args, bad, offline=True)


# --------------------------------------------------------------------- main


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or default


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Check the claims AD FS puts in the Open WebUI client's ID token. "
        "Prints claim names, a roles type/count and yes/no answers only. "
        "Every flag also reads the ADFS_* environment variable shown.",
    )
    p.add_argument(
        "--issuer",
        default=env("ADFS_ISSUER"),
        help="issuer URL, e.g. https://<adfs-host>/adfs (ADFS_ISSUER)",
    )
    p.add_argument("--client-id", default=env("ADFS_CLIENT_ID"), help="(ADFS_CLIENT_ID)")
    p.add_argument(
        "--client-secret-file",
        default=env("ADFS_CLIENT_SECRET_FILE"),
        help="file holding the secret; omit to be prompted (ADFS_CLIENT_SECRET_FILE)",
    )
    p.add_argument(
        "--ca-bundle",
        default=env("ADFS_CA_BUNDLE"),
        help="PEM CA bundle for the AD FS certificate (ADFS_CA_BUNDLE)",
    )
    p.add_argument(
        "--roles-claim",
        default=env("ADFS_ROLES_CLAIM", "roles"),
        help="OPENWEBUI_OIDC_ROLES_CLAIM (ADFS_ROLES_CLAIM, default roles)",
    )
    p.add_argument(
        "--email-claim",
        default=env("ADFS_EMAIL_CLAIM", "upn"),
        help="OPENWEBUI_OIDC_EMAIL_CLAIM (ADFS_EMAIL_CLAIM, default upn)",
    )
    p.add_argument(
        "--username-claim",
        default=env("ADFS_USERNAME_CLAIM", "name"),
        help="OPENWEBUI_OIDC_USERNAME_CLAIM (ADFS_USERNAME_CLAIM, default name)",
    )
    p.add_argument(
        "--baseline-role",
        default=env("ADFS_BASELINE_ROLE", "signed-in"),
        help="baseline role value, default signed-in; only yes/no is printed (ADFS_BASELINE_ROLE)",
    )
    p.add_argument(
        "--expect-baseline-only",
        action="store_true",
        help="the test account is in no role group: pass only if the baseline "
        "is the single role value",
    )
    p.add_argument("--scopes", default=env("ADFS_SCOPES", DEFAULT_SCOPES))
    p.add_argument("--port", type=int, default=int(env("ADFS_CALLBACK_PORT", "8400") or 8400))
    p.add_argument(
        "--redirect-host",
        default="localhost",
        help="host in the registered redirect URI (default localhost)",
    )
    p.add_argument(
        "--paste",
        action="store_true",
        help="no listener: print the authorize URL, then read the redirected URL pasted "
        "from the browser address bar (needs --redirect-uri)",
    )
    p.add_argument(
        "--redirect-uri",
        default=env("ADFS_REDIRECT_URI"),
        help="with --paste: the exact registered redirect URI, e.g. "
        "https://<openwebui-host>/oauth/oidc/callback (ADFS_REDIRECT_URI)",
    )
    p.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for the callback")
    p.add_argument("--http-timeout", type=float, default=15.0, help="per-request socket timeout")
    p.add_argument(
        "--total-timeout",
        type=float,
        default=float(env("ADFS_TOTAL_TIMEOUT", "300") or 300),
        help="seconds allowed for the whole flow after the secret is read (default 300)",
    )
    p.add_argument(
        "--decode-only", metavar="FILE", help="analyse a saved ID token file offline; no network"
    )
    p.add_argument("--expect-iss", help="with --decode-only: expected iss (optional)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.decode_only:
            run_decode_only(args)
        else:
            missing = [n for n in ("issuer", "client_id", "ca_bundle") if not getattr(args, n)]
            if missing or not args.issuer.startswith("https://"):
                raise CheckError(EXIT_USAGE, "issuer (https), client ID and CA bundle are required")
            if args.paste and not (args.redirect_uri or "").startswith(("https://", "http://")):
                raise CheckError(EXIT_USAGE, "--paste needs --redirect-uri with the registered URI")
            if args.redirect_uri and not args.paste:
                raise CheckError(EXIT_USAGE, "--redirect-uri is only used with --paste")
            if not args.baseline_role:
                raise CheckError(EXIT_USAGE, "baseline role must not be empty")
            if not 1 <= args.port <= 65535:
                raise CheckError(EXIT_USAGE, "callback port is out of range")
            run_flow(args)
    except CheckError as err:
        print(f"FAIL: {err.reason}")
        return err.code
    except (KeyboardInterrupt, EOFError):
        print("FAIL: interrupted")
        return EXIT_USAGE
    except (TimeoutError, OSError, http.client.HTTPException):
        print("FAIL: unexpected local or network error")
        return EXIT_NET
    except Exception:  # noqa: BLE001  # never show exception or server text
        print("FAIL: internal error in the check")
        return EXIT_INTERNAL
    print("RESULT: ok - the ID token meets every expectation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
