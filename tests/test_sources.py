import asyncio
import json
from datetime import date

import httpx
import pytest

from app.sources import ProfileNotFound, SourceError
from app.sources.github import normalise_repo
from app.sources.leetcode import _parse_submission_calendar, fetch_activity


def run_with_response(response: httpx.Response, username: str = "someone", years=(2026,)):
    """Drive fetch_activity against a canned HTTP response."""
    transport = httpx.MockTransport(lambda request: response)

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await fetch_activity(client, username, list(years))

    return asyncio.run(go())


def graphql(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


class TestLeetCodeCalendar:
    def test_parses_utc_midnight_epoch_keys(self):
        # 1735689600 == 2025-01-01T00:00:00Z
        counts = _parse_submission_calendar('{"1735689600": 3, "1735776000": 1}')
        assert counts == {date(2025, 1, 1): 3, date(2025, 1, 2): 1}

    def test_ignores_zero_and_malformed_entries(self):
        counts = _parse_submission_calendar('{"1735689600": 0, "nope": 2, "1735776000": "x"}')
        assert counts == {}

    def test_missing_or_broken_payload_is_empty(self):
        assert _parse_submission_calendar(None) == {}
        assert _parse_submission_calendar("") == {}
        assert _parse_submission_calendar("not json") == {}


class TestLeetCodeErrors:
    """LeetCode reports a missing profile as a GraphQL error, not a null user."""

    def test_unknown_username_raises_profile_not_found(self):
        response = graphql(
            {
                "errors": [{"message": "That user does not exist."}],
                "data": {"matchedUser": None},
            }
        )
        with pytest.raises(ProfileNotFound) as caught:
            run_with_response(response, username="ghost")
        assert "ghost" in str(caught.value)

    def test_other_graphql_errors_stay_generic(self):
        response = graphql({"errors": [{"message": "internal server error"}]})
        with pytest.raises(SourceError) as caught:
            run_with_response(response)
        assert not isinstance(caught.value, ProfileNotFound)

    def test_rate_limiting_says_so(self):
        with pytest.raises(SourceError, match="rate limit"):
            run_with_response(httpx.Response(429))

    def test_server_errors_are_reported_not_swallowed(self):
        with pytest.raises(SourceError, match="HTTP 503"):
            run_with_response(httpx.Response(503))

    def test_non_json_body_is_reported(self):
        with pytest.raises(SourceError, match="non-JSON"):
            run_with_response(httpx.Response(200, text="<html>maintenance</html>"))

    def test_a_real_payload_is_parsed_into_days(self):
        response = graphql(
            {
                "data": {
                    "matchedUser": {
                        "username": "someone",
                        "userCalendar": {
                            "submissionCalendar": json.dumps({"1735689600": 4}),
                        },
                    }
                }
            }
        )
        assert run_with_response(response) == {date(2025, 1, 1): 4}

    def test_an_empty_username_short_circuits_without_a_request(self):
        def explode(request):  # pragma: no cover - must never be called
            raise AssertionError("should not hit the network")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(explode)) as client:
                return await fetch_activity(client, "   ", [2026])

        assert asyncio.run(go()) == {}


class TestRepoNormalisation:
    @pytest.mark.parametrize(
        "raw",
        [
            "octocat/leetcode",
            "https://github.com/octocat/leetcode",
            "https://www.github.com/octocat/leetcode.git",
            "  github.com/octocat/leetcode/  ",
            "https://github.com/octocat/leetcode/tree/main/problems",
        ],
    )
    def test_accepts_the_shapes_people_actually_paste(self, raw):
        assert normalise_repo(raw) == "octocat/leetcode"

    def test_blank_stays_blank(self):
        assert normalise_repo("") == ""
        assert normalise_repo("   ") == ""

    @pytest.mark.parametrize("raw", ["octocat", "octo cat/repo", "owner/repo!"])
    def test_rejects_nonsense(self, raw):
        with pytest.raises(ValueError):
            normalise_repo(raw)
