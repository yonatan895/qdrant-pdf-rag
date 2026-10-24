"""Team-tool caller verification (issue #1076, tracker #1075).

Every team-tool MCP server (Splunk, GitLab, mail) verifies its caller itself:
an IdP-issued OIDC JWT, checked against the IdP's JWKS for signature, issuer,
audience, expiry and not-before, then group membership from a configured
claim. Headers set by Open WebUI never authorize on their own. An optional
RFC 8693 token exchange turns the token Open WebUI forwards into one
addressed to this gateway (#1088). Contract: docs/team-tools.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
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
_MAX_EXCHANGE_BYTES: Final = 64 * 1024
_MAX_TOKEN_CHARS: Final = 32 * 1024
_SCOPE_KEY: Final = "tool_auth.principal"
_OPENWEBUI_PREFIX: Final = b"x-openwebui-"
ACCESS_TOKEN_TYPE: Final = "urn:ietf:params:oauth:token-type:access_token"
_EXCHANGE_GRANT: Final = "urn:ietf:params:oauth:grant-type:token-exchange"

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


def _str_set(name: str, value: object) -> frozenset[str]:
    """A set of non-empty strings. A bare str is refused: frozenset("team")
    would silently become {"t", "e", "a", "m"}."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ValueError(f"{name} must be a set of strings, not a single string")  # noqa: TRY004 - config errors are ValueError
    items = frozenset(value)
    if any(not isinstance(item, str) or not item for item in items):
        raise ValueError(f"{name} must hold non-empty strings")
    return items


