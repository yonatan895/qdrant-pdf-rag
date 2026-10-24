"""Team-tool caller verification (issue #1076, tracker #1075).

Every team-tool MCP server (Splunk, GitLab, mail) verifies its caller itself:
an IdP-issued OIDC JWT, checked against the IdP's JWKS for signature, issuer,
audience, expiry and not-before, then group membership from a configured
claim. Headers set by Open WebUI never authorize on their own. Contract:
docs/tool-auth.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import httpx2
import jwt

AuthErrorCode = Literal["unauthenticated", "forbidden", "token_expired"]

UNAUTHENTICATED: Final = "unauthenticated"
FORBIDDEN: Final = "forbidden"
TOKEN_EXPIRED: Final = "token_expired"

_ERRORS: Final[dict[str, tuple[int, str]]] = {
    UNAUTHENTICATED: (401, "authentication is required"),
    TOKEN_EXPIRED: (401, "the token has expired"),
    FORBIDDEN: (403, "the caller is not authorized for this tool"),
}

# Asymmetric signature algorithms this library can verify, with the JWK key
# type (and curve) each one requires. Symmetric (HS*) and `none` are absent
# on purpose: a gateway only ever holds the IdP's public keys.
_KEY_TYPES: Final[dict[str, tuple[str, str | None]]] = {
    "RS256": ("RSA", None),
    "ES256": ("EC", "P-256"),
}
_MIN_RSA_BITS: Final = 2048
_MAX_JWKS_BYTES: Final = 256 * 1024
_MAX_TOKEN_CHARS: Final = 32 * 1024
_SCOPE_KEY: Final = "tool_auth.principal"

ASGIApp = Callable[
    [MutableMapping[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]],
    Awaitable[None],
]

log = logging.getLogger("tool_auth")


class AuthError(Exception):
    """A refused call. `code` and `message` are fixed client-facing values;
    `sub` is set only once the token's signature and claims verified."""

    def __init__(self, code: AuthErrorCode, *, sub: str | None = None) -> None:
        status, message = _ERRORS[code]
        super().__init__(code)
        self.code: AuthErrorCode = code
        self.status = status
        self.message = message
        self.sub = sub


@dataclass(frozen=True)
class Principal:
    """A verified IdP user: `sub` and every string group the token carried."""

    sub: str
    groups: frozenset[str]


