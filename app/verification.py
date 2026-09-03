"""Emailed verification codes.

The code itself is six digits, which is only safe because of the limits enforced here:
a short expiry, a hard attempt cap, and a resend cooldown. Codes are stored as an HMAC
keyed with SECRET_KEY rather than in the clear, so a leaked database does not hand
someone a working code.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from . import db, store

CODE_DIGITS = 6
TTL_MINUTES = 15
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60

PURPOSE_REGISTER = "register"
PURPOSE_RESET = "reset"


@dataclass(frozen=True)
class Issued:
    code: str
    expires_at: datetime


class VerificationError(Exception):
    """Shown to the person verbatim."""


def generate_code() -> str:
    # randbelow rather than randint: no modulo bias, and it is the CSPRNG.
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def hash_code(secret_key: str, email: str, purpose: str, code: str) -> str:
    # Email and purpose are bound into the digest so a code cannot be replayed
    # against a different address.
    payload = f"{email}\0{purpose}\0{code}".encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), payload, sha256).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seconds_until_resend(email: str, purpose: str = PURPOSE_REGISTER) -> int:
    """How long the caller must wait before another code may be sent."""
    with db.connection() as conn:
        row = conn.execute(
            "SELECT created_at FROM email_codes WHERE email = %s AND purpose = %s",
            (email, purpose),
        ).fetchone()
    created = store.parse_iso(row["created_at"]) if row else None
    if created is None:
        return 0
    elapsed = (_now() - created).total_seconds()
    return max(0, int(RESEND_COOLDOWN_SECONDS - elapsed))


def issue(secret_key: str, email: str, purpose: str = PURPOSE_REGISTER) -> Issued:
    """Create and store a fresh code, replacing any outstanding one."""
    code = generate_code()
    now = _now()
    expires = now + timedelta(minutes=TTL_MINUTES)

    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO email_codes (email, purpose, code_hash, created_at,
                                        expires_at, attempts)
               VALUES (%s, %s, %s, %s, %s, 0)
               ON CONFLICT (email, purpose) DO UPDATE SET
                   code_hash = excluded.code_hash,
                   created_at = excluded.created_at,
                   expires_at = excluded.expires_at,
                   attempts = 0""",
            (
                email,
                purpose,
                hash_code(secret_key, email, purpose, code),
                now.isoformat(timespec="seconds"),
                expires.isoformat(timespec="seconds"),
            ),
        )
    return Issued(code=code, expires_at=expires)


EXPIRED = "That code has expired. Ask for a new one."
EXHAUSTED = "Too many wrong codes. Ask for a new one."


def check(secret_key: str, email: str, code: str, purpose: str = PURPOSE_REGISTER) -> None:
    """Consume a code. Returns None on success, raises VerificationError otherwise.

    The failure is raised *after* the transaction commits, never inside it:
    db.transaction() rolls back on an exception, which would undo the attempt
    counter and leave a six-digit code freely brute-forceable.
    """
    code = (code or "").strip().replace(" ", "").replace("-", "")
    failure: str | None = None

    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM email_codes WHERE email = %s AND purpose = %s", (email, purpose)
        ).fetchone()
        delete = "DELETE FROM email_codes WHERE email = %s AND purpose = %s"

        if row is None:
            failure = EXPIRED
        elif (expires := store.parse_iso(row["expires_at"])) is None or expires < _now():
            conn.execute(delete, (email, purpose))
            failure = EXPIRED
        elif row["attempts"] >= MAX_ATTEMPTS:
            conn.execute(delete, (email, purpose))
            failure = EXHAUSTED
        elif not hmac.compare_digest(
            hash_code(secret_key, email, purpose, code), row["code_hash"]
        ):
            attempts = row["attempts"] + 1
            remaining = MAX_ATTEMPTS - attempts
            if remaining <= 0:
                conn.execute(delete, (email, purpose))
                failure = EXHAUSTED
            else:
                conn.execute(
                    """UPDATE email_codes SET attempts = %s
                        WHERE email = %s AND purpose = %s""",
                    (attempts, email, purpose),
                )
                failure = f"That code is not right. {remaining} attempt(s) left."
        else:
            # Correct: single use, so it goes now.
            conn.execute(delete, (email, purpose))

    if failure:
        raise VerificationError(failure)


def purge_expired() -> int:
    with db.transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM email_codes WHERE expires_at < %s", (_now().isoformat(),)
        )
        return cursor.rowcount
