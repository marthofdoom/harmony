"""Passphrase-derived encryption for credential copies (engine layer, no GTK).

When a full instance copies credentials from a peer, the payload is encrypted
with a key derived from the shared **personal key**, so the secrets are
confidential on the wire even over plain HTTP — the personal key both authorizes
the request (the header gate) and encrypts what crosses. Uses PBKDF2-HMAC-SHA256
→ Fernet (AES-128-CBC + HMAC), from ``cryptography`` (already present via
keyring).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

_ITERATIONS = 200_000


def _key_from(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt_json(obj: Any, passphrase: str) -> dict[str, Any]:
    """Encrypt ``obj`` under a key derived from ``passphrase`` + a fresh salt."""
    from cryptography.fernet import Fernet

    salt = os.urandom(16)
    token = Fernet(_key_from(passphrase, salt)).encrypt(json.dumps(obj).encode("utf-8"))
    return {"v": 1, "salt": base64.b64encode(salt).decode("ascii"), "token": token.decode("ascii")}


def decrypt_json(envelope: dict[str, Any], passphrase: str) -> Any:
    """Reverse :func:`encrypt_json`. Raises on a wrong passphrase / tampering."""
    from cryptography.fernet import Fernet

    salt = base64.b64decode(envelope["salt"])
    plaintext = Fernet(_key_from(passphrase, salt)).decrypt(envelope["token"].encode("ascii"))
    return json.loads(plaintext)
