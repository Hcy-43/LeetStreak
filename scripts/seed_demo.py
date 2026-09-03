"""Fill a database with a fake group so you can see the board without waiting on real data.

    uv run python scripts/seed_demo.py

Signs nobody in: use handle sign-in as `dana` afterwards to view the board.
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, passwords, store  # noqa: E402
from app.config import get_settings  # noqa: E402

DEMO_PASSWORD = "demo-password-1"

# The handles carry a nonsense suffix on purpose. Short plausible names like "samo"
# and "riyab" are real LeetCode accounts with empty calendars, and a live sync of one
# correctly replaces this fake history with nothing - which looks like the demo broke.
# These 404 instead, so the sync errors and leaves the seeded rows alone.
PEOPLE = [
    # email, display name, leetcode handle, solve probability, current run length
    ("dana@example.com", "Dana Whitfield", "demo-dana-qx7k2", 0.86, 23),
    ("sam@example.com", "Sam Okonkwo", "demo-sam-qx7k2", 0.72, 5),
    ("riya@example.com", "Riya Balan", "demo-riya-qx7k2", 0.94, 61),
    ("theo@example.com", "Theo Lindqvist", "demo-theo-qx7k2", 0.45, 0),
    ("nina@example.com", "Nina Sørensen", "demo-nina-qx7k2", 0.6, 1),
]


def _pin_sync(user_id: int) -> None:
    """Mark this user as synced far in the future so nothing overwrites the demo.

    The made-up handles below can collide with real LeetCode accounts, and a real
    sync of an account with an empty calendar would (correctly) wipe these rows.
    """
    stamp = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat(timespec="seconds")
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO sync_state (user_id, source, last_attempt_at, last_success_at, status)
               VALUES (%s, 'leetcode', %s, %s, 'ok')
               ON CONFLICT (user_id, source) DO UPDATE SET
                   last_attempt_at = excluded.last_attempt_at,
                   last_success_at = excluded.last_success_at,
                   status = 'ok', error = ''""",
            (user_id, stamp, stamp),
        )


def main() -> None:
    settings = get_settings()
    db.configure(settings.database_url)
    db.init_db()
    random.seed(7)

    today = datetime.now(timezone.utc).date()
    owner = None

    # One hash, reused: scrypt is intentionally slow and this is throwaway data.
    password_hash = passwords.hash_password(DEMO_PASSWORD)

    for email, name, leetcode, probability, run in PEOPLE:
        existing = store.get_user_by_email(email)
        if existing:
            user = existing
        else:
            user = store.create_password_user(
                email=email,
                password_hash=password_hash,
                leetcode_username=leetcode,
                display_name=name,
            )
        owner = owner or user

        counts: dict = {}
        for offset in range(1, 371):
            day = today - timedelta(days=offset)
            # Weekends are quieter, and everyone was flakier six months ago.
            weight = probability * (0.65 if day.weekday() >= 5 else 1.0)
            weight *= 0.55 + 0.45 * (1 - offset / 370)
            if random.random() < weight:
                counts[day] = random.choice([1, 1, 1, 2, 2, 3, 5])

        # Overwrite the tail so each person has the intended current streak.
        for offset in range(run):
            counts[today - timedelta(days=offset)] = random.choice([1, 1, 2, 3])
        if run == 0:
            counts.pop(today, None)
            counts.pop(today - timedelta(days=1), None)

        store.replace_activity(user["id"], "leetcode", counts)
        _pin_sync(user["id"])

    group = store.create_group("Daily grind crew", owner["id"])
    for email, *_ in PEOPLE[1:]:
        store.add_member(group["id"], store.get_user_by_email(email)["id"])

    print(f"Seeded {len(PEOPLE)} people into '{group['name']}' (group id {group['id']}).")
    print(f"Invite link: {settings.base_url}/join/{group['invite_code']}")
    print(f"Sign in as {PEOPLE[0][0]} with password {DEMO_PASSWORD!r} to view it.")


if __name__ == "__main__":
    main()
