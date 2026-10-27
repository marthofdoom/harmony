"""The suite must never touch the developer's real Harmony profile or keyring
(a past run wrote test values into the real ``~/.config/harmony/settings.json``).
See the autouse fixture in ``conftest.py``."""

from __future__ import annotations

import os
from pathlib import Path

from harmony import config


def _real_home() -> Path:
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def test_profile_dirs_resolve_inside_the_per_test_sandbox(tmp_path):
    real = _real_home()
    for fn in (config.config_dir, config.data_dir, config.cache_dir):
        path = fn()
        assert path.is_dir()
        assert real / ".config" not in path.parents and real / ".local" not in path.parents
        assert real / ".cache" not in path.parents
        assert "harmony-tests-" in str(path) or "host" in str(path)
    assert Path.home() != real


def test_unpatched_settings_save_lands_in_the_sandbox():
    s = config.Settings.load()
    s.qobuz_app_id = "isolation-canary"
    s.save()
    assert config.settings_path().read_text().count("isolation-canary") == 1
    assert _real_home() / ".config" not in config.settings_path().parents


def test_keyring_is_the_null_backend():
    import keyring

    assert os.environ["PYTHON_KEYRING_BACKEND"] == "keyring.backends.fail.Keyring"
    assert type(keyring.get_keyring()).__module__ == "keyring.backends.fail"
    assert config.CredentialStore()._backend_ok is False
