"""2.4.0: grid outage tracking from the gateway's grid status."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.tesla_tedapi.const import DOMAIN, EVENT_GRID_OUTAGE
from custom_components.tesla_tedapi.outages import OutageTracker, month_start

from .conftest import local
from .test_storm import ACCOUNT, V1R, _eid, _refresh, _setup

T0 = datetime(2026, 10, 31, 3, 0, tzinfo=dt_util.UTC)


def _state(hass: HomeAssistant, key: str):
    return hass.states.get(_eid(hass, "sensor", key))


# ---- the tracker itself ----

def test_outage_counted_once_with_duration() -> None:
    t = OutageTracker()
    assert t.update("UP", T0) is None
    started = t.update("DOWN", T0 + timedelta(minutes=1))
    assert started.phase == "started"
    assert t.update("DOWN", T0 + timedelta(minutes=2)) is None  # still the same outage
    assert t.update("SYNCING", T0 + timedelta(minutes=10)) is None  # not back until UP
    ended = t.update("UP", T0 + timedelta(minutes=11))
    assert ended.phase == "ended" and ended.duration_seconds == 600
    s = t.summary(T0 + timedelta(minutes=12))
    assert s["month_count"] == 1 and s["month_seconds"] == 600 and s["total_count"] == 1
    assert s["last_start"] == T0 + timedelta(minutes=1) and s["last_duration"] == 600
    assert s["ongoing"] is False


def test_unknown_status_changes_nothing() -> None:
    t = OutageTracker()
    t.update("UP", T0)
    assert t.update(None, T0 + timedelta(minutes=1)) is None
    assert t.summary(T0)["month_count"] == 0


def test_intentional_run_is_not_an_outage() -> None:
    t = OutageTracker()
    assert t.update("DOWN", T0, intentional=True) is None
    # The run ended (no longer intentional) before the grid synced back.
    assert t.update("SYNCING", T0 + timedelta(minutes=5)) is None
    assert t.update("UP", T0 + timedelta(minutes=6)) is None
    assert t.summary(T0)["month_count"] == 0
    assert t.ongoing_seconds(T0) is None


def test_ongoing_outage_counts_as_last() -> None:
    t = OutageTracker()
    t.update("UP", T0)
    t.update("DOWN", T0)
    s = t.summary(T0 + timedelta(minutes=3))
    assert s["ongoing"] is True and s["last_start"] == T0 and s["last_end"] is None
    assert s["month_seconds"] == 180


def test_new_month_resets_the_month_counts() -> None:
    t = OutageTracker()
    t.update("DOWN", T0)
    t.update("UP", T0 + timedelta(minutes=5))
    november = datetime(2026, 11, 2, 3, 0, tzinfo=dt_util.UTC)
    s = t.summary(november)
    assert s["month_count"] == 0 and s["month_seconds"] == 0 and s["total_count"] == 1
    assert s["month"] == dt_util.as_local(november).strftime("%Y-%m")
    assert s["last_duration"] == 300  # the last outage is still the last one


def test_round_trip_through_storage() -> None:
    t = OutageTracker()
    t.update("DOWN", T0)
    t.update("UP", T0 + timedelta(minutes=5))
    t.update("DOWN", T0 + timedelta(minutes=30), intentional=True)
    copy = OutageTracker()
    copy.restore(t.as_dict())
    assert copy.as_dict() == t.as_dict()
    copy.restore({})
    assert copy.current is None and copy.month_count == 0


def test_month_start_is_local_midnight() -> None:
    start = month_start("2026-11")
    assert dt_util.as_local(start).day == 1 and dt_util.as_local(start).hour == 0


# ---- in Home Assistant ----

async def test_outage_sensors_and_events(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    events = async_capture_events(hass, EVENT_GRID_OUTAGE)
    assert _state(hass, "grid_outages_month").state == "0"
    assert _state(hass, "last_grid_outage").state == "unknown"

    fake_powerwall.grid = "DOWN"
    await _refresh(hass, entry)
    assert [e.data["phase"] for e in events] == ["started"]
    assert _state(hass, "grid_outages_month").state == "1"
    assert _state(hass, "last_grid_outage").attributes["ongoing"] is True

    with patch("custom_components.tesla_tedapi.coordinator.dt_util.utcnow",
               return_value=dt_util.utcnow() + timedelta(minutes=12)):
        fake_powerwall.grid = "UP"
        await _refresh(hass, entry)
    assert [e.data["phase"] for e in events] == ["started", "ended"]
    assert 11 <= events[-1].data["duration_seconds"] / 60 <= 13
    assert 11 <= float(_state(hass, "last_grid_outage_duration").state) <= 13
    state = _state(hass, "grid_outage_time_month")
    assert 11 <= float(state.state) <= 13
    assert state.attributes["state_class"] == "total"
    assert _state(hass, "last_grid_outage").attributes["ongoing"] is False


async def test_unreachable_gateway_is_not_an_outage(hass: HomeAssistant, fake_powerwall) -> None:
    """With a cloud backup the coordinator keeps going while the gateway is
    unreachable: no outage, and the history stays available."""
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    events = async_capture_events(hass, EVENT_GRID_OUTAGE)
    fake_powerwall.fail_local = True
    local(fake_powerwall).power = lambda: (_ for _ in ()).throw(ConnectionError("down"))
    await _refresh(hass, entry)
    assert events == []
    # The history stays visible meanwhile.
    assert _state(hass, "grid_outages_month").state == "0"


async def test_off_grid_run_is_not_an_outage(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    events = async_capture_events(hass, EVENT_GRID_OUTAGE)
    await hass.services.async_call(DOMAIN, "go_off_grid", {"confirm": True, "duration_minutes": 30},
                                   blocking=True)
    await _refresh(hass, entry)
    await hass.services.async_call(DOMAIN, "reconnect_grid", {}, blocking=True)
    fake_powerwall.grid = "UP"
    await _refresh(hass, entry)
    assert events == []
    assert _state(hass, "grid_outages_month").state == "0"


async def test_counts_survive_a_restart(hass: HomeAssistant, fake_powerwall, hass_storage) -> None:
    entry = await _setup(hass, V1R)
    fake_powerwall.grid = "DOWN"
    await _refresh(hass, entry)
    fake_powerwall.grid = "UP"
    await _refresh(hass, entry)
    saved = entry.runtime_data.coordinator.outages.as_dict()
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 1, "key": f"{DOMAIN}.{entry.entry_id}",
                                                   "data": {"outages": saved}}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, "grid_outages_month").state == "1"
    assert entry.runtime_data.coordinator._state_to_save()["outages"]["total_count"] == 1
