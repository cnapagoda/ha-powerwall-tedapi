"""Battery economics: round-trip efficiency and the capacity trend.

Round-trip efficiency is the battery's lifetime energy out over its lifetime
energy in (the gateway's own counters).

The capacity trend keeps one sample a day: the day's mean full capacity per
pack (the system's full capacity over the number of packs, so it also covers
packs that aren't read one by one). A straight line through the samples gives
the fade per year and, when the line falls, the date it reaches the warranty
level (70 % of the rated 13.5 kWh). The gateway's capacity estimate moves with
temperature and with how deeply the battery cycles, so nothing is reported
until there are MIN_SAMPLES samples over at least MIN_DAYS days, and the date
is an estimate from a straight line, not a prediction of the battery.

No Home Assistant imports: the arithmetic is tested on its own.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

NOMINAL_PACK_KWH = 13.5
WARRANTY_SHARE = 0.7

# Efficiency needs this much energy through the battery to mean anything.
MIN_EFFICIENCY_KWH = 50.0

MIN_SAMPLES = 30
MIN_DAYS = 90
MAX_SAMPLES = 1500  # about four years of daily samples
# A projected date further away than this isn't reported.
MAX_PROJECTION_DAYS = 365 * 50


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def round_trip_efficiency(charged_kwh: Any, discharged_kwh: Any) -> float | None:
    """Lifetime energy out over energy in (%), or None with too little
    history. The energy still in the battery is ignored: over a lifetime of
    cycles it is a rounding error."""
    if not _number(charged_kwh) or not _number(discharged_kwh) or charged_kwh < MIN_EFFICIENCY_KWH:
        return None
    return round(discharged_kwh / charged_kwh * 100, 1)


class CapacityTrend:
    """Daily samples of the full capacity per pack, and the line through them."""

    def __init__(self) -> None:
        # Finished days: (day ordinal, mean kWh per pack), oldest first.
        self.samples: list[tuple[int, float]] = []
        self.packs: int | None = None
        # Today so far.
        self.day: int | None = None
        self._sum = 0.0
        self._count = 0
        # A pack count that differs from self.packs, and the day it was first
        # seen: readings with it are skipped until it has lasted into a later
        # day, so one odd read (the gateway's config not answering, a pack
        # missing from one poll) doesn't throw the history away.
        self.pending: tuple[int, int] | None = None

    def update(self, day: date, kwh_per_pack: Any, packs: int) -> None:
        """One reading of the full capacity per pack on `day` (local date)."""
        if not _number(kwh_per_pack) or kwh_per_pack <= 0 or packs <= 0:
            return
        ordinal = day.toordinal()
        if self.packs is not None and packs != self.packs:
            if self.pending is None or self.pending[0] != packs:
                self.pending = (packs, ordinal)
            if ordinal <= self.pending[1]:
                return
            # The new count lasted into a later day: a pack was added or
            # removed. The average per pack jumps, so the earlier samples no
            # longer describe this battery.
            self.samples, self.day, self._sum, self._count = [], None, 0.0, 0
        self.pending = None
        self.packs = packs
        if self.day is not None and ordinal != self.day:
            self._finish_day()
        self.day = ordinal
        self._sum += float(kwh_per_pack)
        self._count += 1

    def _finish_day(self) -> None:
        if self.day is not None and self._count:
            self.samples.append((self.day, self._sum / self._count))
            self.samples = self.samples[-MAX_SAMPLES:]
        self._sum, self._count = 0.0, 0

    def _points(self) -> list[tuple[int, float]]:
        points = list(self.samples)
        if self.day is not None and self._count and (not points or points[-1][0] != self.day):
            points.append((self.day, self._sum / self._count))
        return points

    def summary(self) -> dict[str, Any]:
        """{"samples", "days", and once there is enough history:
        "kwh_per_year" (negative while the capacity falls), "pct_per_year" (of
        the rated capacity), "fit_kwh" (the line's value today) and
        "warranty_date" (None unless the line falls and reaches it)}."""
        points = self._points()
        days = points[-1][0] - points[0][0] if points else 0
        result: dict[str, Any] = {"samples": len(points), "days": days, "kwh_per_year": None,
                                  "pct_per_year": None, "fit_kwh": None, "warranty_date": None}
        if len(points) < MIN_SAMPLES or days < MIN_DAYS:
            return result
        n = len(points)
        mean_x = sum(x for x, _ in points) / n
        mean_y = sum(y for _, y in points) / n
        # Never zero: the samples span at least MIN_DAYS days.
        spread = sum((x - mean_x) ** 2 for x, _ in points)
        slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / spread  # kWh per day
        last_x = points[-1][0]
        fit = mean_y + slope * (last_x - mean_x)
        result["kwh_per_year"] = round(slope * 365.25, 3)
        result["pct_per_year"] = round(slope * 365.25 / NOMINAL_PACK_KWH * 100, 2)
        result["fit_kwh"] = round(fit, 2)
        warranty = NOMINAL_PACK_KWH * WARRANTY_SHARE
        if fit <= warranty:
            result["warranty_date"] = date.fromordinal(last_x)
        elif slope < 0:
            days_left = (warranty - fit) / slope
            if days_left <= MAX_PROJECTION_DAYS:
                result["warranty_date"] = date.fromordinal(last_x) + timedelta(days=round(days_left))
        return result

    def as_dict(self) -> dict[str, Any]:
        return {"samples": [[x, round(y, 4)] for x, y in self.samples], "packs": self.packs,
                "day": self.day, "sum": self._sum, "count": self._count,
                "pending": list(self.pending) if self.pending else None}

    def restore(self, stored: dict[str, Any]) -> None:
        if not isinstance(stored, dict):
            return
        samples = stored.get("samples")
        if isinstance(samples, list):
            self.samples = [(int(s[0]), float(s[1])) for s in samples
                            if isinstance(s, (list, tuple)) and len(s) == 2
                            and _number(s[0]) and _number(s[1])][-MAX_SAMPLES:]
        packs = stored.get("packs")
        self.packs = int(packs) if _number(packs) and packs > 0 else None
        day, total, count = stored.get("day"), stored.get("sum"), stored.get("count")
        if _number(day) and _number(total) and _number(count) and count > 0:
            self.day, self._sum, self._count = int(day), float(total), int(count)
        pending = stored.get("pending")
        if isinstance(pending, (list, tuple)) and len(pending) == 2 and all(_number(v) for v in pending):
            self.pending = (int(pending[0]), int(pending[1]))
