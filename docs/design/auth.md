# Authentication & credentials

How Harmony signs in to each service, and where secrets live.

## Where secrets live

**Status: Built & verified.**

Secrets go to the system keyring (Secret Service — GNOME Keyring / KWallet) via
the `keyring` package, through `config.CredentialStore`. If no keyring backend
is usable, `CredentialStore` falls back to a `0600` JSON file and says so
loudly. In the Flatpak the keyring is reached through the secrets portal
(`--talk-name=org.freedesktop.secrets`), verified working in-sandbox.

Non-secret preferences live in `config.Settings` (`settings.json`). A recurring
rule: anything on the worker-thread startup path (e.g.
`QobuzProvider.has_credentials`) must decide from `Settings`, never by reading
the keyring — keyring reads there caused a startup-cost regression once.

## YouTube Music

**Status: Built.** Via `ytmusicapi`. Two methods, in Preferences → Accounts:

- **Browser headers** — paste request headers from a logged-in
  music.youtube.com session; written to `browser.json`. Expire after months.
- **OAuth** — a TV/Limited-Input OAuth client id/secret. Longer-lived.

## Qobuz

Qobuz has **no official API and no third-party OAuth.** Harmony uses the
reverse-engineered web-player API. Every request authenticates with `X-App-Id` +
`X-User-Auth-Token`; `user/login` exists only to mint that token. So a **session
token is the real credential** — the sign-in methods differ only in how the
token is obtained.

### Password login — limited

**Status: Built, but does not work for social-login accounts.** Email + md5
password against `user/login`. Verified dead for a Google-linked account: a
correct and a deliberately wrong password return byte-identical 401s (Qobuz
rejects before evaluating credentials), because such accounts have no
API-usable password. Fine for accounts that were created with a real Qobuz
password.

### Token paste

**Status: Built & verified.** Paste the `X-User-Auth-Token` header from devtools.
Stored in keyring (`QOBUZ_TOKEN`), validated cheaply via `user/get`. Works for
any account. The blunt fallback.

### Embedded WebKit login

**Status: Built, token capture unverified.** An in-app WebKitGTK browser loads
Qobuz's own login page; the user signs in normally (Google included, since it's
a real engine); the token is lifted from **Harmony's own webview storage** by
deep-scanning `localStorage` for `user_auth_token` (keys are runtime-obfuscated,
values are JSON). Reads only our webview, never another app's browser data.
Feeds the existing token mode, so the provider is unchanged. WebKit is
soft-imported: present in the Flatpak (GNOME runtime), hidden on a bare source
checkout.

**Unverified:** the `localStorage` extraction is an inference about Qobuz's
storage shape and needs one real interactive login to confirm; it may need to
fall back to cookie/IndexedDB scanning or network-header capture. Until then,
token paste remains the reliable method.

## The federation angle

Once [federation](federation.md) exists, only the one credential-holding
instance authenticates to Qobuz/YT; other clients authenticate to *that
instance*. The embedded WebKit login is how that instance gets its token cleanly.
A hosted web app can't embed such a login (same-origin policy forbids reading
`play.qobuz.com` storage); its only routes are a browser extension or token
paste — another reason federation (client → trusted instance) beats
hosted-holds-everything.

See [ADR 0002](../decisions/0002-token-auth-for-qobuz.md).
