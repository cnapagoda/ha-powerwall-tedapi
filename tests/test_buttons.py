"""2.6.0: buttons that end a battery dispatch, a max backup or an off-grid run
(signed connection only)."""
from __future__ import annotations

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.tesla_tedapi import button
from custom_components.tesla_tedapi.const import DOMAIN, EVENT_DISPATCH_ENDED, EVENT_OFF_GRID_ENDED

from .conftest import local
from .test_storm import ACCOUNT, V1R, WIFI, _eid, _refresh, _setup

KEYS = ("stop_dispatch", "cancel_max_backup", "reconnect_grid")


async def _press(hass: HomeAssistant, key: str) -> None:
    await hass.services.async_call(BUTTON_DOMAIN, SERVICE_PRESS,
                                   {ATTR_ENTITY_ID: _eid(hass, "button", key)}, blocking=True)
    await hass.async_block_till_done()


async def test_buttons_on_a_signed_connection(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    names = {key: hass.states.get(_eid(hass, "button", key)).name for key in KEYS}
    assert names == {"stop_dispatch": "Powerwall TEDAPI Stop battery dispatch",
                     "cancel_max_backup": "Powerwall TEDAPI Cancel max backup",
                     "reconnect_grid": "Powerwall TEDAPI Reconnect to the grid"}


async def test_entity_ids_match_the_docs(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    assert {_eid(hass, "button", key) for key in KEYS} == {
        "button.powerwall_tedapi_stop_battery_dispatch",
        "button.powerwall_tedapi_cancel_max_backup",
        "button.powerwall_tedapi_reconnect_to_the_grid",
    }


async def test_no_buttons_over_wifi(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, {**WIFI, **ACCOUNT})
    assert all(_eid(hass, "button", key) is None for key in KEYS)


async def test_stop_dispatch(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                   {"power_watts": 2000, "duration_seconds": 1800}, blocking=True)
    assert entry.runtime_data.dispatch.active
    await _press(hass, "stop_dispatch")
    assert not entry.runtime_data.dispatch.active
    assert local(fake_powerwall).resumes == 1
    assert [e.data["reason"] for e in ended] == ["stopped"]
    assert hass.states.get(_eid(hass, "binary_sensor", "dispatch_active")).state == "off"


async def test_stop_dispatch_without_a_session(hass: HomeAssistant, fake_powerwall) -> None:
    """Like resume_battery_operation: hands the battery back anyway (a
    dispatch left over from before a restart), without an ended event."""
    await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    await _press(hass, "stop_dispatch")
    assert local(fake_powerwall).resumes == 1
    assert ended == []


async def test_cancel_max_backup(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    await hass.services.async_call(DOMAIN, "start_max_backup", {"duration_minutes": 60}, blocking=True)
    await hass.async_block_till_done()
    event = _eid(hass, "binary_sensor", "backup_event")
    assert hass.states.get(event).state == "on"
    await _press(hass, "cancel_max_backup")
    assert local(fake_powerwall).writes[-1] == ("max_backup", None)
    assert hass.states.get(event).state == "off"


async def test_reconnect_grid(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_OFF_GRID_ENDED)
    await hass.services.async_call(DOMAIN, "go_off_grid", {"confirm": True, "duration_minutes": 30},
                                   blocking=True)
    await hass.async_block_till_done()
    await _press(hass, "reconnect_grid")
    assert local(fake_powerwall).writes[-1] == ("island", 1)
    assert [e.data["reason"] for e in ended] == ["requested"]
    grid = hass.states.get(_eid(hass, "binary_sensor", "grid_connected"))
    assert grid.attributes["off_grid_run"] is False


async def test_press_errors_reach_the_user(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    fake_powerwall.contactor_answers = False
    with pytest.raises(HomeAssistantError):
        await _press(hass, "reconnect_grid")


async def test_buttons_unavailable_without_the_gateway(hass: HomeAssistant, fake_powerwall) -> None:
    """The cloud backup keeps the integration running, but these commands are
    local only."""
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    assert hass.states.get(_eid(hass, "button", "stop_dispatch")).state != STATE_UNAVAILABLE
    fake_powerwall.fail_local = True
    entry.runtime_data.coordinator._pw = None
    await _refresh(hass, entry)
    assert entry.runtime_data.coordinator.last_update_success
    for key in KEYS:
        assert hass.states.get(_eid(hass, "button", key)).state == STATE_UNAVAILABLE
    fake_powerwall.fail_local = False
    await _refresh(hass, entry)
    assert hass.states.get(_eid(hass, "button", "stop_dispatch")).state != STATE_UNAVAILABLE


def test_parallel_updates() -> None:
    assert button.PARALLEL_UPDATES == 1