@dataclass(frozen=True)
class GatewayAuthConfig:
    """Per-gateway verification policy. Exactly one JWKS source: an https
    `jwks_url` (the only URL ever fetched) or a pinned `jwks_file`. An empty
    `allowed_groups` (the default) denies every caller."""

    gateway: str
    issuer: str
    audience: str
    jwks_url: str | None = None
    jwks_file: Path | None = None
    group_claim: str = "groups"
    allowed_groups: frozenset[str] = field(default_factory=frozenset)
    algorithms: tuple[str, ...] = ("RS256",)
    leeway_s: float = 30.0
    jwks_ttl_s: float = 300.0
    jwks_min_refetch_s: float = 30.0
    jwks_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        for name in ("gateway", "issuer", "audience", "group_claim"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if (self.jwks_url is None) == (self.jwks_file is None):
            raise ValueError("configure exactly one of jwks_url or jwks_file")
        if self.jwks_url is not None and not self.jwks_url.startswith("https://"):
            raise ValueError("jwks_url must be an https URL")
        algorithms = tuple(self.algorithms)
        if not algorithms or any(alg not in _KEY_TYPES for alg in algorithms):
            raise ValueError(f"algorithms must be a non-empty subset of {sorted(_KEY_TYPES)}")
        groups = frozenset(self.allowed_groups)
        if any(not isinstance(g, str) or not g for g in groups):
            raise ValueError("allowed_groups must hold non-empty strings")
        if not 0 <= self.leeway_s <= 300:
            raise ValueError("leeway_s must be within 0..300 seconds")
        for name in ("jwks_ttl_s", "jwks_min_refetch_s", "jwks_timeout_s"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")
        object.__setattr__(self, "algorithms", algorithms)
        object.__setattr__(self, "allowed_groups", groups)


def _parse_jwks(document: object) -> dict[str, dict[str, Any]]:
    """kid -> JWK. Keys without a string kid are unusable; a kid listed twice
    is ambiguous and dropped (fail closed) rather than first-wins."""
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise TypeError("JWKS document must be an object with a keys list")
    keys: dict[str, dict[str, Any]] = {}
    duplicated: set[str] = set()
    for jwk in document["keys"]:
        if not isinstance(jwk, dict):
            continue
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        if kid in keys:
            duplicated.add(kid)
        keys[kid] = jwk
    for kid in duplicated:
        del keys[kid]
    return keys


def _bind_key(jwk: dict[str, Any], alg: str) -> Any:
    """The verification key for `alg`, or AuthError when the JWK's type,
    curve, declared alg or use disagree with it (alg-confusion guard)."""
    kty, crv = _KEY_TYPES[alg]
    if jwk.get("kty") != kty or (crv is not None and jwk.get("crv") != crv):
        raise AuthError(UNAUTHENTICATED)
    if "alg" in jwk and jwk["alg"] != alg:
        raise AuthError(UNAUTHENTICATED)
    if "use" in jwk and jwk["use"] != "sig":
        raise AuthError(UNAUTHENTICATED)
    ops = jwk.get("key_ops")
    if ops is not None and (not isinstance(ops, list) or "verify" not in ops):
        raise AuthError(UNAUTHENTICATED)
    try:
        key = jwt.PyJWK(jwk, algorithm=alg).key
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise AuthError(UNAUTHENTICATED) from None
    if kty == "RSA" and getattr(key, "key_size", 0) < _MIN_RSA_BITS:
        raise AuthError(UNAUTHENTICATED)
    return key


class JwksCache:
    """The IdP signing keys for one gateway.

    URL mode fetches only `config.jwks_url` (no redirects, no token-supplied
    `jku`/`x5u`), keeps the key set for `jwks_ttl_s`, and refetches early on
    an unknown kid at most once per `jwks_min_refetch_s` (key rotation without
    a refetch storm). A failed fetch fails closed. File mode loads the pinned
    JWKS once at construction and never touches the network.
    """

    def __init__(
        self,
        config: GatewayAuthConfig,
        *,
        client: httpx2.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._clock = clock
        self._owns_client = client is None and config.jwks_url is not None
        self._client = client
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()
        if config.jwks_file is not None:
            self._keys = _parse_jwks(json.loads(config.jwks_file.read_bytes()))

    async def get_key(self, kid: str, alg: str) -> Any:
        """The public key for `kid`, bound to `alg`; AuthError otherwise."""
        if self._config.jwks_file is None:
            await self._refresh(kid)
        jwk = self._keys.get(kid)
        if jwk is None:
            raise AuthError(UNAUTHENTICATED)
        return _bind_key(jwk, alg)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _due(self, kid: str) -> bool:
        if self._fetched_at is None:
            return True
        age = self._clock() - self._fetched_at
        if age >= self._config.jwks_ttl_s:
            return True
        return kid not in self._keys and age >= self._config.jwks_min_refetch_s

    async def _refresh(self, kid: str) -> None:
        if not self._due(kid):
            return
        async with self._lock:
            if not self._due(kid):
                return
            # Concurrent callers wait on the lock and re-check, so they see
            # the new set. A failed fetch leaves no keys (fail closed) and is
            # retried at the refetch floor, not on every request.
            self._keys = await self._fetch()
            self._fetched_at = self._clock()

    async def _fetch(self) -> dict[str, dict[str, Any]]:
        if self._client is None:
            self._client = httpx2.AsyncClient(timeout=self._config.jwks_timeout_s, follow_redirects=False)
        url = self._config.jwks_url
        assert url is not None
        try:
            response = await self._client.get(url, headers={"Accept": "application/json"})
            if response.status_code != 200 or len(response.content) > _MAX_JWKS_BYTES:
                return {}
            return _parse_jwks(response.json())
        except (httpx2.HTTPError, ValueError, TypeError):
            return {}


def _groups(claims: dict[str, Any], claim: str) -> frozenset[str]:
    raw = claims.get(claim)
    if isinstance(raw, str):
        return frozenset({raw}) if raw else frozenset()
    if isinstance(raw, list):
        return frozenset(g for g in raw if isinstance(g, str) and g)
    return frozenset()


async def verify_token(token: str, config: GatewayAuthConfig, jwks: JwksCache) -> Principal:
    """Verify an IdP-issued JWT and the caller's group membership.

    Raises AuthError: `unauthenticated` (malformed, unknown key, bad signature,
    disallowed alg, wrong issuer/audience, not yet valid, missing exp/sub),
    `token_expired`, or `forbidden` (verified, but in no allowed group).
    """
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_CHARS:
        raise AuthError(UNAUTHENTICATED)
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise AuthError(UNAUTHENTICATED) from None
    alg, kid = header.get("alg"), header.get("kid")
    if not isinstance(alg, str) or alg not in config.algorithms:
        raise AuthError(UNAUTHENTICATED)
    if not isinstance(kid, str) or not kid:
        raise AuthError(UNAUTHENTICATED)
    key = await jwks.get_key(kid, alg)
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=[alg],
            audience=config.audience,
            issuer=config.issuer,
            leeway=config.leeway_s,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError(TOKEN_EXPIRED) from None
    except jwt.PyJWTError:
        raise AuthError(UNAUTHENTICATED) from None
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise AuthError(UNAUTHENTICATED)
    groups = _groups(claims, config.group_claim)
    if not groups & config.allowed_groups:
        raise AuthError(FORBIDDEN, sub=sub)
    return Principal(sub=sub, groups=groups)


def principal_from_scope(scope: MutableMapping[str, Any]) -> Principal:
    """The Principal GatewayAuthMiddleware verified for this request. Raises
    AuthError when absent, so an app mounted without the middleware (or on an
    exempt path) can never act for an anonymous caller."""
    principal = scope.get(_SCOPE_KEY)
    if not isinstance(principal, Principal):
        raise AuthError(UNAUTHENTICATED)
    return principal


def _bearer(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    values = [value for name, value in headers if name.lower() == b"authorization"]
    if len(values) != 1:
        return None
    scheme, _, token = values[0].decode("latin-1").strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def _log_decision(gateway: str, sub: str | None, decision: str) -> None:
    level = logging.INFO if decision == "allow" else logging.WARNING
    log.log(level, json.dumps({"action": "tool_auth", "gateway": gateway, "sub": sub, "decision": decision}))


class GatewayAuthMiddleware:
    """ASGI middleware: authenticate every http/websocket request from its
    single `Authorization: Bearer` header, then hand the app a scope carrying
    the Principal (read it with `principal_from_scope`). Nothing else in the
    request (Open WebUI's X-OpenWebUI-* headers included) is consulted.
    `exempt_paths` are exact paths (e.g. a kubelet health probe) that skip
    verification and receive no Principal."""

    def __init__(
        self,
        app: ASGIApp,
        config: GatewayAuthConfig,
        jwks: JwksCache,
        *,
        exempt_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.app = app
        self.config = config
        self.jwks = jwks
        self.exempt_paths = frozenset(exempt_paths)

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        kind = scope["type"]
        if kind == "lifespan" or (kind == "http" and scope.get("path") in self.exempt_paths):
            await self.app(scope, receive, send)
            return
        token = _bearer(scope.get("headers", ()))
        try:
            if token is None:
                raise AuthError(UNAUTHENTICATED)
            principal = await verify_token(token, self.config, self.jwks)
        except AuthError as exc:
            _log_decision(self.config.gateway, exc.sub, exc.code)
            await _refuse(kind, exc, send, presented=token is not None)
            return
        _log_decision(self.config.gateway, principal.sub, "allow")
        await self.app({**scope, _SCOPE_KEY: principal}, receive, send)


async def _refuse(
    kind: str, exc: AuthError, send: Callable[[Any], Awaitable[None]], *, presented: bool
) -> None:
    if kind != "http":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"code": exc.code, "message": exc.message}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if exc.status == 401:
        challenge = b'Bearer error="invalid_token"' if presented else b"Bearer"
        headers.append((b"www-authenticate", challenge))
    else:
        headers.append((b"www-authenticate", b'Bearer error="insufficient_scope"'))
    await send({"type": "http.response.start", "status": exc.status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
