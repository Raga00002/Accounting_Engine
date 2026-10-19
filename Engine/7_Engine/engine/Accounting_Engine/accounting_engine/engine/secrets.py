"""Password hashing and encryption of stored secrets (user API keys, payment keys).

* Passwords: PBKDF2-HMAC-SHA256, 600,000 iterations, 16-byte random salt (stdlib only).
* Secrets at rest: Fernet (AES-128-CBC + HMAC) with a server key. The key comes from the
  ``AE_SECRET_KEY`` environment variable; if unset, one is generated once and kept in
  ``data/master.key`` (mode 600). Losing that key makes stored API keys unreadable – users
  simply re-enter them; nothing else depends on it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as _secrets
from pathlib import Path
from typing import Optional

from . import config

_ITER = 600_000


def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    salt = _secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored or not password:
        return False
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
    return hmac.compare_digest(dk, expected)


def new_token(nbytes: int = 32) -> str:
    return _secrets.token_urlsafe(nbytes)


def temp_password(length: int = 12) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(_secrets.choice(alphabet) for _ in range(length))


# --------------------------------------------------------------------------- #
# Fernet for secrets at rest
# --------------------------------------------------------------------------- #
def _key_file() -> Path:
    return config.DATA_DIR / "master.key"


def server_key() -> bytes:
    env = os.getenv("AE_SECRET_KEY", "").strip()
    if env:
        raw = env.encode("utf-8")
        # accept any string: derive a proper 32-byte urlsafe-base64 key from it
        return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    kf = _key_file()
    if kf.exists():
        return kf.read_bytes().strip()
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    kf.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(kf), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # private from the first byte
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def encrypt(text: Optional[str]) -> Optional[str]:
    if text is None or text == "":
        return None
    from cryptography.fernet import Fernet

    return Fernet(server_key()).encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    from cryptography.fernet import Fernet, InvalidToken

    try:
        return Fernet(server_key()).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def mask(secret: Optional[str], keep: int = 4) -> str:
    if not secret:
        return "—"
    return "…" + secret[-keep:] if len(secret) > keep else "…"
