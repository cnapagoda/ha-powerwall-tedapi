"""2.9.0: a gap in the readings is filled into the energy flows from the
gateway's lifetime counters at the next full reading."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.tesla_tedapi.flows import COUNTER_FLOWS, FLOWS, EnergyFlows, split_energy

from .conftest import FakePowerwall, local
from .test_storm import V1R, _setup

T0 = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
COUNTERS = {"solar_energy_produced": 100.0, "battery_energy_charged": 50.0, "battery_energy_discharged": 40.0,
            "grid_energy_imported": 200.0, "grid_energy_exported": 30.0, "home_energy_consumed": 280.0}


def _plus(**gained: float) -> dict[str, float]:
    return {key: value + gained.get(key, 0.0) for key, value in COUNTERS.items()}


def test_split_energy_order_and_caps() -> None:
    gained = dict.fromkeys(COUNTER_FLOWS, 0.0) | {
        "solar_energy_produced": 5.0, "battery_energy_charged": 2.0, "home_energy_consumed": 2.5,
        "grid_energy_exported": 0.5, "battery_energy_discharged": 0.0, "grid_energy_imported": 0.0}
    flows = split_energy(gained)
    assert flows["solar_to_battery"] == 2.0 and flows["solar_to_home"] == 2.5 and flows["solar_to_grid"] == 0.5
    # More charged than solar and grid gave (the counters count the Powerwall's
    # losses): capped, never invented.
    gained = dict.fromkeys(COUNTER_FLOWS, 0.0) | {"battery_energy_charged": 3.0, "grid_energy_imported": 1.0}
    flows = split_energy(gained)
    assert flows["grid_to_battery"] == 1.0 and sum(flows.values()) == 1.0
    assert all(value >= 0 for value in flows.values())


def test_no_gap_no_fill() -> None:
    flows = EnergyFlows(30)
    assert flows.update(T0, 1000, 0, 0, 1000, counters=COUNTERS) == 0
    assert flows.update(T0 + timedelta(seconds=30), 1000, 0, 0, 1000, counters=_plus(solar_energy_produced=5)) == 0
    # Only the readings count: 1 kW for 30 s.
    assert flows.totals["solar_to_home"] == pytest.approx(1000 * 30 / 3_600_000)


def test_a_gap_is_filled_from_the_counters() -> None:
    flows = EnergyFlows(30)
    flows.update(T0, 2000, 0, 0, 2000, counters=COUNTERS)
    flows.pause()  # the gateway away for an hour
    filled = flows.update(T0 + timedelta(hours=1), 2000, 0, 0, 2000, counters=_plus(
        solar_energy_produced=2.0, home_energy_consumed=1.5, grid_energy_exported=0.5))
    assert filled == pytest.approx(2.0)
    assert flows.totals["solar_to_home"] == pytest.approx(1.5)
    assert flows.totals["solar_to_grid"] == pytest.approx(0.5)
    assert flows.filled_gaps == 1 and flows.filled_kwh == pytest.approx(2.0)
    # Filled once: the next reading bridges as usual.
    assert flows.update(T0 + timedelta(hours=1, seconds=30), 2000, 0, 0, 2000,
                        counters=_plus(solar_energy_produced=2.0)) == 0


def test_what_the_readings_counted_isnt_counted_twice() -> None:
    """Fast polls after the full reading, then the gap: the counters gained
    over both, the readings already counted the first part."""
    flows = EnergyFlows(5)
    flows.update(T0, 3600, 0, 0, 3600, counters=COUNTERS)
    for second in range(5, 605, 5):  # 10 minutes of fast polls at 3.6 kW
        flows.update(T0 + timedelta(seconds=second), 3600, 0, 0, 3600)
    counted = flows.totals["solar_to_home"]
    assert counted == pytest.approx(0.6)
    flows.pause()
    flows.update(T0 + timedelta(minutes=30), 3600, 0, 0, 3600,
                 counters=_plus(solar_energy_produced=1.8, home_energy_consumed=1.8))
    # 1.8 kWh in all: 0.6 counted, 1.2 filled.
    assert flows.totals["solar_to_home"] == pytest.approx(1.8)


def test_a_missing_reading_between_fast_polls_is_filled_at_the_next_full_read() -> None:
    flows = EnergyFlows(5)
    flows.update(T0, 1000, 0, 0, 1000, counters=COUNTERS)
    flows.update(T0 + timedelta(seconds=5), None, 0, 0, 1000)  # a meter didn't answer
    flows.update(T0 + timedelta(seconds=10), 1000, 0, 0, 1000)
    filled = flows.update(T0 + timedelta(seconds=30), 1000, 0, 0, 1000,
                          counters=_plus(solar_energy_produced=0.01, home_energy_consumed=0.01))
    assert filled > 0
    assert flows.totals["solar_to_home"] == pytest.approx(0.01, abs=1e-6)


@pytest.mark.parametrize(("after", "gained"), [
    (timedelta(hours=25), {"solar_energy_produced": 5.0, "home_energy_consumed": 5.0}),  # too long
    (timedelta(hours=1), {"solar_energy_produced": -5.0}),  # a counter went down
])
def test_not_filled(after, gained) -> None:
    flows = EnergyFlows(30)
    flows.update(T0, 1000, 0, 0, 1000, counters=COUNTERS)
    flows.pause()
    assert flows.update(T0 + after, 1000, 0, 0, 1000, counters=_plus(**gained)) == 0
    assert sum(flows.totals.values()) == 0


def test_a_counter_missing_at_one_end_or_both() -> None:
    flows = EnergyFlows(30)
    flows.update(T0, 1000, 0, 0, 1000, counters={**COUNTERS, "solar_energy_produced": None})
    flows.pause()
    assert flows.update(T0 + timedelta(hours=1), 1000, 0, 0, 1000, counters=_plus(solar_energy_produced=1)) == 0
    # No solar meter at all: the rest is still filled.
    no_solar = {**COUNTERS, "solar_energy_produced": None}
    flows = EnergyFlows(30)
    flows.update(T0, 0, 0, 1000, 1000, counters=no_solar)
    flows.pause()
    filled = flows.update(T0 + timedelta(hours=1), 0, 0, 1000, 1000,
                          counters={**no_solar, "grid_energy_imported": 201.0, "home_energy_consumed": 281.0})
    assert filled == pytest.approx(1.0) and flows.totals["grid_to_home"] == pytest.approx(1.0)


def test_a_restart_is_filled() -> None:
    flows = EnergyFlows(30)
    flows.update(T0, 1000, 0, 0, 1000, counters=COUNTERS)
    flows.update(T0 + timedelta(seconds=30), 1000, 0, 0, 1000)
    saved = flows.as_dict()
    again = EnergyFlows(30)
    again.restore(saved)
    assert again.broken and again.anchor is not None
    filled = again.update(T0 + timedelta(hours=2), 1000, 0, 0, 1000,
                          counters=_plus(solar_energy_produced=2.0, home_energy_consumed=2.0))
    before = saved["totals"]["solar_to_home"]
    assert filled == pytest.approx(2.0 - before)
    assert again.totals["solar_to_home"] == pytest.approx(2.0)


@pytest.mark.parametrize("stored", [
    {"totals": dict.fromkeys(FLOWS, 1.0)},  # before 2.9.0: totals only
    {"totals": dict.fromkeys(FLOWS, 1.0), "anchor": {"at": "not a time", "counters": {}}, "since_anchor": {}},
    {"totals": dict.fromkeys(FLOWS, 1.0), "anchor": {"at": "2026-11-26T09:00:00", "counters": {}},
     "since_anchor": {}},  # no time zone
    {"totals": dict.fromkeys(FLOWS, 1.0), "anchor": {"at": T0.isoformat(), "counters": {"solar_energy_produced": "x"}},
     "since_anchor": {}},
    "junk",
])
def test_restore_ignores_what_it_cant_use(stored) -> None:
    flows = EnergyFlows(30)
    flows.restore(stored)
    assert flows.anchor is None


async def test_the_coordinator_fills_an_outage(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    before = dict(coordinator.flows.totals)
    # The gateway away for 20 minutes.
    fake_powerwall.fail_local = True
    coordinator._pw = None
    freezer.tick(timedelta(minutes=10))
    await coordinator.async_refresh()
    fake_powerwall.fail_local = False
    freezer.tick(timedelta(minutes=10))
    aggregates = local(fake_powerwall).poll("/api/meters/aggregates")
    later = {**aggregates,
             "solar": {**aggregates["solar"], "energy_exported": aggregates["solar"]["energy_exported"] + 1000},
             "load": {**aggregates["load"], "energy_imported": aggregates["load"]["energy_imported"] + 1000}}
    with patch.object(FakePowerwall, "poll", lambda self, path: later):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.flows.totals["solar_to_home"] - before["solar_to_home"] == pytest.approx(1.0, abs=0.01)
    assert coordinator.flows.filled_gaps == 1
    from custom_components.tesla_tedapi.diagnostics import async_get_config_entry_diagnostics  # noqa: PLC0415

    assert (await async_get_config_entry_diagnostics(hass, entry))["flows_filled"]["gaps"] == 1
