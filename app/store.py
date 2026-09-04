from __future__ import annotations

import re
import secrets
import psycopg
from datetime import date, datetime, timezone
from typing import Any, Iterable

from . import db

HANDLE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,38}$")
# Deliberately permissive: the only authority on whether an address is real is the
# address itself, and over-clever patterns reject valid mail.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
MAX_EMAIL_LENGTH = 254


class DuplicateEmail(Exception):
    pass


class DuplicateLeetCode(Exception):
    pass


def normalise_email(value: str) -> str:
    return (value or "").strip().lower()


def problem_with_email(value: str) -> str | None:
    if not value:
        return "Enter your email address."
    if len(value) > MAX_EMAIL_LENGTH:
        return "That email address is too long."
    if not EMAIL_RE.match(value):
        return "That does not look like an email address."
    return None


def _raise_for_conflict(exc: psycopg.errors.UniqueViolation) -> None:
    """Turn a unique-index violation into something we can show a person."""
    message = f"{exc.diag.constraint_name or ''} {exc}"
    if "idx_users_email" in message:
        raise DuplicateEmail("That email address is already registered.") from exc
    if "idx_users_leetcode" in message:
        raise DuplicateLeetCode(
            "Someone on this server has already claimed that LeetCode account."
        ) from exc
    raise exc


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _row_to_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------- users


def unique_handle(conn: psycopg.Connection, desired: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]", "", desired or "").strip("-_") or "user"
    base = base[:32]
    candidate = base
    suffix = 1
    while conn.execute("SELECT 1 FROM users WHERE handle = %s", (candidate,)).fetchone():
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def get_user_by_identity(provider: str, subject: str) -> dict[str, Any] | None:
    with db.connection() as conn:
        return _row_to_dict(
            conn.execute(
                """SELECT u.* FROM users u
                     JOIN identities i ON i.user_id = u.id
                    WHERE i.provider = %s AND i.subject = %s""",
                (provider, str(subject)),
            ).fetchone()
        )


def link_identity(user_id: int, provider: str, subject: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO identities (provider, subject, user_id, created_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (provider, str(subject), user_id, now_iso()),
        )


def identity_providers(user_id: int) -> list[str]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT provider FROM identities WHERE user_id = %s ORDER BY provider", (user_id,)
        ).fetchall()
    return [row["provider"] for row in rows]


def upsert_oauth_user(
    provider: str,
    subject: str,
    handle: str,
    display_name: str,
    avatar_url: str,
    email: str = "",
    github_login: str = "",
) -> dict[str, Any]:
    """Find, link or create the account behind an OAuth identity.

    An address is only matched against an existing account when the provider has
    verified it (the caller's job), so signing in with Google adds a second way into
    the account you already registered by email rather than making a duplicate.
    """
    subject = str(subject)
    email = normalise_email(email)

    existing = get_user_by_identity(provider, subject)
    if existing is None and email:
        existing = get_user_by_email(email)
        if existing is not None:
            link_identity(existing["id"], provider, subject)

    if existing is not None:
        with db.transaction() as conn:
            # Fill in gaps without clobbering anything the person chose themselves.
            conn.execute(
                """UPDATE users
                      SET avatar_url = CASE WHEN avatar_url = '' THEN %s ELSE avatar_url END,
                          display_name = CASE WHEN display_name = '' THEN %s ELSE display_name END,
                          github_login = CASE WHEN github_login = '' THEN %s ELSE github_login END,
                          email = CASE WHEN email = '' THEN %s ELSE email END
                    WHERE id = %s""",
                (avatar_url, display_name, github_login, email, existing["id"]),
            )
        return get_user(existing["id"])

    try:
        with db.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO users
                       (auth_provider, auth_subject, handle, email, display_name,
                        avatar_url, github_login, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    provider,
                    subject,
                    unique_handle(conn, handle),
                    email,
                    display_name,
                    avatar_url,
                    github_login,
                    now_iso(),
                ),
            )
            user_id = cursor.fetchone()["id"]
            conn.execute(
                """INSERT INTO identities (provider, subject, user_id, created_at)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (provider, subject, user_id, now_iso()),
            )
            return _row_to_dict(
                conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
            )
    except psycopg.errors.UniqueViolation as exc:
        _raise_for_conflict(exc)


def create_password_user(
    email: str,
    password_hash: str,
    leetcode_username: str,
    display_name: str = "",
    timezone_name: str = "UTC",
) -> dict[str, Any]:
    """Register an email/password account. The LeetCode handle doubles as the identity."""
    email = normalise_email(email)
    leetcode_username = leetcode_username.strip()
    display_name = display_name.strip() or leetcode_username

    try:
        with db.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO users
                       (auth_provider, auth_subject, handle, email, password_hash,
                        display_name, leetcode_username, timezone, created_at)
                   VALUES ('password', %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    email,
                    unique_handle(conn, leetcode_username),
                    email,
                    password_hash,
                    display_name,
                    leetcode_username,
                    timezone_name,
                    now_iso(),
                ),
            )
            user_id = cursor.fetchone()["id"]
            conn.execute(
                """INSERT INTO identities (provider, subject, user_id, created_at)
                   VALUES ('password', %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (email, user_id, now_iso()),
            )
            return _row_to_dict(
                conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
            )
    except psycopg.errors.UniqueViolation as exc:
        _raise_for_conflict(exc)


def get_user_by_email(email: str) -> dict[str, Any] | None:
    with db.connection() as conn:
        return _row_to_dict(
            conn.execute(
                "SELECT * FROM users WHERE lower(email) = %s", (normalise_email(email),)
            ).fetchone()
        )


def set_password_hash(user_id: int, password_hash: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id)
        )


