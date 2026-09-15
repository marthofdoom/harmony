"""Full instances copy credentials from a key-matching peer, encrypted by the
personal key — adopting only *working* connections and never clobbering a
working local one (no real secrets touched)."""

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


def _engine(working: dict[str, bool] | None = None) -> Engine:
    """An Engine with its service-working probe stubbed (no real providers)."""
    e = Engine()
    e._service_working = lambda: dict(working or {})  # type: ignore[method-assign]
    return e


def test_cryptobox_roundtrip_and_wrong_key() -> None:
    env = encrypt_json({"secrets": {"a": "b"}}, "shared-key")
    assert "token" in env and "secrets" not in env  # payload is opaque
    assert decrypt_json(env, "shared-key") == {"secrets": {"a": "b"}}
    with pytest.raises(Exception):  # noqa: B017,PT011 - wrong key must fail
        decrypt_json(env, "wrong-key")


def test_export_is_encrypted_then_adopts(monkeypatch: pytest.MonkeyPatch) -> None:
    import harmony.config as config

    monkeypatch.setattr(config, "CredentialStore", _FakeCS)

    # Source holds a WORKING Qobuz connection; export it (encrypted, not plaintext).
    _FakeCS.store = {"qobuz.user_auth_token": "TOK", "qobuz.app_secret": "SEC"}
    _use_settings(monkeypatch, _FakeSettings())
    envelope = _engine({"qobuz": True}).export_credentials()
    assert "token" in envelope and "secrets" not in envelope

    payload = decrypt_json(envelope, "shared-key")
    assert set(payload["secrets"]) == {"qobuz.user_auth_token", "qobuz.app_secret"}
    assert payload["working"] == {"qobuz": True}  # advertises what works

    # Target has NO working Qobuz -> adopts it and becomes an independent holder.
    _FakeCS.store = {}
    target = _FakeSettings()
    target.qobuz_token_saved = False
    _use_settings(monkeypatch, target)
    result = _engine({}).import_credentials(payload)
    assert _FakeCS.store["qobuz.user_auth_token"] == "TOK"
    assert target.qobuz_auth_kind == "token"
    assert "qobuz.user_auth_token" in result["imported"]
    assert result["synced"] == ["qobuz"]


# -- only sync working connections; never clobber a working local one ---------


def test_import_skips_a_service_not_working_on_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    import harmony.config as config

    monkeypatch.setattr(config, "CredentialStore", _FakeCS)
    _FakeCS.store = {"qobuz.user_auth_token": "OLD"}  # local, untouched
    _use_settings(monkeypatch, _FakeSettings())
    payload = {"secrets": {"qobuz.user_auth_token": "FROM_PEER"},
               "settings": {}, "ytmusic_auth": None,
               "working": {"qobuz": False}}  # source says Qobuz is NOT working
    result = _engine({}).import_credentials(payload)
    assert _FakeCS.store["qobuz.user_auth_token"] == "OLD"   # a broken source cred isn't pulled
    assert result["synced"] == []
    assert "qobuz.user_auth_token" not in result["imported"]


def test_import_keeps_a_working_local_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    import harmony.config as config

    monkeypatch.setattr(config, "CredentialStore", _FakeCS)
    _FakeCS.store = {"qobuz.user_auth_token": "MINE"}  # my working token
    _use_settings(monkeypatch, _FakeSettings())
    payload = {"secrets": {"qobuz.user_auth_token": "PEERS"},
               "settings": {}, "ytmusic_auth": None,
               "working": {"qobuz": True}}  # source also works — but don't clobber mine
    result = _engine({"qobuz": True}).import_credentials(payload)  # local Qobuz works
    assert _FakeCS.store["qobuz.user_auth_token"] == "MINE"  # not clobbered
    assert result["synced"] == [] and result["kept"] == ["qobuz"]


def test_import_adopts_a_working_source_when_local_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import harmony.config as config

    monkeypatch.setattr(config, "CredentialStore", _FakeCS)
    _FakeCS.store = {}
    _use_settings(monkeypatch, _FakeSettings())
    payload = {"secrets": {"qobuz.user_auth_token": "GOOD"},
               "settings": {"qobuz_auth_kind": "token"}, "ytmusic_auth": None,
               "working": {"qobuz": True}}
    result = _engine({}).import_credentials(payload)  # nothing working locally
    assert _FakeCS.store["qobuz.user_auth_token"] == "GOOD"
    assert result["synced"] == ["qobuz"]


# -- credential-copy hardening: auth_kind always matches an actual token ------


def _use_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import harmony.config as config

    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)


def test_export_labels_yt_kind_by_the_real_token(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import harmony.config as config

    monkeypatch.setattr(config, "CredentialStore", _FakeCS)
    _FakeCS.store = {}
    src = _FakeSettings()
    src.ytmusic_auth_kind = "oauth"  # claims oauth, but the file is browser headers
    authf = tmp_path / "browser.json"
    authf.write_text('{"cookie": "SID=x", "authorization": "y"}', "utf-8")
    src.ytmusic_auth_file = str(authf)
    _use_settings(monkeypatch, src)

    payload = decrypt_json(_engine({"ytmusic": True}).export_credentials(), "shared-key")
    assert payload["settings"]["ytmusic_auth_kind"] == "browser"  # corrected to the file


def test_import_ignores_oauth_label_without_a_token(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import harmony.config as config

    monkeypatch.setattr(config, "CredentialStore", _FakeCS)
    _use_config_dir(monkeypatch, tmp_path)
    _FakeCS.store = {}
    target = _FakeSettings()
    target.ytmusic_auth_kind = "browser"
    target.ytmusic_auth_file = "keep.json"
    _use_settings(monkeypatch, target)

    _engine({}).import_credentials(
        {"secrets": {}, "settings": {"ytmusic_auth_kind": "oauth", "ytmusic_oauth_client_id": "cid"},
         "ytmusic_auth": None})
    assert target.ytmusic_auth_kind == "browser"   # not relabeled to a phantom oauth
    assert target.ytmusic_auth_file == "keep.json"  # left untouched


def test_import_oauth_token_writes_file_and_drops_stale(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import harmony.config as config

    monkeypatch.setattr(config, "CredentialStore", _FakeCS)
    _use_config_dir(monkeypatch, tmp_path)
    _FakeCS.store = {}
    target = _FakeSettings()
    stale = tmp_path / "browser.json"
    stale.write_text("stale cookies", "utf-8")
    target.ytmusic_auth_file = str(stale)
    target.ytmusic_auth_kind = "browser"
    _use_settings(monkeypatch, target)

    token = '{"refresh_token": "RT", "access_token": "AT"}'
    _engine({}).import_credentials({"secrets": {}, "settings": {"ytmusic_auth_kind": "oauth"},
                                    "ytmusic_auth": token})
    new = tmp_path / "ytmusic-auth.json"
    assert new.read_text("utf-8") == token
    assert target.ytmusic_auth_file == str(new)
    assert target.ytmusic_auth_kind == "oauth"
    assert not stale.exists()  # superseded auth file removed
