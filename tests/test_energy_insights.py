"""Energy flows, battery economics and per-phase readings: the arithmetic on
its own, then the sensors."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.tesla_tedapi import phases, wear
from custom_components.tesla_tedapi.flows import FLOWS, MAX_GAP_SECONDS, EnergyFlows, split

from .conftest import FakePowerwall
from .test_storm import V1R, _eid, _refresh, _setup

# -- energy flows ---------------------------------------------------------


@pytest.mark.parametrize(("readings", "expected"), [
    # Solar covers the home, charges the battery; the grid tops the charge up.
    ((3000, -500, 100, 2600), {"solar_to_home": 2600, "solar_to_battery": 400, "grid_to_battery": 100}),
    # Solar covers the home and exports the rest.
    ((5000, 0, -3000, 2000), {"solar_to_home": 2000, "solar_to_grid": 3000}),
    # Night: the battery runs the home, the grid the rest.
    ((0, 1500, 500, 2000), {"battery_to_home": 1500, "grid_to_home": 500}),
    # A dispatch: the battery covers the home and exports.
    ((0, 5000, -4000, 1000), {"battery_to_home": 1000, "battery_to_grid": 4000}),
    # Charging from the grid at night.
    ((-3, -3000, 3500, 500), {"grid_to_home": 500, "grid_to_battery": 3000}),
])
def test_split(readings, expected) -> None:
    flows = split(*readings)
    assert flows == {name: expected.get(name, 0) for name in FLOWS}


def test_split_balances() -> None:
    """Whatever the readings, each flow is positive and the flows add up to
    the readings they came from."""
    for solar in (0, 800, 6000):
        for battery in (-5000, -300, 0, 300, 5000):
            for home in (0, 400, 9000):
                grid = home - solar - battery
                flows = split(solar, battery, grid, home)
                assert all(value >= 0 for value in flows.values())
                from_solar = flows["solar_to_home"] + flows["solar_to_battery"] + flows["solar_to_grid"]
                assert from_solar == pytest.approx(solar)
                assert flows["solar_to_home"] + flows["battery_to_home"] + flows["grid_to_home"] == pytest.approx(home)
                assert flows["grid_to_home"] + flows["grid_to_battery"] == pytest.approx(max(grid, 0))
                assert flows["solar_to_grid"] + flows["battery_to_grid"] == pytest.approx(max(-grid, 0))


def test_energy_flows_integrate_and_skip_gaps() -> None:
    flows = EnergyFlows()
    start = datetime(2026, 11, 1, 12, tzinfo=UTC)
    flows.update(start, 3000, 0, -1000, 2000)
    assert flows.summary() == dict.fromkeys(FLOWS, 0.0)
    flows.update(start + timedelta(seconds=36), 3000, 0, -1000, 2000)
    assert flows.totals["solar_to_home"] == pytest.approx(0.02)  # 2 kW for 36 s
    assert flows.totals["solar_to_grid"] == pytest.approx(0.01)
    before = dict(flows.totals)
    # A gap, a missing reading and time going backwards add nothing.
    flows.update(start + timedelta(seconds=37 + MAX_GAP_SECONDS), 3000, 0, -1000, 2000)
    flows.update(start + timedelta(seconds=400), None, 0, -1000, 2000)
    assert flows.last is None
    flows.update(start + timedelta(seconds=430), 3000, 0, -1000, 2000)
    flows.update(start + timedelta(seconds=420), 3000, 0, -1000, 2000)
    flows.pause()
    flows.update(start + timedelta(seconds=440), 3000, 0, -1000, 2000)
    assert flows.totals == before

    restored = EnergyFlows()
    restored.restore(flows.as_dict())
    assert restored.totals == flows.totals
    restored.restore({"totals": {"solar_to_home": -1, "solar_to_grid": "x", "other": 5}})
    restored.restore({"totals": None})
    restored.restore(None)
    assert restored.totals == flows.totals


async def test_flow_sensors(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    entry = await _setup(hass, V1R)
    state = hass.states.get(_eid(hass, "sensor", "flow_solar_to_home"))
    assert state.state == "0.0"
    assert state.attributes["state_class"] == "total_increasing" and state.attributes["unit_of_measurement"] == "kWh"
    freezer.tick(30)
    await _refresh(hass, entry)
    # The fake gateway: solar 3000 W, battery charging 500 W, grid 100 W in, home 2600 W.
    assert hass.states.get(_eid(hass, "sensor", "flow_solar_to_home")).state == "0.022"  # 2.6 kW for 30 s
    assert hass.states.get(_eid(hass, "sensor", "flow_solar_to_battery")).state == "0.003"
    assert hass.states.get(_eid(hass, "sensor", "flow_grid_to_battery")).state == "0.001"
    assert hass.states.get(_eid(hass, "sensor", "flow_battery_to_grid")).state == "0.0"
    coordinator = entry.runtime_data.coordinator
    assert coordinator._state_to_save()["flows"]["totals"]["solar_to_home"] > 0
    # A reading the meters didn't give breaks the chain: nothing is counted
    # up to the next complete one.
    with patch.object(FakePowerwall, "poll", lambda self, path: {}):
        freezer.tick(30)
        await _refresh(hass, entry)
    assert coordinator.flows.last is None
    freezer.tick(30)
    await _refresh(hass, entry)
    assert hass.states.get(_eid(hass, "sensor", "flow_solar_to_home")).state == "0.022"


# -- battery economics -----------------------------------------------------


def test_round_trip_efficiency() -> None:
    assert wear.round_trip_efficiency(1000, 900) == 90.0
    assert wear.round_trip_efficiency(10, 9) is None  # too little through the battery yet
    assert wear.round_trip_efficiency(None, 5) is None
    assert wear.round_trip_efficiency(500, True) is None
    # More out than in: the counters don't cover the same energy (DC solar).
    assert wear.round_trip_efficiency(1000, 1000) == 100.0
    assert wear.round_trip_efficiency(1000, 1050) is None


def _trend(days: int, start_kwh: float, per_day: float, packs: int = 1) -> wear.CapacityTrend:
    trend = wear.CapacityTrend()
    for day in range(days):
        for _ in range(3):  # several readings a day: one sample, their mean
            trend.update(date(2026, 1, 1) + timedelta(days=day), start_kwh + per_day * day, packs)
    return trend


def test_capacity_trend() -> None:
    trend = _trend(200, 14.0, -0.001)  # 0.365 kWh a year
    summary = trend.summary()
    assert summary["samples"] == 200 and summary["days"] == 199
    assert summary["kwh_per_year"] == pytest.approx(-0.365, abs=0.002)
    assert summary["pct_per_year"] == pytest.approx(-0.365 / 13.5 * 100, abs=0.02)
    assert summary["fit_kwh"] == pytest.approx(13.8, abs=0.01)
    # 13.801 kWh falling 1 Wh a day reaches 9.45 kWh 4351 days after the last sample.
    assert summary["warranty_date"] == date(2026, 1, 1) + timedelta(days=199 + 4351)

    # Restored, it says the same; a changed pack count that lasts into a
    # later day starts over.
    restored = wear.CapacityTrend()
    restored.restore(trend.as_dict())
    assert restored.summary() == summary
    restored.restore(None)
    restored.restore({"samples": [["x", 1], [1], "junk"], "packs": 0})
    assert restored.samples == [] and restored.packs is None
    trend.update(date(2026, 8, 1), 14.0, 2)
    assert trend.summary()["samples"] == 200
    trend.update(date(2026, 8, 2), 14.0, 2)
    assert trend.summary()["samples"] == 1

    # Not enough history; a rising trend; already under the warranty level;
    # a fall too slow to matter; readings that aren't.
    assert _trend(40, 14.0, -0.001).summary()["kwh_per_year"] is None
    rising = _trend(120, 13.0, 0.001).summary()
    assert rising["kwh_per_year"] > 0 and rising["warranty_date"] is None
    assert _trend(100, 9.0, 0.0).summary()["warranty_date"] == date(2026, 1, 1) + timedelta(days=99)
    assert _trend(100, 14.0, -0.000001).summary()["warranty_date"] is None
    empty = wear.CapacityTrend()
    for bad in (None, 0, -1, True):
        empty.update(date(2026, 1, 1), bad, 1)
    empty.update(date(2026, 1, 1), 14.0, 0)
    assert empty.summary()["samples"] == 0


async def test_battery_economics_sensors(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    freezer.move_to("2026-07-20T02:00:00+00:00")  # the day after the history below ends
    entry = await _setup(hass, V1R)
    # The fake gateway's counters: 500 kWh into the battery, 450 kWh out.
    efficiency = hass.states.get(_eid(hass, "sensor", "battery_round_trip_efficiency"))
    assert efficiency.state == "90.0"
    fade = hass.states.get(_eid(hass, "sensor", "battery_capacity_fade"))
    assert fade.state == "unknown"  # one sample: no trend yet
    assert fade.attributes["samples"] == 1 and fade.attributes["days"] == 0
    assert hass.states.get(_eid(hass, "sensor", "battery_warranty_date")).state == "unknown"
    coordinator = entry.runtime_data.coordinator
    assert coordinator.capacity_trend.packs == 1
    assert coordinator._state_to_save()["capacity_trend"]["count"] == 1
    # With history, the trend and the date show.
    coordinator.capacity_trend = _trend(200, 14.0, -0.001)
    await _refresh(hass, entry)
    assert float(hass.states.get(_eid(hass, "sensor", "battery_capacity_fade")).state) < 0
    assert hass.states.get(_eid(hass, "sensor", "battery_warranty_date")).state.startswith("20")


# -- phases ----------------------------------------------------------------

SYNC = {"TESYNC--1232100-00-E--GW1": {
    "ISLAND_VL1N_Main": 241.23, "ISLAND_VL2N_Main": 0.0, "ISLAND_VL3N_Main": None, "ISLAND_VL1N_Load": 240.9,
    "METER_X_CTA_I": 3.6, "METER_X_CTA_InstRealPower": 850.456, "METER_X_CTA_InstReactivePower": -12,
    "METER_Y_CTB_I": 0.0, "METER_Y_CTB_InstRealPower": 0, "METER_X_CTB_I": True}}


def test_phase_extraction() -> None:
    found = phases.extract({**SYNC, "other": "junk"})
    assert found["grid_voltage"] == {"1": 241.2, "2": 0.0}
    assert found["home_voltage"] == {"1": 240.9}
    assert found["meters"] == {"X": {"A": {"power": 850.46, "current": 3.6, "reactive_power": -12.0}},
                               "Y": {"B": {"power": 0.0, "current": 0.0}}}
    assert phases.ct_in_use(found["meters"]["X"]["A"]) and not phases.ct_in_use(found["meters"]["Y"]["B"])
    assert phases.voltage_in_use(241.2) and not phases.voltage_in_use(0.0) and not phases.voltage_in_use(None)
    nothing = {"grid_voltage": {}, "home_voltage": {}, "meters": {}}
    # A Powerwall 3 without a sync board: the block is there, its signals aren't.
    assert phases.extract({"TESYNC--None--None": {"ISLAND_VL1N_Main": None}}) == nothing
    assert phases.extract(None) == nothing


async def test_phase_sensors_only_for_what_is_in_use(hass: HomeAssistant, fake_powerwall) -> None:
    FakePowerwall.extra_packs = SYNC
    await _setup(hass, V1R)
    registry = er.async_get(hass)
    assert hass.states.get(_eid(hass, "sensor", "grid_voltage_l1")).state == "241.2"
    assert _eid(hass, "sensor", "grid_voltage_l2") is None  # 0 V: not wired
    # The home side and the current are there, disabled until wanted.
    assert registry.async_get(_eid(hass, "sensor", "home_voltage_l1")).disabled_by is not None
    assert registry.async_get(_eid(hass, "sensor", "meter_x_ct_a_current")).disabled_by is not None
    power = hass.states.get(_eid(hass, "sensor", "meter_x_ct_a_power"))
    assert power.state == "850.46" and power.attributes["unit_of_measurement"] == "W"
    assert _eid(hass, "sensor", "meter_y_ct_b_power") is None  # nothing on it


async def test_no_phase_sensors_without_the_signals(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    assert entry.runtime_data.coordinator.data["phases"] == {"grid_voltage": {}, "home_voltage": {}, "meters": {}}
    assert _eid(hass, "sensor", "grid_voltage_l1") is None


def test_flows_count_with_a_long_polling_interval() -> None:
    """At a 300 s polling interval the readings are a little more than 300 s
    apart: still counted (the gap allowed grows with the polling interval)."""
    flows = EnergyFlows(300)
    start = datetime(2026, 11, 10, tzinfo=UTC)
    for i in range(13):
        flows.update(start + timedelta(seconds=300.4 * i), 3000, 0, -1000, 2000)
    assert flows.summary()["solar_to_home"] == pytest.approx(2.0, abs=0.01)
    assert EnergyFlows().max_gap == 300 and EnergyFlows(30).max_gap == 300


def test_capacity_trend_keeps_its_history_through_an_odd_read() -> None:
    trend = wear.CapacityTrend()
    first = date(2026, 1, 1)
    for i in range(120):
        trend.update(first + timedelta(days=i), 14.0 - i * 0.001, 3)
    trend.update(first + timedelta(days=120), 14.4, 2)  # one read without the gateway's config
    assert trend.summary()["samples"] == 120
    trend.update(first + timedelta(days=120), 13.88, 3)  # back to normal: nothing pending
    assert trend.pending is None and trend.summary()["samples"] == 121
    # A real change lasts into the next day: then it starts over.
    trend.update(first + timedelta(days=121), 13.0, 4)
    trend.update(first + timedelta(days=121), 13.0, 4)
    assert trend.summary()["samples"] == 121
    trend.update(first + timedelta(days=122), 13.0, 4)
    assert trend.summary()["samples"] == 1 and trend.packs == 4
    stored = wear.CapacityTrend()
    trend.update(first + timedelta(days=122), 13.0, 5)
    stored.restore(trend.as_dict())
    assert stored.pending == (5, (first + timedelta(days=122)).toordinal())
