from __future__ import annotations

import json
from datetime import date, datetime, timezone

import httpx

from . import ProfileNotFound, SourceError

GRAPHQL_URL = "https://leetcode.com/graphql/"

# LeetCode rejects requests that do not look like a browser.
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Referer": "https://leetcode.com",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
}

CALENDAR_QUERY = """
query userProfileCalendar($username: String!, $year: Int) {
  matchedUser(username: $username) {
    username
    userCalendar(year: $year) {
      activeYears
      streak
      totalActiveDays
      submissionCalendar
    }
  }
}
"""

PROFILE_QUERY = """
query userPublicProfile($username: String!) {
  matchedUser(username: $username) {
    username
    profile { realName userAvatar ranking }
    submitStatsGlobal { acSubmissionNum { difficulty count } }
  }
}
"""


# LeetCode caps this at 20 no matter what limit is asked for, so there is no
# backfill: history only accumulates from the first time we look.
RECENT_QUERY = """
query recentAcSubmissions($username: String!, $limit: Int!) {
  recentAcSubmissionList(username: $username, limit: $limit) {
    id
    title
    titleSlug
    timestamp
  }
}
"""

DIFFICULTY_QUERY = """
query questionDetail($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    questionFrontendId
    title
    difficulty
    topicTags { name }
  }
}
"""

RECENT_LIMIT = 20


async def _post(
    client: httpx.AsyncClient, query: str, variables: dict, subject: str = ""
) -> dict:
    try:
        response = await client.post(
            GRAPHQL_URL,
            headers=HEADERS,
            json={"query": query, "variables": variables},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise SourceError(f"Could not reach LeetCode: {exc}") from exc

    if response.status_code == 429:
        raise SourceError("LeetCode is rate limiting us; try again in a few minutes.")
    if response.status_code >= 400:
        raise SourceError(f"LeetCode returned HTTP {response.status_code}.")

    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceError("LeetCode returned a non-JSON response.") from exc

    if payload.get("errors"):
        message = payload["errors"][0].get("message", "unknown GraphQL error")
        # A missing profile arrives as a GraphQL error rather than a null user,
        # so map it explicitly: a typo in a username is not an outage.
        if "does not exist" in message.lower():
            raise ProfileNotFound(f"No LeetCode user named '{subject}'.")
        raise SourceError(f"LeetCode error: {message}")
    return payload.get("data") or {}


def _parse_submission_calendar(raw: str | None) -> dict[date, int]:
    """`submissionCalendar` is a JSON *string* of {utc_midnight_epoch: count}."""
    if not raw:
        return {}
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return {}

    counts: dict[date, int] = {}
    for key, value in entries.items():
        try:
            moment = datetime.fromtimestamp(int(key), tz=timezone.utc)
            count = int(value)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if count > 0:
            counts[moment.date()] = counts.get(moment.date(), 0) + count
    return counts


async def fetch_activity(
    client: httpx.AsyncClient, username: str, years: list[int]
) -> dict[date, int]:
    """Accepted-submission counts per day, keyed by UTC calendar day.

    LeetCode buckets its own calendar and streak by UTC, so we keep that boundary
    rather than re-deriving one; see the note in README about day boundaries.
    """
    username = username.strip()
    if not username:
        return {}

    counts: dict[date, int] = {}
    seen_user = False
    for year in years:
        data = await _post(
            client, CALENDAR_QUERY, {"username": username, "year": year}, subject=username
        )
        matched = data.get("matchedUser")
        if not matched:
            raise ProfileNotFound(f"No LeetCode user named '{username}'.")
        seen_user = True
        calendar = matched.get("userCalendar") or {}
        for day, count in _parse_submission_calendar(calendar.get("submissionCalendar")).items():
            counts[day] = counts.get(day, 0) + count

    if not seen_user:
        raise ProfileNotFound(f"No LeetCode user named '{username}'.")
    return counts


async def fetch_profile(client: httpx.AsyncClient, username: str) -> dict:
    """Used to validate a username at save time and to grab an avatar."""
    username = username.strip()
    data = await _post(client, PROFILE_QUERY, {"username": username}, subject=username)
    matched = data.get("matchedUser")
    if not matched:
        raise ProfileNotFound(f"No LeetCode user named '{username}'.")

    profile = matched.get("profile") or {}
    stats = (matched.get("submitStatsGlobal") or {}).get("acSubmissionNum") or []
    solved = next((row.get("count", 0) for row in stats if row.get("difficulty") == "All"), 0)
    return {
        "username": matched.get("username", username),
        "real_name": profile.get("realName") or "",
        "avatar_url": profile.get("userAvatar") or "",
        "ranking": profile.get("ranking"),
        "total_solved": solved,
    }


async def fetch_recent_solved(
    client: httpx.AsyncClient, username: str
) -> list[dict[str, object]]:
    """The most recent accepted submissions, newest first.

    Returns at most 20 - LeetCode's own ceiling. Someone who solves more than that
    between two syncs loses the overflow, which is why syncing often matters.
    """
    payload = await _post(
        client,
        RECENT_QUERY,
        {"username": username, "limit": RECENT_LIMIT},
        subject=username,
    )
    rows = payload.get("recentAcSubmissionList") or []
    solved = []
    for row in rows:
        slug = (row or {}).get("titleSlug")
        stamp = (row or {}).get("timestamp")
        if not slug or not stamp:
            continue
        try:
            moment = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
        except (TypeError, ValueError):
            continue
        solved.append(
            {"slug": slug, "title": row.get("title") or slug, "solved_at": moment}
        )
    return solved


async def fetch_difficulty(client: httpx.AsyncClient, slug: str) -> dict[str, str]:
    """Number, title, difficulty and tags. None of it changes, so cache it forever."""
    payload = await _post(client, DIFFICULTY_QUERY, {"titleSlug": slug}, subject=slug)
    question = payload.get("question") or {}
    tags = [t.get("name") for t in (question.get("topicTags") or []) if t.get("name")]
    return {
        "slug": slug,
        "number": (question.get("questionFrontendId") or "").strip(),
        "title": question.get("title") or slug,
        "difficulty": question.get("difficulty") or "Unknown",
        "tags": tags,
    }
