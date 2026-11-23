"""Battery energy charged and discharged, netted as Tesla's app counts them.

The gateway's battery counters count energy both ways at once: while the
battery only discharges, its charged counter still creeps up (and the other
way round while it charges), about 0.3-0.8 kWh a day each way on a Powerwall 3
with an Expansion Pack (#35). Tesla's app and cloud net the two in 5-minute
windows: within each window only the difference counts, as charged or as
discharged. These totals do the same with the gateway's counters, and match
Tesla's daily totals to within a few hundredths of a kWh.

A window runs from the first reading after one 5-minute boundary to the first
reading after the next, so no energy falls between windows. A gap (Home
Assistant down, the gateway unreachable) is netted as one window: that can
only leave out energy that went both ways, never count extra. A counter that
goes back (a gateway reset) starts the window again from the new reading.
The totals start from the gateway's own counters and survive restarts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

WINDOW_SECONDS = 300


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class NetBatteryEnergy:
    def __init__(self) -> None:
        self.window: int | None = None  # index of the open window
        self.start: tuple[float, float] | None = None  # counters when it opened
        self.last: tuple[float, float] | None = None  # the latest counters
        self.charged: float | None = None
        self.discharged: float | None = None

    def update(self, now: datetime, charged: Any, discharged: Any) -> None:
        """One reading of the gateway's lifetime counters (kWh)."""
        if not (_number(charged) and _number(discharged)):
            return
        reading = (float(charged), float(discharged))
        window = int(now.timestamp() // WINDOW_SECONDS)
        if self.charged is None or self.discharged is None:
            self.charged, self.discharged = reading
        if self.start is None or self.last is None:
            self.window, self.start, self.last = window, reading, reading
            return
        if reading[0] < self.last[0] or reading[1] < self.last[1]:
            # The counters were reset: count the open window up to the last
            # reading, and start again from this one.
            self._close(self.start, self.last)
            self.window, self.start, self.last = window, reading, reading
            return
        self.last = reading
        if window != self.window:
            self._close(self.start, reading)
            self.window, self.start = window, reading

    def _close(self, start: tuple[float, float], end: tuple[float, float]) -> None:
        net = (end[0] - start[0]) - (end[1] - start[1])
        if net > 0:
            self.charged = round((self.charged or 0.0) + net, 3)
        elif net < 0:
            self.discharged = round((self.discharged or 0.0) - net, 3)

    def summary(self) -> dict[str, float | None]:
        return {"battery_energy_charged": self.charged, "battery_energy_discharged": self.discharged}

    def as_dict(self) -> dict[str, Any]:
        return {"window": self.window, "start": list(self.start) if self.start else None,
                "last": list(self.last) if self.last else None,
                "charged": self.charged, "discharged": self.discharged}

    def restore(self, stored: dict[str, Any]) -> None:
        def pair(value: Any) -> tuple[float, float] | None:
            if isinstance(value, (list, tuple)) and len(value) == 2 and all(_number(v) for v in value):
                return float(value[0]), float(value[1])
            return None

        window = stored.get("window")
        self.window = window if isinstance(window, int) and not isinstance(window, bool) else None
        self.start, self.last = pair(stored.get("start")), pair(stored.get("last"))
        if self.window is None or self.start is None or self.last is None:
            self.window = self.start = self.last = None
        charged, discharged = stored.get("charged"), stored.get("discharged")
        if _number(charged) and _number(discharged):
            self.charged, self.discharged = float(charged), float(discharged)
