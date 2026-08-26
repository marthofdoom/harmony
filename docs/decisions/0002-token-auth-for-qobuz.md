# 0002 — A session token is Qobuz's real credential

**Status:** Accepted.

## Context

Qobuz has no official API and no third-party OAuth. Harmony uses the
reverse-engineered web-player API, where every request carries `X-App-Id` +
`X-User-Auth-Token`. The initial implementation only supported email + md5
password against `user/login`.

The app owner signs in to Qobuz through Google. Testing showed password login
**cannot** authenticate such an account: a correct password and a deliberately
wrong one return byte-identical `401`s, and a bogus app_id returns a *different*
error (`400`) — so Qobuz rejects the request before ever evaluating credentials.
Signed requests and the web player's exact request shape all still `401`.
Google-linked accounts have no API-usable password; creating a website password
did not change this.

## Decision

Treat the **session token as the real credential**, and make sign-in methods
just different ways of obtaining it:

- **Token paste** — copy `X-User-Auth-Token` from devtools. Works for any
  account. The reliable fallback.
- **Embedded WebKit login** — sign in to Qobuz's own page in-app and lift the
  token from our own webview. Nicer, but capture is unverified pending a live
  login.
- **Password** — kept for accounts that do have a real Qobuz password.

The provider stores the token in the keyring (`QOBUZ_TOKEN`) and validates it
cheaply via `user/get`. `qobuz_auth_kind` selects the method; token-producing
methods all converge on the same downstream code.

## Consequences

- `has_credentials` must stay I/O-free (it runs on the worker-thread startup
  path), so token mode keys off a non-secret `qobuz_token_saved` flag rather
  than a keyring read.
- The token is a *better* thing to custody than a password — scoped, revocable
  by signing out, self-expiring — which matters for
  [federation](../design/federation.md) and any hosted deployment.
- Full findings: memory note `qobuz-auth-findings`, and
  [auth](../design/auth.md#qobuz).
