from datetime import date, timedelta

from app.streaks import build_calendar, compute_stats, level_for, merge_sources


def days(*specs: tuple[str, int]) -> dict[date, int]:
    return {date.fromisoformat(day): count for day, count in specs}


def test_empty_history_has_no_streak():
    stats = compute_stats({}, date(2026, 3, 1))
    assert stats.current_streak == 0
    assert stats.longest_streak == 0
    assert stats.last_active is None
    assert not stats.at_risk


def test_streak_counts_today_when_solved():
    stats = compute_stats(
        days(("2026-02-27", 1), ("2026-02-28", 2), ("2026-03-01", 1)), date(2026, 3, 1)
    )
    assert stats.current_streak == 3
    assert stats.done_today
    assert not stats.at_risk


def test_streak_survives_an_unfinished_today_but_is_flagged():
    stats = compute_stats(days(("2026-02-27", 1), ("2026-02-28", 1)), date(2026, 3, 1))
    assert stats.current_streak == 2
    assert not stats.done_today
    assert stats.at_risk


def test_streak_breaks_after_a_full_missed_day():
    stats = compute_stats(days(("2026-02-26", 1), ("2026-02-27", 1)), date(2026, 3, 1))
    assert stats.current_streak == 0
    assert stats.longest_streak == 2
    assert not stats.at_risk


def test_longest_streak_is_the_best_run_not_the_last():
    history = days(
        ("2026-01-01", 1), ("2026-01-02", 1), ("2026-01-03", 1), ("2026-01-04", 1),
        ("2026-02-10", 1), ("2026-02-11", 1),
    )
    stats = compute_stats(history, date(2026, 2, 11))
    assert stats.longest_streak == 4
    assert stats.current_streak == 2


def test_zero_count_days_do_not_hold_a_streak_open():
    stats = compute_stats(
        days(("2026-02-27", 1), ("2026-02-28", 0), ("2026-03-01", 1)), date(2026, 3, 1)
    )
    assert stats.current_streak == 1
    assert stats.active_days == 2


def test_totals_ignore_negative_or_zero_days():
    stats = compute_stats(days(("2026-03-01", 3), ("2026-02-28", 0)), date(2026, 3, 1))
    assert stats.total_solved == 3
    assert stats.active_days == 1


def test_levels_saturate_at_four():
    assert level_for(0) == 0
    assert level_for(1) == 1
    assert level_for(2) == 1
    assert level_for(3) == 2
    assert level_for(5) == 3
    assert level_for(8) == 4
    assert level_for(99) == 4


def test_merge_takes_the_larger_count_not_the_sum():
    merged = merge_sources(
        {
            "leetcode": days(("2026-03-01", 2), ("2026-02-28", 1)),
            "github": days(("2026-03-01", 5)),
        }
    )
    assert merged[date(2026, 3, 1)] == 5
    assert merged[date(2026, 2, 28)] == 1


class TestCalendar:
    def test_grid_shape_and_alignment(self):
        today = date(2026, 3, 4)  # a Wednesday
        cal = build_calendar({}, today, weeks=12)

        assert len(cal.columns) == 12
        assert all(len(column) == 7 for column in cal.columns)
        # Every row starts on a Sunday: weekday() == 6.
        assert all(column[0].day.weekday() == 6 for column in cal.columns)
        assert cal.columns[-1][0].day == date(2026, 3, 1)

    def test_days_after_today_are_marked_future(self):
        today = date(2026, 3, 4)
        cal = build_calendar({}, today, weeks=4)
        last = cal.columns[-1]

        assert [cell.is_future for cell in last] == [False, False, False, False, True, True, True]
        assert sum(1 for cell in last if cell.is_today) == 1

    def test_counts_land_on_the_right_cell(self):
        today = date(2026, 3, 4)
        cal = build_calendar({date(2026, 3, 2): 7}, today, weeks=4)
        monday = cal.columns[-1][1]

        assert monday.day == date(2026, 3, 2)
        assert monday.count == 7
        assert monday.level == 3

    def test_window_covers_exactly_the_requested_weeks(self):
        today = date(2026, 3, 4)
        cal = build_calendar({}, today, weeks=53)
        assert cal.end - cal.start == timedelta(days=53 * 7 - 1)

    def test_month_labels_are_ordered_and_in_range(self):
        cal = build_calendar({}, date(2026, 3, 4), weeks=53)
        columns = [label.column for label in cal.month_labels]

        assert columns == sorted(columns)
        assert all(0 < column < 52 for column in columns)
        # A year of labels, minus the partial month at each end.
        assert len(cal.month_labels) == 11
        assert [label.text for label in cal.month_labels][:2] == ["Apr", "May"]

    def test_month_labels_never_sit_close_enough_to_overlap(self):
        """Labels are ~40px wide over ~14px columns, so they need real spacing."""
        for weeks in (12, 26, 53):
            cal = build_calendar({}, date(2026, 3, 4), weeks=weeks)
            columns = [label.column for label in cal.month_labels]
            gaps = [b - a for a, b in zip(columns, columns[1:])]
            assert all(gap >= 4 for gap in gaps), (weeks, columns)


