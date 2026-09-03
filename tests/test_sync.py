from datetime import datetime, timedelta, timezone

from app.sync import _years_in_window, is_stale, window_start


def ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


class TestStaleness:
    def test_a_user_never_synced_is_stale(self):
        assert is_stale([], ttl_seconds=900)

    def test_a_recent_attempt_is_fresh(self):
        assert not is_stale([{"last_attempt_at": ago(60)}], ttl_seconds=900)

    def test_an_old_attempt_is_stale(self):
        assert is_stale([{"last_attempt_at": ago(3600)}], ttl_seconds=900)

    def test_freshness_uses_the_most_recent_source(self):
        states = [{"last_attempt_at": ago(5000)}, {"last_attempt_at": ago(30)}]
        assert not is_stale(states, ttl_seconds=900)

    def test_a_failed_attempt_still_counts_as_an_attempt(self):
        """Otherwise a broken username would hammer LeetCode on every page load."""
        assert not is_stale(
            [{"last_attempt_at": ago(10), "status": "error", "error": "nope"}], ttl_seconds=900
        )

    def test_rows_without_a_timestamp_are_stale(self):
        assert is_stale([{"last_attempt_at": None}], ttl_seconds=900)

    def test_unparseable_timestamps_are_stale_rather_than_crashing(self):
        assert is_stale([{"last_attempt_at": "not-a-date"}], ttl_seconds=900)


class TestWindow:
    def test_window_covers_a_full_year_grid(self):
        today = datetime(2026, 9, 3, tzinfo=timezone.utc).date()
        assert (today - window_start(today)).days == 53 * 7

    def test_year_list_spans_a_new_year_boundary(self):
        today = datetime(2026, 2, 1, tzinfo=timezone.utc).date()
        assert _years_in_window(window_start(today), today) == [2025, 2026]

    def test_year_list_always_covers_the_start_of_the_window(self):
        """A 53-week window is longer than a year, so it always needs two fetches."""
        for month in range(1, 13):
            today = datetime(2026, month, 15, tzinfo=timezone.utc).date()
            years = _years_in_window(window_start(today), today)
            assert window_start(today).year in years
            assert today.year in years
            assert len(years) == 2
