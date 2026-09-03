"""Shared Postgres setup.

The suite runs against a real Postgres rather than a stand-in, because the whole
point of the port was that the tests should exercise the database production uses.
Set TEST_DATABASE_URL to point somewhere else; the default expects a local server
with a `leetstreak_test` database.
"""

from __future__ import annotations

import os

import pytest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql:///leetstreak_test"
)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Create the schema once for the whole session."""
    from app import db

    try:
        db.configure(TEST_DATABASE_URL)
        db.init_db()
    except Exception as exc:  # pragma: no cover - setup failure, not a test failure
        pytest.exit(
            f"Cannot reach the test database at {TEST_DATABASE_URL!r}: {exc}\n"
            "Start Postgres and run:  createdb leetstreak_test",
            returncode=1,
        )
    yield
    db.close()


@pytest.fixture(autouse=True)
def clean_database(_schema, monkeypatch):
    """Truncating between tests is much faster than recreating the schema."""
    from app import db

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    db.configure(TEST_DATABASE_URL)
    db.reset_all()
    yield
