"""Canonical UTC time, product-local presentation, and generation-window policy."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

PRODUCT_TIMEZONE_NAME = "Asia/Shanghai"
PRODUCT_TIMEZONE = ZoneInfo(PRODUCT_TIMEZONE_NAME)
OFF_PEAK_START = time(18, 30)
OFF_PEAK_END = time(8, 30)


class Clock(Protocol):
    """Provides the current UTC time for deterministic use cases."""

    def now(self) -> datetime:
        """Return an aware UTC timestamp."""


class SystemClock:
    """Clock backed by the operating system."""

    def now(self) -> datetime:
        """Return the current UTC timestamp."""

        return datetime.now(UTC)


def require_aware_utc(value: datetime, field: str = "timestamp") -> datetime:
    """Return an aware UTC instant or reject ambiguous naive input."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def to_product_time(value: datetime) -> datetime:
    """Convert a canonical instant to the product presentation timezone."""

    return require_aware_utc(value).astimezone(PRODUCT_TIMEZONE)


def product_date(value: datetime) -> str:
    """Return the Asia/Shanghai calendar date for an instant."""

    return to_product_time(value).date().isoformat()


def generation_window_open(value: datetime) -> bool:
    """Return whether an instant is inside [18:30, 24:00) or [00:00, 08:30]."""

    local_time = to_product_time(value).timetz().replace(tzinfo=None)
    return local_time >= OFF_PEAK_START or local_time <= OFF_PEAK_END


def generation_decision(
    value: datetime, *, event_name: str, allow_peak_generation: bool = False
) -> str:
    """Return a stable workflow decision without performing model or persistence work."""

    if generation_window_open(value) or allow_peak_generation:
        return "allowed"
    if event_name == "schedule":
        return "scheduled-skip"
    return "manual-blocked"
