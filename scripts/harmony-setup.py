#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests>=2.31",
#     "ytmusicapi>=1.10",
#     "cryptography>=42",
#     "keyring>=24.0",
#     "secretstorage>=3.3; sys_platform == 'linux'",
# ]
# ///
"""Interactive, self-contained account-setup tool for Harmony.

Bootstraps YouTube Music and Qobuz credentials and seeds them wherever a
Harmony install (Flatpak or source checkout) reads them from. This is a
standalone host-side tool -- it does NOT import ``harmony.*`` -- so every
constant it needs (keyring service/keys, settings field names, the Flatpak
app id, Qobuz's bookmarklet) is inlined below and must be kept in sync by
hand with ``src/harmony/config.py`` and ``src/harmony/ui/qobuz_login.py``.

Designed to run from a bare download with no repo present:

    curl -fsSL https://raw.githubusercontent.com/marthofdoom/harmony/main/scripts/harmony-setup.py | python3

or, for automatic dependency management:

    uv run https://raw.githubusercontent.com/marthofdoom/harmony/main/scripts/harmony-setup.py

Only ``sqlite3``, ``json``, ``hashlib``, ``urllib``, ``getpass`` and the rest
of the standard library are required for the guided/paste flows and for
writing settings/auth files. ``requests``, ``ytmusicapi``, and
``cryptography`` (plus ``keyring``/``secretstorage`` for secret storage) are
imported lazily, only where actually needed, and this never hard-crashes
when one is missing -- it offers to build a throwaway virtualenv instead.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import glob
import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# Constants inlined from the app (must match src/harmony/config.py exactly)
# --------------------------------------------------------------------------

APP_ID = "io.github.marthofdoom.Harmony"
KEYRING_SERVICE = "io.github.marthofdoom.Harmony"

QOBUZ_TOKEN = "qobuz.user_auth_token"
QOBUZ_PASSWORD = "qobuz.password"
QOBUZ_APP_SECRET = "qobuz.app_secret"
YTMUSIC_OAUTH_SECRET = "ytmusic.oauth_client_secret"

# settings.json field names (src/harmony/config.py's Settings dataclass):
#   ytmusic_auth_file, ytmusic_auth_kind ("browser"|"oauth"),
#   ytmusic_oauth_client_id, qobuz_email, qobuz_auth_kind ("password"|"token"),
#   qobuz_token_saved (bool), qobuz_app_id

RAW_URL = "https://raw.githubusercontent.com/marthofdoom/harmony/main/scripts/harmony-setup.py"

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

QOBUZ_BASE_URL = "https://www.qobuz.com/api.json/0.2/"
QOBUZ_LOGIN_PAGE_URL = "https://play.qobuz.com/login"
QOBUZ_PLAYER_ORIGIN = "https://play.qobuz.com"

# Same regexes as src/harmony/providers/qobuz.py's _scrape_app_credentials,
# verified against the real web player bundle -- ported here rather than
# reimplemented from the illustrative pattern in the design brief, since
# these are the ones actually proven to work.
_BUNDLE_URL_RE = re.compile(r"/resources/[\d.]+-b\d+/bundle\.js")
_APP_ID_RE = re.compile(r'production:\{api:\{appId:"(?P<app_id>\d+)",appSecret:"(?P<inline_secret>\w+)"')
_SEED_RE = re.compile(r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.utimezone\.(?P<timezone>[a-z]+)\)')
_INFO_RE = re.compile(r'name:"\w+/(?P<timezone>[A-Za-z]+)",info:"(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"')
_SECRET_SUFFIX_LEN = 44

# Copied verbatim from src/harmony/ui/qobuz_login.py's BOOKMARKLET constant --
# do not reinvent, keep byte-for-byte in sync by hand. Extracts the Qobuz
# session token from the signed-in page's own localStorage/cookies.
BOOKMARKLET = (
    "javascript:(function(){"
    "function deep(node,depth){"
    "if(node==null||depth>8)return null;"
    "if(typeof node==='string'){try{return deep(JSON.parse(node),depth+1);}catch(e){return null;}}"
    "if(typeof node!=='object')return null;"
    "for(var k in node){"
    "var v=node[k];"
    "if(typeof v==='string'&&v.length>20&&/token|auth/i.test(k))return v;"
    "var found=deep(v,depth+1);"
    "if(found)return found;"
    "}"
    "return null;"
    "}"
    "function findToken(){"
    "var order=['localuser'];"
    "for(var i=0;i<localStorage.length;i++){"
    "var key=localStorage.key(i);"
    "if(order.indexOf(key)<0)order.push(key);"
    "}"
    "for(var n=0;n<order.length;n++){"
    "var raw=localStorage.getItem(order[n]);"
    "if(!raw)continue;"
    "var val;"
    "try{val=JSON.parse(raw);}catch(e){val=raw;}"
    "var token=deep(val,0);"
    "if(token)return token;"
    "}"
    "var cookies=document.cookie.split(';');"
    "for(var j=0;j<cookies.length;j++){"
    "var parts=cookies[j].split('=');"
    "var name=(parts[0]||'').trim();"
    "var value=(parts.slice(1).join('=')||'').trim();"
    "if(/token|auth/i.test(name)&&value.length>20)return decodeURIComponent(value);"
    "}"
    "return null;"
    "}"
    "try{"
    "var token=findToken();"
    "if(token){"
    "try{navigator.clipboard.writeText(token);}catch(e){}"
    "prompt('Qobuz token (already copied to your clipboard) - paste this into Harmony:',token);"
    "}else{"
    "alert('Could not find a Qobuz token on this page. Make sure you are signed in at "
    "play.qobuz.com, then click this bookmark again.');"
    "}"
    "}catch(e){alert('Qobuz token extraction failed: '+e);}"
    "})();"
)

# --------------------------------------------------------------------------
# Browser cookie/localStorage sources (native + Flatpak paths)
# --------------------------------------------------------------------------

FIREFOX_COOKIE_SOURCES = [
    ("Firefox", "~/.mozilla/firefox/*/cookies.sqlite"),
    ("Firefox (Flatpak)", "~/.var/app/org.mozilla.firefox/.mozilla/firefox/*/cookies.sqlite"),
    ("LibreWolf", "~/.librewolf/*/cookies.sqlite"),
    ("LibreWolf (Flatpak)", "~/.var/app/io.gitlab.librewolf-community/.librewolf/*/cookies.sqlite"),
    ("Waterfox", "~/.waterfox/*/cookies.sqlite"),
]

CHROME_COOKIE_SOURCES = [
    ("Google Chrome", "~/.config/google-chrome/*/Cookies"),
    ("Google Chrome (Flatpak)", "~/.var/app/com.google.Chrome/config/google-chrome/*/Cookies"),
    ("Chromium", "~/.config/chromium/*/Cookies"),
    ("Chromium (Flatpak)", "~/.var/app/org.chromium.Chromium/config/chromium/*/Cookies"),
    ("Ungoogled Chromium", "~/.config/ungoogled-chromium/*/Cookies"),
]

FIREFOX_STORAGE_SOURCES = [
    ("Firefox", "~/.mozilla/firefox/*/webappsstore.sqlite"),
    ("Firefox (Flatpak)", "~/.var/app/org.mozilla.firefox/.mozilla/firefox/*/webappsstore.sqlite"),
    ("LibreWolf", "~/.librewolf/*/webappsstore.sqlite"),
]

# Chromium stores localStorage in a LevelDB directory (not sqlite). We read the
# raw files rather than depend on a LevelDB library -- see
# read_chrome_qobuz_localstorage.
CHROME_STORAGE_SOURCES = [
    ("Google Chrome", "~/.config/google-chrome/*/Local Storage/leveldb"),
    ("Google Chrome (Flatpak)", "~/.var/app/com.google.Chrome/config/google-chrome/*/Local Storage/leveldb"),
    ("Chromium", "~/.config/chromium/*/Local Storage/leveldb"),
    ("Chromium (Flatpak)", "~/.var/app/org.chromium.Chromium/config/chromium/*/Local Storage/leveldb"),
    ("Ungoogled Chromium", "~/.config/ungoogled-chromium/*/Local Storage/leveldb"),
]


def _glob_paths(pattern: str) -> list[Path]:
    return [Path(p) for p in sorted(glob.glob(os.path.expanduser(pattern)))]


# --------------------------------------------------------------------------
# Target detection (Flatpak vs source install)
# --------------------------------------------------------------------------


@dataclass
class Target:
    kind: str  # "flatpak" | "source"
    config_dir: Path


def _flatpak_config_dir() -> Path:
    return Path.home() / ".var" / "app" / APP_ID / "config" / "harmony"


def _source_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "harmony"


def _flatpak_cli_installed() -> bool:
    try:
        result = subprocess.run(
            ["flatpak", "info", APP_ID], capture_output=True, timeout=10, check=False
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def detect_targets() -> list[Target]:
    """Return every plausible Harmony install found on this machine."""
    found = []
    if _flatpak_cli_installed() or _flatpak_config_dir().exists():
        found.append(Target("flatpak", _flatpak_config_dir()))
    if _source_config_dir().exists():
        found.append(Target("source", _source_config_dir()))
    return found


def resolve_target(args: argparse.Namespace) -> Target:
    if args.target == "flatpak":
        return Target("flatpak", _flatpak_config_dir())
    if args.target == "source":
        return Target("source", _source_config_dir())

    found = detect_targets()
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        print("Harmony appears to be installed more than one way:")
        for i, t in enumerate(found, 1):
            print(f"  [{i}] {t.kind} ({t.config_dir})")
        choice = input(f"Which one should this set up? [1-{len(found)}]: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(found):
                return found[idx]
        except ValueError:
            pass
        return found[0]

    print("Could not detect an existing Harmony install (Flatpak or source).")
    choice = input("Set this up for [1] Flatpak or [2] a source checkout? [1/2]: ").strip()
    if choice == "2":
        return Target("source", _source_config_dir())
    return Target("flatpak", _flatpak_config_dir())


# --------------------------------------------------------------------------
# Settings + secret seeding
# --------------------------------------------------------------------------


def read_settings(config_dir: Path) -> dict:
    path = config_dir / "settings.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def merge_settings(config_dir: Path, updates: dict) -> Path:
    """Read settings.json (if any), apply ``updates``, write it back.

    Unrelated existing keys are preserved -- this never clobbers the whole
    file, only the keys it's told to change.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "settings.json"
    existing = read_settings(config_dir)
    existing.update(updates)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2, sort_keys=True), "utf-8")
    tmp.replace(path)
    return path