def _seconds(name: str, value: object, *, low: float, high: float = math.inf, low_open: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < low or (low_open and value == low) or value > high:
        raise ValueError(f"{name} is out of range")


def _https(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ValueError(f"{name} must be an https URL")


@dataclass(frozen=True)
class GatewayAuthConfig:
    """Per-gateway verification policy. Exactly one JWKS source: an https
    `jwks_url` (the only URL ever fetched) or a pinned `jwks_file`. An empty
    `allowed_groups` (the default) denies every caller. `accepted_audiences`
    adds other names for this gateway to `audience`; an `aud` list naming any
    of them is accepted unless `require_single_audience`."""

    gateway: str
    issuer: str
    audience: str
    jwks_url: str | None = None
    jwks_file: Path | None = None
    group_claim: str = "groups"
    allowed_groups: frozenset[str] = field(default_factory=frozenset)
    accepted_audiences: frozenset[str] | None = None
    require_single_audience: bool = False
    algorithms: tuple[str, ...] = ("RS256",)
    leeway_s: float = 30.0
    jwks_ttl_s: float = 300.0
    jwks_min_refetch_s: float = 30.0
    jwks_max_stale_s: float = 3600
    jwks_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        for name in ("gateway", "issuer", "audience", "group_claim"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if (self.jwks_url is None) == (self.jwks_file is None):
            raise ValueError("configure exactly one of jwks_url or jwks_file")
        if self.jwks_url is not None:
            _https("jwks_url", self.jwks_url)
        algorithms = tuple(self.algorithms)
        if not algorithms or any(alg not in _KEY_TYPES for alg in algorithms):
            raise ValueError(f"algorithms must be a non-empty subset of {sorted(_KEY_TYPES)}")
        object.__setattr__(self, "algorithms", algorithms)
        object.__setattr__(self, "allowed_groups", _str_set("allowed_groups", self.allowed_groups))
        if self.accepted_audiences is not None:
            accepted = _str_set("accepted_audiences", self.accepted_audiences)
            if not accepted:
                raise ValueError("accepted_audiences must be non-empty when set")
            object.__setattr__(self, "accepted_audiences", accepted)
        _seconds("leeway_s", self.leeway_s, low=0, high=300)
        _seconds("jwks_max_stale_s", self.jwks_max_stale_s, low=0)
        for name in ("jwks_ttl_s", "jwks_min_refetch_s", "jwks_timeout_s"):
            _seconds(name, getattr(self, name), low=0, low_open=True)

    @property
    def audiences(self) -> frozenset[str]:
        """Every audience value that names this gateway."""
        return frozenset({self.audience}) | (self.accepted_audiences or frozenset())


async def _read_capped(response: httpx2.Response, cap: int) -> bytes | None:
    """The streamed body, or None as soon as it exceeds `cap` bytes."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > cap:
            return None
    return bytes(body)


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

    URL mode fetches only `config.jwks_url`: redirects are never followed
    (per request, whatever the client's default), and a response for any
    other URL is refused; token-supplied `jku`/`x5u` are never read. The set
    is kept for `jwks_ttl_s`, refetched early on an unknown kid at most once
    per `jwks_min_refetch_s`, and always replaced by a successful fetch. A
    failed fetch keeps the last-good set until it is `jwks_max_stale_s` older
    than its successful fetch, then fails closed (0: at once). File mode loads
    the pinned JWKS once at construction and never touches the network.
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
        self._good_at: float | None = None
        self._lock = asyncio.Lock()
        if config.jwks_file is not None:
            self._keys = _parse_jwks(json.loads(config.jwks_file.read_bytes()))

    async def get_key(self, kid: str, alg: str) -> Any:
        """The public key for `kid`, bound to `alg`; AuthError otherwise."""
        if self._config.jwks_file is None:
            await self._refresh(kid)
            if not self._usable():
                raise AuthError(UNAUTHENTICATED)
        jwk = self._keys.get(kid)
        if jwk is None:
            raise AuthError(UNAUTHENTICATED)
        return _bind_key(jwk, alg)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _usable(self) -> bool:
        if self._good_at is None:
            return False
        limit = max(self._config.jwks_ttl_s, self._config.jwks_max_stale_s)
        return self._clock() - self._good_at <= limit

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
            # the outcome; a failure is retried at the refetch floor.
            fetched = await self._fetch()
            now = self._clock()
            self._fetched_at = now
            if fetched is not None:
                self._keys, self._good_at = fetched, now
                return
            age = None if self._good_at is None else now - self._good_at
            if age is not None and age <= self._config.jwks_max_stale_s:
                # Revocation is learned only from a successful fetch, so
                # dropping the set here would revoke nothing sooner.
                log.warning(json.dumps({
                    "action": "tool_auth_jwks_stale", "gateway": self._config.gateway, "age_s": int(age),
                }))
                return
            self._keys = {}

    async def _fetch(self) -> dict[str, dict[str, Any]] | None:
        """A parsed key set, or None for any failure."""
        if self._client is None:
            self._client = httpx2.AsyncClient(timeout=self._config.jwks_timeout_s, follow_redirects=False)
        url = self._config.jwks_url
        assert url is not None
        try:
            async with self._client.stream(
                "GET", url, headers={"Accept": "application/json"}, follow_redirects=False
            ) as response:
                if response.status_code != 200 or response.url != httpx2.URL(url):
                    return None
                body = await _read_capped(response, _MAX_JWKS_BYTES)
            if body is None:
                return None
            return _parse_jwks(json.loads(body))
        except (httpx2.HTTPError, ValueError, TypeError):
            return None


@dataclass(frozen=True)
class TokenExchangeConfig:
    """RFC 8693 token exchange for one gateway (#1088 path a). The client
    secret is read from the environment variable `client_secret_env`
    (a Secret reference), never stored in configuration."""

    token_endpoint: str
    client_id: str
    client_secret_env: str
    audience: str
    subject_token_type: str = ACCESS_TOKEN_TYPE
    requested_token_type: str = ACCESS_TOKEN_TYPE
    timeout_s: float = 5.0

    def __post_init__(self) -> None:
        _https("token_endpoint", self.token_endpoint)
        for name in ("client_id", "client_secret_env", "audience", "subject_token_type", "requested_token_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        _seconds("timeout_s", self.timeout_s, low=0, low_open=True)


class TokenExchanger:
    """Exchanges the token Open WebUI forwards for one addressed to this
    gateway. The result is still verified by `verify_token`; any failure
    (IdP error, redirect, oversized or malformed reply) is `unauthenticated`."""

    def __init__(
        self,
        config: TokenExchangeConfig,
        *,
        client: httpx2.AsyncClient | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        secret = (os.environ if environ is None else environ).get(config.client_secret_env, "")
        if not secret:
            raise ValueError(f"{config.client_secret_env} is not set")
        self._config = config
        self._secret = secret
        self._owns_client = client is None
        self._client = client or httpx2.AsyncClient(timeout=config.timeout_s, follow_redirects=False)

    def __repr__(self) -> str:
        return f"TokenExchanger(client_id={self._config.client_id!r})"

    async def exchange(self, subject_token: str) -> str:
        config = self._config
        form = {
            "grant_type": _EXCHANGE_GRANT,
            "subject_token": subject_token,
            "subject_token_type": config.subject_token_type,
            "requested_token_type": config.requested_token_type,
            "audience": config.audience,
            "client_id": config.client_id,
            "client_secret": self._secret,
        }
        try:
            async with self._client.stream(
                "POST", config.token_endpoint, data=form,
                headers={"Accept": "application/json"}, follow_redirects=False,
            ) as response:
                if response.status_code != 200 or response.url != httpx2.URL(config.token_endpoint):
                    raise AuthError(UNAUTHENTICATED)
                body = await _read_capped(response, _MAX_EXCHANGE_BYTES)
            if body is None:
                raise AuthError(UNAUTHENTICATED)
            reply = json.loads(body)
        except (httpx2.HTTPError, ValueError, TypeError):
            raise AuthError(UNAUTHENTICATED) from None
        if not isinstance(reply, dict):
            raise AuthError(UNAUTHENTICATED)
        token = reply.get("access_token")
        issued = reply.get("issued_token_type", config.requested_token_type)
        if not isinstance(token, str) or not token or issued != config.requested_token_type:
            raise AuthError(UNAUTHENTICATED)
        return token

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


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
    `token_expired` (signature valid, `exp` passed; checked before iss/aud), or
    `forbidden` (verified, but in no allowed group).
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
            audience=sorted(config.audiences),
            issuer=config.issuer,
            leeway=config.leeway_s,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError(TOKEN_EXPIRED) from None
    except jwt.PyJWTError:
        raise AuthError(UNAUTHENTICATED) from None
    aud = claims.get("aud")
    if config.require_single_audience and isinstance(aud, list) and len(aud) != 1:
        raise AuthError(UNAUTHENTICATED)
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
    single `Authorization: Bearer` header (exchanged first when an
    `exchanger` is given), then hand the app a scope carrying the Principal
    (read it with `principal_from_scope`). Nothing else in the request is
    consulted, and `X-OpenWebUI-*` headers are removed before the app unless
    `strip_openwebui_headers=False`. `exempt_paths` are exact paths (e.g. a
    kubelet health probe) that skip verification and receive no Principal."""

    def __init__(
        self,
        app: ASGIApp,
        config: GatewayAuthConfig,
        jwks: JwksCache,
        *,
        exempt_paths: frozenset[str] = frozenset(),
        exchanger: TokenExchanger | None = None,
        strip_openwebui_headers: bool = True,
    ) -> None:
        self.app = app
        self.config = config
        self.jwks = jwks
        self.exempt_paths = _str_set("exempt_paths", exempt_paths)
        self.exchanger = exchanger
        self.strip_openwebui_headers = strip_openwebui_headers

    def _forward(self, scope: MutableMapping[str, Any], **extra: Any) -> MutableMapping[str, Any]:
        headers = scope.get("headers", [])
        if self.strip_openwebui_headers:
            headers = [(n, v) for n, v in headers if not n.lower().startswith(_OPENWEBUI_PREFIX)]
        return {**scope, "headers": headers, **extra}

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self.app(scope, receive, send)
            return
        if kind == "http" and scope.get("path") in self.exempt_paths:
            await self.app(self._forward(scope), receive, send)
            return
        token = _bearer(scope.get("headers", ()))
        try:
            if token is None:
                raise AuthError(UNAUTHENTICATED)
            if self.exchanger is not None:
                token = await self.exchanger.exchange(token)
            principal = await verify_token(token, self.config, self.jwks)
        except AuthError as exc:
            _log_decision(self.config.gateway, exc.sub, exc.code)
            await _refuse(kind, exc, send, presented=token is not None)
            return
        _log_decision(self.config.gateway, principal.sub, "allow")
        await self.app(self._forward(scope, **{_SCOPE_KEY: principal}), receive, send)


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
