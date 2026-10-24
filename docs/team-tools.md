# Team-tool gateways

Read-only team tools (Splunk, GitLab, mail) reach Open WebUI through small MCP
servers this repository owns (tracker #1075). Each gateway is its own pinned
process. This document owns the contracts every gateway shares.

<a id="caller-verification"></a>
## Caller verification (issue #1076)

```text
Contract / scope: who may call a team-tool gateway
Status: implemented with evidence (library and middleware; no gateway mounts it yet)
Identity-team path: #1088 (a) token exchange or (b) extra audiences, both configurable
Source of authority: #1076 under tracker #1075; dependency approved in #1079/#1082 (PyJWT[crypto])
Decision owner: src/mainframe_rag/mcp/tool_auth.py (verify_token, JwksCache, GatewayAuthMiddleware)
Inputs and identities: the single `Authorization: Bearer` header; the IdP JWKS; GatewayAuthConfig
Producers -> persisted state -> consumers: IdP token -> in-memory key cache -> gateway tool handlers
Evidence: tests/test_mcp_tool_auth.py (one test per attack, each killing a mutation)
```

Every gateway verifies its caller itself. Open WebUI's `X-OpenWebUI-User-*`
headers, including its HS256 forward JWT, never authorize on their own: the
middleware reads only one `Authorization: Bearer` header, and the HS256 forward
JWT fails the algorithm allowlist even when sent there. By default
(`strip_openwebui_headers=True`) every `X-OpenWebUI-*` header is also removed
from the scope before the app runs, so tool code cannot read them by mistake.

**Interface.** A gateway builds one `GatewayAuthConfig` and one `JwksCache`, wraps
its ASGI app in `GatewayAuthMiddleware(app, config, jwks, exempt_paths=...,
exchanger=None, strip_openwebui_headers=True)`, and reads the caller with
`principal_from_scope(scope)` (`Principal(sub, groups)`). `principal_from_scope`
raises `AuthError` when no verified principal is present, so an exempt path or a
missing middleware never acts for an anonymous caller. Non-HTTP callers use
`await verify_token(token, config, jwks)`; logging the decision is then the
caller's job. Set-valued settings (`allowed_groups`, `accepted_audiences`,
`exempt_paths`) refuse a bare string, which would otherwise become a set of
single characters.

**Token checks**, in order; any failure refuses the call:

1. At most 32 KiB, a parseable JWS header, an `alg` in `config.algorithms`
   (default `("RS256",)`; `ES256` only when configured; HMAC and `none` are
   refused by configuration validation), and a non-empty `kid`.
2. The key for `kid` from the configured JWKS. The JWK must match the algorithm:
   `kty` (RSA or EC P-256), its `alg` if present, `use` = `sig` if present,
   `key_ops` including `verify` if present, and an RSA modulus of at least 2048 bits.
3. Signature, then `exp`, then `iss` (exact match), `aud`, and `nbf` when present,
   with `leeway_s` (default 30 s, at most 300 s). `exp`, `iss`, `aud` and a
   non-empty string `sub` are required. Because PyJWT checks `exp` before `iss`
   and `aud`, a correctly signed but expired token from the right IdP reports
   `token_expired` even when its issuer or audience is also wrong; it is refused
   either way, and only the code differs.
4. Groups from `config.group_claim` (default `groups`; a single string counts as one
   group, non-string values are ignored) must intersect `allowed_groups`. The
   default `allowed_groups` is empty, which denies every caller.

**Audiences (#1088).** The accepted names are `audience` plus the optional
`accepted_audiences` (non-empty when set), which lists other identifiers for
*this* gateway, such as an AD FS resource URI. Never list the Open WebUI client
there. Following RFC 7519, an `aud` list is accepted when it contains one of
these names. This is path (b) of #1088 (extra audiences on the Open WebUI token),
where one token is valid at every gateway listed in it. `require_single_audience=True`
refuses any multi-audience token, so a token issued for one gateway cannot be
replayed at another; use it with token exchange.

**Token exchange (#1088 path a, off by default).** `TokenExchanger(TokenExchangeConfig(
token_endpoint, client_id, client_secret_env, audience))` exchanges the forwarded
token at the IdP (RFC 8693: `grant_type` token-exchange, access-token subject and
requested types, `audience`, and `client_secret_post` credentials). The secret is
read from the environment variable named by `client_secret_env`, which is fed from
a Secret; a missing secret is a configuration error at start-up. The endpoint is
https and the request never follows a redirect. Any IdP error, redirect, oversized
(> 64 KiB) or malformed reply, or unexpected `issued_token_type` is a fixed
`unauthenticated`; upstream text and tokens are never returned or logged. The
exchanged token then goes through every check above, so the IdP's answer is
verified, not trusted. Each request costs one exchange; caching exchanged tokens
is left to the gateways if latency needs it.

**JWKS.** URL mode fetches only `jwks_url` (https, at most 256 KiB, enforced while
the body streams). Every request passes `follow_redirects=False`, whatever the
client's default, and a response for any URL other than `jwks_url` is refused.
Token-supplied `jku`/`x5u` are never read. Keys are cached for `jwks_ttl_s` (300 s).
An unknown `kid` triggers an early refetch at most once per `jwks_min_refetch_s`
(30 s), so a rotated key is found. Concurrent cold requests share one fetch. A
`kid` listed twice is dropped. File mode (`jwks_file`) loads a pinned JWKS once at
construction for air-gapped testing and never touches the network. Exactly one
source is configured.

**Stale keys (maintainer decision, 2026-10-24).** A *successful* fetch always
replaces the whole set, so a kid the IdP removed stops verifying at once. A
*failed* fetch (transport error, non-200, redirect, oversized or malformed body)
keeps the last-good set until it is `jwks_max_stale_s` (default 3600 s) older than
its successful fetch, then verification fails closed. `0` fails closed on the
first failed refresh; negative, NaN and infinite values are refused. The reason:
revocation is learned only from a successful fetch, so dropping keys during an
IdP outage revokes nothing sooner. It only turns an outage, or one token with a
random `kid` that forces a refetch during it, into a denial of service for every
user. Each failed fetch that keeps stale keys logs one line,
`{"action": "tool_auth_jwks_stale", "gateway", "age_s"}`, and nothing else.

**Errors.** Bodies use the repository envelope `{code, message}` with fixed text:

| Code | Status | Message | Trigger |
|---|---|---|---|
| `unauthenticated` | 401 | `authentication is required` | No or malformed bearer, duplicate `Authorization`, any token check in steps 1-3 except expiry, JWKS unavailable, failed token exchange |
| `token_expired` | 401 | `the token has expired` | Valid signature and claims, `exp` passed (a bad signature on an expired token stays `unauthenticated`) |
| `forbidden` | 403 | `the caller is not authorized for this tool` | Verified token, no allowed group |

A 401 carries `WWW-Authenticate: Bearer` (with `error="invalid_token"` when a token
was presented); a 403 carries `error="insufficient_scope"`. A refused websocket is
closed with code 1008.

**Logs.** One line per request on logger `tool_auth`: `{"action": "tool_auth",
"gateway", "sub", "decision"}`, where `decision` is `allow` or the error code.
`sub` is logged only after the signature verified (`null` otherwise). Tokens,
claims other than `sub`, headers and request content are never logged.
