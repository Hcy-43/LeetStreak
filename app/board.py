from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from . import store, sync
from .streaks import (
    Calendar,
    Stats,
    build_calendar,
    compute_stats,
    merge_sources,
    week_start,
)

# Label -> number of week columns. A month is five columns because a 30-day span
# almost always straddles five calendar weeks.
RANGE_OPTIONS: dict[str, int] = {
    "1w": 1,
    "2w": 2,
    "1m": 5,
    "3m": 13,
    "6m": 26,
    "1y": 53,
}
DEFAULT_RANGE = "6m"

# Old links and bookmarks still carry the previous labels.
RANGE_ALIASES: dict[str, str] = {"12w": "3m", "26w": "6m"}

SOURCE_LABELS = {"leetcode": "LeetCode", "github": "GitHub"}


@dataclass
class MemberBoard:
    user: dict[str, Any]
    stats: Stats
    calendar: Calendar
    sources: list[str]
    problems: dict[date, int]
    errors: list[str]

    @property
    def name(self) -> str:
        return self.user.get("display_name") or self.user.get("handle") or "unknown"

    @property
    def is_tracked(self) -> bool:
        """Whether this person has linked an account at all.

        Deliberately based on configuration rather than on having data: someone
        who joined this morning and has not solved yet is still on the hook, and
        must stay in the "n of m solved today" denominator.
        """
        user = self.user
        return sync.is_linked(user)


@dataclass
class Board:
    members: list[MemberBoard]
    today: date
    weeks: int
    range_key: str
    since: date | None = None
    available_weeks: int | None = None

    @property
    def range_choices(self) -> list[str]:
        """Ranges worth offering. A week-old group has no six-month view to show,
        and rendering buttons that all draw the same grid just looks broken."""
        if self.available_weeks is None:
            return list(RANGE_OPTIONS)
        choices = [key for key, weeks in RANGE_OPTIONS.items() if weeks <= self.available_weeks]
        if not choices:
            return [next(iter(RANGE_OPTIONS))]
        # Keep the next size up so the board can still grow into it.
        remaining = [key for key in RANGE_OPTIONS if key not in choices]
        return choices + remaining[:1]

    @property
    def done_today(self) -> list[MemberBoard]:
        return [member for member in self.members if member.stats.done_today]

    @property
    def pending_today(self) -> list[MemberBoard]:
        return [
            member
            for member in self.members
            if member.is_tracked and not member.stats.done_today
        ]

    @property
    def tracked_count(self) -> int:
        return sum(1 for member in self.members if member.is_tracked)


def resolve_range(value: str | None) -> tuple[str, int]:
    key = RANGE_ALIASES.get(value, value)
    if key not in RANGE_OPTIONS:
        key = DEFAULT_RANGE
    return key, RANGE_OPTIONS[key]


def weeks_since(start: date, today: date) -> int:
    """How many week columns are needed to cover `start` through `today`."""
    span = (week_start(today) - week_start(start)).days // 7
    return max(1, span + 1)


def build_board(
    members: list[dict[str, Any]],
    today: date,
    range_key: str | None,
    since: date | None = None,
) -> Board:
    key, weeks = resolve_range(range_key)
    # A group board should not show history from before the group existed - those
    # squares are not what anyone signed up to be measured on.
    if since is not None:
        weeks = min(weeks, weeks_since(since, today))

    # Totals are counted over exactly what the grid shows, so "12 active days" never
    # sits next to a five-square calendar.
    window_start = week_start(today) - timedelta(weeks=weeks - 1)
    if since is not None:
        window_start = max(window_start, since)
    user_ids = [member["id"] for member in members]
    activity = store.activity_for_users(user_ids)
    sync_states = store.sync_state_for_users(user_ids)

    rows: list[MemberBoard] = []
    for member in members:
        per_source = activity.get(member["id"], {})
        merged = merge_sources(per_source)
        errors = [
            f"{SOURCE_LABELS.get(state['source'], state['source'])}: {state['error']}"
            for state in sync_states.get(member["id"], [])
            if state.get("status") == "error" and state.get("error")
        ]
        rows.append(
            MemberBoard(
                user=member,
                stats=compute_stats(
                    merged, today, since=window_start, streak_since=since
                ),
                calendar=build_calendar(merged, today, weeks, since=since),
                sources=sorted(source for source in per_source if per_source[source]),
                problems=merged,
                errors=errors,
            )
        )

    # Most impressive first, but always keep untracked members at the bottom so
    # the board reads as a leaderboard rather than a directory.
    rows.sort(
        key=lambda row: (
            row.is_tracked,
            row.stats.current_streak,
            row.stats.longest_streak,
            row.stats.active_days,
        ),
        reverse=True,
    )
    return Board(
        members=rows,
        today=today,
        weeks=weeks,
        range_key=key,
        since=since,
        available_weeks=weeks_since(since, today) if since is not None else None,
    )
