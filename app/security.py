"""Password hashing and signed tokens.

Both are built on the standard library (scrypt + HMAC-SHA256) so the container
stays small and there is no crypto dependency to keep patched.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

# scrypt parameters - ~64 MB of memory per hash, interactive-login grade.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LEN = 32
# OpenSSL's default maxmem is 32 MiB, which these parameters exceed.
_SCRYPT_MAXMEM = 128 * 1024 * 1024


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_LEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64d(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_b64d(digest_b64)),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, _b64d(digest_b64))


# ---------------------------------------------------------------------------
# Tokens (compact JWT-style: base64(payload).base64(hmac))
# ---------------------------------------------------------------------------


class TokenError(Exception):
    """Raised when a token is malformed, forged or expired."""


def sign_token(payload: dict[str, Any], secret: str, ttl_seconds: int | None = None) -> str:
    body = dict(payload)
    body.setdefault("iat", int(time.time()))
    body.setdefault("jti", secrets.token_urlsafe(8))
    if ttl_seconds is not None:
        body["exp"] = int(time.time()) + ttl_seconds
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64e(raw)
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64e(signature)}"


def verify_token(token: str, secret: str) -> dict[str, Any]:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError as exc:
        raise TokenError("malformed token") from exc

    expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = _b64d(signature)
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad token
        raise TokenError("malformed signature") from exc
    if not hmac.compare_digest(expected, provided):
        raise TokenError("bad signature")

    try:
        payload = json.loads(_b64d(encoded))
    except Exception as exc:  # noqa: BLE001
        raise TokenError("malformed payload") from exc
    if not isinstance(payload, dict):
        raise TokenError("malformed payload")

    exp = payload.get("exp")
    if exp is not None and time.time() > float(exp):
        raise TokenError("token expired")
    return payload
