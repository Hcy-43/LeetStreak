"""Adapters that turn an external profile into {date: count} activity."""

from __future__ import annotations


class SourceError(RuntimeError):
    """A source could not be read. The message is shown to the user verbatim."""


class ProfileNotFound(SourceError):
    pass