class TestGroupScopedStreaks:
    """A group board measures the run you have kept with these people."""

    TODAY = date(2026, 9, 3)

    def _unbroken(self, days: int) -> dict[date, int]:
        return {self.TODAY - timedelta(days=i): 1 for i in range(days)}

    def test_the_streak_starts_at_the_group_not_your_history(self):
        stats = compute_stats(self._unbroken(30), self.TODAY, streak_since=date(2026, 9, 1))
        assert stats.current_streak == 3
        assert stats.longest_streak == 3

    def test_without_a_group_the_whole_history_counts(self):
        stats = compute_stats(self._unbroken(30), self.TODAY)
        assert stats.current_streak == 30

    def test_a_streak_shorter_than_the_group_is_unaffected(self):
        days = {self.TODAY - timedelta(days=i): 1 for i in range(4)}
        stats = compute_stats(days, self.TODAY, streak_since=date(2026, 1, 1))
        assert stats.current_streak == 4

    def test_a_gap_inside_the_group_still_breaks_it(self):
        days = self._unbroken(10)
        del days[self.TODAY - timedelta(days=2)]
        stats = compute_stats(days, self.TODAY, streak_since=date(2026, 8, 1))
        assert stats.current_streak == 2

    def test_totals_and_streak_use_separate_windows(self):
        stats = compute_stats(
            self._unbroken(30),
            self.TODAY,
            since=date(2026, 9, 2),        # grid shows two days
            streak_since=date(2026, 8, 25),  # group is older than that
        )
        assert stats.active_days == 2
        assert stats.current_streak == 10


class TestCalendarStartDay:
    TODAY = date(2026, 9, 3)  # a Thursday

    def test_days_before_the_start_are_blanked(self):
        """The grid keeps Sunday rows, but nothing before the start day is drawn."""
        start = date(2026, 9, 1)  # Tuesday
        cal = build_calendar({}, self.TODAY, weeks=1, since=start)
        column = cal.columns[0]
        assert [c.is_before_start for c in column][:2] == [True, True]  # Sun, Mon
        assert column[2].day == start and not column[2].is_before_start

    def test_the_first_drawn_square_is_the_start_day(self):
        start = date(2026, 9, 2)
        cal = build_calendar({}, self.TODAY, weeks=1, since=start)
        drawn = [c for col in cal.columns for c in col if not c.is_before_start]
        assert drawn[0].day == start

    def test_a_sunday_start_blanks_nothing(self):
        start = date(2026, 8, 30)  # Sunday
        cal = build_calendar({}, self.TODAY, weeks=1, since=start)
        assert not any(c.is_before_start for col in cal.columns for c in col)

    def test_without_a_start_nothing_is_blanked(self):
        cal = build_calendar({}, self.TODAY, weeks=4)
        assert not any(c.is_before_start for col in cal.columns for c in col)

    def test_earlier_columns_are_unaffected(self):
        """Only the first column can straddle the start date."""
        start = date(2026, 8, 25)
        cal = build_calendar({}, self.TODAY, weeks=3, since=start)
        assert not any(c.is_before_start for c in cal.columns[-1])
