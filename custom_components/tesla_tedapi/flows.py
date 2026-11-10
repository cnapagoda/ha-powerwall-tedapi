"""Energy flows: where the solar, battery and grid energy went.

The gateway reports four power readings (solar, battery, grid, home). They are
split into seven flows by the usual priority, the one the Tesla app shows:

- solar feeds the home first, then the battery, then the grid;
- the battery feeds what the home still needs, then the grid;
- the grid feeds the rest of the home, then the battery.

Each flow's power is integrated between readings into a lifetime energy
counter (kWh). The split is an attribution, not a measurement: the gateway
doesn't meter the flows themselves. A gap between readings longer than
max_gap (MAX_GAP_SECONDS, or GAP_POLLS polls when longer) isn't counted (Home Assistant or the gateway was away), so the
counters only ever miss energy, never invent it. They survive restarts.

No Home Assistant imports: the arithmetic is tested on its own.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

FLOWS = (
    "solar_to_home",
    "solar_to_battery",
    "solar_to_grid",
    "battery_to_home",
    "battery_to_grid",
    "grid_to_home",
    "grid_to_battery",
)

# Longer between two readings: a gap, not counted. At least this, and at least
# GAP_POLLS polls (a long polling interval would otherwise count nothing).
MAX_GAP_SECONDS = 300.0
GAP_POLLS = 2.5


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def split(solar: float, battery: float, grid: float, home: float) -> dict[str, float]:
    """The seven flows (W, never negative) for one set of readings.

    battery: + discharging, - charging. grid: + importing, - exporting. A
    slightly negative solar or home reading (meter noise at night) counts as 0.
    """
    produced = max(solar, 0.0)
    discharge, charge = max(battery, 0.0), max(-battery, 0.0)
    imported, exported = max(grid, 0.0), max(-grid, 0.0)
    need = max(home, 0.0)

    solar_to_home = min(produced, need)
    solar_to_battery = min(produced - solar_to_home, charge)
    solar_to_grid = min(produced - solar_to_home - solar_to_battery, exported)
    need -= solar_to_home

    battery_to_home = min(discharge, need)
    battery_to_grid = min(discharge - battery_to_home, exported - solar_to_grid)
    need -= battery_to_home

    grid_to_home = min(imported, need)
    grid_to_battery = min(imported - grid_to_home, charge - solar_to_battery)

    return {
        "solar_to_home": solar_to_home,
        "solar_to_battery": solar_to_battery,
        "solar_to_grid": solar_to_grid,
        "battery_to_home": battery_to_home,
        "battery_to_grid": battery_to_grid,
        "grid_to_home": grid_to_home,
        "grid_to_battery": grid_to_battery,
    }


class EnergyFlows:
    """Lifetime energy per flow (kWh), integrated from the power readings."""

    def __init__(self, poll_seconds: float = 0) -> None:
        self.max_gap = max(MAX_GAP_SECONDS, GAP_POLLS * poll_seconds)
        self.totals: dict[str, float] = dict.fromkeys(FLOWS, 0.0)
        # The last reading: when, and each flow's power (W).
        self.last: tuple[datetime, dict[str, float]] | None = None

    def update(self, now: datetime, solar: Any, battery: Any, grid: Any, home: Any) -> None:
        """One set of readings (W). A missing reading breaks the chain: the
        time up to the next complete one isn't counted."""
        if not all(_number(v) for v in (solar, battery, grid, home)):
            self.last = None
            return
        flows = split(float(solar), float(battery), float(grid), float(home))
        if self.last is not None:
            seconds = (now - self.last[0]).total_seconds()
            if 0 < seconds <= self.max_gap:
                for name in FLOWS:
                    # Trapezoid: the mean of the two readings over the time between.
                    self.totals[name] += (self.last[1][name] + flows[name]) / 2 * seconds / 3_600_000
        self.last = (now, flows)

    def pause(self) -> None:
        """The gateway can't be read: don't bridge the time until it can."""
        self.last = None

    def summary(self) -> dict[str, float]:
        return {name: round(value, 3) for name, value in self.totals.items()}

    def as_dict(self) -> dict[str, Any]:
        return {"totals": dict(self.totals)}

    def restore(self, stored: dict[str, Any]) -> None:
        totals = stored.get("totals") if isinstance(stored, dict) else None
        if not isinstance(totals, dict):
            return
        for name in FLOWS:
            value = totals.get(name)
            if _number(value) and value >= 0:
                self.totals[name] = float(value)
