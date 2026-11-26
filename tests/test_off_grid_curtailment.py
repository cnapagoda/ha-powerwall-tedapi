"""2.9.0: going off-grid as last-resort solar curtailment: a battery floor, a
daily limit kept across restarts, the end of the day, and a reconnect that
ends only such a run."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.tesla_tedapi.const import DOMAIN, EVENT_OFF_GRID_ENDED

from .conftest import DIN
from .test_off_grid import _go, _grid_attrs, _islands
from .test_storm import V1R, _refresh, _setup

# 02:00 local (US/Pacific, the tests' time zone): the day has 22 hours left.
MORNING = "2026-11-27 10:00:00+00:00"


async def _reconnect(hass: HomeAssistant, **data) -> dict:
    return await hass.services.async_call(DOMAIN, "reconnect_grid", data, blocking=True, return_response=True)


async def _synced(hass: HomeAssistant, entry, fake) -> None:
    """The Powerwall has synced back to the grid, and a reading shows it."""
    fake.grid = "UP"
    await _refresh(hass, entry)


def _left(entry) -> float:
    state = entry.runtime_data.off_grid.state
    return (state.ends - dt_util.utcnow()).total_seconds()


async def test_curtailment_run(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    freezer.move_to(MORNING)
    entry = await _setup(hass, V1R)
    events = async_capture_events(hass, EVENT_OFF_GRID_ENDED)
    await _go(hass, curtailment=True)
    attrs = _grid_attrs(hass)
    assert attrs["curtailment"] is True
    assert attrs["reconnect_below"] == 40.0  # the floor, above the 20 % reserve
    assert _left(entry) == pytest.approx(6 * 3600, abs=5)  # the default daily limit
    await _reconnect(hass, curtailment_only=True)
    await hass.async_block_till_done()
    assert _islands(fake_powerwall) == [("island", 6), ("island", 1)]
    assert [(e.data["reason"], e.data["curtailment"]) for e in events] == [("requested", True)]


async def test_floor_reserve_and_own_level(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    entry.runtime_data.coordinator.data["reserve"] = 45.0
    await _go(hass, curtailment=True)
    assert entry.runtime_data.off_grid.state.min_soc == 45.0  # the reserve, above the floor
    await _reconnect(hass)
    await _synced(hass, entry, fake_powerwall)
    await _go(hass, curtailment=True, min_soc=25, duration_minutes=30)
    assert entry.runtime_data.off_grid.state.min_soc == 25
    assert _left(entry) == pytest.approx(1800, abs=5)


async def test_daily_limit(hass: HomeAssistant, fake_powerwall, freezer, hass_storage) -> None:
    freezer.move_to(MORNING)
    entry = await _setup(hass, V1R)
    await _go(hass, curtailment=True, daily_limit_minutes=60)
    freezer.tick(timedelta(minutes=50))
    await _reconnect(hass, curtailment_only=True)
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}.off_grid"]["data"]
    assert stored == {"curtailed": {"date": "2026-11-27", "seconds": 3000}}
    await _synced(hass, entry, fake_powerwall)

    # Kept across a restart: 10 minutes are left today.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _go(hass, curtailment=True, daily_limit_minutes=60)
    assert _left(entry) == pytest.approx(600, abs=5)
    freezer.tick(timedelta(minutes=10))
    await _reconnect(hass, curtailment_only=True)
    await _synced(hass, entry, fake_powerwall)
    with pytest.raises(ServiceValidationError) as err:
        await _go(hass, curtailment=True, daily_limit_minutes=60)
    assert err.value.translation_key == "off_grid_daily_limit"
    # A run that isn't for curtailment doesn't count, and isn't limited.
    await _go(hass, duration_minutes=30)
    await _reconnect(hass)
    await _synced(hass, entry, fake_powerwall)

    # A new day: the full limit again.
    freezer.tick(timedelta(days=1))
    await _go(hass, curtailment=True, daily_limit_minutes=60)
    assert _left(entry) == pytest.approx(3600, abs=5)


async def test_ends_at_midnight(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    freezer.move_to("2026-11-27 07:30:00+00:00")  # 23:30 local
    entry = await _setup(hass, V1R)
    await _go(hass, curtailment=True, duration_minutes=120)
    assert _left(entry) == pytest.approx(1800, abs=5)


async def test_reconnect_only_a_curtailment_run(hass: HomeAssistant, fake_powerwall) -> None:
    """A run started otherwise (here, or in the Tesla app) is left alone."""
    entry = await _setup(hass, V1R)
    assert await _reconnect(hass, curtailment_only=True) == {"reconnected": False}
    await _go(hass, duration_minutes=60)
    assert await _reconnect(hass, curtailment_only=True) == {"reconnected": False}
    assert _islands(fake_powerwall) == [("island", 6)]
    assert entry.runtime_data.off_grid.state is not None


async def test_running_run_survives_a_reload(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    freezer.move_to(MORNING)
    entry = await _setup(hass, V1R)
    await _go(hass, curtailment=True)
    freezer.tick(timedelta(minutes=20))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    session = entry.runtime_data.off_grid
    assert session.state.curtailment is True
    assert session.curtailed_seconds(dt_util.utcnow()) == pytest.approx(1200, abs=5)
    await _reconnect(hass, curtailment_only=True)
    assert _islands(fake_powerwall)[-1] == ("island", 1)


async def test_bad_saved_usage_is_ignored(hass: HomeAssistant, fake_powerwall, hass_storage) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, minor_version=2)
    hass_storage[f"{DOMAIN}.{entry.entry_id}.off_grid"] = {
        "version": 1, "key": f"{DOMAIN}.{entry.entry_id}.off_grid", "data": {"curtailed": {"date": 5}}}
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.off_grid.curtailed_seconds(dt_util.utcnow()) == 0