def seed_secret(target: Target, key: str, value: str) -> None:
    """Write a secret into wherever the *app itself* will read it from.

    The Flatpak's keyring is isolated from the host login keyring but
    persists across the app's own launches, so for a Flatpak target this
    invokes ``keyring.set_password`` *inside* the app's own sandbox via
    ``flatpak run --command=python3``, feeding the secret over stdin --
    never as a command-line argument, where it would be visible in the
    process list. For a source install this writes straight to the host
    keyring.
    """
    if target.kind == "flatpak":
        inner = f"import sys, keyring; keyring.set_password({KEYRING_SERVICE!r}, {key!r}, sys.stdin.read())"
        result = subprocess.run(
            ["flatpak", "run", "--command=python3", APP_ID, "-c", inner],
            input=value,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(
                "Could not write into the Flatpak app's own keyring "
                f"(exit {result.returncode}){f': {stderr}' if stderr else ''}"
            )
    else:
        import keyring

        keyring.set_password(KEYRING_SERVICE, key, value)


def ensure_keyring_for_target(target: Target) -> bool:
    """Make sure a secret can actually be seeded for ``target`` before we ask
    for any credentials -- the Flatpak branch of ``seed_secret`` needs
    nothing local (it shells out to ``flatpak run``), but a source install
    needs the ``keyring`` package importable on *this* interpreter.
    """
    if target.kind != "source":
        return True
    if try_import("keyring") is not None:
        return True
    print("\nSaving a secret for a source install needs the 'keyring' package, which isn't installed.")
    if offer_venv_bootstrap(["keyring", "secretstorage"]):
        return False  # unreachable: the process is replaced on success
    print("Install them yourself (`pip install keyring secretstorage`) and re-run this script.\n")
    return False


# --------------------------------------------------------------------------
# Optional-dependency handling: lazy import + throwaway venv bootstrap
# --------------------------------------------------------------------------


def try_import(name: str):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "harmony-setup"


CACHE_DIR = _cache_dir()
VENV_DIR = CACHE_DIR / "venv"


def _venv_bin(name: str) -> Path:
    return VENV_DIR / "bin" / name


def _self_source_path() -> Path | None:
    """The path of *this* script on disk, if it has one.

    ``__file__`` is unreliable (or absent) when the script was piped
    straight into ``python3`` from ``curl`` -- there's no file to point at,
    only the source that was already read from stdin and compiled. In that
    case this returns ``None`` and the caller falls back to re-downloading.
    """
    try:
        candidate = Path(__file__).resolve()
    except NameError:
        return None
    return candidate if candidate.is_file() else None


def _reexec_in_venv() -> None:
    python = str(_venv_bin("python3"))
    argv_rest = sys.argv[1:]
    src = _self_source_path()
    if src is not None:
        os.execv(python, [python, str(src), *argv_rest])
        return  # pragma: no cover - execv never returns on success
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached = CACHE_DIR / "harmony-setup.py"
        with urllib.request.urlopen(RAW_URL, timeout=20) as resp:  # noqa: S310 - fixed https URL
            cached.write_bytes(resp.read())
        os.execv(python, [python, str(cached), *argv_rest])
    except Exception as exc:  # noqa: BLE001 - re-exec is best-effort
        print(f"Could not re-launch inside the virtual environment: {exc}")
        print("Continuing without the optional packages.")


def _in_virtualenv() -> bool:
    """True when running inside a venv/virtualenv (an isolated interpreter)."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _reexec_current_python() -> None:
    """Restart the script under the *current* interpreter after an in-place install.

    A module that already failed to import earlier in this process can't be
    retried in place, so once we've pip-installed into the current environment
    we re-exec to get a clean import. Mirrors ``_reexec_in_venv`` but keeps the
    same interpreter instead of switching to the throwaway venv's.
    """
    python = sys.executable
    src = _self_source_path()
    if src is not None:
        os.execv(python, [python, str(src), *sys.argv[1:]])
        return  # pragma: no cover - execv never returns on success
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached = CACHE_DIR / "harmony-setup.py"
        with urllib.request.urlopen(RAW_URL, timeout=20) as resp:  # noqa: S310 - fixed https URL
            cached.write_bytes(resp.read())
        os.execv(python, [python, str(cached), *sys.argv[1:]])
    except Exception as exc:  # noqa: BLE001 - re-exec is best-effort
        print(f"Installed the packages, but couldn't relaunch automatically: {exc}")
        print("Re-run the script and it'll pick them up.")
        raise SystemExit(0) from None


def offer_venv_bootstrap(packages: list[str]) -> bool:
    """Offer to install missing ``packages`` and re-exec with them available.

    When already inside a virtualenv, installs straight into it with pip -- the
    right move when the user launched us from their own venv (the reported case
    where the old throwaway-venv-only path offered "no actual solution").
    Otherwise builds a throwaway venv under the cache dir and re-execs into it.

    Returns True only when the process is being replaced (via ``os.execv``);
    callers treat a True return as "stop what you were doing".
    """
    if _in_virtualenv():
        return _install_into_current_env(packages)
    return _bootstrap_throwaway_venv(packages)


def _install_into_current_env(packages: list[str]) -> bool:
    try:
        ans = (
            input(f"Install {', '.join(packages)} into this environment ({sys.prefix}) now? [Y/n] ")
            .strip()
            .lower()
        )
    except EOFError:
        ans = "n"
    if ans in ("n", "no"):
        print(f"Install them yourself:  pip install {' '.join(packages)}\n")
        return False
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *packages], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Could not install into the current environment: {exc}")
        print(f"Install them yourself:  pip install {' '.join(packages)}\n")
        return False
    _reexec_current_python()
    return True


def _bootstrap_throwaway_venv(packages: list[str]) -> bool:
    try:
        ans = (
            input(f"Create a throwaway virtual environment and install {', '.join(packages)} now? [y/N] ")
            .strip()
            .lower()
        )
    except EOFError:
        ans = "n"
    if ans not in ("y", "yes"):
        return False
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Creating a virtual environment in {VENV_DIR} ...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
        pip = str(_venv_bin("pip"))
        subprocess.run([pip, "install", "-q", "--upgrade", "pip"], check=True)
        subprocess.run([pip, "install", "-q", *packages], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Could not set up the virtual environment: {exc}")
        return False
    _reexec_in_venv()
    return True


def ensure_ytmusicapi():
    mod = try_import("ytmusicapi")
    if mod is not None:
        return mod
    print("\nYouTube Music setup needs the 'ytmusicapi' package, which isn't installed.")
    if offer_venv_bootstrap(["ytmusicapi", "requests"]):
        return None  # unreachable: the process is replaced on success
    print(
        "Install them yourself (`pip install ytmusicapi requests`) and re-run this script, "
        "or run `uv run harmony-setup.py` which installs everything automatically.\n"
    )
    return None


# --------------------------------------------------------------------------
# Firefox cookies (unencrypted sqlite)
# --------------------------------------------------------------------------


def _copy_sqlite_to_temp(path: Path) -> str:
    """Copy a possibly browser-locked sqlite db to a temp file before reading.

    Uses ``copyfile`` (data only), NOT ``copy2``/``copystat`` — the source
    cookie/localStorage DBs are often 0644, and copying that mode onto the temp
    file would undo the 0600 ``mkstemp`` gives it, briefly exposing plaintext
    session cookies in a world-readable temp dir on a shared host.
    """
    fd, name = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    shutil.copyfile(path, name)
    os.chmod(name, 0o600)  # belt-and-suspenders in case the umask/mkstemp differs
    return name


def read_firefox_cookies(cookies_path: Path) -> list[tuple[str, str, str]]:
    """Read ``(host, name, value)`` for youtube.com/google.com cookies.

    Firefox's cookie store is plain, unencrypted sqlite -- no decryption
    needed, unlike Chrome's.
    """
    tmp = _copy_sqlite_to_temp(cookies_path)
    try:
        conn = sqlite3.connect(tmp)
        try:
            cur = conn.execute(
                "SELECT host, name, value FROM moz_cookies "
                "WHERE host LIKE '%youtube.com' OR host LIKE '%google.com'"
            )
            return [(row[0], row[1], row[2]) for row in cur.fetchall()]
        finally:
            conn.close()
    finally:
        os.unlink(tmp)


# --------------------------------------------------------------------------
# Chrome/Chromium cookies (AES-128-CBC encrypted, v10/v11)
# --------------------------------------------------------------------------


def get_chrome_safe_storage_password() -> bytes:
    """The "Chrome Safe Storage" secret from the Secret Service, or "peanuts"."""
    try:
        import secretstorage

        conn = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(conn)
        if collection.is_locked():
            collection.unlock()
        for item in collection.get_all_items():
            attrs = item.get_attributes()
            app = (attrs.get("application") or "").lower()
            if app in ("chrome", "chromium"):
                return item.get_secret()
    except Exception:  # noqa: BLE001 - any backend failure just falls back
        pass
    return b"peanuts"


def chrome_decrypt(encrypted_value: bytes, password: bytes) -> str:
    """Decrypt a Chrome/Chromium cookie value on Linux.

    key = PBKDF2-HMAC-SHA1(password, salt=b"saltysalt", 1 iteration, 16 bytes)
    AES-128-CBC, IV = 16 spaces, PKCS7-padded. The ``v10``/``v11`` prefix is
    stripped first; for ``v10`` some Chrome versions also prepend a 32-byte
    SHA256 domain hash after unpadding, which is stripped if the plain
    unpadded bytes don't decode as UTF-8 on their own.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    prefix = encrypted_value[:3]
    if prefix not in (b"v10", b"v11"):
        raise ValueError(f"unsupported cookie value prefix {prefix!r}")

    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1, 16)
    iv = b" " * 16
    ciphertext = encrypted_value[3:]
    if not ciphertext or len(ciphertext) % 16 != 0:
        raise ValueError("ciphertext is not a multiple of the AES block size")

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    pad_len = padded[-1] if padded else 0
    if not (1 <= pad_len <= 16):
        raise ValueError("invalid PKCS7 padding")
    decrypted = padded[:-pad_len]

    if prefix == b"v10" and len(decrypted) > 32:
        try:
            return decrypted.decode("utf-8")
        except UnicodeDecodeError:
            return decrypted[32:].decode("utf-8")
    return decrypted.decode("utf-8")


