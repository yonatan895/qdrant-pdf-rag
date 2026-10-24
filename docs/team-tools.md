# Team-tool gateways

Read-only team tools (Splunk, GitLab, mail) reach Open WebUI through small MCP
servers this repository owns (tracker #1075). Each gateway is its own pinned
process. This document owns the contracts every gateway shares.

<a id="caller-verification"></a>
## Caller verification (issue #1076)

```text
Contract / scope: who may call a team-tool gateway
Status: implemented with evidence (library and middleware; no gateway mounts it yet)
Source of authority: #1076 under tracker #1075; dependency approved in #1079/#1082 (PyJWT[crypto])
Decision owner: src/mainframe_rag/mcp/tool_auth.py (verify_token, JwksCache, GatewayAuthMiddleware)
Inputs and identities: the single `Authorization: Bearer` header; the IdP JWKS; GatewayAuthConfig
Producers -> persisted state -> consumers: IdP token -> in-memory key cache -> gateway tool handlers
Evidence: tests/test_mcp_tool_auth.py (one test per attack, each killing a mutation)
```

Every gateway verifies its caller itself. Open WebUI's `X-OpenWebUI-User-*`
headers, including its HS256 forward JWT, never authorize on their own: the
middleware reads only one `Authorization: Bearer` header, and the HS256 forward
JWT fails the algorithm allowlist even when sent there.

**Interface.** A gateway builds one `GatewayAuthConfig` and one `JwksCache`, wraps
its ASGI app in `GatewayAuthMiddleware(app, config, jwks, exempt_paths=...)`, and
reads the caller with `principal_from_scope(scope)` (`Principal(sub, groups)`).
`principal_from_scope` raises `AuthError` when no verified principal is present,
so an exempt path or a missing middleware never acts for an anonymous caller.
Non-HTTP callers use `await verify_token(token, config, jwks)`; logging the
decision is then the caller's job.

**Token checks**, in order; any failure refuses the call:

1. At most 32 KiB, a parseable JWS header, an `alg` in `config.algorithms`
   (default `("RS256",)`; `ES256` only when configured; HMAC and `none` are
   refused by configuration validation), and a non-empty `kid`.
2. The key for `kid` from the configured JWKS. The JWK must match the algorithm:
   `kty` (RSA or EC P-256), its `alg` if present, `use` = `sig` if present,
   `key_ops` including `verify` if present, and an RSA modulus of at least 2048 bits.
3. Signature, then `iss` (exact match), `aud` (the gateway's audience), `exp`,
   and `nbf` when present, with `leeway_s` (default 30 s, at most 300 s).
   `exp`, `iss`, `aud` and a non-empty string `sub` are required.
4. Groups from `config.group_claim` (default `groups`; a single string counts as one
   group, non-string values are ignored) must intersect `allowed_groups`. The
   default `allowed_groups` is empty, which denies every caller.

**JWKS.** URL mode fetches only `jwks_url` (https, no redirects, at most 256 KiB);
token-supplied `jku`/`x5u` are never read. Keys are cached for `jwks_ttl_s`
(300 s). An unknown `kid` triggers an early refetch at most once per
`jwks_min_refetch_s` (30 s), so a rotated key is found and a retired key stops
verifying at the next refetch. Concurrent cold requests share one fetch. A
failed or malformed fetch leaves no keys: verification fails closed until a
later fetch succeeds. A `kid` listed twice is dropped. File mode (`jwks_file`)
loads a pinned JWKS once at construction for air-gapped testing and never
touches the network. Exactly one source is configured.

**Errors.** Bodies use the repository envelope `{code, message}` with fixed text:

| Code | Status | Message | Trigger |
|---|---|---|---|
| `unauthenticated` | 401 | `authentication is required` | No or malformed bearer, duplicate `Authorization`, any token check in steps 1-3 except expiry, JWKS unavailable |
| `token_expired` | 401 | `the token has expired` | Valid signature and claims, `exp` passed (a bad signature on an expired token stays `unauthenticated`) |
| `forbidden` | 403 | `the caller is not authorized for this tool` | Verified token, no allowed group |

A 401 carries `WWW-Authenticate: Bearer` (with `error="invalid_token"` when a token
was presented); a 403 carries `error="insufficient_scope"`. A refused websocket is
closed with code 1008.

**Logs.** One line per request on logger `tool_auth`: `{"action": "tool_auth",
"gateway", "sub", "decision"}`, where `decision` is `allow` or the error code.
`sub` is logged only after the signature verified (`null` otherwise). Tokens,
claims other than `sub`, headers and request content are never logged.
