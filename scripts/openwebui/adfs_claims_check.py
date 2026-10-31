#!/usr/bin/env python3
"""Offline AD FS OIDC claims check for the site bastion (#1070).

Stdlib only; run as ``python3 -I scripts/openwebui/adfs_claims_check.py``.
It runs the authorization-code flow against the AD FS client registered for
Open WebUI and reports which claims the ID token carries.

Two modes. Baseline mode (default) expects the IdP to issue a constant baseline
value plus role values. Role-list mode (``--allowed-roles``/``--admin-roles``) is
for an IdP that can only issue LDAP attributes: the roles claim is the group
claim itself, and the check predicts the Open WebUI v0.12.3 outcome (admin, user
or refused) from the configured role lists. A refusal is a 403 inside Open WebUI; the
browser gets a redirect to /auth?error=... and no session cookie.

Output rules: claim NAMES, a roles-claim JSON type and value COUNT, the ID token
size, yes/no answers and, in role-list mode, the CONFIGURED role values that
matched. Tokens, the client secret, other claim values, group names and user
identity are never printed. Error text from the network, TLS or the server is
never printed either; every failure is one fixed line.

Access-token mode (``--access-token``, #1075 T0, #1088 item 1) reports on the
ACCESS token of the same token response instead: the token Open WebUI forwards to
the team-tool gateways. It prints one fixed verdict code (ACCESS_VERDICTS), most
severe first; ``userinfo_audience`` means AD FS issued the token for
urn:microsoft:userinfo, which every gateway refuses. It prints claim names, the
aud count with yes/no answers, the group claim's type, count and form, the
lifetime in seconds and whether a refresh token was issued; it never prints aud,
iss, sub or group values from the token, only the CONFIGURED gateway audience and
team group when they match.

The ID token signature is NOT verified: this is a diagnostic of what AD FS
issues, not an authentication step. The same holds for the access token.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import getpass
import hashlib
import http.client
import http.server
import json
import math
import os
import secrets
import shlex
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

# ID token size (#1070). The chart sets ENABLE_OAUTH_ID_TOKEN_COOKIE=false, so the
# sign-in callback no longer carries the token as a cookie; before that, the pod's
# allow-list proxy (nginx default proxy_buffer_size, one 4 KiB page) answered 502
# once the callback headers passed 4096 bytes. Open WebUI v0.12.3 keeps the token
# in its database (models/oauth_sessions.py), with no size limit found. The step
# that still carries the whole token is sign-out: routers/auths.py signout sends
# the browser to the IdP's end_session_endpoint with it as id_token_hint in the
# URL. AD FS runs on Windows http.sys, whose request line and headers are limited
# to 16384 bytes by default (MaxRequestBytes and MaxFieldLength in Microsoft's
# http.sys registry documentation; not verified here), shared with the IdP's own
# cookies. Above ID_TOKEN_WARN_BYTES, confirm sign-out on the test client.
ID_TOKEN_WARN_BYTES = 8000

# Mirrors scripts/airgap/model_config.py and the chart (values.schema.json,
# templates/_helpers.tpl): comma-separated names without spaces; never * or the
# baseline value.
REFUSED_ROLE_VALUES = ("*", "signed-in")

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
    EXIT_ACCESS,
) = (2, 3, 4, 5, 6, 7, 8, 9, 10)
SOCKET_TIMEOUT = 3.0  # per accepted callback connection


# Access-token mode (#1075 T0). AD FS issues an access token for this resource
# when the authorize request names no other resource; no gateway accepts it.
USERINFO_AUDIENCE = "urn:microsoft:userinfo"
# The gateway library's default algorithm (src/mainframe_rag/mcp/tool_auth.py); ES256
# needs explicit gateway configuration, so the probe does not count it as ready.
GATEWAY_ALGS = ("RS256",)
# The gateway library refuses longer tokens (tool_auth._MAX_TOKEN_CHARS).
GATEWAY_MAX_TOKEN_CHARS = 32 * 1024
# Header alg values printed as-is; anything else prints as "other".
KNOWN_ALGS = GATEWAY_ALGS + ("ES256", "RS384", "RS512", "PS256", "PS384", "ES384", "HS256", "none")
# Open WebUI v0.12.3 refreshes the access token 5 minutes before it expires.
MIN_LIFETIME_S = 600

# One verdict per run, most severe first. Codes and text are fixed and stable.
ACCESS_VERDICTS = {
    "absent": "the token response carries no access token",
    "opaque": "the access token is not a JWT; no gateway can verify it",
    "too_large": "the access token is above the gateways' 32 KiB limit",
    "userinfo_audience": "the access token is for urn:microsoft:userinfo; every gateway refuses it",
    "unsupported_alg": "the access token is not signed with RS256 under a key id",
    "claims_missing": "the access token lacks a usable sub, iss, aud or exp",
    "issuer_mismatch": "the access token's iss differs from the expected issuer",
    "expired": "the access token has already expired",
    "no_groups": "the access token carries no value in the group claim",
    "team_group_missing": "the access token's group claim lacks the team group",
    "gateway_audience_missing": "the access token's aud names no configured gateway audience",
    "audience_unchecked": "no --gateway-audience was given; the token fits a gateway only "
    "through token exchange",
    "ok": "the access token meets every gateway expectation",
}


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


def openwebui_roles(value: Any) -> list[Any]:
    """The roles Open WebUI v0.12.3 reads from the claim (utils/oauth.py get_user_role).

    A list is taken as-is, a string is split on ',' when it holds one, an int
    becomes its decimal string, anything else gives no roles. Values are not
    stripped and the comparison with the configured roles is exact.
    """
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        return value.split(",") if "," in value else [value]
    if isinstance(value, int):
        return [str(value)]
    return []


def parse_role_list(raw: str | None) -> list[str]:
    """Split a configured role list exactly as the chart and validate accept it."""
    if raw is None or raw == "":
        return []
    items = raw.split(",")
    for item in items:
        if not item or any(c in item for c in (" ", "\t", "\n", "\r", "\v", "\f")):
            raise CheckError(
                EXIT_USAGE, "role lists are comma-separated names without spaces or empty items"
            )
        if item in REFUSED_ROLE_VALUES:
            raise CheckError(EXIT_USAGE, "role lists must not contain * or signed-in")
    return items


def predict_outcome(roles: list[Any], allowed: list[str], admin: list[str]) -> str:
    """Open WebUI v0.12.3 role decision at sign-in: admin, user, refused or unchanged."""
    if not roles:
        return "unchanged"
    if any(r in roles for r in admin):
        return "admin"
    if any(r in roles for r in allowed):
        return "user"
    return "refused"


OUTCOME_TEXT = {
    "admin": "admin",
    "user": "user",
    "refused": "refused (redirect to /auth?error=..., no session cookie)",
    "unchanged": "no role decision (claim absent or empty): an existing user keeps the "
    "stored role and a new user is pending",
}


def value_form(values: list[str]) -> str:
    """'domain-qualified' (every value has DOMAIN\\), 'bare' (none has) or 'mixed'."""
    qualified = sum(1 for v in values if "\\" in v)
    if qualified == len(values):
        return "domain-qualified"
    return "bare" if qualified == 0 else "mixed"


def near_miss(role: str, values: list[str]) -> str | None:
    """A fixed hint when a configured role differs from a claim value only by a
    domain prefix or letter case. Only the configured role is ever printed."""
    for v in values:
        if v.casefold() == role.casefold():
            return "a claim value equals it ignoring letter case (the match is exact)"
    for v in values:
        if "\\" in v and v.replace("\\", "") == role:
            return (
                "a claim value is it with a backslash: an unquoted backslash was removed "
                "by the shell; single-quote the value"
            )
        if v.rpartition("\\")[2] == role and "\\" in v:
            return "a claim value is it with a domain prefix (DOMAIN\\name)"
        if role.rpartition("\\")[2] == v and "\\" in role:
            return "a claim value is it without the domain prefix"
    return None


def analyse_role_lists(
    claims: dict[str, Any],
    *,
    roles_claim: str,
    allowed: list[str],
    admin: list[str],
    expect_refused: bool,
) -> tuple[list[str], list[str]]:
    """Role-list mode: the roles claim is the group claim; predict the outcome."""
    lines: list[str] = []
    failures: list[str] = []
    if roles_claim in claims:
        value = claims[roles_claim]
        kind, _, _ = role_values(value)
        roles = openwebui_roles(value)
        if isinstance(value, str):
            form = "string split on ','" if "," in value else "single string"
        else:
            form = kind
        lines.append(
            f"roles claim '{roles_claim}': present, type={kind}, form={form}, count={len(roles)}"
        )
    else:
        roles = []
        lines.append(f"roles claim '{roles_claim}': absent")
    strings = [r for r in roles if isinstance(r, str)]
    near = False
    configured = allowed + [r for r in admin if r not in allowed]
    config_form = value_form(configured)
    lines.append(f"configured role form: {config_form}")
    form_mismatch = False
    if strings:
        claim_form = value_form(strings)
        lines.append(f"claim value form: {claim_form}")
        if {config_form, claim_form} == {"bare", "domain-qualified"}:
            form_mismatch = True
            lines.append(
                f"MISMATCH: the configured roles are {config_form} names but the claim's "
                f"values are {claim_form}; no configured role can match"
            )
    if config_form != "domain-qualified":
        lines.append(
            "WARN: a bare configured role also matches a same-named group from any other "
            "domain whose users sign in through this IdP; use DOMAIN\\group unless the "
            "forest has one domain and no trusts (confirm that with the identity team)"
        )
    for role in configured:
        if "\\" in role:
            continue
        domains = {
            v.rpartition("\\")[0] for v in strings if v.rpartition("\\")[2] == role and "\\" in v
        }
        if len(domains) > 1:
            lines.append(
                f"WARN: configured role '{role}' is a bare name and the claim holds a group "
                f"of that name from {len(domains)} domains"
            )
    for label, configured in (("allowed", allowed), ("admin", admin)):
        matched = [r for r in configured if r in roles]
        lines.append(
            f"configured {label} roles matched: " + (", ".join(matched) if matched else "(none)")
        )
    for role in allowed + [r for r in admin if r not in allowed]:
        if role not in roles:
            hint = near_miss(role, strings)
            if hint:
                lines.append(f"hint: configured role '{role}' is not in the claim; {hint}")
                near = True
    outcome = predict_outcome(roles, allowed, admin)
    lines.append("predicted Open WebUI v0.12.3 outcome: " + OUTCOME_TEXT[outcome])
    if form_mismatch:
        failures.append("the configured role form does not match the claim's value form")
    if outcome == "unchanged":
        failures.append("roles claim is absent or empty: Open WebUI would not recompute the role")
    if near:
        # The account is in a group the configuration means but does not name exactly.
        failures.append("a configured role differs from a claim value only by form (see hint)")
    elif expect_refused and outcome != "refused":
        failures.append("the account matches a configured role; expected a refusal")
    elif not expect_refused and outcome == "refused":
        failures.append("the account matches no configured role; Open WebUI would refuse it")
    return lines, failures


def id_token_size_lines(size: int) -> list[str]:
    lines = [f"ID token size: {size} bytes"]
    if size > ID_TOKEN_WARN_BYTES:
        lines.append(
            f"WARN: the ID token is above {ID_TOKEN_WARN_BYTES} bytes: sign-out sends it to "
            "the IdP in the URL (id_token_hint) and may pass the IdP web server's request "
            "limit (16384 bytes by default on http.sys); confirm sign-out on the test client"
        )
    return lines


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


def _jwt_part(token: str, index: int) -> dict[str, Any] | None:
    """One decoded JSON object segment of a JWT, or None when it is not one."""
    parts = token.strip().split(".")
    if len(parts) != 3 or not parts[index]:
        return None
    try:
        seg = parts[index] + "=" * (-len(parts[index]) % 4)
        doc = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")))
    except (ValueError, UnicodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _number(value: Any) -> bool:
    """A finite JSON number (json accepts NaN and Infinity)."""
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    return ok and math.isfinite(value)


def analyse_access_token(
    token: str | None,
    *,
    issuers: list[tuple[str, str]] | None,
    client_id: str | None,
    group_claim: str,
    team_group: str | None,
    gateway_audiences: list[str],
    refresh_issued: bool | None,
    now: float | None,
) -> tuple[list[str], str]:
    """Return (report lines, verdict code). Lines carry no value from the token.

    ``issuers`` lists (label, value) pairs the iss may equal; None skips the comparison and ``now`` None the expiry check
    (offline); ``refresh_issued`` None means the token response is unknown.
    """
    if not token:
        return ["access token: absent"], "absent"
    header, claims = _jwt_part(token, 0), _jwt_part(token, 1)
    size = len(token.strip().encode("utf-8"))
    if header is None or claims is None:
        return [f"access token: present, format=opaque, size={size} bytes"], "opaque"
    if size > GATEWAY_MAX_TOKEN_CHARS:
        return [f"access token: present, format=jwt, size={size} bytes"], "too_large"
    lines = [f"access token: present, format=jwt, size={size} bytes"]
    alg, kid = header.get("alg"), header.get("kid")
    alg_text = alg if isinstance(alg, str) and alg in KNOWN_ALGS else "other"
    has_kid = isinstance(kid, str) and bool(kid)
    lines.append(f"access token alg: {alg_text}, key id present: {'yes' if has_kid else 'no'}")
    lines.append("claim names: " + (", ".join(sorted(claims)) or "(none)"))

    aud = claims.get("aud")
    raw_auds = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    auds = [a for a in raw_auds if isinstance(a, str) and a]
    aud_ok = bool(auds) and len(auds) == len(raw_auds)  # PyJWT refuses a non-string member
    userinfo = USERINFO_AUDIENCE in auds
    lines.append(
        f"aud: count={len(auds)}, urn:microsoft:userinfo: {'yes' if userinfo else 'no'}, "
        + "the client ID: "
        + ("not checked" if client_id is None else "yes" if client_id in auds else "no")
    )
    matched_aud = [a for a in gateway_audiences if a in auds]
    if gateway_audiences:
        lines.append(
            "configured gateway audiences matched: " + (", ".join(matched_aud) or "(none)")
        )

    iss = claims.get("iss")
    iss_match: bool | None = None
    if not isinstance(iss, str) or not iss:
        lines.append("iss: absent")
    elif issuers is None:
        lines.append("iss: present (not compared offline without --expect-iss)")
    else:
        label = next((name for name, value in issuers if value == iss), None)
        iss_match = label is not None
        lines.append(
            f"iss: equals the {label}; set the gateway issuer to that value"
            if label
            else "iss: differs from " + " and ".join(name for name, _ in issuers)
        )
    sub_ok = isinstance(claims.get("sub"), str) and bool(claims.get("sub"))
    lines.append(f"sub: {'present' if sub_ok else 'absent'}")
    exp, iat = claims.get("exp"), claims.get("iat")
    iat_ok = "iat" not in claims or _number(iat)  # PyJWT refuses a non-numeric iat
    if _number(exp) and _number(iat):
        lifetime = int(exp - iat)
        lines.append(f"lifetime: {lifetime} s")
        if lifetime < MIN_LIFETIME_S:
            lines.append(
                f"WARN: the lifetime is under {MIN_LIFETIME_S} s; Open WebUI refreshes 300 s "
                "before expiry"
            )
    else:
        lines.append("lifetime: unknown (exp or iat absent)")
    if refresh_issued is None:
        lines.append("refresh token issued: not checked offline")
    else:
        lines.append(f"refresh token issued: {'yes' if refresh_issued else 'no'}")
        if not refresh_issued:
            lines.append(
                "WARN: no refresh token; Open WebUI drops the session near expiry and a "
                "tool call may go out with no token (#1088 item 3)"
            )

    raw = claims.get(group_claim)
    groups: list[str] = []
    if group_claim not in claims:
        lines.append(f"group claim '{group_claim}': absent")
    else:
        kind, count, groups = role_values(raw)
        if isinstance(raw, str):  # the gateway library takes a string as one group
            groups, count = [raw] if raw else [], 1 if raw else 0
        line = f"group claim '{group_claim}': present, type={kind}, count={count}"
        if groups:
            line += f", form={value_form(groups)}"
        lines.append(line)
    team_ok = team_group is None or team_group in groups
    if team_group is not None:
        lines.append(
            f"configured team group '{team_group}': " + ("present" if team_ok else "absent")
        )
        hint = None if team_ok else near_miss(team_group, groups)
        if hint:
            lines.append(f"hint: configured team group '{team_group}' is not in the claim; {hint}")

    if userinfo:
        verdict = "userinfo_audience"
    elif alg not in GATEWAY_ALGS or not has_kid:
        verdict = "unsupported_alg"
    elif not (aud_ok and sub_ok and isinstance(iss, str) and iss and _number(exp) and iat_ok):
        verdict = "claims_missing"
    elif iss_match is False:
        verdict = "issuer_mismatch"
    elif now is not None and exp <= now:
        verdict = "expired"
    elif not groups:
        verdict = "no_groups"
    elif not team_ok:
        verdict = "team_group_missing"
    elif gateway_audiences and not matched_aud:
        verdict = "gateway_audience_missing"
    elif not gateway_audiences:
        verdict = "audience_unchecked"
    else:
        verdict = "ok"
    return lines, verdict


def report_access(lines: list[str], verdict: str) -> None:
    """Print the access-token report; raise CheckError unless the verdict is ok."""
    print("note: the access token signature is NOT verified (diagnostic only)")
    for line in lines:
        print(line)
    print(f"access token verdict: {verdict}")
    if verdict != "ok":
        raise CheckError(EXIT_ACCESS, ACCESS_VERDICTS[verdict])


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
    if args.access_token:
        token = tok.get("access_token")
        refresh = tok.get("refresh_token")
        issuers = [
            (f"discovery {key}", disc[key])
            for key in ("access_token_issuer", "issuer")
            if isinstance(disc.get(key), str)
        ]
        lines, verdict = analyse_access_token(
            token if isinstance(token, str) else None,
            issuers=issuers,
            client_id=args.client_id,
            group_claim=args.group_claim,
            team_group=args.team_group,
            gateway_audiences=args.gateway_audience,
            refresh_issued=isinstance(refresh, str) and bool(refresh),
            now=time.time(),
        )
        report_access(lines, verdict)
        return {}
    id_token = tok.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise CheckError(EXIT_TOKEN, "token response carries no id_token")
    claims = decode_jwt_payload(id_token)
    bad = validate_claims(
        claims, issuer=disc["issuer"], client_id=args.client_id, nonce=nonce, now=time.time()
    )
    report_and_fail(claims, args, bad, offline=False, token_size=len(id_token.encode("utf-8")))
    return claims


def suggest_claim(wanted: str, names: list[str]) -> str | None:
    """The claim name in the token closest to ``wanted``, or None.

    Tries, in order: the same name in another letter case, a URI whose last path
    segment is the name, a name ending in it after '-', '_', '/' or ':' (an IdP
    form may emit its rule name, such as role-<name>), then difflib similarity.
    """
    low = wanted.lower()
    for n in names:
        if n.lower() == low:
            return n
    for n in names:
        if n.lower().rstrip("/").rpartition("/")[2] == low:
            return n
    for n in names:
        if any(n.lower().endswith(sep + low) for sep in ("-", "_", "/", ":")):
            return n
    close = difflib.get_close_matches(wanted, names, n=1, cutoff=0.6)
    return close[0] if close else None


CLAIM_KEYS = (
    ("email", "email_claim", "OPENWEBUI_OIDC_EMAIL_CLAIM"),
    ("username", "username_claim", "OPENWEBUI_OIDC_USERNAME_CLAIM"),
    ("group", "group_claim", "OPENWEBUI_OIDC_GROUP_CLAIM"),
    ("roles", "roles_claim", "OPENWEBUI_OIDC_ROLES_CLAIM"),
)


# Other names an IdP commonly uses for the same claim (AD FS: upn, unique_name).
CLAIM_ALTERNATIVES = {
    "email": ("upn", "mail", "email", "preferred_username"),
    "username": ("name", "display_name", "displayname", "unique_name"),
    "group": ("groups", "group", "tokengroups", "memberof"),
}


def claim_name_lines(claims: dict[str, Any], args: argparse.Namespace) -> tuple[list[str], dict]:
    """Report each configured claim name; for a missing one, the closest name.

    Returns (lines, found) where ``found`` maps each airgap.env key to the claim
    name to use, or None when nothing in the token fits. Names only, never values.
    """
    names = sorted(claims)
    lines: list[str] = []
    found: dict[str, str | None] = {}
    for label, attr, key in CLAIM_KEYS:
        wanted = getattr(args, attr)
        if wanted in claims:
            lines.append(f"{label} claim name '{wanted}': present")
            found[key] = wanted
            continue
        guess = suggest_claim(wanted, names)
        for alt in CLAIM_ALTERNATIVES.get(label, ()):
            guess = guess or suggest_claim(alt, names)
        if label == "roles" and args.role_lists and found.get("OPENWEBUI_OIDC_GROUP_CLAIM"):
            guess = found["OPENWEBUI_OIDC_GROUP_CLAIM"]  # role-list mode: roles = groups
        lines.append(
            f"{label} claim name '{wanted}': absent; closest claim name in the token: "
            + (f"'{guess}'" if guess else "(none)")
        )
        found[key] = guess
    return lines, found


def env_lines(found: dict, args: argparse.Namespace) -> list[str]:
    """airgap.env lines matching what was found, ready to paste after review.

    shlex.quote single-quotes any value with a backslash or other shell syntax,
    so sourcing the file keeps it verbatim.
    """
    out = ["airgap.env lines for what this token carries (review, then paste):"]
    for _label, _attr, key in CLAIM_KEYS:
        name = found.get(key)
        out.append(f"{key}={shlex.quote(name)}" if name else f"# {key}=  (no matching claim found)")
    if args.role_lists:
        allowed, admin = args.role_lists
        out.append(f"OPENWEBUI_OIDC_ALLOWED_ROLES={shlex.quote(','.join(allowed))}")
        out.append(f"OPENWEBUI_OIDC_ADMIN_ROLES={shlex.quote(','.join(admin))}")
    return out


def report_and_fail(
    claims: dict[str, Any],
    args: argparse.Namespace,
    std_failures: list[str],
    *,
    offline: bool,
    token_size: int,
) -> None:
    lines, failures = analyse(
        claims,
        roles_claim=args.roles_claim,
        email_claim=args.email_claim,
        username_claim=args.username_claim,
        baseline=args.baseline_role,
        expect_baseline_only=args.expect_baseline_only,
    )
    if args.role_lists:
        # Role-list mode replaces the baseline checks; keep the identity lines.
        allowed, admin = args.role_lists
        role_lines, failures_rl = analyse_role_lists(
            claims,
            roles_claim=args.roles_claim,
            allowed=allowed,
            admin=admin,
            expect_refused=args.expect_refused,
        )
        keep = [ln for ln in lines if not ln.startswith(("roles claim ", "baseline role "))]
        lines = keep[:1] + role_lines + keep[1:]
        failures = failures_rl + [f for f in failures if f.startswith(("email ", "username "))]
    lines += id_token_size_lines(token_size)
    name_lines, found = claim_name_lines(claims, args)
    lines += name_lines + env_lines(found, args)
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
        what = "access token" if args.access_token else "ID token"
        raise CheckError(EXIT_USAGE, f"{what} file is unreadable") from None
    if args.access_token:
        lines, verdict = analyse_access_token(
            token.strip() or None,
            issuers=[("--expect-iss value", args.expect_iss)] if args.expect_iss else None,
            client_id=args.client_id,
            group_claim=args.group_claim,
            team_group=args.team_group,
            gateway_audiences=args.gateway_audience,
            refresh_issued=None,
            now=None,
        )
        report_access(lines, verdict)
        return
    claims = decode_jwt_payload(token)
    bad = validate_claims(
        claims, issuer=args.expect_iss, client_id=args.client_id, nonce=None, now=None
    )
    report_and_fail(claims, args, bad, offline=True, token_size=len(token.strip().encode("utf-8")))


# --------------------------------------------------------------------- main


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or default


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Check the claims AD FS puts in the Open WebUI client's ID token. "
        "Prints claim names, a roles type/count, the ID token size, yes/no answers and "
        "(role-list mode) the configured roles that matched. "
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
        help="OPENWEBUI_OIDC_ROLES_CLAIM (ADFS_ROLES_CLAIM, default roles, as the chart)",
    )
    p.add_argument(
        "--group-claim",
        default=env("ADFS_GROUP_CLAIM", "groups"),
        help="OPENWEBUI_OIDC_GROUP_CLAIM (ADFS_GROUP_CLAIM, default groups, as the chart)",
    )
    p.add_argument(
        "--email-claim",
        default=env("ADFS_EMAIL_CLAIM", "email"),
        help="OPENWEBUI_OIDC_EMAIL_CLAIM (ADFS_EMAIL_CLAIM, default email, as the chart; "
        "AD FS usually upn)",
    )
    p.add_argument(
        "--username-claim",
        default=env("ADFS_USERNAME_CLAIM", "name"),
        help="OPENWEBUI_OIDC_USERNAME_CLAIM (ADFS_USERNAME_CLAIM, default name, as the chart)",
    )
    p.add_argument(
        "--baseline-role",
        default=env("ADFS_BASELINE_ROLE", "signed-in"),
        help="baseline role value, default signed-in; only yes/no is printed (ADFS_BASELINE_ROLE)",
    )
    p.add_argument(
        "--expect-baseline-only",
        action="store_true",
        help="baseline mode: the test account is in no role group: pass only if the "
        "baseline is the single role value",
    )
    p.add_argument(
        "--allowed-roles",
        default=env("ADFS_ALLOWED_ROLES"),
        help="role-list mode: OPENWEBUI_OIDC_ALLOWED_ROLES, comma-separated "
        "(ADFS_ALLOWED_ROLES); with --admin-roles",
    )
    p.add_argument(
        "--admin-roles",
        default=env("ADFS_ADMIN_ROLES"),
        help="role-list mode: OPENWEBUI_OIDC_ADMIN_ROLES, comma-separated (ADFS_ADMIN_ROLES)",
    )
    p.add_argument(
        "--expect-refused",
        action="store_true",
        help="role-list mode: the test account is in no docs group: pass only if the "
        "roles claim is present and Open WebUI would refuse the sign-in",
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
    p.add_argument(
        "--access-token",
        action="store_true",
        help="report on the access token Open WebUI forwards to the team-tool gateways "
        "instead of the ID token (with --decode-only: the file holds an access token)",
    )
    p.add_argument(
        "--team-group",
        help="access-token mode: the team group the gateways allow, e.g. 'DOMAIN\\team' "
        "(ADFS_TEAM_GROUP)",
    )
    p.add_argument(
        "--gateway-audience",
        action="append",
        default=[],
        help="access-token mode: an audience a gateway accepts (repeatable); the verdict "
        "needs one of them in aud",
    )
    return p.parse_args(argv)


def resolve_mode(args: argparse.Namespace) -> None:
    """Set ``args.role_lists`` to (allowed, admin) in role-list mode, else None."""
    args.role_lists = None
    if args.access_token:
        if args.team_group is None:
            args.team_group = env("ADFS_TEAM_GROUP")
        id_flags = (args.allowed_roles, args.admin_roles, args.expect_refused)
        if any(id_flags) or args.expect_baseline_only:
            raise CheckError(
                EXIT_USAGE, "--access-token does not take the ID token role or expect flags"
            )
        if args.team_group is not None and not args.team_group:
            raise CheckError(EXIT_USAGE, "--team-group must not be empty")
        if any(not a for a in args.gateway_audience):
            raise CheckError(EXIT_USAGE, "--gateway-audience must not be empty")
        return
    if args.team_group or args.gateway_audience:
        raise CheckError(EXIT_USAGE, "--team-group and --gateway-audience need --access-token")
    if args.allowed_roles is None and args.admin_roles is None:
        if args.expect_refused:
            raise CheckError(EXIT_USAGE, "--expect-refused needs --allowed-roles and --admin-roles")
        return
    if not args.allowed_roles or not args.admin_roles:
        raise CheckError(EXIT_USAGE, "role-list mode needs both --allowed-roles and --admin-roles")
    if args.expect_baseline_only:
        raise CheckError(
            EXIT_USAGE, "--expect-baseline-only is for baseline mode; use --expect-refused"
        )
    args.role_lists = (parse_role_list(args.allowed_roles), parse_role_list(args.admin_roles))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        resolve_mode(args)
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
    if args.access_token:
        print("RESULT: ok - the access token meets every gateway expectation")
    else:
        print("RESULT: ok - the ID token meets every expectation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
