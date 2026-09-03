from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import ProfileNotFound, SourceError

REST_URL = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

CONTRIBUTIONS_QUERY = """
query contributions($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def normalise_repo(value: str) -> str:
    """Accept `owner/name` or a full GitHub URL; return `owner/name`."""
    value = (value or "").strip()
    if not value:
        return ""
    value = re.sub(r"^(https?://)?(www\.)?github\.com/", "", value, flags=re.IGNORECASE)
    value = value.removesuffix(".git").strip("/")
    parts = value.split("/")
    if len(parts) >= 2:
        value = f"{parts[0]}/{parts[1]}"
    if not REPO_RE.match(value):
        raise ValueError("Repository must look like 'owner/name'.")
    return value


def _zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "leetstreak",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _check(response: httpx.Response, what: str) -> None:
    if response.status_code == 404:
        raise ProfileNotFound(f"{what} not found on GitHub (or it is private).")
    if response.status_code in (401, 403):
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining == "0":
            raise SourceError(
                "GitHub API rate limit reached. Set GITHUB_TOKEN to raise it to 5000/hour."
            )
        raise SourceError(f"GitHub denied the request for {what} (HTTP {response.status_code}).")
    if response.status_code >= 400:
        raise SourceError(f"GitHub returned HTTP {response.status_code} for {what}.")


async def fetch_repo_commits(
    client: httpx.AsyncClient,
    repo: str,
    author: str,
    since: date,
    until: date,
    token: str,
    timezone_name: str = "UTC",
) -> dict[date, int]:
    """Commits by `author` in `repo`, bucketed into the user's local days.

    This is the accurate signal when someone uses LeetHub/LeetSync to push each
    accepted solution: one commit per solve, in one dedicated repo.
    """
    zone = _zone(timezone_name)
    since_utc = datetime.combine(since, time.min, tzinfo=zone).astimezone(timezone.utc)
    until_utc = datetime.combine(until + timedelta(days=1), time.min, tzinfo=zone).astimezone(
        timezone.utc
    )

    counts: dict[date, int] = {}
    page = 1
    while page <= 20:  # 2000 commits is far more than a year of daily solving
        try:
            response = await client.get(
                f"{REST_URL}/repos/{repo}/commits",
                headers=_headers(token),
                params={
                    "author": author,
                    "since": since_utc.isoformat().replace("+00:00", "Z"),
                    "until": until_utc.isoformat().replace("+00:00", "Z"),
                    "per_page": 100,
                    "page": page,
                },
                timeout=25.0,
            )
        except httpx.HTTPError as exc:
            raise SourceError(f"Could not reach GitHub: {exc}") from exc

        _check(response, f"{repo}")
        batch = response.json()
        if not isinstance(batch, list) or not batch:
            break

        for item in batch:
            stamp = ((item.get("commit") or {}).get("author") or {}).get("date")
            if not stamp:
                continue
            try:
                moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            local_day = moment.astimezone(zone).date()
            counts[local_day] = counts.get(local_day, 0) + 1

        if len(batch) < 100:
            break
        page += 1

    return counts


async def fetch_contributions(
    client: httpx.AsyncClient, login: str, since: date, until: date, token: str
) -> dict[date, int]:
    """The public contribution calendar — the literal green squares.

    Counts *all* GitHub activity, not just LeetCode, so it is the fallback for
    people who have not pointed us at a dedicated solutions repo.
    """
    if not token:
        raise SourceError("The GitHub contributions calendar requires GITHUB_TOKEN to be set.")

    counts: dict[date, int] = {}
    # The GraphQL API caps each query at one year, so walk the range in chunks.
    window_start = since
    while window_start <= until:
        window_end = min(until, window_start + timedelta(days=364))
        variables = {
            "login": login,
            "from": datetime.combine(window_start, time.min, tzinfo=timezone.utc).isoformat(),
            "to": datetime.combine(window_end, time.max, tzinfo=timezone.utc).isoformat(),
        }
        try:
            response = await client.post(
                GRAPHQL_URL,
                headers=_headers(token),
                json={"query": CONTRIBUTIONS_QUERY, "variables": variables},
                timeout=25.0,
            )
        except httpx.HTTPError as exc:
            raise SourceError(f"Could not reach GitHub: {exc}") from exc

        _check(response, f"contributions for {login}")
        payload = response.json()
        if payload.get("errors"):
            message = payload["errors"][0].get("message", "unknown error")
            raise SourceError(f"GitHub error: {message}")

        user = (payload.get("data") or {}).get("user")
        if not user:
            raise ProfileNotFound(f"No GitHub user named '{login}'.")

        weeks = (
            ((user.get("contributionsCollection") or {}).get("contributionCalendar") or {}).get(
                "weeks"
            )
            or []
        )
        for week in weeks:
            for entry in week.get("contributionDays") or []:
                try:
                    day = date.fromisoformat(entry["date"])
                    count = int(entry.get("contributionCount", 0))
                except (KeyError, TypeError, ValueError):
                    continue
                if count > 0:
                    counts[day] = counts.get(day, 0) + count

        window_start = window_end + timedelta(days=1)

    return counts
