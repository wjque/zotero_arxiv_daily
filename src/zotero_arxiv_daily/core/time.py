"""Injectable clock abstractions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Provides the current UTC time for deterministic use cases."""

    def now(self) -> datetime:
        """Return an aware UTC timestamp."""


class SystemClock:
    """Clock backed by the operating system."""

    def now(self) -> datetime:
        """Return the current UTC timestamp."""

        return datetime.now(UTC)
