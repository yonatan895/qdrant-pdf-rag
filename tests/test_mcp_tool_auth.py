"""Team-tool auth (issue #1076): OIDC token verification, group authorization
and the ASGI middleware every team-tool MCP server mounts.

Each attack the issue lists has its own test. Keys are generated with the
locked `cryptography` library and tokens are signed with PyJWT, so the
expected verdicts come from real signatures, not from the code under test.
The JWKS endpoint is an `httpx2.MockTransport`: nothing reaches a network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from mainframe_rag.mcp.tool_auth import (
    AuthError,
    GatewayAuthConfig,
    GatewayAuthMiddleware,
    JwksCache,
    Principal,
    TokenExchangeConfig,
    TokenExchanger,
    principal_from_scope,
    verify_token,
)

ISSUER = "https://idp.example.test/adfs"
AUDIENCE = "urn:team-tools:splunk"
JWKS_URL = "https://idp.example.test/adfs/discovery/keys"
TOKEN_URL = "https://idp.example.test/adfs/oauth2/token"
FOREIGN = "https://attacker.test/keys"
GROUP = "team-mainframe"

_RSA_A = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_RSA_B = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_EC = ec.generate_private_key(ec.SECP256R1())


def _rsa_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, object]:
    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


def _ec_jwk(key: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, object]:
    jwk = ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "use": "sig", "alg": "ES256"}


def _config(**overrides: object) -> GatewayAuthConfig:
    fields: dict[str, object] = {
        "gateway": "splunk",
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "jwks_url": JWKS_URL,
        "allowed_groups": frozenset({GROUP}),
    }
    fields.update(overrides)
    return GatewayAuthConfig(**fields)  # type: ignore[arg-type]


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-1",
        "iat": now,
        "nbf": now - 5,
        "exp": now + 300,
        "groups": [GROUP, "everyone"],
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(key: object = _RSA_A, kid: str = "a", alg: str = "RS256", **claims: object) -> str:
    return jwt.encode(_claims(**claims), key, algorithm=alg, headers={"kid": kid})  # type: ignore[arg-type]


class _Idp:
    """JWKS endpoint double: serves `keys`, records each requested URL."""

    def __init__(self, *keys: dict[str, object]) -> None:
        self.keys = list(keys)
        self.requests: list[str] = []
        self.status = 200

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(str(request.url))
        # A non-200 still carries a well-formed key set: only the status may
        # be what refuses it.
        return httpx2.Response(self.status, json={"keys": self.keys})

    def cache(self, config: GatewayAuthConfig, clock: Callable[[], float] = time.monotonic) -> JwksCache:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))
        return JwksCache(config, client=client, clock=clock)


def _verify(token: str, config: GatewayAuthConfig | None = None, idp: _Idp | None = None) -> Principal:
    config = config or _config()
    idp = idp or _Idp(_rsa_jwk(_RSA_A, "a"))
    return asyncio.run(verify_token(token, config, idp.cache(config)))


def _code(token: str, config: GatewayAuthConfig | None = None, idp: _Idp | None = None) -> str:
    with pytest.raises(AuthError) as caught:
        _verify(token, config, idp)
    return caught.value.code


# --- the accepted path ----------------------------------------------------------


def test_valid_token_from_team_member_yields_principal():
    principal = _verify(_token())
    assert principal == Principal(sub="user-1", groups=frozenset({GROUP, "everyone"}))


def test_es256_is_accepted_only_when_configured():
    idp = _Idp(_ec_jwk(_EC, "e"))
    token = _token(_EC, kid="e", alg="ES256")
    assert _code(token, _config(), idp) == "unauthenticated"
    config = _config(algorithms=("RS256", "ES256"))
    assert _verify(token, config, idp).sub == "user-1"


# --- signature failure ------------------------------------------------------------


def test_token_signed_by_a_foreign_key_under_a_known_kid_is_refused():
    assert _code(_token(_RSA_B, kid="a")) == "unauthenticated"


def test_tampered_payload_is_refused():
    head, _, sig = _token(groups=["everyone"]).split(".")
    forged = jwt.utils.base64url_encode(json.dumps(_claims()).encode()).decode()
    assert _code(f"{head}.{forged}.{sig}") == "unauthenticated"


def test_malformed_token_is_refused_with_the_fixed_message():
    with pytest.raises(AuthError) as caught:
        _verify("not-a-jwt")
    assert (caught.value.code, caught.value.status) == ("unauthenticated", 401)
    assert caught.value.message == "authentication is required"
    assert "not-a-jwt" not in str(caught.value)


# --- issuer, audience, expiry and not-before -----------------------------------------


def test_wrong_audience_is_refused():
    assert _code(_token(aud="urn:team-tools:gitlab")) == "unauthenticated"


def test_missing_audience_is_refused():
    assert _code(_token(aud=None)) == "unauthenticated"


def test_audience_list_naming_this_gateway_is_accepted_unless_single_is_required():
    token = _token(aud=["open-webui", AUDIENCE])
    assert _verify(token).sub == "user-1"
    strict = _config(require_single_audience=True)
    assert _code(token, strict) == "unauthenticated"
    assert _verify(_token(aud=[AUDIENCE]), strict).sub == "user-1"
    assert _verify(_token(), strict).sub == "user-1"


def test_accepted_audiences_add_names_for_this_gateway():
    token = _token(aud="api://splunk-gateway")
    assert _code(token) == "unauthenticated"
    config = _config(accepted_audiences=frozenset({"api://splunk-gateway"}))
    assert _verify(token, config).sub == "user-1"
    assert _verify(_token(), config).sub == "user-1"
    assert _code(_token(aud="open-webui"), config) == "unauthenticated"


def test_set_valued_config_fields_refuse_a_bare_string_or_an_empty_override():
    with pytest.raises(ValueError):
        _config(allowed_groups="team")  # would otherwise be {"t", "e", "a", "m"}
    with pytest.raises(ValueError):
        _config(accepted_audiences="api://splunk")
    with pytest.raises(ValueError):
        _config(accepted_audiences=frozenset())
    config = _config()
    with pytest.raises(ValueError):
        GatewayAuthMiddleware(_App(), config, _Idp().cache(config), exempt_paths="/healthz")  # type: ignore[arg-type]


def test_wrong_issuer_is_refused():
    assert _code(_token(iss="https://idp.example.test/adfs-evil")) == "unauthenticated"


def test_issuer_prefix_is_not_a_match():
    assert _code(_token(iss="https://idp.example.test/ad")) == "unauthenticated"


def test_expired_token_has_its_own_code():
    now = int(time.time())
    with pytest.raises(AuthError) as caught:
        _verify(_token(iat=now - 900, nbf=now - 900, exp=now - 600))
    assert (caught.value.code, caught.value.status) == ("token_expired", 401)
    assert caught.value.message == "the token has expired"


def test_token_without_expiry_is_refused():
    assert _code(_token(exp=None)) == "unauthenticated"


def test_expired_token_with_a_bad_signature_is_unauthenticated_not_expired():
    now = int(time.time())
    assert _code(_token(_RSA_B, iat=now - 900, nbf=now - 900, exp=now - 600)) == "unauthenticated"


def test_not_yet_valid_token_is_refused():
    now = int(time.time())
    assert _code(_token(nbf=now + 600, exp=now + 900)) == "unauthenticated"


def test_token_without_subject_is_refused():
    assert _code(_token(sub=None)) == "unauthenticated"


# --- group authorization -------------------------------------------------------------


def test_member_of_another_group_only_is_forbidden():
    with pytest.raises(AuthError) as caught:
        _verify(_token(groups=["everyone"]))
    assert (caught.value.code, caught.value.status) == ("forbidden", 403)


def test_missing_group_claim_is_forbidden():
    assert _code(_token(groups=None)) == "forbidden"


def test_default_config_denies_every_member():
    config = GatewayAuthConfig(gateway="splunk", issuer=ISSUER, audience=AUDIENCE, jwks_url=JWKS_URL)
    assert config.allowed_groups == frozenset()
    assert _code(_token(), config) == "forbidden"


def test_group_claim_name_is_configurable():
    config = _config(group_claim="roles")
    assert _code(_token(), config) == "forbidden"
    assert _verify(_token(groups=None, roles=[GROUP]), config).sub == "user-1"


def test_single_string_group_claim_is_one_group():
    assert _verify(_token(groups=GROUP)).groups == frozenset({GROUP})
    assert _code(_token(groups=GROUP + "-x")) == "forbidden"


def test_non_string_group_values_never_match():
    assert _code(_token(groups=[{"name": GROUP}, 7])) == "forbidden"


# --- algorithm confusion ---------------------------------------------------------------


def test_alg_none_is_refused():
    token = jwt.encode(_claims(), None, algorithm="none", headers={"kid": "a"})
    assert _code(token) == "unauthenticated"


def test_hmac_signed_with_the_public_key_is_refused():
    pem = _RSA_A.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    head = jwt.utils.base64url_encode(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "a"}).encode())
    body = jwt.utils.base64url_encode(json.dumps(_claims()).encode())
    signing_input = head + b"." + body
    import hashlib
    import hmac

    sig = jwt.utils.base64url_encode(hmac.new(pem, signing_input, hashlib.sha256).digest())
    token = (signing_input + b"." + sig).decode()
    assert _code(token, _config(algorithms=("RS256", "ES256"))) == "unauthenticated"


def test_header_alg_must_match_the_key_type():
    # An ES256 token whose kid names an RSA key: the key and alg disagree.
    idp = _Idp(_rsa_jwk(_RSA_A, "a"))
    token = _token(_EC, kid="a", alg="ES256")
    assert _code(token, _config(algorithms=("RS256", "ES256")), idp) == "unauthenticated"


def test_jwk_alg_must_match_the_header_alg():
    jwk = {**_rsa_jwk(_RSA_A, "a"), "alg": "RS512"}
    assert _code(_token(), idp=_Idp(jwk)) == "unauthenticated"


def test_encryption_keys_are_not_signature_keys():
    jwk = {**_rsa_jwk(_RSA_A, "a"), "use": "enc"}
    assert _code(_token(), idp=_Idp(jwk)) == "unauthenticated"


def test_a_kid_listed_twice_is_ambiguous_and_refused():
    idp = _Idp(_rsa_jwk(_RSA_A, "a"), _rsa_jwk(_RSA_B, "a"))
    assert _code(_token(_RSA_A, kid="a"), idp=idp) == "unauthenticated"
    assert _code(_token(_RSA_B, kid="a"), idp=idp) == "unauthenticated"


def test_config_refuses_symmetric_or_none_algorithms():
    for algorithms in (("HS256",), ("none",), ("RS256", "HS512"), ()):
        with pytest.raises(ValueError):
            _config(algorithms=algorithms)


# --- JWKS: rotation, offline, static file --------------------------------------------------


def test_rotation_fetches_new_kid_and_drops_the_retired_key():
    now = [0.0]
    idp = _Idp(_rsa_jwk(_RSA_A, "a"))
    config = _config()
    cache = idp.cache(config, clock=lambda: now[0])

    async def run() -> None:
        assert (await verify_token(_token(), config, cache)).sub == "user-1"
        idp.keys = [_rsa_jwk(_RSA_B, "b")]
        now[0] += 60.0  # past the unknown-kid refetch floor, inside the TTL
        assert (await verify_token(_token(_RSA_B, kid="b"), config, cache)).sub == "user-1"
        with pytest.raises(AuthError) as caught:
            await verify_token(_token(_RSA_A, kid="a"), config, cache)
        assert caught.value.code == "unauthenticated"

    asyncio.run(run())
    assert idp.requests == [JWKS_URL, JWKS_URL]


def test_unknown_kid_refetch_is_rate_limited():
    now = [0.0]
    idp = _Idp(_rsa_jwk(_RSA_A, "a"))
    config = _config()
    cache = idp.cache(config, clock=lambda: now[0])

    async def run() -> None:
        await verify_token(_token(), config, cache)
        for n in range(5):
            with pytest.raises(AuthError):
                await verify_token(_token(_RSA_B, kid=f"x{n}"), config, cache)

    asyncio.run(run())
    assert len(idp.requests) == 1


def test_concurrent_cold_requests_share_one_fetch_and_all_verify():
    idp = _Idp(_rsa_jwk(_RSA_A, "a"))

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0.01)
        return idp.handler(request)

    config = _config()
    cache = JwksCache(config, client=httpx2.AsyncClient(transport=httpx2.MockTransport(slow)))

    async def run() -> list[Principal]:
        return await asyncio.gather(*(verify_token(_token(), config, cache) for _ in range(5)))

    assert [p.sub for p in asyncio.run(run())] == ["user-1"] * 5
    assert idp.requests == [JWKS_URL]


def test_cached_keys_expire_after_the_ttl():
    now = [0.0]
    idp = _Idp(_rsa_jwk(_RSA_A, "a"))
    config = _config(jwks_ttl_s=300.0)
    cache = idp.cache(config, clock=lambda: now[0])

    async def run() -> None:
        await verify_token(_token(), config, cache)
        now[0] += 100.0
        await verify_token(_token(), config, cache)
        idp.keys = [_rsa_jwk(_RSA_B, "b")]
        now[0] += 250.0
        with pytest.raises(AuthError):
            await verify_token(_token(), config, cache)

    asyncio.run(run())
    assert len(idp.requests) == 2


def test_jwks_fetch_failure_fails_closed():
    idp = _Idp(_rsa_jwk(_RSA_A, "a"))
    idp.status = 503
    assert _code(_token(), idp=idp) == "unauthenticated"


def _stale_run(max_stale_s: int) -> list[str]:
    """Good fetch at t=0, IdP down from then on; verdicts at t=350 (past the
    TTL), t=640, t=700, then the IdP recovers with a rotated key set at t=750."""
    now = [0.0]
    idp = _Idp(_rsa_jwk(_RSA_A, "a"))
    config = _config(jwks_max_stale_s=max_stale_s)
    cache = idp.cache(config, clock=lambda: now[0])
    verdicts: list[str] = []

    async def attempt(token: str) -> None:
        try:
            await verify_token(token, config, cache)
            verdicts.append("allow")
        except AuthError as exc:
            verdicts.append(exc.code)

    async def run() -> None:
        await attempt(_token())
        idp.status = 503
        now[0] = 350.0
        await attempt(_token(_RSA_B, kid="random"))  # unknown kid during the outage
        await attempt(_token())
        now[0] = 640.0  # inside the TTL of the failed refresh, past max-stale
        await attempt(_token())
        now[0] = 700.0
        await attempt(_token())
        idp.status, idp.keys = 200, [_rsa_jwk(_RSA_B, "b")]
        now[0] = 750.0
        await attempt(_token(_RSA_B, kid="b"))
        await attempt(_token())

    asyncio.run(run())
    return verdicts


def test_failed_refresh_fails_closed_by_default():
    assert _stale_run(0) == [
        "allow", "unauthenticated", "unauthenticated", "unauthenticated", "unauthenticated", "allow", "unauthenticated",
    ]


def test_max_stale_keeps_last_good_keys_through_a_failed_refresh_only():
    assert _stale_run(600) == [
        "allow", "unauthenticated", "allow", "unauthenticated", "unauthenticated", "allow", "unauthenticated",
    ]


def test_redirecting_jwks_is_refused_even_with_a_redirect_following_client():
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        if str(request.url) == JWKS_URL:
            return httpx2.Response(302, headers={"Location": FOREIGN})
        return httpx2.Response(200, json={"keys": [_rsa_jwk(_RSA_B, "b")]})

    config = _config()
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), follow_redirects=True)
    cache = JwksCache(config, client=client)
    with pytest.raises(AuthError) as caught:
        asyncio.run(verify_token(_token(_RSA_B, kid="b"), config, cache))
    assert caught.value.code == "unauthenticated"
    assert seen == [JWKS_URL]


def test_oversized_jwks_is_refused_without_reading_it_all():
    pulled = [0]
    chunk = b" " * 65536

    async def body():
        yield b'{"keys": [' + json.dumps(_rsa_jwk(_RSA_A, "a")).encode() + b"]"
        for _ in range(200):
            pulled[0] += 1
            yield chunk
        yield b"}"

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=body())

    config = _config()
    cache = JwksCache(config, client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    with pytest.raises(AuthError):
        asyncio.run(verify_token(_token(), config, cache))
    assert pulled[0] <= 8


def test_token_supplied_key_locations_are_never_fetched():
    head = {"kid": "evil", "jku": "https://attacker.test/keys", "x5u": "https://attacker.test/c"}
    token = jwt.encode(_claims(), _RSA_B, algorithm="RS256", headers=head)
    idp = _Idp(_rsa_jwk(_RSA_A, "a"))
    assert _code(token, idp=idp) == "unauthenticated"
    assert set(idp.requests) <= {JWKS_URL}


def test_static_jwks_file_makes_no_network_call(tmp_path: Path):
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps({"keys": [_rsa_jwk(_RSA_A, "a")]}))
    config = _config(jwks_url=None, jwks_file=path)

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("static JWKS mode must not reach the network")

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(refuse))
    cache = JwksCache(config, client=client)
    assert asyncio.run(verify_token(_token(), config, cache)).sub == "user-1"
    with pytest.raises(AuthError):
        asyncio.run(verify_token(_token(_RSA_B, kid="b"), config, cache))


def test_config_needs_exactly_one_jwks_source(tmp_path: Path):
    with pytest.raises(ValueError):
        _config(jwks_url=None)
    with pytest.raises(ValueError):
        _config(jwks_file=tmp_path / "jwks.json")
    with pytest.raises(ValueError):
        _config(jwks_url="http://idp.example.test/keys")


# --- ASGI middleware -----------------------------------------------------------------------


class _App:
    """Records every call it receives, with or without a Principal, so only
    the middleware (not this app) can be what keeps a caller out."""

    def __init__(self) -> None:
        self.calls: list[Principal | None] = []
        self.headers: list[list[bytes]] = []

    async def __call__(self, scope, receive, send) -> None:
        self.headers.append([name for name, _ in scope.get("headers", [])])
        try:
            self.calls.append(principal_from_scope(scope))
        except AuthError:
            self.calls.append(None)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _request(
    headers: dict[str, str] | list[tuple[str, str]],
    *,
    idp: _Idp | None = None,
    config: GatewayAuthConfig | None = None,
    path: str = "/mcp",
    exempt: frozenset[str] = frozenset(),
    **options: object,
) -> tuple[httpx2.Response, _App]:
    config = config or _config()
    idp = idp or _Idp(_rsa_jwk(_RSA_A, "a"))
    app = _App()
    middleware = GatewayAuthMiddleware(app, config, idp.cache(config), exempt_paths=exempt, **options)  # type: ignore[arg-type]

    async def run() -> httpx2.Response:
        transport = httpx2.ASGITransport(app=middleware)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
            return await client.post(path, headers=headers, content=b"{}")

    return asyncio.run(run()), app


def test_middleware_passes_the_principal_to_the_app():
    response, app = _request({"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 200
    assert app.calls == [Principal(sub="user-1", groups=frozenset({GROUP, "everyone"}))]


def test_principal_from_scope_refuses_an_unauthenticated_scope():
    with pytest.raises(AuthError) as caught:
        principal_from_scope({"type": "http", "headers": [(b"x-openwebui-user-id", b"user-1")]})
    assert caught.value.code == "unauthenticated"


def test_open_webui_headers_alone_never_authorize():
    owui_jwt = jwt.encode(
        {"sub": "user-1", "email": "u@example.test", "role": "admin", "exp": int(time.time()) + 300},
        "webui-forward-secret-0123456789abcdef",
        algorithm="HS256",
    )
    headers = {
        "X-OpenWebUI-User-Id": "user-1",
        "X-OpenWebUI-User-Email": "u@example.test",
        "X-OpenWebUI-User-Role": "admin",
        "X-OpenWebUI-User-Groups": GROUP,
        "X-OpenWebUI-User-Jwt": owui_jwt,
    }
    response, app = _request(headers)
    assert (response.status_code, response.json()["code"]) == (401, "unauthenticated")
    response, app2 = _request({**headers, "Authorization": f"Bearer {owui_jwt}"})
    assert (response.status_code, response.json()["code"]) == (401, "unauthenticated")
    assert app.calls == app2.calls == []


def test_open_webui_identity_headers_are_stripped_before_the_app_by_default():
    headers = {"Authorization": f"Bearer {_token()}", "X-OpenWebUI-User-Id": "someone-else", "X-Trace": "1"}
    response, app = _request(headers)
    assert response.status_code == 200
    names = app.headers[0]
    assert not [n for n in names if n.startswith(b"x-openwebui-")]
    assert b"x-trace" in names and b"authorization" in names
    _, kept = _request(headers, strip_openwebui_headers=False)
    assert b"x-openwebui-user-id" in kept.headers[0]


def test_middleware_errors_are_fixed_envelopes():
    response, _ = _request({"Authorization": f"Bearer {_token(groups=['everyone'])}"})
    assert response.status_code == 403
    assert response.json() == {"code": "forbidden", "message": "the caller is not authorized for this tool"}
    response, _ = _request({"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated", "message": "authentication is required"}
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_duplicate_authorization_headers_are_refused():
    good = f"Bearer {_token()}"
    response, app = _request([("Authorization", good), ("Authorization", good)])
    assert response.status_code == 401
    assert app.calls == []


def test_unauthenticated_websocket_is_closed_before_the_app():
    app = _App()
    config = _config()
    middleware = GatewayAuthMiddleware(app, config, _Idp().cache(config))
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {"type": "websocket", "path": "/mcp", "headers": [(b"x-openwebui-user-id", b"user-1")]}
    asyncio.run(middleware(scope, receive, send))
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert app.calls == []


def test_exempt_path_skips_auth_without_a_principal():
    app = _App()

    async def health(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    config = _config()
    middleware = GatewayAuthMiddleware(health, config, _Idp().cache(config), exempt_paths=frozenset({"/healthz"}))

    async def run() -> tuple[int, int]:
        transport = httpx2.ASGITransport(app=middleware)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
            ok = await client.get("/healthz")
            refused = await client.get("/healthz/x")
            return ok.status_code, refused.status_code

    assert asyncio.run(run()) == (204, 401)
    assert app.calls == []


# --- RFC 8693 token exchange (#1088 path a) ------------------------------------------------

SECRET_ENV = "SPLUNK_GATEWAY_CLIENT_SECRET"


def _exchange_config(**overrides: object) -> TokenExchangeConfig:
    fields: dict[str, object] = {
        "token_endpoint": TOKEN_URL,
        "client_id": "splunk-gateway",
        "client_secret_env": SECRET_ENV,
        "audience": AUDIENCE,
    }
    fields.update(overrides)
    return TokenExchangeConfig(**fields)  # type: ignore[arg-type]


class _TokenEndpoint:
    """RFC 8693 endpoint double: checks the request, mints a gateway token."""

    def __init__(self, reply: Callable[[dict[str, str]], httpx2.Response] | None = None) -> None:
        self.seen: list[str] = []
        self.forms: list[dict[str, str]] = []
        self.reply = reply

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        from urllib.parse import parse_qsl

        self.seen.append(str(request.url))
        if str(request.url) != TOKEN_URL:
            return httpx2.Response(200, json={"access_token": _token(), "token_type": "Bearer"})
        form = dict(parse_qsl(request.content.decode()))
        self.forms.append(form)
        if self.reply is not None:
            return self.reply(form)
        return httpx2.Response(200, json={
            "access_token": _token(sub="user-1"),
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "token_type": "Bearer",
        })

    def exchanger(self, *, follow_redirects: bool = False, **overrides: object) -> TokenExchanger:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler), follow_redirects=follow_redirects)
        return TokenExchanger(_exchange_config(**overrides), client=client, environ={SECRET_ENV: "s3cret-value"})


def _owui_token() -> str:
    """What Open WebUI forwards: IdP-signed, but addressed to Open WebUI."""
    return _token(aud="open-webui")


def test_exchange_turns_the_forwarded_token_into_one_for_this_gateway(caplog: pytest.LogCaptureFixture):
    forwarded = _owui_token()
    assert _request({"Authorization": f"Bearer {forwarded}"})[0].status_code == 401
    endpoint = _TokenEndpoint()
    with caplog.at_level(logging.INFO, logger="tool_auth"):
        response, app = _request({"Authorization": f"Bearer {forwarded}"}, exchanger=endpoint.exchanger())
    assert response.status_code == 200
    assert [p.sub for p in app.calls if p] == ["user-1"]
    assert endpoint.forms == [{
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": forwarded,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": AUDIENCE,
        "client_id": "splunk-gateway",
        "client_secret": "s3cret-value",
    }]
    assert forwarded not in caplog.text and "s3cret-value" not in caplog.text


def test_exchanged_token_is_still_verified():
    def wrong_audience(form: dict[str, str]) -> httpx2.Response:
        return httpx2.Response(200, json={"access_token": _token(aud="gitlab-gateway"), "token_type": "Bearer"})

    response, app = _request({"Authorization": f"Bearer {_owui_token()}"},
                             exchanger=_TokenEndpoint(wrong_audience).exchanger())
    assert (response.status_code, response.json()["code"]) == (401, "unauthenticated")
    assert app.calls == []


def test_exchange_idp_error_is_a_fixed_401(caplog: pytest.LogCaptureFixture):
    def refused(form: dict[str, str]) -> httpx2.Response:
        return httpx2.Response(400, json={"error": "invalid_grant", "error_description": "upstream-detail-xyz"})

    with caplog.at_level(logging.INFO, logger="tool_auth"):
        response, app = _request({"Authorization": f"Bearer {_owui_token()}"},
                                 exchanger=_TokenEndpoint(refused).exchanger())
    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated", "message": "authentication is required"}
    assert "upstream-detail-xyz" not in response.text + caplog.text
    assert app.calls == []


def test_exchange_redirect_is_refused_even_with_a_redirect_following_client():
    def moved(form: dict[str, str]) -> httpx2.Response:
        return httpx2.Response(307, headers={"Location": FOREIGN})

    endpoint = _TokenEndpoint(moved)
    response, app = _request({"Authorization": f"Bearer {_owui_token()}"},
                             exchanger=endpoint.exchanger(follow_redirects=True))
    assert response.status_code == 401
    assert endpoint.seen == [TOKEN_URL]
    assert app.calls == []


def test_exchange_config_needs_https_and_a_present_secret():
    with pytest.raises(ValueError):
        _exchange_config(token_endpoint="http://idp.example.test/token")
    with pytest.raises(ValueError):
        TokenExchanger(_exchange_config(), environ={})
    with pytest.raises(ValueError):
        TokenExchanger(_exchange_config(), environ={SECRET_ENV: ""})


def test_decision_log_carries_only_sub_gateway_and_decision(caplog: pytest.LogCaptureFixture):
    token = _token()
    refused = _token(groups=["everyone"])
    with caplog.at_level(logging.INFO, logger="tool_auth"):
        _request({"Authorization": f"Bearer {token}"})
        _request({"Authorization": f"Bearer {refused}"})
        _request({"Authorization": "Bearer garbage.token.value"})
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "tool_auth"]
    assert lines == [
        {"action": "tool_auth", "gateway": "splunk", "sub": "user-1", "decision": "allow"},
        {"action": "tool_auth", "gateway": "splunk", "sub": "user-1", "decision": "forbidden"},
        {"action": "tool_auth", "gateway": "splunk", "sub": None, "decision": "unauthenticated"},
    ]
    text = caplog.text
    assert token not in text and refused not in text and "garbage" not in text
