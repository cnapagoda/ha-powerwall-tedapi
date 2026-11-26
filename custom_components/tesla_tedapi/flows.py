"""Energy flows: where the solar, battery and grid energy went.

The gateway reports four power readings (solar, battery, grid, home). They are
split into seven flows in this order:

- solar charges the battery first, then feeds the home, then the grid;
- the battery feeds what the home still needs, then the grid;
- the grid feeds the rest of the home, then the battery.

Solar into the battery before the home only matters while the grid is
imported and the battery charges at the same time: the home runs from the grid
while solar charges the battery (time-based control off-peak, for example), or
grid charging with the sun up. Solar is then counted into the battery and the
grid into the home. That's
how Tesla's own counters (the Tesla app, tesla_fleet, Teslemetry) and Home
Assistant's Energy dashboard count it (#33). Solar on a Powerwall 3's own
inputs does reach the battery first; for a separate solar inverter it's a
convention, kept the same so the counters match Tesla's.

Each flow's power is integrated between readings into a lifetime energy
counter (kWh). The split is an attribution, not a measurement: the gateway
doesn't meter the flows themselves. A gap between readings longer than
max_gap (MAX_GAP_SECONDS, or GAP_POLLS polls when longer) can't be integrated
(Home Assistant or the gateway was away). Since 2.9.0 such a gap is filled at
the next full reading from the gateway's lifetime counters: what they gained
since the full reading before the gap, less what the readings counted
meanwhile, split in the same order (split_energy). Each flow is capped by its
source and its sink, so the counters' Powerwall losses can't invent energy.
Not filled: a gap over MAX_FILL_SECONDS, or a counter that went down. The
counters and the totals survive restarts, so a restart's gap is filled too.

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
# The longest gap filled from the lifetime counters: over a longer one the
# split of the totals says too little about what went where.
MAX_FILL_SECONDS = 24 * 3600.0
# The lifetime counters (kWh, as the energy sensors) and the flows they add up.
COUNTER_FLOWS: dict[str, tuple[str, ...]] = {
    "solar_energy_produced": ("solar_to_home", "solar_to_battery", "solar_to_grid"),
    "battery_energy_charged": ("solar_to_battery", "grid_to_battery"),
    "battery_energy_discharged": ("battery_to_home", "battery_to_grid"),
    "grid_energy_imported": ("grid_to_home", "grid_to_battery"),
    "grid_energy_exported": ("solar_to_grid", "battery_to_grid"),
    "home_energy_consumed": ("solar_to_home", "battery_to_home", "grid_to_home"),
}


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

    solar_to_battery = min(produced, charge)
    solar_to_home = min(produced - solar_to_battery, need)
    solar_to_grid = min(produced - solar_to_battery - solar_to_home, exported)
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


def split_energy(gained: dict[str, float]) -> dict[str, float]:
    """The seven flows (kWh) for what each lifetime counter (COUNTER_FLOWS)
    gained over a stretch of time, in the same order as split(). Each flow is
    at most what its source gave and its sink took."""
    solar, charged = gained["solar_energy_produced"], gained["battery_energy_charged"]
    discharged, home = gained["battery_energy_discharged"], gained["home_energy_consumed"]
    imported, exported = gained["grid_energy_imported"], gained["grid_energy_exported"]
    solar_to_battery = min(solar, charged)
    solar_to_home = min(solar - solar_to_battery, home)
    solar_to_grid = min(solar - solar_to_battery - solar_to_home, exported)
    need = home - solar_to_home
    battery_to_home = min(discharged, need)
    battery_to_grid = min(discharged - battery_to_home, exported - solar_to_grid)
    need -= battery_to_home
    grid_to_home = min(imported, need)
    grid_to_battery = min(imported - grid_to_home, charged - solar_to_battery)
    flows = {
        "solar_to_home": solar_to_home,
        "solar_to_battery": solar_to_battery,
        "solar_to_grid": solar_to_grid,
        "battery_to_home": battery_to_home,
        "battery_to_grid": battery_to_grid,
        "grid_to_home": grid_to_home,
        "grid_to_battery": grid_to_battery,
    }
    return {name: max(0.0, value) for name, value in flows.items()}


class EnergyFlows:
    """Lifetime energy per flow (kWh), integrated from the power readings."""

    def __init__(self, poll_seconds: float = 0) -> None:
        self.set_poll_seconds(poll_seconds)
        self.totals: dict[str, float] = dict.fromkeys(FLOWS, 0.0)
        # The last reading: when, and each flow's power (W).
        self.last: tuple[datetime, dict[str, float]] | None = None
        # The lifetime counters at the last full reading (when, kWh), what the
        # readings counted since (kWh per flow), and whether the chain broke
        # since: then the next full reading fills the gap from the counters.
        self.anchor: tuple[datetime, dict[str, float | None]] | None = None
        self.since_anchor: dict[str, float] = dict.fromkeys(FLOWS, 0.0)
        self.broken = False
        # The gaps filled so far: how many, and how much energy (kWh).
        self.filled_gaps = 0
        self.filled_kwh = 0.0

    def set_poll_seconds(self, poll_seconds: float) -> None:
        """The polling interval (s), which sets the longest time between two
        readings that still counts; also when it changes without a reload."""
        self.max_gap = max(MAX_GAP_SECONDS, GAP_POLLS * poll_seconds)

    def update(self, now: datetime, solar: Any, battery: Any, grid: Any, home: Any, *,
               counters: dict[str, Any] | None = None) -> float:
        """One set of readings (W), with the lifetime counters (kWh) on a full
        reading. A missing reading breaks the chain: the time up to the next
        complete one isn't integrated, and the next full reading fills it from
        the counters. Returns the energy filled now (kWh)."""
        if not all(_number(v) for v in (solar, battery, grid, home)):
            self.pause()
            return 0.0
        flows = split(float(solar), float(battery), float(grid), float(home))
        if self.last is not None:
            seconds = (now - self.last[0]).total_seconds()
            if 0 < seconds <= self.max_gap:
                for name in FLOWS:
                    # Trapezoid: the mean of the two readings over the time between.
                    energy = (self.last[1][name] + flows[name]) / 2 * seconds / 3_600_000
                    self.totals[name] += energy
                    self.since_anchor[name] += energy
            else:
                self.broken = True
        else:
            self.broken = True
        self.last = (now, flows)
        filled = 0.0
        if counters is not None:
            if self.broken and self.anchor is not None:
                filled = self._fill(now, counters)
            self.anchor = (now, {key: counters.get(key) for key in COUNTER_FLOWS})
            self.since_anchor = dict.fromkeys(FLOWS, 0.0)
            self.broken = False
        return filled

    def _fill(self, now: datetime, counters: dict[str, Any]) -> float:
        """Add what the counters gained since the anchor, less what the
        readings counted meanwhile, split into the flows. 0 when it can't be
        told (a counter missing at one end, or lower, or too long a gap)."""
        assert self.anchor is not None
        at, before = self.anchor
        if not 0 < (now - at).total_seconds() <= MAX_FILL_SECONDS:
            return 0.0
        gained: dict[str, float] = {}
        for key, names in COUNTER_FLOWS.items():
            old, new = before.get(key), counters.get(key)
            if old is None and new is None:
                gained[key] = 0.0  # no such meter (no solar, say)
                continue
            if not (_number(old) and _number(new)) or new < old:
                return 0.0
            gained[key] = max(0.0, new - old - sum(self.since_anchor[name] for name in names))
        flows = split_energy(gained)
        for name, energy in flows.items():
            self.totals[name] += energy
        filled = sum(flows.values())
        if filled > 0:
            self.filled_gaps += 1
            self.filled_kwh += filled
        return filled

    def pause(self) -> None:
        """The gateway can't be read: don't bridge the time until it can."""
        self.last = None
        self.broken = True

    def summary(self) -> dict[str, float]:
        return {name: round(value, 3) for name, value in self.totals.items()}

    def as_dict(self) -> dict[str, Any]:
        return {
            "totals": dict(self.totals),
            "anchor": {"at": self.anchor[0].isoformat(), "counters": dict(self.anchor[1])} if self.anchor else None,
            "since_anchor": dict(self.since_anchor),
            "filled_gaps": self.filled_gaps,
            "filled_kwh": self.filled_kwh,
        }

    def restore(self, stored: dict[str, Any]) -> None:
        if not isinstance(stored, dict):
            return
        totals = stored.get("totals")
        if not isinstance(totals, dict):
            return
        for name in FLOWS:
            value = totals.get(name)
            if _number(value) and value >= 0:
                self.totals[name] = float(value)
        anchor = stored.get("anchor")
        since = stored.get("since_anchor")
        if isinstance(anchor, dict) and isinstance(anchor.get("counters"), dict) and isinstance(since, dict):
            try:
                at = datetime.fromisoformat(str(anchor.get("at")))
            except ValueError:
                at = None
            counters = {key: anchor["counters"].get(key) for key in COUNTER_FLOWS}
            if at is not None and at.tzinfo is not None and all(v is None or _number(v) for v in counters.values()):
                self.anchor = (at, counters)
                self.since_anchor = {name: float(since[name]) if _number(since.get(name)) else 0.0
                                     for name in FLOWS}
                # Away since it was saved (a restart): fill at the next full reading.
                self.broken = True
        if _number(stored.get("filled_gaps")) and _number(stored.get("filled_kwh")):
            self.filled_gaps = int(stored["filled_gaps"])
            self.filled_kwh = float(stored["filled_kwh"])
