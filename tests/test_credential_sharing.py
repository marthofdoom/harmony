"""Full instances copy credentials from a key-matching peer (no real secrets)."""

from __future__ import annotations

import pytest

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
        self.personal_key = "k"

    def save(self) -> None:
        pass


def _use_settings(monkeypatch: pytest.MonkeyPatch, s: _FakeSettings) -> None:
    import harmony.config as config

    monkeypatch.setattr(config, "Settings", type("S", (), {"load": staticmethod(lambda: s)}))


def test_export_import_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    import harmony.config as config

    monkeypatch.setattr(config, "CredentialStore", _FakeCS)

    # Source instance holds credentials; export them.
    _FakeCS.store = {"qobuz.user_auth_token": "TOK", "qobuz.app_secret": "SEC"}
    _use_settings(monkeypatch, _FakeSettings())
    exported = Engine().export_credentials()
    assert set(exported["secrets"]) == {"qobuz.user_auth_token", "qobuz.app_secret"}
    assert exported["settings"]["qobuz_auth_kind"] == "token"

    # Target instance (fresh store) imports and becomes an independent holder.
    _FakeCS.store = {}
    target = _FakeSettings()
    target.qobuz_token_saved = False
    _use_settings(monkeypatch, target)
    result = Engine().import_credentials(exported)
    assert _FakeCS.store["qobuz.user_auth_token"] == "TOK"
    assert target.qobuz_auth_kind == "token"  # settings copied too
    assert "qobuz.user_auth_token" in result["imported"]