def read_chrome_cookies(cookies_path: Path, password: bytes) -> list[tuple[str, str, str]]:
    """Read ``(host, name, value)`` for youtube.com/google.com cookies."""
    tmp = _copy_sqlite_to_temp(cookies_path)
    try:
        conn = sqlite3.connect(tmp)
        try:
            cur = conn.execute(
                "SELECT host_key, name, encrypted_value FROM cookies "
                "WHERE host_key LIKE '%youtube.com' OR host_key LIKE '%google.com'"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    finally:
        os.unlink(tmp)

    out: list[tuple[str, str, str]] = []
    for host, name, encrypted in rows:
        if not encrypted:
            continue
        try:
            value = chrome_decrypt(bytes(encrypted), password)
        except Exception:  # noqa: BLE001 - a single bad cookie shouldn't sink the batch
            continue
        out.append((host, name, value))
    return out


# --------------------------------------------------------------------------
# Cookie -> ytmusicapi headers_raw
# --------------------------------------------------------------------------


def build_cookie_header(cookies: list[tuple[str, str, str]]) -> str:
    seen: dict[str, str] = {}
    for _host, name, value in cookies:
        seen[name] = value
    return "; ".join(f"{name}={value}" for name, value in seen.items())


# Google's SAPISID cookie under the names a YouTube Music session may store it,
# most specific first.
_SAPISID_COOKIE_NAMES = ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID")
_YTM_ORIGIN = "https://music.youtube.com"


def find_sapisid(cookies: list[tuple[str, str, str]]) -> str | None:
    """Return the Google SAPISID value from extracted cookies, or None.

    ytmusicapi >= 1.12 classifies an auth file as *browser* (rather than
    defaulting it to OAuth) only when it carries an ``Authorization:
    SAPISIDHASH ...`` header, and it recomputes that hash per request from the
    SAPISID cookie -- so a session without one can't drive browser auth at all.
    """
    by_name = {name: value for _host, name, value in cookies}
    for key in _SAPISID_COOKIE_NAMES:
        if by_name.get(key):
            return by_name[key]
    return None


def sapisid_hash(sapisid: str, origin: str = _YTM_ORIGIN) -> str:
    """Compute an ``Authorization: SAPISIDHASH`` value the way Google's web apps do.

    ytmusicapi only inspects this header to decide the auth is browser type; it
    recomputes a fresh hash from the cookie on every request, so the timestamp
    baked in here only needs to be well-formed, not fresh at use time.
    """
    ts = str(int(time.time()))
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def build_headers_raw(cookie_header: str, sapisid: str | None = None) -> str:
    lines = [
        f"Cookie: {cookie_header}",
        f"User-Agent: {DESKTOP_USER_AGENT}",
        "X-Goog-AuthUser: 0",
        f"Origin: {_YTM_ORIGIN}",
    ]
    if sapisid:
        # Required by ytmusicapi >= 1.12 to recognise this as browser auth
        # rather than falling through to its OAuth default.
        lines.insert(0, f"Authorization: {sapisid_hash(sapisid)}")
    return "\n".join(lines)


@dataclass
class BrowserCandidate:
    label: str
    read: Callable[[], list[tuple[str, str, str]]]


def discover_browser_sessions() -> list[BrowserCandidate]:
    out: list[BrowserCandidate] = []
    for label, pattern in FIREFOX_COOKIE_SOURCES:
        for path in _glob_paths(pattern):
            out.append(
                BrowserCandidate(
                    label=f"{label}: {path.parent.name}",
                    read=lambda p=path: read_firefox_cookies(p),
                )
            )

    if try_import("cryptography") is not None:
        password = get_chrome_safe_storage_password()
        for label, pattern in CHROME_COOKIE_SOURCES:
            for path in _glob_paths(pattern):
                out.append(
                    BrowserCandidate(
                        label=f"{label}: {path.parent.name}",
                        read=lambda p=path, pw=password: read_chrome_cookies(p, pw),
                    )
                )
    else:
        chrome_found = any(_glob_paths(pattern) for _, pattern in CHROME_COOKIE_SOURCES)
        if chrome_found:
            print(
                "\nFound Chrome/Chromium profiles, but decrypting their cookies needs "
                "the 'cryptography' package, which isn't installed."
            )
            if offer_venv_bootstrap(["cryptography"]):
                return out  # unreachable: the process is replaced on success
            print("Install it (`pip install cryptography`) and re-run to include Chrome.\n")
    return out


# --------------------------------------------------------------------------
# YouTube Music: verification, OAuth device flow
# --------------------------------------------------------------------------


def verify_ytmusic_auth(
    auth_path: Path, kind: str, client_id: str = "", client_secret: str = ""
) -> tuple[bool, str]:
    """Load the produced auth file and make one authed call. Never seeds on failure."""
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        return False, "ytmusicapi is not installed"
    try:
        if kind == "oauth":
            from ytmusicapi import OAuthCredentials

            creds = OAuthCredentials(client_id=client_id, client_secret=client_secret)
            yt = YTMusic(auth=str(auth_path), oauth_credentials=creds)
        else:
            yt = YTMusic(auth=str(auth_path))
        yt.get_library_playlists(limit=1)
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as the failure reason
        return False, str(exc)


def ytmusic_oauth_device_flow(config_dir: Path, client_id: str, client_secret: str) -> Path:
    """Drive the OAuth device-code flow to completion; returns the oauth.json path."""
    from ytmusicapi.auth.oauth import OAuthCredentials
    from ytmusicapi.auth.oauth.token import RefreshingToken

    credentials = OAuthCredentials(client_id=client_id, client_secret=client_secret)
    try:
        raw_code = credentials.get_code()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Could not start Google sign-in: {exc}") from exc

    user_code = raw_code["user_code"]
    verification_url = raw_code["verification_url"]
    expires_in = int(raw_code.get("expires_in", 300))
    interval = max(int(raw_code.get("interval", 5)), 1)
    full_url = f"{verification_url}?user_code={user_code}"

    print(f"\nGo to: {full_url}")
    print(f"(or open {verification_url} and enter code: {user_code})\n")
    try:
        webbrowser.open(full_url)
    except Exception:  # noqa: BLE001 - non-fatal; the URL is printed above
        pass

    device_code = raw_code["device_code"]
    deadline = time.monotonic() + expires_in
    print("Waiting for you to approve in the browser...")
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            raw_token = credentials.token_from_code(device_code)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Google rejected the OAuth client: {exc}") from exc

        if "access_token" in raw_token:
            refresh_expires_in = raw_token.get("refresh_token_expires_in", raw_token["expires_in"])
            token = RefreshingToken(
                credentials=credentials,
                access_token=raw_token["access_token"],
                refresh_token=raw_token["refresh_token"],
                scope=raw_token["scope"],
                token_type=raw_token["token_type"],
                expires_in=refresh_expires_in,
            )
            token.update(raw_token)
            oauth_path = config_dir / "oauth.json"
            token.store_token(str(oauth_path))
            return oauth_path

        error = raw_token.get("error")
        if error in ("authorization_pending", "slow_down"):
            continue
        raise RuntimeError(f"Google sign-in failed: {error or 'unknown error'}")

    raise RuntimeError("The sign-in code expired before it was approved. Try again.")


# --------------------------------------------------------------------------
# Qobuz: app credentials, login, verification, localStorage auto-grab
# --------------------------------------------------------------------------


def scrape_qobuz_app_credentials() -> tuple[str, str]:
    req = urllib.request.Request(QOBUZ_LOGIN_PAGE_URL, headers={"User-Agent": DESKTOP_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https URL
            page = resp.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Qobuz to auto-detect app credentials: {exc}") from exc

    bundle_match = _BUNDLE_URL_RE.search(page)
    if not bundle_match:
        raise RuntimeError("Could not find the Qobuz web player bundle to scrape credentials from")

    bundle_req = urllib.request.Request(
        QOBUZ_PLAYER_ORIGIN + bundle_match.group(0), headers={"User-Agent": DESKTOP_USER_AGENT}
    )
    try:
        with urllib.request.urlopen(bundle_req, timeout=15) as resp:  # noqa: S310
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not fetch the Qobuz web player bundle: {exc}") from exc

    app_id_match = _APP_ID_RE.search(text)
    if not app_id_match:
        raise RuntimeError("Could not extract a Qobuz app_id from the web player bundle")
    app_id = app_id_match.group("app_id")

    seeds = {m.group("timezone").lower(): m.group("seed") for m in _SEED_RE.finditer(text)}
    secret = ""
    for m in _INFO_RE.finditer(text):
        seed = seeds.get(m.group("timezone").lower())
        if not seed:
            continue
        candidate = seed + m.group("info") + m.group("extras")
        try:
            decoded = base64.b64decode(candidate)[:-_SECRET_SUFFIX_LEN].decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            continue
        secret = decoded
    return app_id, secret


def qobuz_password_login(app_id: str, email: str, password: str) -> str:
    digest = hashlib.md5(password.encode("utf-8")).hexdigest()  # noqa: S324 - Qobuz's own scheme
    params = urllib.parse.urlencode(
        {"app_id": app_id, "username": email, "email": email, "password": digest}
    ).encode("utf-8")
    req = urllib.request.Request(
        QOBUZ_BASE_URL + "user/login",
        data=params,
        headers={"X-App-Id": app_id, "User-Agent": DESKTOP_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Qobuz login failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Qobuz: {exc.reason}") from exc

    token = data.get("user_auth_token")
    if not token:
        raise RuntimeError("Qobuz login failed: no auth token in the response")
    return token


def verify_qobuz_token(app_id: str, token: str) -> tuple[bool, str, str | None]:
    """Call user/get with the token. Returns (ok, detail, display_name)."""
    req = urllib.request.Request(
        QOBUZ_BASE_URL + "user/get",
        headers={"X-App-Id": app_id, "X-User-Auth-Token": token, "User-Agent": DESKTOP_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, f"Qobuz returned HTTP {exc.code}", None
    except urllib.error.URLError as exc:
        return False, f"Could not reach Qobuz: {exc.reason}", None

    user = data.get("user") if isinstance(data.get("user"), dict) else data
    if not isinstance(user, dict) or not user.get("id"):
        return False, "Qobuz did not return a user for this token", None
    return True, "ok", user.get("display_name")


def _deep_search_token(node, depth: int = 0) -> str | None:
    """Python port of the bookmarklet's ``deep()``: find a token/auth field."""
    if node is None or depth > 8:
        return None
    if isinstance(node, str):
        try:
            return _deep_search_token(json.loads(node), depth + 1)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and len(v) > 20 and re.search(r"(?i)token|auth", k):
                return v
            found = _deep_search_token(v, depth + 1)
            if found:
                return found
        return None
    if isinstance(node, list):
        for v in node:
            found = _deep_search_token(v, depth + 1)
            if found:
                return found
    return None


def read_firefox_qobuz_localstorage(webappsstore_path: Path) -> str | None:
    """Best-effort read of a legacy Firefox ``webappsstore.sqlite`` for a
    Qobuz session token, mirroring the bookmarklet's own search order
    (``localuser`` first, then every other key for that scope).
    """
    tmp = _copy_sqlite_to_temp(webappsstore_path)
    try:
        conn = sqlite3.connect(tmp)
        try:
            cur = conn.execute(
                "SELECT scope, key, value FROM webappsstore2 WHERE scope LIKE '%qobuz%'"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    finally:
        os.unlink(tmp)

    ordered = sorted(rows, key=lambda r: 0 if r[1] == "localuser" else 1)
    for _scope, _key, value in ordered:
        token = _deep_search_token(value)
        if token:
            return token
    return None


# Matches a "<key containing token/auth>": "<long value>" pair in raw bytes.
_CHROME_TOKEN_RE = re.compile(
    rb'([a-z_]*(?:token|auth)[a-z_]*)"?\s*[:=]\s*"?([A-Za-z0-9_\-.=/+]{25,})', re.IGNORECASE
)


def read_chrome_qobuz_localstorage(leveldb_dir: Path) -> str | None:
    """Best-effort scan of a Chromium ``Local Storage/leveldb`` dir for a Qobuz token.

    Chromium keeps localStorage in LevelDB, whose ``.ldb`` blocks may be
    snappy-compressed but whose recent writes sit uncompressed in the ``.log``
    write-ahead file. Rather than depend on a LevelDB reader, read the raw
    files and, scoped to a window right after each ``play.qobuz.com`` origin
    marker (the store is shared across all sites, so scoping avoids grabbing a
    different site's token), pull out the first token/auth-looking value. The
    common case -- an ASCII JSON value Chromium stores one-byte-encoded -- is
    recoverable this way; a UTF-16-encoded value isn't, and falls through to
    the paste-token option.
    """
    chunks: list[bytes] = []
    for name in ("*.log", "*.ldb"):
        for path in sorted(glob.glob(os.path.join(str(leveldb_dir), name))):
            try:
                chunks.append(Path(path).read_bytes())
            except OSError:
                continue
    raw = b"".join(chunks)
    # Try the raw bytes, then a crude UTF-16LE collapse (Chromium stores some
    # localStorage values UTF-16, i.e. ASCII chars interleaved with NULs).
    for view in (raw, raw.replace(b"\x00", b"")):
        for origin in re.finditer(rb"qobuz", view):
            match = _CHROME_TOKEN_RE.search(view[origin.start() : origin.start() + 16384])
            if match:
                return match.group(2).decode("ascii", "ignore")
    return None


def _qobuz_token_from_cookie_value(value: str) -> str | None:
    """Pull an auth token out of a decrypted ``qobuz-session`` cookie value.

    The value is typically URL-encoded JSON carrying the credential; try the
    deep search (JSON-aware), then a token/auth-field regex, then a bare token.
    """
    if os.environ.get("HARMONY_DEBUG"):
        masked = re.sub(r"[A-Za-z0-9+/=_\-]{16,}", lambda m: f"<{len(m.group())}>", urllib.parse.unquote(value))
        print(f"[debug] qobuz-session structure: {masked[:600]}")
    for candidate in (value, urllib.parse.unquote(value)):
        token = _deep_search_token(candidate)
        if token:
            return token
        match = re.search(
            r"(?i)(?:user_auth_token|auth_token|token)\"?\s*[:=]\s*\"?([A-Za-z0-9_\-.=/+]{20,})",
            candidate,
        )
        if match:
            return match.group(1)
    bare = urllib.parse.unquote(value).strip()
    return bare if re.fullmatch(r"[A-Za-z0-9_\-.=/+]{20,}", bare) else None


def read_chrome_qobuz_cookie(cookies_path: Path, password: bytes) -> str | None:
    """Decrypt Chrome's ``qobuz-session`` cookie and extract the auth token."""
    tmp = _copy_sqlite_to_temp(cookies_path)
    try:
        conn = sqlite3.connect(tmp)
        try:
            row = conn.execute(
                "SELECT encrypted_value FROM cookies WHERE name='qobuz-session' AND host_key LIKE '%qobuz%'"
            ).fetchone()
        finally:
            conn.close()
    finally:
        os.unlink(tmp)
    debug = os.environ.get("HARMONY_DEBUG")
    if not row:
        if debug:
            print(f"[debug] no qobuz-session cookie in {cookies_path}")
        return None
    try:
        value = chrome_decrypt(row[0], password)
    except Exception as exc:  # noqa: BLE001 - a decrypt failure just means "no token here"
        if debug:
            print(f"[debug] decrypt failed for {cookies_path}: {exc}")
        return None
    return _qobuz_token_from_cookie_value(value)


def read_firefox_qobuz_cookie(cookies_path: Path) -> str | None:
    """Read Firefox's ``qobuz-session`` cookie (unencrypted) and extract the token."""
    tmp = _copy_sqlite_to_temp(cookies_path)
    try:
        conn = sqlite3.connect(tmp)
        try:
            row = conn.execute(
                "SELECT value FROM moz_cookies WHERE name='qobuz-session' AND host LIKE '%qobuz%'"
            ).fetchone()
        finally:
            conn.close()
    finally:
        os.unlink(tmp)
    return _qobuz_token_from_cookie_value(row[0]) if row else None


# --------------------------------------------------------------------------
# Interactive flow: YouTube Music
# --------------------------------------------------------------------------


def _ytmusic_auto_extract(target: Target) -> None:
    if ensure_ytmusicapi() is None:
        return
    print("Scanning installed browsers for a YouTube/Google session...")
    candidates = discover_browser_sessions()
    if not candidates:
        print("No browser with a YouTube/Google session was found.")
        return
    for i, c in enumerate(candidates, 1):
        print(f"  [{i}] {c.label}")
    choice = input(f"Pick a browser/profile [1-{len(candidates)}] (or 'b' to go back): ").strip().lower()
    if choice in ("b", ""):
        return
    try:
        picked = candidates[int(choice) - 1]
    except (ValueError, IndexError):
        print("Not a valid choice.")
        return

    try:
        cookies = picked.read()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read cookies: {exc}")
        return
    if not cookies:
        print("No YouTube/Google cookies found in that profile.")
        return

    sapisid = find_sapisid(cookies)
    if not sapisid:
        print(
            "That profile has YouTube cookies but no Google SAPISID cookie, so it "
            "can't be used for browser auth. Make sure you're signed in to "
            "music.youtube.com in that browser, or use OAuth / manual paste instead."
        )
        return
    headers_raw = build_headers_raw(build_cookie_header(cookies), sapisid)
    target.config_dir.mkdir(parents=True, exist_ok=True)
    auth_path = target.config_dir / "browser.json"

    import ytmusicapi

    try:
        ytmusicapi.setup(filepath=str(auth_path), headers_raw=headers_raw)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not build the YouTube Music auth file: {exc}")
        return

    print("Verifying...")
    ok, detail = verify_ytmusic_auth(auth_path, "browser")
    if not ok:
        print(f"Verification failed ({detail}); nothing was saved to Harmony's settings.")
        auth_path.unlink(missing_ok=True)
        return

    merge_settings(target.config_dir, {"ytmusic_auth_file": str(auth_path), "ytmusic_auth_kind": "browser"})
    print(f"YouTube Music verified and saved to {auth_path}.")
    print("Done -- (re)start Harmony.")


def _ytmusic_oauth_setup(target: Target) -> None:
    if ensure_ytmusicapi() is None:
        return
    if not ensure_keyring_for_target(target):
        return
    print("Create a 'TV and Limited Input' OAuth client in a Google Cloud project with the")
    print("YouTube Data API enabled: https://console.cloud.google.com/apis/credentials")
    client_id = input("Client ID: ").strip()
    client_secret = getpass.getpass("Client secret (hidden): ").strip()
    if not client_id or not client_secret:
        print("Client ID and secret are required.")
        return

    target.config_dir.mkdir(parents=True, exist_ok=True)
    try:
        oauth_path = ytmusic_oauth_device_flow(target.config_dir, client_id, client_secret)
    except Exception as exc:  # noqa: BLE001
        print(f"Sign-in failed: {exc}")
        return

    print("Verifying...")
    ok, detail = verify_ytmusic_auth(oauth_path, "oauth", client_id, client_secret)
    if not ok:
        print(f"Verification failed ({detail}); nothing was saved to Harmony's settings.")
        oauth_path.unlink(missing_ok=True)
        return

    try:
        seed_secret(target, YTMUSIC_OAUTH_SECRET, client_secret)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not save the client secret ({exc}); settings were not updated.")
        # Don't leave a refresh-token-bearing oauth.json behind for a sign-in
        # that never completed; settings don't reference it, so it's just an
        # orphaned credential at rest.
        oauth_path.unlink(missing_ok=True)
        return

    merge_settings(
        target.config_dir,
        {
            "ytmusic_auth_file": str(oauth_path),
            "ytmusic_auth_kind": "oauth",
            "ytmusic_oauth_client_id": client_id,
        },
    )
    print(f"YouTube Music verified and saved to {oauth_path}.")
    print("Done -- (re)start Harmony.")


def _ytmusic_manual_paste(target: Target) -> None:
    if ensure_ytmusicapi() is None:
        return
    import ytmusicapi

    print("Paste the raw request headers from a signed-in music.youtube.com request")
    print("(browser devtools -> Network -> a request to music.youtube.com -> Copy request headers).")
    print("Paste them below, then finish with a blank line:")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "":
            break
        lines.append(line)
    headers_raw = "\n".join(lines)
    if not headers_raw.strip():
        print("Nothing pasted; aborting.")
        return

    target.config_dir.mkdir(parents=True, exist_ok=True)
    auth_path = target.config_dir / "browser.json"
    try:
        ytmusicapi.setup(filepath=str(auth_path), headers_raw=headers_raw)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not build the YouTube Music auth file: {exc}")
        return

    print("Verifying...")
    ok, detail = verify_ytmusic_auth(auth_path, "browser")
    if not ok:
        print(f"Verification failed ({detail}); nothing was saved to Harmony's settings.")
        auth_path.unlink(missing_ok=True)
        return

    merge_settings(target.config_dir, {"ytmusic_auth_file": str(auth_path), "ytmusic_auth_kind": "browser"})
    print(f"YouTube Music verified and saved to {auth_path}.")
    print("Done -- (re)start Harmony.")


def ytmusic_menu(target: Target) -> None:
    while True:
        print("\nYouTube Music")
        print("  [1] Auto-extract from browser (recommended)")
        print("  [2] OAuth device flow")
        print("  [3] Manual paste of raw headers")
        print("  [b] Back")
        choice = input("> ").strip().lower()
        if choice == "1":
            _ytmusic_auto_extract(target)
        elif choice == "2":
            _ytmusic_oauth_setup(target)
        elif choice == "3":
            _ytmusic_manual_paste(target)
        elif choice in ("b", "back", ""):
            return
        else:
            print("Not a valid choice.")


# --------------------------------------------------------------------------
# Interactive flow: Qobuz
# --------------------------------------------------------------------------


def _get_or_scrape_qobuz_app_id(target: Target) -> str | None:
    existing = read_settings(target.config_dir).get("qobuz_app_id")
    if existing:
        return existing
    print("Looking up Qobuz's app_id from the web player...")
    try:
        app_id, secret = scrape_qobuz_app_credentials()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not auto-detect app_id ({exc}).")
        app_id = input("Enter a Qobuz app_id manually (or leave blank to cancel): ").strip()
        if not app_id:
            return None
        secret = ""
    if secret:
        try:
            seed_secret(target, QOBUZ_APP_SECRET, secret)
        except Exception:  # noqa: BLE001 - the app_id alone is still useful without the secret
            pass
    return app_id


def _qobuz_password_login(target: Target) -> None:
    if not ensure_keyring_for_target(target):
        return
    email = input("Qobuz email: ").strip()
    password = getpass.getpass("Qobuz password (hidden): ")
    if not email or not password:
        print("Email and password are required.")
        return

    app_id = _get_or_scrape_qobuz_app_id(target)
    if not app_id:
        return

    print("Signing in...")
    try:
        token = qobuz_password_login(app_id, email, password)
    except Exception as exc:  # noqa: BLE001
        print(f"Login failed: {exc}")
        return

    print("Verifying...")
    ok, detail, display_name = verify_qobuz_token(app_id, token)
    if not ok:
        print(f"Verification failed ({detail}); nothing was saved.")
        return

    try:
        seed_secret(target, QOBUZ_TOKEN, token)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not save the token ({exc}).")
        return

    merge_settings(
        target.config_dir,
        {
            "qobuz_email": email,
            "qobuz_auth_kind": "token",
            "qobuz_token_saved": True,
            "qobuz_app_id": app_id,
        },
    )
    who = f" as {display_name}" if display_name else ""
    print(f"Qobuz verified{who} and token saved.")
    print("Done -- (re)start Harmony.")


def _qobuz_paste_token(target: Target) -> None:
    if not ensure_keyring_for_target(target):
        return
    print("For accounts signed up via Google or another social login, Qobuz's password")
    print("login won't work against the API -- grab a session token instead:\n")
    print("  1. Sign in at https://play.qobuz.com in your normal browser.")
    print("  2. Make a bookmark whose URL is the snippet below, then click it while the")
    print("     Qobuz tab is open and you're signed in.\n")
    print(BOOKMARKLET)
    print()
    token = getpass.getpass("Paste the token here (hidden): ").strip()
    if not token:
        print("Nothing pasted; aborting.")
        return

    app_id = _get_or_scrape_qobuz_app_id(target)
    if not app_id:
        return

    print("Verifying...")
    ok, detail, display_name = verify_qobuz_token(app_id, token)
    if not ok:
        print(f"Verification failed ({detail}); nothing was saved.")
        return

    try:
        seed_secret(target, QOBUZ_TOKEN, token)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not save the token ({exc}).")
        return

    email = input("Qobuz email (optional, just for display): ").strip()
    updates = {"qobuz_auth_kind": "token", "qobuz_token_saved": True, "qobuz_app_id": app_id}
    if email:
        updates["qobuz_email"] = email
    merge_settings(target.config_dir, updates)
    who = f" as {display_name}" if display_name else ""
    print(f"Qobuz verified{who} and token saved.")
    print("Done -- (re)start Harmony.")


def _find_qobuz_token_in_browsers() -> str | None:
    """Find a Qobuz token across browsers: cookies first (the reliable path), then localStorage."""

    def _scan(sources, reader) -> str | None:
        for _label, pattern in sources:
            for path in _glob_paths(pattern):
                try:
                    token = reader(path)
                except Exception:  # noqa: BLE001 - try the next profile
                    token = None
                if token:
                    return token
        return None

    # Cookies (qobuz-session) — where the auth token actually lives.
    if token := _scan(FIREFOX_COOKIE_SOURCES, read_firefox_qobuz_cookie):
        return token
    chrome_pw = get_chrome_safe_storage_password()
    return _scan(CHROME_COOKIE_SOURCES, lambda p: read_chrome_qobuz_cookie(p, chrome_pw))


def _qobuz_browser_autograb(target: Target) -> None:
    if not ensure_keyring_for_target(target):
        return
    if try_import("cryptography") is None:
        print("\nReading a Chrome/Chromium session needs the 'cryptography' package.")
        if offer_venv_bootstrap(["cryptography"]):
            return  # process is being replaced
        print("Install it (`pip install cryptography`) and retry, or use paste-token.\n")
    print("Looking for a Qobuz session in your browsers (experimental)...")
    token = _find_qobuz_token_in_browsers()
    if not token:
        print("Could not find a Qobuz token in Firefox or Chrome. Make sure you're signed in")
        print("at play.qobuz.com, then try again, or use the paste-token option.")
        return

    app_id = _get_or_scrape_qobuz_app_id(target)
    if not app_id:
        return

    print("Verifying...")
    ok, detail, display_name = verify_qobuz_token(app_id, token)
    if not ok:
        print(f"Verification failed ({detail}); nothing was saved.")
        return

    try:
        seed_secret(target, QOBUZ_TOKEN, token)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not save the token ({exc}).")
        return

    merge_settings(
        target.config_dir, {"qobuz_auth_kind": "token", "qobuz_token_saved": True, "qobuz_app_id": app_id}
    )
    who = f" as {display_name}" if display_name else ""
    print(f"Qobuz verified{who} and token saved.")
    print("Done -- (re)start Harmony.")


def qobuz_menu(target: Target) -> None:
    while True:
        print("\nQobuz")
        print("  [1] Password login")
        print("  [2] Paste token (Google/social accounts)")
        print("  [3] Auto-grab from browser (Firefox/Chrome, experimental)")
        print("  [b] Back")
        choice = input("> ").strip().lower()
        if choice == "1":
            _qobuz_password_login(target)
        elif choice == "2":
            _qobuz_paste_token(target)
        elif choice == "3":
            _qobuz_browser_autograb(target)
        elif choice in ("b", "back", ""):
            return
        else:
            print("Not a valid choice.")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

BANNER = """\
Harmony account setup
======================
Bootstraps YouTube Music and Qobuz credentials into a Flatpak or source
install of Harmony. Nothing is ever printed or logged -- credentials are
verified against the real service before anything is written.
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harmony-setup.py",
        description="Interactive account setup for Harmony (YouTube Music + Qobuz).",
        epilog=(
            "One-liner:\n"
            "  curl -fsSL https://raw.githubusercontent.com/marthofdoom/harmony/main/scripts/"
            "harmony-setup.py | python3\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target",
        choices=["auto", "flatpak", "source"],
        default="auto",
        help="Which Harmony install to seed credentials into (default: auto-detect, asking if ambiguous).",
    )
    return parser


def reattach_tty_stdin() -> None:
    """Make interactive prompts work under ``curl … | python3``.

    When the script is piped into the interpreter, ``sys.stdin`` is the pipe
    carrying the script's own source, so the very first ``input()`` sees EOF
    and the tool aborts before asking anything (the reported "Aborted."
    immediately after the banner). Re-open stdin on the controlling terminal so
    prompts read the user's keystrokes instead. ``getpass`` already reads
    ``/dev/tty`` directly, so password entry was never affected -- this only
    fixes the plain ``input()`` menu/choice prompts.
    """
    if sys.stdin is not None and sys.stdin.isatty():
        return
    try:
        sys.stdin = open("/dev/tty")  # noqa: SIM115 - kept open for the process lifetime
    except OSError:
        raise SystemExit(
            "This setup tool is interactive, but stdin is not a terminal and no\n"
            "controlling terminal (/dev/tty) is available. Download it and run it\n"
            "directly instead of piping it in:\n"
            "  curl -fsSLO https://raw.githubusercontent.com/marthofdoom/harmony/main/scripts/harmony-setup.py\n"
            "  python3 harmony-setup.py"
        ) from None


_ALL_DEPS = ["requests", "ytmusicapi", "cryptography", "keyring", "secretstorage"]


def ensure_dependencies() -> None:
    """Front-load every dependency once, so no flow prompts for one mid-way.

    Offers to install into the current venv (or a throwaway) and re-execs with
    them present. Declining is allowed — individual flows still degrade — but
    the default is a complete environment.
    """
    missing = [d for d in _ALL_DEPS if try_import(d) is None]
    if not missing:
        return
    print(f"\nHarmony setup uses: {', '.join(missing)}.")
    if offer_venv_bootstrap(missing):
        return  # process is being replaced
    print("Continuing without them — some options may be unavailable.\n")


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    print(BANNER)
    reattach_tty_stdin()
    ensure_dependencies()
    try:
        target = resolve_target(args)
        print(f"Setting up: {target.kind} install -> {target.config_dir}\n")
        while True:
            print("\n[1] YouTube Music  [2] Qobuz  [q] quit")
            choice = input("> ").strip().lower()
            if choice == "1":
                ytmusic_menu(target)
            elif choice == "2":
                qobuz_menu(target)
            elif choice in ("q", "quit", "exit"):
                break
            else:
                print("Not a valid choice.")
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
