"""Grid outages, from the gateway's own grid status.

An outage starts when the gateway reports the grid as not up (DOWN, or
SYNCING back) and ends when it is UP again. Readings that don't arrive (the
gateway unreachable) change nothing, and an off-grid run started here is not
an outage. Counts are kept per calendar month (local time) and survive
restarts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util


@dataclass
class OutageChange:
    phase: str  # "started" or "ended"
    start: datetime
    duration_seconds: float | None = None


def _month(when: datetime) -> str:
    return dt_util.as_local(when).strftime("%Y-%m")


def month_start(month: str) -> datetime:
    year, mon = (int(x) for x in month.split("-"))
    return dt_util.as_utc(datetime(year, mon, 1, tzinfo=dt_util.get_default_time_zone()))


class OutageTracker:
    def __init__(self) -> None:
        self.current: datetime | None = None
        self.intentional = False
        self.last_start: datetime | None = None
        self.last_end: datetime | None = None
        self.last_duration: float | None = None
        self.month: str | None = None
        self.month_count = 0
        self.month_seconds = 0.0
        self.total_count = 0

    def _roll(self, now: datetime) -> None:
        month = _month(now)
        if month != self.month:
            self.month = month
            self.month_count = 0
            self.month_seconds = 0.0

    def update(self, grid_status: str | None, now: datetime, intentional: bool = False) -> OutageChange | None:
        """One reading. Returns the change it caused, if any (not for an
        off-grid run started here)."""
        self._roll(now)
        if grid_status is None:
            return None
        if grid_status != "UP":
            if self.current is None:
                self.current, self.intentional = now, intentional
                if not intentional:
                    self.month_count += 1
                    self.total_count += 1
                    return OutageChange("started", now)
            return None
        if self.current is None:
            return None
        start, was_intentional = self.current, self.intentional
        self.current, self.intentional = None, False
        if was_intentional:
            return None
        duration = (now - start).total_seconds()
        self.last_start, self.last_end, self.last_duration = start, now, duration
        self.month_seconds += duration
        return OutageChange("ended", start, duration)

    def ongoing_seconds(self, now: datetime) -> float | None:
        if self.current is None or self.intentional:
            return None
        return (now - self.current).total_seconds()

    def summary(self, now: datetime) -> dict[str, Any]:
        """For the sensors: the outage in progress counts as the last one."""
        self._roll(now)
        ongoing = self.ongoing_seconds(now)
        return {
            "ongoing": ongoing is not None,
            "last_start": self.current if ongoing is not None else self.last_start,
            "last_end": None if ongoing is not None else self.last_end,
            "last_duration": self.last_duration,
            "month": self.month,
            "month_count": self.month_count,
            "month_seconds": self.month_seconds + (ongoing or 0.0),
            "total_count": self.total_count,
        }

    def as_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "current": iso(self.current), "intentional": self.intentional,
            "last_start": iso(self.last_start), "last_end": iso(self.last_end),
            "last_duration": self.last_duration, "month": self.month,
            "month_count": self.month_count, "month_seconds": self.month_seconds,
            "total_count": self.total_count,
        }

    def restore(self, stored: dict[str, Any]) -> None:
        def parse(value: Any) -> datetime | None:
            return dt_util.parse_datetime(value) if isinstance(value, str) else None

        self.current = parse(stored.get("current"))
        self.intentional = bool(stored.get("intentional"))
        self.last_start = parse(stored.get("last_start"))
        self.last_end = parse(stored.get("last_end"))
        self.last_duration = stored.get("last_duration")
        self.month = stored.get("month")
        self.month_count = int(stored.get("month_count") or 0)
        self.month_seconds = float(stored.get("month_seconds") or 0.0)
        self.total_count = int(stored.get("total_count") or 0)
