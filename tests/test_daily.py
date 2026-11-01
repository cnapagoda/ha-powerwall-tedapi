"""2.5.0: today's energy (like Tesla Fleet's daily sensors), from the
gateway's lifetime counters."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.tesla_tedapi.const import DOMAIN
from custom_components.tesla_tedapi.daily import DailyEnergy, day_start

from .conftest import FakePowerwall
from .test_storm import V1R, _eid, _refresh, _setup


@pytest.fixture
def adelaide():
    previous = dt_util.get_default_time_zone()
    zone = dt_util.get_time_zone("Australia/Adelaide")
    dt_util.set_default_time_zone(zone)
    yield zone
    dt_util.set_default_time_zone(previous)


def _at(zone, *args) -> datetime:
    return datetime(*args, tzinfo=zone)


def test_today_from_the_counters(adelaide) -> None:
    daily = DailyEnergy()
    # 23:58 the day before, then just after midnight: the baseline is the last
    # reading before midnight, so nothing is lost at the boundary.
    daily.update({"grid": 100.0, "solar": 50.0}, _at(adelaide, 2026, 11, 1, 23, 58))
    got = daily.update({"grid": 100.5, "solar": 50.0}, _at(adelaide, 2026, 11, 2, 0, 1))
    assert got == {"grid": 0.5, "solar": 0.0}
    got = daily.update({"grid": 103.25, "solar": 58.0}, _at(adelaide, 2026, 11, 2, 12, 0))
    assert got == {"grid": 3.25, "solar": 8.0}
    assert daily.day.isoformat() == "2026-11-02"


def test_down_over_midnight_uses_the_first_reading(adelaide) -> None:
    daily = DailyEnergy()
    daily.update({"grid": 100.0}, _at(adelaide, 2026, 11, 1, 21, 0))
    # Home Assistant was down from 21:00 to 07:00: today starts at 07:00's reading.
    assert daily.update({"grid": 110.0}, _at(adelaide, 2026, 11, 2, 7, 0)) == {"grid": 0.0}
    assert daily.update({"grid": 111.0}, _at(adelaide, 2026, 11, 2, 8, 0)) == {"grid": 1.0}


def test_counter_reset_and_missing_readings(adelaide) -> None:
    daily = DailyEnergy()
    daily.update({"grid": 100.0, "home": 20.0}, _at(adelaide, 2026, 11, 2, 1, 0))
    daily.update({"grid": 102.0, "home": 21.0}, _at(adelaide, 2026, 11, 2, 2, 0))
    # The gateway's counter restarts from 0: today's 2 kWh carry on.
    assert daily.update({"grid": 0.5, "home": 22.0}, _at(adelaide, 2026, 11, 2, 3, 0)) == \
        {"grid": 2.5, "home": 2.0}
    # A counter without a reading has no value; one that appears later starts at 0.
    got = daily.update({"grid": None, "home": 23.0, "solar": 7.0}, _at(adelaide, 2026, 11, 2, 4, 0))
    assert got == {"grid": None, "home": 3.0, "solar": 0.0}
    assert daily.values({"grid": True}) == {"grid": None}


def test_daylight_saving_day(adelaide) -> None:
    """4 Oct 2026, clocks go forward at 02:00: the day still starts at local
    midnight and runs to the next local midnight (23 hours)."""
    daily = DailyEnergy()
    daily.update({"grid": 10.0}, _at(adelaide, 2026, 10, 3, 23, 59))
    assert daily.update({"grid": 10.1}, _at(adelaide, 2026, 10, 4, 3, 30)) == {"grid": 0.1}
    start, end = day_start(daily.day), day_start(daily.day + timedelta(days=1))
    assert dt_util.as_local(start).hour == 0
    assert dt_util.as_utc(end) - dt_util.as_utc(start) == timedelta(hours=23)
    assert daily.update({"grid": 12.0}, _at(adelaide, 2026, 10, 4, 23, 59)) == {"grid": 2.0}


def test_round_trip(adelaide) -> None:
    daily = DailyEnergy()
    daily.update({"grid": 100.0}, _at(adelaide, 2026, 11, 2, 1, 0))
    copy = DailyEnergy()
    copy.restore(daily.as_dict())
    assert copy.as_dict() == daily.as_dict()
    copy.restore({})
    assert copy.day is None and copy.baseline == {}


async def test_today_sensors(hass: HomeAssistant, fake_powerwall, hass_storage, freezer) -> None:
    await hass.config.async_set_time_zone("Australia/Adelaide")
    freezer.move_to("2026-11-02T01:30:00+00:00")  # 12:00 local
    meters = {"site": {"energy_imported": 1_000_000, "energy_exported": 2_000_000},
              "battery": {"energy_imported": 500_000, "energy_exported": 450_000},
              "solar": {"energy_exported": 3_000_000}, "load": {"energy_imported": 1_500_000}}
    original = FakePowerwall.poll
    FakePowerwall.poll = lambda self, path: meters
    try:
        entry = await _setup(hass, V1R)
        meters["site"]["energy_imported"] += 1_234  # 1.234 kWh more
        meters["solar"]["energy_exported"] += 5_000
        await _refresh(hass, entry)
        state = hass.states.get(_eid(hass, "sensor", "grid_energy_imported_today"))
        assert float(state.state) == pytest.approx(1.234)
        assert state.attributes["state_class"] == "total" and state.attributes["unit_of_measurement"] == "kWh"
        assert dt_util.as_local(dt_util.parse_datetime(state.attributes["last_reset"])).hour == 0
        assert float(hass.states.get(_eid(hass, "sensor", "solar_energy_produced_today")).state) == 5.0
        assert float(hass.states.get(_eid(hass, "sensor", "home_energy_consumed_today")).state) == 0.0

        # A restart during the day keeps the baseline.
        saved = entry.runtime_data.coordinator._state_to_save()
        assert await hass.config_entries.async_unload(entry.entry_id)
        hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 1, "key": f"{DOMAIN}.{entry.entry_id}",
                                                       "data": saved}
        meters["site"]["energy_imported"] += 766
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert float(hass.states.get(_eid(hass, "sensor", "grid_energy_imported_today")).state) == \
            pytest.approx(2.0)

        # Gateway unreachable: unavailable, like the other gateway readings.
        from .conftest import local  # noqa: PLC0415

        local(fake_powerwall).power = lambda: (_ for _ in ()).throw(ConnectionError("down"))
        await _refresh(hass, entry)
        assert hass.states.get(_eid(hass, "sensor", "grid_energy_imported_today")).state == "unavailable"
    finally:
        FakePowerwall.poll = original


# ---- grid counter check ----

from custom_components.tesla_tedapi.daily import GridCheck  # noqa: E402


def _feed(check: GridCheck, start: datetime, minutes: int, watts, counters, *, step: int = 60) -> None:
    """Readings every `step` seconds: power from watts(t) and counters from
    counters(t, imported, exported) given the true energy so far."""
    imported = exported = 0.0
    for i in range(minutes * 60 // step + 1):
        now = start + timedelta(seconds=i * step)
        p = watts(i)
        if i:
            kwh = p * step / 3_600_000
            imported, exported = (imported + kwh, exported) if p > 0 else (imported, exported - kwh)
        ci, ce = counters(i, imported, exported)
        check.update(now.date(), p, ci, ce, now)


def test_grid_check_ok_when_the_counters_agree(adelaide) -> None:
    check = GridCheck()
    start = _at(adelaide, 2026, 11, 2, 8, 0)
    # 2 hours: 3 kW import, then 2 kW export.
    _feed(check, start, 120, lambda i: 3000 if i <= 60 else -2000, lambda i, a, b: (100 + a, 50 + b))
    s = check.summary()
    assert s["state"] == "ok" and s["covered_minutes"] == 120 and s["gaps"] == 0
    assert s["power_import_kwh"] == pytest.approx(3.0, abs=0.05)
    assert s["power_export_kwh"] == pytest.approx(2.0, abs=0.05)
    assert abs(s["import_difference_pct"]) < 3


def test_grid_check_mismatch_when_small_flows_count_both_ways(adelaide) -> None:
    """The live site: both counters ~2.3 kWh high over a day, net right."""
    check = GridCheck()
    start = _at(adelaide, 2026, 11, 2, 0, 0)
    extra = 2.3 / 1440  # kWh a minute, counted both ways
    _feed(check, start, 1439, lambda i: 840 if i < 720 else -500,
          lambda i, a, b: (100 + a + extra * i, 50 + b + extra * i))
    s = check.summary()
    assert s["state"] == "mismatch"
    assert s["counter_import_kwh"] - s["power_import_kwh"] == pytest.approx(2.3, abs=0.05)
    assert s["import_difference_pct"] > 10 and s["export_difference_pct"] > 10


def test_grid_check_needs_enough_data(adelaide) -> None:
    check = GridCheck()
    start = _at(adelaide, 2026, 11, 2, 8, 0)
    _feed(check, start, 30, lambda i: 3000, lambda i, a, b: (a * 2, b))  # way off, but only 30 min
    assert check.summary()["state"] == "unknown"
    check = GridCheck()
    _feed(check, start, 120, lambda i: 100, lambda i, a, b: (a * 2, b))  # 0.2 kWh in 2 hours
    assert check.summary()["state"] == "unknown"
    check.update(None, None, 1, 1, start)  # a missing reading changes nothing
    assert check.summary()["covered_minutes"] == 0


def test_grid_check_skips_gaps_and_resets(adelaide) -> None:
    check = GridCheck()
    day = _at(adelaide, 2026, 11, 2, 8, 0)
    check.update(day.date(), 3000, 100.0, 50.0, day)
    check.update(day.date(), 3000, 100.05, 50.0, day + timedelta(minutes=1))
    # 20 minutes without readings (energy in the counters, none integrated): left out.
    check.update(day.date(), 3000, 101.05, 50.0, day + timedelta(minutes=21))
    # A counter reset: left out too.
    check.update(day.date(), 3000, 0.1, 50.0, day + timedelta(minutes=22))
    s = check.summary()
    assert s["gaps"] == 2 and s["covered_minutes"] == 1
    assert s["counter_import_kwh"] == pytest.approx(0.05) and s["power_import_kwh"] == pytest.approx(0.05)
    # A sign change inside an interval is split at zero.
    check.update(day.date(), -3000, 0.125, 50.025, day + timedelta(minutes=23))
    assert check.summary()["power_export_kwh"] == pytest.approx(0.0125, abs=0.001)
    # Midnight: a new day starts from nothing.
    tomorrow = _at(adelaide, 2026, 11, 3, 0, 1)
    check.update(tomorrow.date(), 1000, 0.2, 50.1, tomorrow)
    assert check.summary()["covered_minutes"] == 0 and check.summary()["power_import_kwh"] == 0
    # Round trip through storage.
    copy = GridCheck()
    copy.restore(check.as_dict())
    assert copy.as_dict() == check.as_dict()
    copy.restore({})
    assert copy.day is None and copy.last is None


async def test_grid_check_sensor(hass: HomeAssistant, fake_powerwall, hass_storage, freezer) -> None:
    await hass.config.async_set_time_zone("Australia/Adelaide")
    freezer.move_to("2026-11-02T01:30:00+00:00")
    meters = {"site": {"energy_imported": 1_000_000, "energy_exported": 2_000_000},
              "battery": {"energy_imported": 500_000, "energy_exported": 450_000},
              "solar": {"energy_exported": 3_000_000}, "load": {"energy_imported": 1_500_000}}
    original = FakePowerwall.poll
    FakePowerwall.poll = lambda self, path: meters
    try:
        entry = await _setup(hass, V1R)
        eid = _eid(hass, "sensor", "grid_counter_check")
        assert hass.states.get(eid).state == "unknown"
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        assert er.async_get(hass).async_get(eid).entity_category == "diagnostic"
        coordinator = entry.runtime_data.coordinator
        # Pretend 90 minutes of matching readings were seen today.
        check = coordinator.grid_check
        check.power_import = check.counter_import = 3.0
        check.covered = 90 * 60
        await _refresh(hass, entry)
        state = hass.states.get(eid)
        assert state.state == "ok" and state.attributes["covered_minutes"] >= 90
        # The totals survive a restart.
        saved = coordinator._state_to_save()
        assert await hass.config_entries.async_unload(entry.entry_id)
        hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 1, "key": f"{DOMAIN}.{entry.entry_id}",
                                                       "data": saved}
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(eid).attributes["power_import_kwh"] == pytest.approx(3.0, abs=0.01)
    finally:
        FakePowerwall.poll = original
