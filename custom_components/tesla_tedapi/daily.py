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


# Grid counter check: today's grid energy from the counters against the same
# energy integrated from the grid power readings, over the same intervals.
MAX_GAP = timedelta(minutes=5)  # longer between readings: a gap, not counted
MIN_COVERED = timedelta(hours=1)
MIN_KWH = 0.5
MISMATCH_PCT = 10.0
MISMATCH_KWH = 0.5


class GridCheck:
    def __init__(self) -> None:
        self._reset(None)

    def _reset(self, day: date | None) -> None:
        self.day: date | None = day
        self.power_import = self.power_export = 0.0  # kWh, integrated
        self.counter_import = self.counter_export = 0.0  # kWh, over the same intervals
        self.covered = 0.0  # seconds
        self.gaps = 0
        self.last: tuple[datetime, float, float, float] | None = None  # time, W, import, export

    def update(self, day: date | None, power: Any, imported: Any, exported: Any, now: datetime) -> None:
        """One reading: grid power (W, + import) and the lifetime grid counters (kWh)."""
        if day != self.day:
            self._reset(day)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (power, imported, exported)):
            return
        reading = (now, float(power), float(imported), float(exported))
        if self.last is not None:
            seconds = (now - self.last[0]).total_seconds()
            d_import, d_export = reading[2] - self.last[2], reading[3] - self.last[3]
            if seconds > MAX_GAP.total_seconds() or d_import < 0 or d_export < 0:
                self.gaps += 1  # too long, or a counter reset: leave it out on both sides
            elif seconds > 0:
                self._integrate(self.last[1], reading[1], seconds)
                self.counter_import += d_import
                self.counter_export += d_export
                self.covered += seconds
        self.last = reading

    def _integrate(self, p1: float, p2: float, seconds: float) -> None:
        """Trapezoid between two readings, split at the zero crossing into
        import (+) and export (-)."""
        if p1 * p2 < 0:
            first = seconds * abs(p1) / (abs(p1) + abs(p2))
            parts = [(p1 / 2, first), (p2 / 2, seconds - first)]
        else:
            parts = [((p1 + p2) / 2, seconds)]
        for watts, span in parts:
            kwh = watts * span / 3_600_000
            if kwh > 0:
                self.power_import += kwh
            else:
                self.power_export -= kwh

    def summary(self) -> dict[str, Any]:
        def pct(counter: float, power: float) -> float | None:
            return round((counter - power) / power * 100, 1) if power > 0 else None

        state = "unknown"
        enough = self.covered >= MIN_COVERED.total_seconds() and \
            self.power_import + self.power_export >= MIN_KWH
        if enough:
            off = any(abs(c - p) > MISMATCH_KWH and (p <= 0 or abs(c - p) / p * 100 > MISMATCH_PCT)
                      for c, p in ((self.counter_import, self.power_import),
                                   (self.counter_export, self.power_export)))
            state = "mismatch" if off else "ok"
        return {
            "state": state,
            "counter_import_kwh": round(self.counter_import, 3),
            "counter_export_kwh": round(self.counter_export, 3),
            "power_import_kwh": round(self.power_import, 3),
            "power_export_kwh": round(self.power_export, 3),
            "import_difference_pct": pct(self.counter_import, self.power_import),
            "export_difference_pct": pct(self.counter_export, self.power_export),
            "covered_minutes": round(self.covered / 60),
            "gaps": self.gaps,
        }

    def as_dict(self) -> dict[str, Any]:
        last = self.last
        return {"day": self.day.isoformat() if self.day else None,
                "power_import": self.power_import, "power_export": self.power_export,
                "counter_import": self.counter_import, "counter_export": self.counter_export,
                "covered": self.covered, "gaps": self.gaps,
                "last": [last[0].isoformat(), *last[1:]] if last else None}

    def restore(self, stored: dict[str, Any]) -> None:
        day = stored.get("day")
        self.day = date.fromisoformat(day) if isinstance(day, str) else None
        for key in ("power_import", "power_export", "counter_import", "counter_export", "covered"):
            setattr(self, key, float(stored.get(key) or 0.0))
        self.gaps = int(stored.get("gaps") or 0)
        last = stored.get("last")
        when = dt_util.parse_datetime(last[0]) if isinstance(last, list) and len(last) == 4 else None
        self.last = (when, float(last[1]), float(last[2]), float(last[3])) if when else None
