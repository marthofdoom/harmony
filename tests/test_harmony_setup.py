"""Offline tests for scripts/harmony-setup.py.

The script lives outside the ``harmony`` package (it's a standalone,
self-contained host-side tool) and its filename has a hyphen, so it's loaded
by path via ``importlib`` rather than a normal package import. Everything
here is pure-logic / offline: no network, no real browser, no real keyring.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "harmony-setup.py"
_spec = importlib.util.spec_from_file_location("harmony_setup", _SCRIPT_PATH)
harmony_setup = importlib.util.module_from_spec(_spec)
sys.modules["harmony_setup"] = harmony_setup
_spec.loader.exec_module(harmony_setup)


# --------------------------------------------------------------------------
# Chrome cookie decryption
# --------------------------------------------------------------------------


def _chrome_encrypt(plaintext: str, password: bytes, prefix: bytes) -> bytes:
    """Encrypt ``plaintext`` with the exact algorithm/params chrome_decrypt expects."""
    import hashlib

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1, 16)
    iv = b" " * 16
    data = plaintext.encode("utf-8")
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return prefix + ciphertext


@pytest.mark.parametrize("prefix", [b"v10", b"v11"])
def test_chrome_decrypt_recovers_known_plaintext(prefix):
    pytest.importorskip("cryptography")
    password = b"peanuts"
    plaintext = "SID=abc123; a cookie value with spaces and punctuation!"
    encrypted = _chrome_encrypt(plaintext, password, prefix)
    assert harmony_setup.chrome_decrypt(encrypted, password) == plaintext


def test_chrome_decrypt_rejects_unknown_prefix():
    pytest.importorskip("cryptography")
    with pytest.raises(ValueError):
        harmony_setup.chrome_decrypt(b"v09" + b"0" * 16, b"peanuts")


def test_chrome_decrypt_v10_strips_leading_domain_hash_when_present():
    """Some Chrome versions prepend a 32-byte SHA256 domain hash for v10
    cookies; chrome_decrypt should recover the real value by noticing the
    direct decode fails and falling back to stripping those 32 bytes.
    """
    import hashlib as _hashlib

    pytest.importorskip("cryptography")
    password = b"peanuts"
    real_value = "a-real-cookie-value"
    # A genuine SHA256 digest is virtually guaranteed to not be valid UTF-8
    # when directly prefixed onto ASCII text (unlike bytes 0-31, which are
    # all valid single-byte UTF-8 on their own).
    fake_hash = _hashlib.sha256(b"youtube.com").digest()
    payload = fake_hash + real_value.encode("utf-8")
    with pytest.raises(UnicodeDecodeError):
        payload.decode("utf-8")

    encrypted = _chrome_encrypt_bytes(payload, password, b"v10")
    assert harmony_setup.chrome_decrypt(encrypted, password) == real_value


def _chrome_encrypt_bytes(data: bytes, password: bytes, prefix: bytes) -> bytes:
    import hashlib

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1, 16)
    iv = b" " * 16
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return prefix + ciphertext


# --------------------------------------------------------------------------
# Firefox cookie sqlite read
# --------------------------------------------------------------------------


def _build_firefox_cookies_db(path: Path, rows: list[tuple[str, str, str]]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT)"
    )
    for host, name, value in rows:
        conn.execute("INSERT INTO moz_cookies (host, name, value) VALUES (?, ?, ?)", (host, name, value))
    conn.commit()
    conn.close()


def test_read_firefox_cookies_filters_to_youtube_and_google(tmp_path):
    db_path = tmp_path / "cookies.sqlite"
    _build_firefox_cookies_db(
        db_path,
        [
            (".youtube.com", "SID", "yt-sid-value"),
            (".google.com", "SSID", "google-ssid-value"),
            ("music.youtube.com", "VISITOR_INFO1_LIVE", "visitor-value"),
            ("example.com", "unrelated", "should-not-appear"),
        ],
    )
    rows = harmony_setup.read_firefox_cookies(db_path)
    hosts = {r[0] for r in rows}
    names = {r[1] for r in rows}
    assert hosts == {".youtube.com", ".google.com", "music.youtube.com"}
    assert "unrelated" not in names
    assert len(rows) == 3


def test_read_firefox_cookies_does_not_mutate_original_db(tmp_path):
    """read_firefox_cookies must copy to a temp file, not touch the original."""
    db_path = tmp_path / "cookies.sqlite"
    _build_firefox_cookies_db(db_path, [(".youtube.com", "SID", "value")])
    before = db_path.read_bytes()
    harmony_setup.read_firefox_cookies(db_path)
    after = db_path.read_bytes()
    assert before == after


def test_build_cookie_header_and_headers_raw():
    cookies = [(".youtube.com", "SID", "abc"), (".google.com", "SSID", "def")]
    header = harmony_setup.build_cookie_header(cookies)
    assert header == "SID=abc; SSID=def"
    headers_raw = harmony_setup.build_headers_raw(header)
    assert f"Cookie: {header}" in headers_raw
    assert "X-Goog-AuthUser: 0" in headers_raw
    assert "Origin: https://music.youtube.com" in headers_raw
    assert "User-Agent:" in headers_raw


# --------------------------------------------------------------------------
# settings.json merge
# --------------------------------------------------------------------------


def test_merge_settings_preserves_unrelated_keys_and_creates_dir(tmp_path):
    config_dir = tmp_path / "harmony"
    # No existing settings.json / config dir yet.
    path = harmony_setup.merge_settings(config_dir, {"qobuz_email": "me@example.com"})
    assert path == config_dir / "settings.json"
    data = json.loads(path.read_text("utf-8"))
    assert data["qobuz_email"] == "me@example.com"


def test_merge_settings_merges_without_clobbering(tmp_path):
    config_dir = tmp_path / "harmony"
    config_dir.mkdir(parents=True)
    existing = {
        "window_width": 1280,
        "match_high_threshold": 0.88,
        "qobuz_email": "old@example.com",
    }
    (config_dir / "settings.json").write_text(json.dumps(existing), "utf-8")

    harmony_setup.merge_settings(
        config_dir,
        {"qobuz_email": "new@example.com", "qobuz_auth_kind": "token", "qobuz_token_saved": True},
    )

    data = json.loads((config_dir / "settings.json").read_text("utf-8"))
    # Unrelated keys survive untouched.
    assert data["window_width"] == 1280
    assert data["match_high_threshold"] == 0.88
    # Updated/new keys land.
    assert data["qobuz_email"] == "new@example.com"
    assert data["qobuz_auth_kind"] == "token"
    assert data["qobuz_token_saved"] is True


def test_read_settings_missing_file_returns_empty_dict(tmp_path):
    assert harmony_setup.read_settings(tmp_path / "nope") == {}


# --------------------------------------------------------------------------
# seed_secret: Flatpak branch sends the secret via stdin, never argv
# --------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


def test_seed_secret_flatpak_uses_stdin_not_argv(monkeypatch):
    captured = {}

    def fake_run(args, input=None, text=None, capture_output=None, check=None):  # noqa: A002
        captured["args"] = args
        captured["input"] = input
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(harmony_setup.subprocess, "run", fake_run)

    target = harmony_setup.Target("flatpak", Path("/unused"))
    secret_value = "super-secret-token-value"
    harmony_setup.seed_secret(target, harmony_setup.QOBUZ_TOKEN, secret_value)

    assert captured["input"] == secret_value
    assert captured["args"][0] == "flatpak"
    assert captured["args"][1] == "run"
    assert "io.github.marthofdoom.Harmony" in captured["args"]
    # The secret must never appear as a literal argv element.
    assert not any(secret_value in str(a) for a in captured["args"])


def test_seed_secret_flatpak_raises_on_nonzero_exit(monkeypatch):
    def fake_run(args, input=None, text=None, capture_output=None, check=None):  # noqa: A002
        return _FakeCompletedProcess(returncode=1, stderr="boom")

    monkeypatch.setattr(harmony_setup.subprocess, "run", fake_run)
    target = harmony_setup.Target("flatpak", Path("/unused"))
    with pytest.raises(RuntimeError):
        harmony_setup.seed_secret(target, harmony_setup.QOBUZ_TOKEN, "value")


def test_seed_secret_source_uses_host_keyring(monkeypatch):
    calls = {}

    class FakeKeyring:
        @staticmethod
        def set_password(service, key, value):
            calls["service"] = service
            calls["key"] = key
            calls["value"] = value

    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring)

    target = harmony_setup.Target("source", Path("/unused"))
    harmony_setup.seed_secret(target, harmony_setup.QOBUZ_TOKEN, "the-token")

    assert calls == {
        "service": harmony_setup.KEYRING_SERVICE,
        "key": harmony_setup.QOBUZ_TOKEN,
        "value": "the-token",
    }


# --------------------------------------------------------------------------
# Target detection (flatpak vs source), with monkeypatched checks
# --------------------------------------------------------------------------


class _Args:
    def __init__(self, target="auto"):
        self.target = target


def test_detect_targets_flatpak_only(monkeypatch, tmp_path):
    flatpak_dir = tmp_path / "flatpak-config"
    source_dir = tmp_path / "source-config"
    monkeypatch.setattr(harmony_setup, "_flatpak_cli_installed", lambda: True)
    monkeypatch.setattr(harmony_setup, "_flatpak_config_dir", lambda: flatpak_dir)
    monkeypatch.setattr(harmony_setup, "_source_config_dir", lambda: source_dir)

    found = harmony_setup.detect_targets()
    assert len(found) == 1
    assert found[0].kind == "flatpak"
    assert found[0].config_dir == flatpak_dir


def test_detect_targets_source_only(monkeypatch, tmp_path):
    flatpak_dir = tmp_path / "flatpak-config"
    source_dir = tmp_path / "source-config"
    source_dir.mkdir()
    monkeypatch.setattr(harmony_setup, "_flatpak_cli_installed", lambda: False)
    monkeypatch.setattr(harmony_setup, "_flatpak_config_dir", lambda: flatpak_dir)
    monkeypatch.setattr(harmony_setup, "_source_config_dir", lambda: source_dir)

    found = harmony_setup.detect_targets()
    assert len(found) == 1
    assert found[0].kind == "source"


def test_detect_targets_none_found(monkeypatch, tmp_path):
    flatpak_dir = tmp_path / "flatpak-config"
    source_dir = tmp_path / "source-config"
    monkeypatch.setattr(harmony_setup, "_flatpak_cli_installed", lambda: False)
    monkeypatch.setattr(harmony_setup, "_flatpak_config_dir", lambda: flatpak_dir)
    monkeypatch.setattr(harmony_setup, "_source_config_dir", lambda: source_dir)

    assert harmony_setup.detect_targets() == []


def test_resolve_target_explicit_override_skips_detection(monkeypatch, tmp_path):
    flatpak_dir = tmp_path / "flatpak-config"
    monkeypatch.setattr(harmony_setup, "_flatpak_config_dir", lambda: flatpak_dir)
    # Detection should never even be consulted for an explicit --target.
    monkeypatch.setattr(
        harmony_setup,
        "detect_targets",
        lambda: (_ for _ in ()).throw(AssertionError("detect_targets should not be called")),
    )
    target = harmony_setup.resolve_target(_Args(target="flatpak"))
    assert target.kind == "flatpak"
    assert target.config_dir == flatpak_dir


def test_resolve_target_ambiguous_prompts_and_honours_choice(monkeypatch, tmp_path):
    flatpak_dir = tmp_path / "flatpak-config"
    source_dir = tmp_path / "source-config"
    flatpak_dir.mkdir()
    source_dir.mkdir()
    monkeypatch.setattr(harmony_setup, "_flatpak_cli_installed", lambda: True)
    monkeypatch.setattr(harmony_setup, "_flatpak_config_dir", lambda: flatpak_dir)
    monkeypatch.setattr(harmony_setup, "_source_config_dir", lambda: source_dir)
    monkeypatch.setattr("builtins.input", lambda *_a: "2")

    target = harmony_setup.resolve_target(_Args(target="auto"))
    assert target.kind == "source"
    assert target.config_dir == source_dir


def test_resolve_target_none_found_asks_and_defaults_to_flatpak(monkeypatch, tmp_path):
    flatpak_dir = tmp_path / "flatpak-config"
    source_dir = tmp_path / "source-config"
    monkeypatch.setattr(harmony_setup, "_flatpak_cli_installed", lambda: False)
    monkeypatch.setattr(harmony_setup, "_flatpak_config_dir", lambda: flatpak_dir)
    monkeypatch.setattr(harmony_setup, "_source_config_dir", lambda: source_dir)
    monkeypatch.setattr("builtins.input", lambda *_a: "1")

    target = harmony_setup.resolve_target(_Args(target="auto"))
    assert target.kind == "flatpak"


# --------------------------------------------------------------------------
# Misc pure-logic helpers
# --------------------------------------------------------------------------


def test_deep_search_token_finds_nested_double_encoded_value():
    inner = json.dumps({"someAuthToken": "x" * 25})
    outer = {"localuser": inner}
    assert harmony_setup._deep_search_token(outer) == "x" * 25


def test_deep_search_token_ignores_short_values():
    outer = {"authToken": "short"}
    assert harmony_setup._deep_search_token(outer) is None


def test_glob_paths_expands_user_and_globs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    profile_dir = tmp_path / "profile1"
    profile_dir.mkdir()
    (profile_dir / "cookies.sqlite").write_bytes(b"")
    found = harmony_setup._glob_paths("~/profile*/cookies.sqlite")
    assert found == [profile_dir / "cookies.sqlite"]


# --------------------------------------------------------------------------
# Temp-copy permissions (a world-readable copy would leak plaintext cookies)
# --------------------------------------------------------------------------


def test_copy_sqlite_to_temp_is_0600_even_from_world_readable_source(tmp_path):
    import os
    import stat

    src = tmp_path / "cookies.sqlite"
    src.write_bytes(b"not-really-sqlite-but-fine-for-a-copy")
    os.chmod(src, 0o644)  # browsers commonly leave these 0644

    dest = harmony_setup._copy_sqlite_to_temp(src)
    try:
        mode = stat.S_IMODE(os.stat(dest).st_mode)
        # The copy must NOT inherit the source's 0644 — only the owner may read it.
        assert mode == 0o600, oct(mode)
    finally:
        os.unlink(dest)


# --------------------------------------------------------------------------
# curl | python3 must not abort: interactive prompts read /dev/tty, not the pipe
# --------------------------------------------------------------------------


class _FakeTTY:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_reattach_tty_stdin_noop_when_already_a_terminal(monkeypatch):
    already = _FakeTTY(tty=True)
    monkeypatch.setattr(harmony_setup.sys, "stdin", already)
    harmony_setup.reattach_tty_stdin()
    assert harmony_setup.sys.stdin is already  # left untouched


def test_reattach_tty_stdin_reopens_from_dev_tty_when_piped(monkeypatch):
    monkeypatch.setattr(harmony_setup.sys, "stdin", _FakeTTY(tty=False))
    sentinel = object()
    seen = {}

    def _fake_open(path, *a, **k):
        seen["path"] = path
        return sentinel

    monkeypatch.setattr("builtins.open", _fake_open)
    harmony_setup.reattach_tty_stdin()
    assert seen["path"] == "/dev/tty"
    assert harmony_setup.sys.stdin is sentinel  # reattached to the terminal


def test_reattach_tty_stdin_exits_cleanly_without_a_terminal(monkeypatch):
    monkeypatch.setattr(harmony_setup.sys, "stdin", _FakeTTY(tty=False))

    def _no_tty(path, *a, **k):
        raise OSError("no controlling terminal")

    monkeypatch.setattr("builtins.open", _no_tty)
    with pytest.raises(SystemExit):
        harmony_setup.reattach_tty_stdin()


# --------------------------------------------------------------------------
# Dependency bootstrap: install into the CURRENT venv, not only a throwaway one
# --------------------------------------------------------------------------


def test_offer_venv_bootstrap_installs_into_current_env_when_in_a_venv(monkeypatch):
    calls = {}
    monkeypatch.setattr(harmony_setup, "_in_virtualenv", lambda: True)
    monkeypatch.setattr(harmony_setup, "_install_into_current_env", lambda pkgs: calls.setdefault("current", pkgs) or True)
    monkeypatch.setattr(harmony_setup, "_bootstrap_throwaway_venv", lambda pkgs: calls.setdefault("throwaway", pkgs) or True)
    harmony_setup.offer_venv_bootstrap(["cryptography"])
    assert calls == {"current": ["cryptography"]}  # current env used, throwaway untouched


def test_offer_venv_bootstrap_builds_throwaway_venv_when_not_in_a_venv(monkeypatch):
    calls = {}
    monkeypatch.setattr(harmony_setup, "_in_virtualenv", lambda: False)
    monkeypatch.setattr(harmony_setup, "_install_into_current_env", lambda pkgs: calls.setdefault("current", pkgs) or True)
    monkeypatch.setattr(harmony_setup, "_bootstrap_throwaway_venv", lambda pkgs: calls.setdefault("throwaway", pkgs) or True)
    harmony_setup.offer_venv_bootstrap(["ytmusicapi"])
    assert calls == {"throwaway": ["ytmusicapi"]}


def test_install_into_current_env_declined_prints_command_and_returns_false(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_a: "n")
    ran = {"pip": False}
    monkeypatch.setattr(harmony_setup.subprocess, "run", lambda *a, **k: ran.__setitem__("pip", True))
    result = harmony_setup._install_into_current_env(["cryptography", "keyring"])
    assert result is False
    assert ran["pip"] is False  # declining must not shell out
    assert "pip install cryptography keyring" in capsys.readouterr().out


# --------------------------------------------------------------------------
# YouTube Music browser auth needs an Authorization: SAPISIDHASH header
# (ytmusicapi >= 1.12 otherwise treats the extracted file as OAuth)
# --------------------------------------------------------------------------


def test_find_sapisid_prefers_sapisid_then_secure_variants():
    cookies = [("h", "__Secure-3PAPISID", "third"), ("h", "SAPISID", "plain")]
    assert harmony_setup.find_sapisid(cookies) == "plain"
    assert harmony_setup.find_sapisid([("h", "__Secure-3PAPISID", "third")]) == "third"
    assert harmony_setup.find_sapisid([("h", "FOO", "bar")]) is None


def test_sapisid_hash_format():
    val = harmony_setup.sapisid_hash("secret")
    assert val.startswith("SAPISIDHASH ")
    ts, _, digest = val[len("SAPISIDHASH "):].partition("_")
    assert ts.isdigit()
    assert len(digest) == 40 and all(c in "0123456789abcdef" for c in digest)


def test_build_headers_raw_includes_authorization_when_sapisid_present():
    raw = harmony_setup.build_headers_raw("SAPISID=x; FOO=bar", sapisid="x")
    assert "Authorization: SAPISIDHASH " in raw
    assert "Cookie: SAPISID=x; FOO=bar" in raw


def test_build_headers_raw_omits_authorization_without_sapisid():
    assert "Authorization" not in harmony_setup.build_headers_raw("FOO=bar")


def test_browser_auth_file_is_classified_as_browser_by_ytmusicapi(tmp_path):
    ytmusicapi = pytest.importorskip("ytmusicapi")
    raw = harmony_setup.build_headers_raw("SAPISID=abc; __Secure-3PAPISID=abc", sapisid="abc")
    path = tmp_path / "browser.json"
    ytmusicapi.setup(filepath=str(path), headers_raw=raw)
    from ytmusicapi import YTMusic
    from ytmusicapi.auth.types import AuthType
    assert YTMusic(auth=str(path)).auth_type == AuthType.BROWSER


# --------------------------------------------------------------------------
# Qobuz token retrieval from Chrome/Chromium Local Storage (LevelDB byte scan)
# --------------------------------------------------------------------------


def test_read_chrome_qobuz_localstorage_extracts_scoped_token(tmp_path):
    leveldb = tmp_path / "leveldb"
    leveldb.mkdir()
    # A different site's token first (must NOT be picked), then far away the
    # qobuz origin marker immediately followed by its localuser JSON.
    blob = (
        b'_https://accounts.google.com\x00\x01auth_token":"OTHERSITEtokenmustnotbepicked999"'
        + b"\x00" * 20000
        + b'_https://play.qobuz.com\x00\x01localuser\x01{"user_auth_token":"QOBUZtok3nABCDEFGH1234567890"}'
    )
    (leveldb / "000005.log").write_bytes(blob)

    assert harmony_setup.read_chrome_qobuz_localstorage(leveldb) == "QOBUZtok3nABCDEFGH1234567890"


def test_read_chrome_qobuz_localstorage_none_when_no_qobuz(tmp_path):
    leveldb = tmp_path / "leveldb"
    leveldb.mkdir()
    (leveldb / "x.ldb").write_bytes(b"just some leveldb noise, no qobuz origin here")

    assert harmony_setup.read_chrome_qobuz_localstorage(leveldb) is None


def test_find_qobuz_token_in_browsers_falls_through_to_chrome(monkeypatch):
    monkeypatch.setattr(harmony_setup, "FIREFOX_STORAGE_SOURCES", [])
    monkeypatch.setattr(harmony_setup, "CHROME_STORAGE_SOURCES", [("Chrome", "/fake/*/leveldb")])
    monkeypatch.setattr(harmony_setup, "_glob_paths", lambda pattern: [harmony_setup.Path("/fake/p/leveldb")])
    monkeypatch.setattr(harmony_setup, "read_chrome_qobuz_localstorage", lambda p: "TOKENfromChrome")

    assert harmony_setup._find_qobuz_token_in_browsers() == "TOKENfromChrome"


def test_qobuz_token_from_cookie_value_json_and_bare():
    j = "%7B%22user_auth_token%22%3A%22TOKtok123456789012345%22%7D"  # url-enc {"user_auth_token":"..."}
    assert harmony_setup._qobuz_token_from_cookie_value(j) == "TOKtok123456789012345"
    assert harmony_setup._qobuz_token_from_cookie_value("BAREtoken1234567890abcdef") == "BAREtoken1234567890abcdef"
    assert harmony_setup._qobuz_token_from_cookie_value("short") is None