def get_user(user_id: int) -> dict[str, Any] | None:
    with db.connection() as conn:
        return _row_to_dict(conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone())


def get_user_by_handle(handle: str) -> dict[str, Any] | None:
    with db.connection() as conn:
        return _row_to_dict(
            conn.execute("SELECT * FROM users WHERE handle = %s", (handle,)).fetchone()
        )


def update_profile(
    user_id: int,
    *,
    display_name: str,
    leetcode_username: str,
    github_login: str,
    github_repo: str,
    timezone_name: str,
) -> None:
    try:
        with db.transaction() as conn:
            conn.execute(
                """UPDATE users
                      SET display_name = %s, leetcode_username = %s, github_login = %s,
                          github_repo = %s, timezone = %s
                    WHERE id = %s""",
                (
                    display_name,
                    leetcode_username,
                    github_login,
                    github_repo,
                    timezone_name,
                    user_id,
                ),
            )
    except psycopg.errors.UniqueViolation as exc:
        _raise_for_conflict(exc)


# -------------------------------------------------------------------------- groups


def create_group(name: str, owner_id: int) -> dict[str, Any]:
    with db.transaction() as conn:
        for _ in range(10):
            code = secrets.token_urlsafe(9)
            if not conn.execute(
                "SELECT 1 FROM groups WHERE invite_code = %s", (code,)
            ).fetchone():
                break
        else:  # pragma: no cover - astronomically unlikely
            raise RuntimeError("could not allocate an invite code")

        cursor = conn.execute(
            """INSERT INTO groups (name, invite_code, owner_id, created_at)
               VALUES (%s, %s, %s, %s)
               RETURNING id""",
            (name, code, owner_id, now_iso()),
        )
        group_id = cursor.fetchone()["id"]
        conn.execute(
            "INSERT INTO memberships (group_id, user_id, role, joined_at) VALUES (%s, %s, %s, %s)",
            (group_id, owner_id, "owner", now_iso()),
        )
        return _row_to_dict(
            conn.execute("SELECT * FROM groups WHERE id = %s", (group_id,)).fetchone()
        )


def get_group(group_id: int) -> dict[str, Any] | None:
    with db.connection() as conn:
        return _row_to_dict(
            conn.execute("SELECT * FROM groups WHERE id = %s", (group_id,)).fetchone()
        )


def get_group_by_invite(code: str) -> dict[str, Any] | None:
    with db.connection() as conn:
        return _row_to_dict(
            conn.execute("SELECT * FROM groups WHERE invite_code = %s", (code,)).fetchone()
        )


