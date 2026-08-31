"""Full instances copy credentials from a key-matching peer, encrypted by the
personal key (no real secrets touched)."""

from __future__ import annotations

import pytest

from harmony.cryptobox import decrypt_json, encrypt_json
from harmony.web.api import Engine


class _FakeCS:
    store: dict = {}

    def get(self, k: str):
        return _FakeCS.store.get(k)

    def set(self, k: str, v: str) -> None:
        _FakeCS.store[k] = v


class _FakeSettings:
    def __init__(self) -> None:
        self.qobuz_auth_kind = "token"
        self.qobuz_token_saved = True
        self.ytmusic_auth_kind = "browser"
        self.ytmusic_oauth_client_id = "cid"
        self.ytmusic_auth_file = ""
        self.personal_key = "shared-key"

    def save(self) -> None:
        pass


def _use_settings(monkeypatch: pytest.MonkeyPatch, s: _FakeSettings) -> None:
    import harmony.config as config

    monkeypatch.setattr(config, "Settings", type("S", (), {"load": staticmethod(lambda: s)}))


def test_cryptobox_roundtrip_and_wrong_key() -> None:
    env = encrypt_json({"secrets": {"a": "b"}}, "shared-key")
    assert "token" in env and "secrets" not in env  # payload is opaque
    assert decrypt_json(env, "shared-key") == {"secrets": {"a": "b"}}
    with pytest.raises(Exception):  # noqa: B017,PT011 - wrong key must fail
        decrypt_json(env, "wrong-key")


def test_export_is_encrypted_then_adopts(monkeypatch: pytest.MonkeyPatch) -> None:
    import harmony.config as config

    monkeypatch.setattr(config, "CredentialStore", _FakeCS)

    # Source holds credentials; export them (encrypted envelope, not plaintext).
    _FakeCS.store = {"qobuz.user_auth_token": "TOK", "qobuz.app_secret": "SEC"}
    _use_settings(monkeypatch, _FakeSettings())
    envelope = Engine().export_credentials()
    assert "token" in envelope and "secrets" not in envelope

    # Only the matching key decrypts it back to the real payload.
    payload = decrypt_json(envelope, "shared-key")
    assert set(payload["secrets"]) == {"qobuz.user_auth_token", "qobuz.app_secret"}
    assert payload["settings"]["qobuz_auth_kind"] == "token"

    # Target imports the decrypted payload and becomes an independent holder.
    _FakeCS.store = {}
    target = _FakeSettings()
    target.qobuz_token_saved = False
    _use_settings(monkeypatch, target)
    result = Engine().import_credentials(payload)
    assert _FakeCS.store["qobuz.user_auth_token"] == "TOK"
    assert target.qobuz_auth_kind == "token"
    assert "qobuz.user_auth_token" in result["imported"]
