from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

# Buckets for the five green shades. Tuned for LeetCode volume, where a normal
# good day is 1-2 problems rather than the 20-commit days GitHub quartiles assume.
LEVEL_THRESHOLDS = (1, 3, 5, 8)


def level_for(count: int) -> int:
    if count <= 0:
        return 0
    for index, threshold in enumerate(LEVEL_THRESHOLDS):
        if count < threshold:
            return index
    return len(LEVEL_THRESHOLDS)


@dataclass(frozen=True)
class Stats:
    current_streak: int
    longest_streak: int
    active_days: int
    total_solved: int
    done_today: bool
    # True when the streak is alive only because yesterday counted: solve today
    # or it breaks at midnight.
    at_risk: bool
    last_active: date | None


def compute_stats(
    days: dict[date, int],
    today: date,
    since: date | None = None,
    streak_since: date | None = None,
) -> Stats:
    """Streak math over a {day: count} map. Only days with count > 0 count.

    Two independent windows, because they answer different questions:

    `since` scopes the totals to whatever the grid is showing, so "12 active days"
    never sits beside a five-square calendar.

    `streak_since` scopes the streak itself. On a group board that is the day the
    group started: the run you care about is the one you have kept up with these
    people, not one you brought with you. Left unset (your own home page) the streak
    is measured over your whole history.
    """
    active = {day for day, count in days.items() if count > 0}
    if not active:
        return Stats(0, 0, 0, 0, False, False, None)

    in_window = (
        {day for day in active if day >= since} if since is not None else active
    )
    for_streak = (
        {day for day in active if day >= streak_since}
        if streak_since is not None
        else active
    )

    done_today = today in active
    # A streak stays alive through the whole of today: if today is still blank we
    # measure back from yesterday and flag it as at risk.
    anchor = today if done_today else today - timedelta(days=1)

    current = 0
    cursor = anchor
    while cursor in for_streak:
        current += 1
        cursor -= timedelta(days=1)

    longest = 0
    run = 0
    previous: date | None = None
    for day in sorted(for_streak):
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day

    return Stats(
        current_streak=current,
        longest_streak=longest,
        active_days=len(in_window),
        total_solved=sum(
            count
            for day, count in days.items()
            if count > 0 and (since is None or day >= since)
        ),
        done_today=done_today,
        at_risk=current > 0 and not done_today,
        last_active=max(active),
    )


@dataclass(frozen=True)
class Cell:
    day: date
    count: int
    level: int
    is_future: bool
    is_today: bool
    # Days in the first column that fall before the board's start date. The grid
    # keeps its Sunday-aligned rows so the weekday labels stay honest, but these
    # render as nothing, so the first square you see is the day you started.
    is_before_start: bool = False


@dataclass(frozen=True)
class MonthLabel:
    column: int
    text: str


@dataclass(frozen=True)
class Calendar:
    """A GitHub-style grid: one column per week, Sunday at the top."""

    columns: list[list[Cell]] = field(default_factory=list)
    month_labels: list[MonthLabel] = field(default_factory=list)
    start: date | None = None
    end: date | None = None


def week_start(day: date) -> date:
    """Sunday on or before `day`. date.weekday() is Mon=0..Sun=6."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


# At or below this many columns the grid is rendered larger, and labelled more
# eagerly, because a handful of thin columns reads as a sliver rather than a chart.
NARROW_WEEKS = 5


def build_calendar(
    days: dict[date, int], today: date, weeks: int, since: date | None = None
) -> Calendar:
    last_column_start = week_start(today)
    first_column_start = last_column_start - timedelta(weeks=weeks - 1)

    columns: list[list[Cell]] = []
    labels: list[MonthLabel] = []
    previous_month: int | None = None

    for index in range(weeks):
        column_start = first_column_start + timedelta(weeks=index)
        column: list[Cell] = []
        for offset in range(7):
            day = column_start + timedelta(days=offset)
            count = days.get(day, 0)
            column.append(
                Cell(
                    day=day,
                    count=count,
                    level=level_for(count),
                    is_future=day > today,
                    is_today=day == today,
                    is_before_start=since is not None and day < since,
                )
            )
        columns.append(column)

        # Label a column when its month changes. On a wide grid the first column is
        # skipped, because it is a partial month whose label would collide with the
        # next one, and the last for want of room. A short range has neither problem
        # and needs the label more, since it may otherwise carry no date at all.
        month = column_start.month
        narrow = weeks <= NARROW_WEEKS
        if month != previous_month and (narrow or 0 < index < weeks - 1):
            labels.append(MonthLabel(column=index, text=column_start.strftime("%b")))
        previous_month = month

    return Calendar(
        columns=columns,
        month_labels=labels,
        start=first_column_start,
        end=last_column_start + timedelta(days=6),
    )


def merge_sources(per_source: dict[str, dict[date, int]]) -> dict[date, int]:
    """Combine sources for one person.

    A day counts once even if both LeetCode and GitHub saw it, so we take the max
    rather than the sum: two sources watching the same solve is not two solves.
    """
    merged: dict[date, int] = {}
    for counts in per_source.values():
        for day, count in counts.items():
            if count > merged.get(day, 0):
                merged[day] = count
    return merged
