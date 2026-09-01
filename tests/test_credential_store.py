"""The file-fallback credential store is encrypted at rest with the personal key."""

from __future__ import annotations

import json

import pytest

import harmony.config as config


def _file_store(tmp_path, monkeypatch: pytest.MonkeyPatch, passphrase: str | None):
    path = tmp_path / "secrets.json"
    monkeypatch.setattr(config, "_fallback_secrets_path", lambda: path)
    cs = config.CredentialStore()
    cs._backend_ok = False  # force the file fallback (no keyring)
    monkeypatch.setattr(cs, "_passphrase", lambda: passphrase)
    return cs, path


def test_file_store_encrypts_with_the_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cs, path = _file_store(tmp_path, monkeypatch, "pw1")
    cs.set("qobuz.user_auth_token", "SECRET123")

    raw = json.loads(path.read_text())
    assert "token" in raw and "salt" in raw            # an encrypted envelope
    assert "SECRET123" not in json.dumps(raw)          # not plaintext on disk
    assert cs.get("qobuz.user_auth_token") == "SECRET123"


def test_wrong_key_cannot_read(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cs, path = _file_store(tmp_path, monkeypatch, "pw1")
    cs.set("qobuz.user_auth_token", "SECRET123")
    monkeypatch.setattr(cs, "_passphrase", lambda: "wrong-key")
    assert cs.get("qobuz.user_auth_token") is None


def test_no_key_stores_plaintext(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Before a personal key is set, fall back to plaintext 0600 (still readable).
    cs, path = _file_store(tmp_path, monkeypatch, None)
    cs.set("qobuz.user_auth_token", "SECRET123")
    assert cs.get("qobuz.user_auth_token") == "SECRET123"
    assert "SECRET123" in path.read_text()  # plaintext when no key
