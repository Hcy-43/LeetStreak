from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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
    # Titles solved on the board's today, when this person shares them.
    solved_today: list[dict[str, str]] = field(default_factory=list)
    # When this person's own record starts: the later of the group's creation and
    # the day they joined it. Nobody is measured on days before they were here.
    since: date | None = None

    @property
    def days_tracked(self) -> int:
        """Days this person has been on this board, today included."""
        if self.since is None or self.today is None:
            return 0
        return (self.today - self.since).days + 1

    today: date | None = None

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
    def by_missed(self) -> list[MemberBoard]:
        """Fewest misses first. Ranking the other way round would make the board a
        pillory, and these are meant to be friends."""
        return sorted(
            (m for m in self.members if m.is_tracked),
            key=lambda m: (m.stats.missed_days, -m.stats.current_streak, m.name.lower()),
        )

    @property
    def days_since_start(self) -> int:
        """Days the group has existed, today included - the denominator for misses."""
        if self.since is None:
            return 0
        return (self.today - self.since).days + 1

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


def reconcile(utc_counts: dict[date, int], local: dict[date, int]) -> dict[date, int]:
    """Prefer timestamp-derived local days wherever we have them.

    From the earliest day we have a timestamp for, the local calendar takes over
    entirely: those days are cut on real submission times rather than on UTC
    midnights relabelled as local ones. Anything older keeps its UTC bucket, which
    is close enough for squares nobody is checking to the hour.

    The two sources also count different things, and that is deliberate. The
    calendar counts every submission, including failed ones; the timestamped list
    counts only accepted ones. Inside the window a day of attempts with nothing
    accepted is therefore *not* a solved day, and will not hold up a streak - which
    is the point of a streak here. Older days keep the calendar's attempt counts,
    because nothing better exists for them.

    LeetCode caps the timestamped window at 20 accepted submissions, so the oldest
    local day can be short a few solves. That moves a square's shade.
    """
    if not local:
        return utc_counts
    cutover = min(local)
    merged = {day: n for day, n in utc_counts.items() if day < cutover}
    merged.update(local)
    return merged


def weeks_since(start: date, today: date) -> int:
    """How many week columns are needed to cover `start` through `today`."""
    span = (week_start(today) - week_start(start)).days // 7
    return max(1, span + 1)


def build_board(
    members: list[dict[str, Any]],
    today: date,
    range_key: str | None,
    since: date | None = None,
    timezone_name: str | None = None,
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
    def joined_on(member: dict[str, Any]) -> date | None:
        """The later of the group's start and this person's arrival."""
        if since is None:
            return None
        moment = store.parse_iso(member.get("member_since"))
        if moment is None:
            return since
        try:
            zone = ZoneInfo(timezone_name or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("UTC")
        return max(since, moment.astimezone(zone).date())

    user_ids = [member["id"] for member in members]
    activity = store.activity_for_users(user_ids)
    local = store.local_activity(user_ids, timezone_name) if timezone_name else {}
    sync_states = store.sync_state_for_users(user_ids)
    solved = store.problems_on(user_ids, today, timezone_name)

    rows: list[MemberBoard] = []
    for member in members:
        per_source = activity.get(member["id"], {})
        merged = reconcile(merge_sources(per_source), local.get(member["id"], {}))
        errors = [
            f"{SOURCE_LABELS.get(state['source'], state['source'])}: {state['error']}"
            for state in sync_states.get(member["id"], [])
            if state.get("status") == "error" and state.get("error")
        ]
        mine = joined_on(member)
        # Someone who joined last week has no business being shown four weeks of
        # misses, so their window starts when they did.
        my_window = window_start if mine is None else max(window_start, mine)
        rows.append(
            MemberBoard(
                user=member,
                stats=compute_stats(
                    merged, today, since=my_window, streak_since=mine
                ),
                calendar=build_calendar(merged, today, weeks, since=mine),
                sources=sorted(source for source in per_source if per_source[source]),
                problems=merged,
                errors=errors,
                solved_today=solved.get(member["id"], []),
                since=mine,
                today=today,
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