def rename_group(group_id: int, name: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE groups SET name = %s WHERE id = %s", (name, group_id))


def rotate_invite(group_id: int) -> str:
    code = secrets.token_urlsafe(9)
    with db.transaction() as conn:
        conn.execute("UPDATE groups SET invite_code = %s WHERE id = %s", (code, group_id))
    return code


def delete_group(group_id: int) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM groups WHERE id = %s", (group_id,))


def add_member(group_id: int, user_id: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO memberships (group_id, user_id, role, joined_at)
               VALUES (%s, %s, 'member', %s)
               ON CONFLICT DO NOTHING""",
            (group_id, user_id, now_iso()),
        )


def known_problem_slugs(slugs: Iterable[str]) -> set[str]:
    """Which of these we already have difficulty for, so we only fetch the rest."""
    wanted = list(slugs)
    if not wanted:
        return set()
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT slug FROM problems WHERE slug = ANY(%s)", (wanted,)
        ).fetchall()
    return {row["slug"] for row in rows}


def save_problems(problems: Iterable[dict[str, str]]) -> None:
    rows = [(p["slug"], p["title"], p.get("difficulty") or "Unknown") for p in problems]
    if not rows:
        return
    with db.transaction() as conn, conn.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO problems (slug, title, difficulty) VALUES (%s, %s, %s)
               ON CONFLICT (slug) DO UPDATE SET
                   title = excluded.title, difficulty = excluded.difficulty""",
            rows,
        )


def record_solved(user_id: int, solved: Iterable[dict[str, Any]]) -> None:
    """Add to what we know. Never deletes: LeetCode only shows the last 20, so a
    wholesale replace would throw away everything older than that window."""
    rows = [
        (user_id, s["slug"], s["solved_at"].date().isoformat(), s["solved_at"].isoformat())
        for s in solved
    ]
    if not rows:
        return
    with db.transaction() as conn, conn.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO solved_problems (user_id, slug, solved_on, solved_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (user_id, slug, solved_on) DO NOTHING""",
            rows,
        )


def problems_on(user_ids: Iterable[int], day: date) -> dict[int, list[dict[str, str]]]:
    """What each of these people solved on one day, respecting their visibility choice."""
    ids = list(user_ids)
    if not ids:
        return {}
    with db.connection() as conn:
        rows = conn.execute(
            """SELECT s.user_id, p.slug, p.title, p.difficulty
                 FROM solved_problems s
                 JOIN problems p ON p.slug = s.slug
                 JOIN users u ON u.id = s.user_id
                WHERE s.user_id = ANY(%s) AND s.solved_on = %s AND u.show_problems
                ORDER BY s.solved_at""",
            (ids, day.isoformat()),
        ).fetchall()
    out: dict[int, list[dict[str, str]]] = {}
    for row in rows:
        out.setdefault(row["user_id"], []).append(
            {"slug": row["slug"], "title": row["title"], "difficulty": row["difficulty"]}
        )
    return out


def set_show_problems(user_id: int, visible: bool) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE users SET show_problems = %s WHERE id = %s", (visible, user_id))


def all_groups() -> list[dict[str, Any]]:
    with db.connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM groups ORDER BY id").fetchall()]


def set_nudges(user_id: int, enabled: bool) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE users SET nudge_enabled = %s WHERE id = %s", (enabled, user_id))


def mark_nudged(user_id: int, day: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE users SET last_nudged_on = %s WHERE id = %s", (day, user_id))


def set_group_timezone(group_id: int, timezone_name: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE groups SET timezone = %s WHERE id = %s", (timezone_name, group_id)
        )


def remove_member(group_id: int, user_id: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM memberships WHERE group_id = %s AND user_id = %s", (group_id, user_id)
        )


def is_member(group_id: int, user_id: int) -> bool:
    with db.connection() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM memberships WHERE group_id = %s AND user_id = %s",
                (group_id, user_id),
            ).fetchone()
            is not None
        )


def groups_for_user(user_id: int) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            """SELECT g.*,
                      m.role AS my_role,
                      (SELECT COUNT(*) FROM memberships x WHERE x.group_id = g.id) AS member_count
                 FROM groups g
                 JOIN memberships m ON m.group_id = g.id
                WHERE m.user_id = %s
                ORDER BY lower(g.name)""",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def members_of(group_id: int) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            """SELECT u.*, m.role, m.joined_at AS member_since
                 FROM users u
                 JOIN memberships m ON m.user_id = u.id
                WHERE m.group_id = %s""",
            (group_id,),
        ).fetchall()
        return [dict(row) for row in rows]


# ------------------------------------------------------------------------ activity


def replace_activity(user_id: int, source: str, counts: dict[date, int]) -> None:
    """Swap in a fresh window of data for one source, atomically."""
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM daily_activity WHERE user_id = %s AND source = %s", (user_id, source)
        )
        rows = [
            (user_id, source, day.isoformat(), count)
            for day, count in counts.items()
            if count > 0
        ]
        if rows:
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO daily_activity (user_id, source, day, count) "
                    "VALUES (%s, %s, %s, %s)",
                    rows,
                )


def activity_for_users(user_ids: Iterable[int]) -> dict[int, dict[str, dict[date, int]]]:
    ids = list(user_ids)
    if not ids:
        return {}
    placeholders = ",".join("%s" for _ in ids)
    with db.connection() as conn:
        rows = conn.execute(
            f"""SELECT user_id, source, day, count
                  FROM daily_activity
                 WHERE user_id IN ({placeholders})""",
            ids,
        ).fetchall()

    result: dict[int, dict[str, dict[date, int]]] = {uid: {} for uid in ids}
    for row in rows:
        try:
            day = date.fromisoformat(row["day"])
        except ValueError:
            continue
        result[row["user_id"]].setdefault(row["source"], {})[day] = row["count"]
    return result


def record_sync(user_id: int, source: str, status: str, error: str = "") -> None:
    stamp = now_iso()
    success = stamp if status == "ok" else None
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO sync_state (user_id, source, last_attempt_at, last_success_at,
                                       status, error)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (user_id, source) DO UPDATE SET
                   last_attempt_at = excluded.last_attempt_at,
                   last_success_at = COALESCE(excluded.last_success_at,
                                              sync_state.last_success_at),
                   status = excluded.status,
                   error = excluded.error""",
            (user_id, source, stamp, success, status, error),
        )


def sync_state_for_users(user_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
    ids = list(user_ids)
    if not ids:
        return {}
    placeholders = ",".join("%s" for _ in ids)
    with db.connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM sync_state WHERE user_id IN ({placeholders})", ids
        ).fetchall()
    result: dict[int, list[dict[str, Any]]] = {uid: [] for uid in ids}
    for row in rows:
        result[row["user_id"]].append(dict(row))
    return result


def all_trackable_users() -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            """SELECT * FROM users
                WHERE leetcode_username <> '' OR github_repo <> '' OR github_login <> ''"""
        ).fetchall()
        return [dict(row) for row in rows]
