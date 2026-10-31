"""2.4.0: firmware updates are announced, and signed access is checked on the
new firmware; logbook entries for the integration's events."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.tesla_tedapi import logbook
from custom_components.tesla_tedapi.const import (
    DOMAIN,
    EVENT_DISPATCH_ENDED,
    EVENT_FIRMWARE_UPDATED,
    EVENT_GRID_OUTAGE,
    EVENT_OFF_GRID_ENDED,
)
from custom_components.tesla_tedapi.firmware import FirmwareTracker, split_version

from .conftest import local
from .test_storm import V1R, WIFI, _eid, _refresh, _setup

T0 = datetime(2026, 10, 31, 3, 0, tzinfo=dt_util.UTC)


def _new_firmware(fake, version: str = "26.34.3 f2e0e391") -> None:
    local(fake).version = lambda: version


def _issue(hass: HomeAssistant, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"firmware_signed_access_{entry.entry_id}")


# ---- the tracker ----

def test_first_reading_is_the_baseline() -> None:
    t = FirmwareTracker()
    assert t.update("26.34.2 aaa", T0) is None
    assert t.update("26.34.2 aaa", T0) is None
    assert t.update(None, T0) is None
    change = t.update("26.34.3 bbb", T0)
    assert (change.previous, change.version) == ("26.34.2 aaa", "26.34.3 bbb")
    assert t.attributes() == {"previous_version": "26.34.2", "updated_at": T0.isoformat()}


def test_signed_news_only_on_change() -> None:
    t = FirmwareTracker()
    assert t.note_signed(True) is True
    assert t.note_signed(True) is False
    assert t.note_signed(False) is True


def test_round_trip_and_split() -> None:
    t = FirmwareTracker()
    t.update("26.34.2 aaa", T0)
    t.update("26.34.3 bbb", T0)
    copy = FirmwareTracker()
    copy.restore(t.as_dict())
    assert copy.as_dict() == t.as_dict()
    copy.restore({})
    assert copy.version is None and copy.updated_at is None
    assert split_version("26.34.3") == ("26.34.3", None)
    assert split_version(None) == (None, None)


# ---- in Home Assistant ----

async def test_update_is_announced_and_checked(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    events = async_capture_events(hass, EVENT_FIRMWARE_UPDATED)
    await _refresh(hass, entry)
    assert events == []  # the same firmware again

    _new_firmware(fake_powerwall)
    await _refresh(hass, entry)
    assert len(events) == 1
    assert events[0].data | {"config_entry_id": None} == {
        "config_entry_id": None, "previous_version": "26.34.1", "version": "26.34.3", "build": "f2e0e391"}
    sensor = hass.states.get(_eid(hass, "sensor", "firmware_version"))
    assert sensor.state == "26.34.3"
    assert sensor.attributes["previous_version"] == "26.34.1"
    assert sensor.attributes["updated_at"] is not None
    assert entry.runtime_data.coordinator.firmware.signed_ok is True
    assert _issue(hass, entry) is None


async def test_signed_access_lost_after_update(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    pw = local(fake_powerwall)
    _new_firmware(fake_powerwall)
    pw.tedapi_mode = "full"  # fell back to the unsigned Wi-Fi session
    await _refresh(hass, entry)
    issue = _issue(hass, entry)
    assert issue.translation_key == "firmware_signed_access"
    assert issue.translation_placeholders == {"version": "26.34.3", "previous": "26.34.1"}

    pw.tedapi_mode = "v1r"
    await _refresh(hass, entry)
    assert _issue(hass, entry) is None


async def test_wifi_entry_is_not_checked(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, WIFI)
    events = async_capture_events(hass, EVENT_FIRMWARE_UPDATED)
    _new_firmware(fake_powerwall)
    await _refresh(hass, entry)
    assert len(events) == 1
    assert entry.runtime_data.coordinator.firmware.signed_ok is None
    assert _issue(hass, entry) is None


async def test_unload_clears_the_repair(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    _new_firmware(fake_powerwall)
    local(fake_powerwall).tedapi_mode = "full"
    await _refresh(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, entry) is None


async def test_baseline_survives_a_restart(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    assert entry.runtime_data.coordinator._state_to_save()["firmware"]["version"] == "26.34.1 dcee474d"


# ---- logbook ----

@pytest.mark.parametrize(("event_type", "data", "message"), [
    (EVENT_FIRMWARE_UPDATED, {"previous_version": "26.34.2", "version": "26.34.3"},
     "firmware updated from 26.34.2 to 26.34.3"),
    (EVENT_GRID_OUTAGE, {"phase": "started"}, "grid outage started"),
    (EVENT_GRID_OUTAGE, {"phase": "ended", "duration_seconds": 750}, "grid back after an outage of 12 min"),
    (EVENT_GRID_OUTAGE, {"phase": "ended"}, "grid back after an outage of ?"),
    (EVENT_DISPATCH_ENDED, {"reason": "target_reached"}, "battery dispatch ended (target_reached)"),
    (EVENT_OFF_GRID_ENDED, {"reason": "completed"}, "off-grid run ended (completed)"),
])
async def test_logbook_messages(hass: HomeAssistant, fake_powerwall, event_type, data, message) -> None:
    entry = await _setup(hass, V1R)
    described = {}
    logbook.async_describe_events(hass, lambda domain, kind, fn: described.__setitem__(kind, fn))
    assert set(described) == {EVENT_FIRMWARE_UPDATED, EVENT_GRID_OUTAGE, EVENT_DISPATCH_ENDED, EVENT_OFF_GRID_ENDED}
    result = described[event_type](SimpleNamespace(data={"config_entry_id": entry.entry_id, **data}))
    assert result == {LOGBOOK_ENTRY_NAME: "Powerwall", LOGBOOK_ENTRY_MESSAGE: message}
    unknown = described[event_type](SimpleNamespace(data=data))
    assert unknown[LOGBOOK_ENTRY_NAME] == "Powerwall"
