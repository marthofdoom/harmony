# 0004 — Federation over hosted-holds-everything

**Status:** Proposed. Records the intended direction; not yet built.

## Context

A hosted web version was on the roadmap. It runs into a hard problem: a server
that logs users in to Qobuz ends up **holding their credentials**, and with no
Qobuz OAuth that means session tokens (or passwords) for a paid service, for
other people — a serious custody and liability burden. YouTube Music's
browser-header auth is the user's Google session, which is worse. Same-origin
policy also means a hosted page simply *cannot* read `play.qobuz.com` storage to
get a token itself; its only options are a browser extension or manual paste.

## Decision

Prefer a **federated** model. Instances are standalone; a lightweight client
(Android, second desktop, browser) can be **pointed at a trusted instance** and
authenticates *to that instance*, which does the provider work. Only the one
credential-holding instance ever stores Qobuz/YT tokens.

## Consequences

- **Credential custody is solved by structure:** clients never hold streaming
  credentials; a lost client session exposes nothing about the user's Qobuz
  account. The trusted instance is typically the user's own desktop/home box,
  not a third-party server holding everyone's secrets.
- Raises the value of the embedded [WebKit login](../design/auth.md#qobuz):
  it's how the single credential-holding instance obtains its token cleanly.
- **Requires** a stable engine HTTP API that behaves identically in-process and
  remote — which is exactly what the [layering rule](0001-engine-frontend-separation.md)
  keeps possible. The API surface is the main work.
- Open questions remain (instance↔client auth model, pairing/consent, discovery,
  local+federated conflicts) — see [federation](../design/federation.md). Marked
  *Proposed* until those are worked out.
