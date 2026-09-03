"""In-process throttle for sign-in attempts.

Deliberately simple: this is a single-process app, so a dict is the whole story. It
slows down online guessing; it is not a defence against a distributed attacker, and it
resets when the process restarts.
"""

from __future__ import annotations

import time
from collections import defaultdict

MAX_ATTEMPTS = 8
# The email-lookup step on /start is not a credential check, and a whole household or
# office can share one IP, so it gets a much looser cap than password attempts do.
LOOKUP_ATTEMPTS = 30
WINDOW_SECONDS = 15 * 60

_failures: dict[str, list[float]] = defaultdict(list)


def _recent(key: str, now: float) -> list[float]:
    kept = [stamp for stamp in _failures[key] if now - stamp < WINDOW_SECONDS]
    _failures[key] = kept
    return kept


def is_blocked(key: str, limit: int = MAX_ATTEMPTS) -> bool:
    return len(_recent(key, time.monotonic())) >= limit


def record_failure(key: str) -> None:
    _failures[key].append(time.monotonic())


def clear(key: str) -> None:
    _failures.pop(key, None)


def retry_after_seconds(key: str) -> int:
    attempts = _recent(key, time.monotonic())
    if not attempts:
        return 0
    return max(1, int(WINDOW_SECONDS - (time.monotonic() - min(attempts))))


def reset_all() -> None:
    """Test hook."""
    _failures.clear()
