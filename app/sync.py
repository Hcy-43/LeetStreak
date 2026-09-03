from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from . import store
from .config import Settings
from .sources import SourceError, github, leetcode

log = logging.getLogger("leetstreak.sync")

# The GitHub source (contribution graph / LeetHub-style solutions repo) is parked for
# now to keep the app to one thing: your LeetCode calendar. The source module and its
# tests are intact - flip this back to True and restore the two settings fields to
# re-enable it.
GITHUB_SOURCE_ENABLED = False

# How much history we keep. 53 weeks fills a full GitHub-style year grid.
WINDOW_WEEKS = 53

_inflight: set[int] = set()
_inflight_lock = asyncio.Lock()
_background_tasks: set[asyncio.Task] = set()


def window_start(today: date) -> date:
    return today - timedelta(weeks=WINDOW_WEEKS)


def _years_in_window(start: date, end: date) -> list[int]:
    return list(range(start.year, end.year + 1))


def is_stale(states: list[dict[str, Any]], ttl_seconds: int) -> bool:
    """True when nothing has been fetched recently enough for this user."""
    if not states:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
    attempts = [store.parse_iso(state.get("last_attempt_at")) for state in states]
    attempts = [moment for moment in attempts if moment is not None]
    if not attempts:
        return True
    return max(attempts) < cutoff


async def sync_user(
    client: httpx.AsyncClient, user: dict[str, Any], settings: Settings
) -> dict[str, str]:
    """Refresh every configured source for one user. Never raises."""
    today = datetime.now(timezone.utc).date()
    start = window_start(today)
    results: dict[str, str] = {}

    if user.get("leetcode_username"):
        try:
            counts = await leetcode.fetch_activity(
                client, user["leetcode_username"], _years_in_window(start, today)
            )
            counts = {day: n for day, n in counts.items() if start <= day <= today}
            store.replace_activity(user["id"], "leetcode", counts)
            store.record_sync(user["id"], "leetcode", "ok")
            results["leetcode"] = "ok"
        except SourceError as exc:
            store.record_sync(user["id"], "leetcode", "error", str(exc))
            results["leetcode"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - a bad source must not break the page
            log.exception("leetcode sync failed for user %s", user["id"])
            store.record_sync(user["id"], "leetcode", "error", repr(exc))
            results["leetcode"] = repr(exc)

    # Prefer a dedicated solutions repo; fall back to the whole-account calendar.
    if not GITHUB_SOURCE_ENABLED:
        pass
    elif user.get("github_repo") and user.get("github_login"):
        try:
            counts = await github.fetch_repo_commits(
                client,
                user["github_repo"],
                user["github_login"],
                start,
                today,
                settings.github_token,
                user.get("timezone") or "UTC",
            )
            store.replace_activity(user["id"], "github", counts)
            store.record_sync(user["id"], "github", "ok")
            results["github"] = "ok"
        except SourceError as exc:
            store.record_sync(user["id"], "github", "error", str(exc))
            results["github"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("github repo sync failed for user %s", user["id"])
            store.record_sync(user["id"], "github", "error", repr(exc))
            results["github"] = repr(exc)

    elif user.get("github_login") and settings.github_api_enabled:
        try:
            counts = await github.fetch_contributions(
                client, user["github_login"], start, today, settings.github_token
            )
            store.replace_activity(user["id"], "github", counts)
            store.record_sync(user["id"], "github", "ok")
            results["github"] = "ok"
        except SourceError as exc:
            store.record_sync(user["id"], "github", "error", str(exc))
            results["github"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("github contributions sync failed for user %s", user["id"])
            store.record_sync(user["id"], "github", "error", repr(exc))
            results["github"] = repr(exc)

    return results


async def sync_users(
    users: Iterable[dict[str, Any]], settings: Settings, concurrency: int = 4
) -> dict[int, dict[str, str]]:
    """Refresh several users, skipping any already being refreshed elsewhere."""
    pending = [user for user in users if user.get("id") is not None]
    if not pending:
        return {}

    async with _inflight_lock:
        claimed = [user for user in pending if user["id"] not in _inflight]
        _inflight.update(user["id"] for user in claimed)

    if not claimed:
        return {}

    semaphore = asyncio.Semaphore(concurrency)
    results: dict[int, dict[str, str]] = {}

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:

            async def run(user: dict[str, Any]) -> None:
                async with semaphore:
                    results[user["id"]] = await sync_user(client, user, settings)

            await asyncio.gather(*(run(user) for user in claimed))
    finally:
        async with _inflight_lock:
            _inflight.difference_update(user["id"] for user in claimed)

    return results


def schedule(users: list[dict[str, Any]], settings: Settings) -> None:
    """Fire-and-forget refresh so page loads never block on a slow API."""
    if not users:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(sync_users(users, settings))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def is_linked(user: dict[str, Any]) -> bool:
    """Whether this account has any source we can actually pull."""
    if user.get("leetcode_username"):
        return True
    if GITHUB_SOURCE_ENABLED:
        return bool(user.get("github_login") or user.get("github_repo"))
    return False


def refresh_stale_in_background(users: list[dict[str, Any]], settings: Settings) -> None:
    states = store.sync_state_for_users([user["id"] for user in users])
    stale = [
        user
        for user in users
        if is_stale(states.get(user["id"], []), settings.sync_ttl_seconds)
        and is_linked(user)
    ]
    schedule(stale, settings)
