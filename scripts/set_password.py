"""Set a user's password from the server. There is no self-service reset without SMTP.

    uv run python scripts/set_password.py someone@example.com

Prompts for the new password without echoing it.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, passwords, store  # noqa: E402
from app.config import get_settings  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    db.configure(get_settings().database_url)
    db.init_db()

    email = sys.argv[1]
    user = store.get_user_by_email(email)
    if not user:
        print(f"No account with email {email!r}.", file=sys.stderr)
        return 1
    if not user["password_hash"]:
        print(
            f"{email} signs in with {user['auth_provider']}, not a password. "
            "Setting one will give them both.",
            file=sys.stderr,
        )

    new_password = getpass.getpass("New password: ")
    if problem := passwords.problem_with(new_password):
        print(problem, file=sys.stderr)
        return 1
    if new_password != getpass.getpass("Confirm: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1

    store.set_password_hash(user["id"], passwords.hash_password(new_password))
    print(f"Password updated for {user['display_name'] or user['handle']} ({email}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
