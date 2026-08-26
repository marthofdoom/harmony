# Federation: instances pointed at one another

**Status: Planned.** No code yet; this records the intended model so the engine
boundary stays compatible with it.

## The model

Every Harmony instance is standalone. Additionally, a client can be **pointed at
another instance** and use it as a backend — "login via another instance." The
lightweight client (an Android phone, a second desktop, a browser) authenticates
to a *trusted instance*, and that instance does the provider work on its behalf.

```
  ┌────────────┐   authenticates to    ┌─────────────────────────┐
  │ Android    │ ───────────────────▶  │ Home instance           │
  │ client     │   engine API (HTTP)   │ (holds Qobuz/YT tokens,  │
  └────────────┘                       │  runs providers/sync)    │
  ┌────────────┐                       │                         │
  │ 2nd desktop│ ───────────────────▶  │  ── talks to Qobuz/YT ──▶│
  └────────────┘                       └─────────────────────────┘
```

## Why: this is the credential-custody answer

Earlier the hosted-web idea ran into a real problem — a server that logs people
in to Qobuz ends up **holding their credentials**, and Qobuz has no third-party
OAuth, so that means passwords or long-lived session tokens for a paid service,
for other people. See [auth](auth.md).

Federation dissolves that. Only the **one credential-holding instance** stores
Qobuz/YT tokens. Other clients authenticate *to that instance* (its own account
system) and never custody streaming credentials at all. A phone that loses its
Harmony session exposes nothing about the user's Qobuz account.

This also raises the value of the embedded
[WebKit Qobuz login](auth.md#qobuz): it's how the single credential-holding
instance cleanly obtains its token, once, locally.

## What it requires from the engine

The engine must present a **stable API** that behaves identically whether it's
called in-process or over HTTP. Clients depend on the interface
(`providers`, `sync`, `search`, `playlists`), not on a local implementation.
The [layering rule](../architecture/layering.md) is the precondition; the HTTP
surface is the work.

## Open questions (unresolved)

- Instance-to-client auth: account model, token issuance, revocation.
- Trust: a client trusts an instance with its listening; an instance trusts a
  client with API access. What's the pairing/consent flow?
- Discovery: manual URL, or LAN discovery (mDNS) for home instances?
- Conflict handling when a client is federated *and* has local providers.

See [ADR 0004](../decisions/0004-federation-for-credential-custody.md).
