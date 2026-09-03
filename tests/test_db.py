"""Schema and migration bookkeeping against a real Postgres."""

from __future__ import annotations

import psycopg
import pytest

from app import db, store


def columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    ).fetchall()
    return {row["column_name"] for row in rows}


class TestSchema:
    def test_every_table_exists(self):
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        present = {row["table_name"] for row in rows}
        assert set(db.TABLES) <= present

    def test_users_carries_the_auth_columns(self):
        with db.connection() as conn:
            assert {"email", "password_hash"} <= columns(conn, "users")

    def test_ids_are_generated(self):
        """Identity columns replace SQLite's AUTOINCREMENT; inserts must not need an id."""
        user = store.create_password_user(
            email="a@example.com", password_hash="x", leetcode_username="aaa"
        )
        assert isinstance(user["id"], int) and user["id"] > 0


class TestMigrations:
    def test_every_migration_is_recorded(self):
        with db.connection() as conn:
            rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        assert {row["version"] for row in rows} == set(range(len(db.MIGRATIONS)))

    def test_running_it_again_changes_nothing(self):
        db.init_db()
        db.init_db()
        with db.connection() as conn:
            rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        assert len(rows) == len(db.MIGRATIONS)


class TestConstraints:
    def test_email_is_unique_case_insensitively(self):
        store.create_password_user(
            email="Dana@Example.com", password_hash="x", leetcode_username="dana"
        )
        with pytest.raises(store.DuplicateEmail):
            store.create_password_user(
                email="dana@example.com", password_hash="x", leetcode_username="other"
            )

    def test_leetcode_is_unique_case_insensitively(self):
        store.create_password_user(
            email="a@example.com", password_hash="x", leetcode_username="DanaCodes"
        )
        with pytest.raises(store.DuplicateLeetCode):
            store.create_password_user(
                email="b@example.com", password_hash="x", leetcode_username="danacodes"
            )

    def test_blank_emails_do_not_collide(self):
        """The unique index is partial, so several accounts may have no address."""
        store.upsert_oauth_user(
            provider="github", subject="1", handle="one", display_name="", avatar_url=""
        )
        store.upsert_oauth_user(
            provider="github", subject="2", handle="two", display_name="", avatar_url=""
        )
        with db.connection() as conn:
            count = conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"]
        assert count == 2

    def test_deleting_a_user_cascades(self):
        user = store.create_password_user(
            email="a@example.com", password_hash="x", leetcode_username="aaa"
        )
        store.replace_activity(user["id"], "leetcode", {})
        with db.transaction() as conn:
            conn.execute("DELETE FROM users WHERE id = %s", (user["id"],))
        with db.connection() as conn:
            left = conn.execute(
                "SELECT count(*) AS n FROM identities WHERE user_id = %s", (user["id"],)
            ).fetchone()["n"]
        assert left == 0


class TestPool:
    def test_a_failed_transaction_rolls_back(self):
        try:
            with db.transaction() as conn:
                conn.execute(
                    """INSERT INTO users (auth_provider, auth_subject, handle, created_at)
                       VALUES ('x', 'y', 'rollback-me', '2026-01-01T00:00:00+00:00')"""
                )
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with db.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE handle = 'rollback-me'"
            ).fetchone()
        assert row is None

    def test_postgres_urls_are_normalised(self):
        assert db.normalise_dsn("postgres://u:p@h/db") == "postgresql://u:p@h/db"
        assert db.normalise_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"
