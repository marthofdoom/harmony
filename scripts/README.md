# harmony-setup.py

A self-contained, interactive script that bootstraps YouTube Music and Qobuz
credentials for [Harmony](../README.md) and seeds them wherever your install
(Flatpak or source checkout) actually reads them from. It is a standalone
host-side tool, not part of the app package -- it never imports `harmony.*`.

Every credential is **verified against the real service before anything is
written**: a YouTube Music auth file is loaded with `YTMusic(...)` and used
for one authed call, and a Qobuz token is checked against `user/get`. If
verification fails, nothing gets saved.

## Run it

Straight from GitHub, no clone required:

```sh
curl -fsSL https://raw.githubusercontent.com/marthofdoom/harmony/main/scripts/harmony-setup.py | python3
```

Or, if you have [uv](https://docs.astral.sh/uv/) and want its optional
dependencies (`requests`, `ytmusicapi`, `cryptography`, `keyring`,
`secretstorage`) installed automatically:

```sh
uv run https://raw.githubusercontent.com/marthofdoom/harmony/main/scripts/harmony-setup.py
```

From a local checkout:

```sh
python3 scripts/harmony-setup.py
```

Pass `--target flatpak` or `--target source` to skip auto-detection when you
have both installed. `--help` lists all options.

The script's guided flows (manual header paste, Qobuz password/token login,
and writing settings/auth files) only need the Python standard library. Its
headline feature -- auto-extracting a YouTube Music session from an already
signed-in browser -- needs `ytmusicapi` always, and `cryptography` too for
Chrome/Chromium (cookie decryption). If those aren't installed, the script
offers to build a throwaway virtualenv and re-launch itself in it; if you
decline, it falls back to the paths that don't need them, and never crashes
on a missing package.

## Security note

- Secrets (Qobuz tokens, the YouTube Music OAuth client secret) are **never
  printed, logged, or passed as command-line arguments**. Passwords and
  tokens are read with `getpass` and, for the Flatpak keyring bridge, piped
  over stdin to the app's own sandbox rather than embedded in argv (which
  would leak into the process list).
- Harmony's Flatpak keyring is isolated from your host login keyring but
  persists across app launches, so seeding a Flatpak install runs
  `flatpak run --command=python3 io.github.marthofdoom.Harmony -c "..."`
  to write the secret with the app's *own* `keyring` module, in its own
  sandbox. A source install's secrets go straight into your host keyring.
- Non-secret settings and auth files (`settings.json`, `browser.json`,
  `oauth.json`) are written directly into the target's config directory,
  merging into any existing `settings.json` rather than overwriting it.
