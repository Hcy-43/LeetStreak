"""Password hashing built on hashlib.scrypt.

stdlib only and deliberately so: this app has no compiled dependencies, and scrypt is a
memory-hard KDF that is a perfectly respectable choice next to argon2 or bcrypt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# 128 * N * r bytes of memory == 32 MiB per hash at these parameters, and roughly
# 100 ms on a modern laptop. maxmem must be passed explicitly: OpenSSL's default
# ceiling is 32 MiB and would reject exactly this size.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32
MAXMEM = 96 * 1024 * 1024

# scrypt's cost does not grow with input length, but an unbounded password is still
# free memory for an attacker to make us copy.
MAX_PASSWORD_BYTES = 1024
MIN_PASSWORD_LENGTH = 10

_DUMMY_HASH: str | None = None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8")[:MAX_PASSWORD_BYTES],
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=DKLEN,
        maxmem=MAXMEM,
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check. Returns False for anything malformed rather than raising."""
    if not encoded:
        return False
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        if scheme != "scrypt":
            return False
        derived = _derive(password, _unb64(salt), int(n), int(r), int(p))
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(derived, _unb64(expected))


def waste_time() -> None:
    """Burn a comparable amount of work when no account matched.

    Without this, "no such email" returns far faster than "wrong password", which
    tells an attacker which addresses are registered.
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    verify_password("not-the-password", _DUMMY_HASH)


def problem_with(password: str) -> str | None:
    """Returns a human-readable complaint, or None when the password is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return "That password is too long."
    if password.strip() == "":
        return "That password is only whitespace."
    return None
