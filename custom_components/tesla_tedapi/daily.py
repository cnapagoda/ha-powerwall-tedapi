"""Today's energy, from the gateway's lifetime counters.

Each value is the lifetime counter now minus the counter at the start of
today (local midnight, Home Assistant's time zone). At midnight the baseline
is the last reading of the day before when it's recent; if Home Assistant
was down over midnight, it's the first reading after. A counter that goes
backwards (a gateway reset, counting again from zero) re-bases, so today's
value carries on instead of going negative. Baselines survive restarts.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

# A reading this close before midnight is a good enough baseline for the new day.
FRESH = timedelta(minutes=10)


def today(now: datetime) -> date:
    return dt_util.as_local(now).date()


def day_start(day: date) -> datetime:
    """Local midnight at the start of `day`, as an aware datetime."""
    return dt_util.start_of_local_day(day)


class DailyEnergy:
    def __init__(self) -> None:
        self.day: date | None = None
        self.baseline: dict[str, float] = {}
        self.last: dict[str, float] = {}
        self.last_at: datetime | None = None

    def update(self, counters: dict[str, Any], now: datetime) -> dict[str, float | None]:
        """One set of readings; returns today's energy per counter (None
        while a counter has no reading)."""
        day = today(now)
        if day != self.day:
            fresh = self.last_at is not None and day_start(day) - self.last_at <= FRESH \
                and today(self.last_at) < day
            self.baseline = dict(self.last) if fresh else {}
            self.day = day
        for key, raw in counters.items():
            if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                continue
            value = float(raw)
            if key not in self.baseline:
                self.baseline[key] = value
            elif value < self.last.get(key, value):
                # The counter restarted from zero: keep what today already
                # had and add the new count on top.
                self.baseline[key] = -(self.last[key] - self.baseline[key])
            self.last[key] = value
        self.last_at = now
        return self.values(counters)

    def values(self, counters: dict[str, Any]) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for key, value in counters.items():
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and key in self.baseline
            out[key] = round(max(float(value) - self.baseline[key], 0.0), 3) if ok else None
        return out

    def as_dict(self) -> dict[str, Any]:
        return {"day": self.day.isoformat() if self.day else None, "baseline": self.baseline,
                "last": self.last, "last_at": self.last_at.isoformat() if self.last_at else None}

    def restore(self, stored: dict[str, Any]) -> None:
        day = stored.get("day")
        self.day = date.fromisoformat(day) if isinstance(day, str) else None
        self.baseline = {k: float(v) for k, v in (stored.get("baseline") or {}).items()}
        self.last = {k: float(v) for k, v in (stored.get("last") or {}).items()}
        last_at = stored.get("last_at")
        self.last_at = dt_util.parse_datetime(last_at) if isinstance(last_at, str) else None
