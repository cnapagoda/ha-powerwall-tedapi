"""2.9.0: option "Turn on grid charging for a charge": a charge dispatch turns
Grid charging on if it's off, and off again when it ends, unless it was
changed in the meantime; also after a restart."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi.const import CONF_DISPATCH_GRID_CHARGING, DOMAIN

from .conftest import DIN, FakePowerwall
from .test_storm import V1R, _setup

ON = {CONF_DISPATCH_GRID_CHARGING: True}


def _writes() -> list[bool]:
    return [value for pw in FakePowerwall.instances for key, value in pw.writes if key == "grid_charging"]


async def _dispatch(hass: HomeAssistant, watts: int, seconds: int = 3600) -> None:
    await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                   {"power_watts": watts, "duration_seconds": seconds}, blocking=True)
    await hass.async_block_till_done()


async def _stop(hass: HomeAssistant) -> None:
    await hass.services.async_call(DOMAIN, "resume_battery_operation", {}, blocking=True)
    await hass.async_block_till_done()


async def test_off_by_default_only_warns(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    entry = await _setup(hass, V1R)
    with caplog.at_level(logging.WARNING):
        await _dispatch(hass, -2000)
    assert "charges from solar only" in caplog.text
    await _stop(hass)
    assert _writes() == []
    assert not entry.runtime_data.coordinator.grid_charging_by_dispatch


async def test_a_charge_turns_it_on_and_off_again(hass: HomeAssistant, fake_powerwall, caplog,
                                                  hass_storage) -> None:
    entry = await _setup(hass, V1R, ON)
    coordinator = entry.runtime_data.coordinator
    with caplog.at_level(logging.INFO):
        await _dispatch(hass, -2000)
    assert _writes() == [True]
    assert coordinator.grid_charging_by_dispatch and coordinator.data["grid_charging"] is True
    assert "charges from solar only" not in caplog.text
    # Saved, so a restart turns it off again.
    await coordinator.async_save_state()
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["grid_charging_by_dispatch"] is True
    # A planner re-sending its charge doesn't write again.
    await _dispatch(hass, -2000)
    await _dispatch(hass, -3000)
    assert _writes() == [True]
    await _stop(hass)
    assert _writes() == [True, False]
    assert not coordinator.grid_charging_by_dispatch
    assert coordinator.wanted["grid_charging"] is False  # the dropped-setting restore keeps it off
    assert "Grid charging turned off again" in caplog.text


async def test_already_on_is_left_alone(hass: HomeAssistant, fake_powerwall) -> None:
    FakePowerwall.grid_charging_disallow = False
    entry = await _setup(hass, V1R, ON)
    await _dispatch(hass, -2000)
    await _stop(hass)
    assert _writes() == []
    assert not entry.runtime_data.coordinator.grid_charging_by_dispatch


async def test_a_discharge_doesnt_touch_it(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R, ON)
    await _dispatch(hass, 2000)
    await _stop(hass)
    assert _writes() == []


async def test_switching_to_a_discharge_turns_it_off(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, ON)
    await _dispatch(hass, -2000)
    await _dispatch(hass, 2000)
    assert _writes() == [True, False]
    assert entry.runtime_data.dispatch.active
    assert not entry.runtime_data.coordinator.grid_charging_by_dispatch


async def test_changed_by_the_user_meanwhile(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, ON)
    coordinator = entry.runtime_data.coordinator
    await _dispatch(hass, -2000)
    # The user turns it on themselves (the switch): theirs from now on.
    await coordinator.async_set_grid_charging(True)
    assert not coordinator.grid_charging_by_dispatch
    await _stop(hass)
    assert _writes() == [True, True]


async def test_turned_off_elsewhere_meanwhile(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, ON)
    coordinator = entry.runtime_data.coordinator
    await _dispatch(hass, -2000)
    FakePowerwall.grid_charging_disallow = True  # e.g. in the Tesla app, a while later
    coordinator._grid_charging_written = None
    await coordinator.async_refresh()
    await _stop(hass)
    assert _writes() == [True]
    assert not coordinator.grid_charging_by_dispatch


async def test_turning_it_on_fails(hass: HomeAssistant, fake_powerwall, monkeypatch, caplog) -> None:
    entry = await _setup(hass, V1R, ON)
    monkeypatch.setattr(FakePowerwall, "set_grid_charging", lambda self, mode: None)
    with caplog.at_level(logging.WARNING):
        await _dispatch(hass, -2000)
    assert "could not turn on Grid charging" in caplog.text
    assert entry.runtime_data.dispatch.active  # still charges, from solar
    assert not entry.runtime_data.coordinator.grid_charging_by_dispatch


async def test_turning_it_off_is_retried(hass: HomeAssistant, fake_powerwall, monkeypatch, caplog) -> None:
    entry = await _setup(hass, V1R, ON)
    coordinator = entry.runtime_data.coordinator
    session = entry.runtime_data.dispatch
    await _dispatch(hass, -2000)
    original = FakePowerwall.set_grid_charging
    monkeypatch.setattr(FakePowerwall, "set_grid_charging", lambda self, mode: None)
    with caplog.at_level(logging.WARNING):
        await _stop(hass)
    assert "Could not turn Grid charging off again" in caplog.text
    assert coordinator.grid_charging_by_dispatch
    # Not again on every reading...
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.grid_charging_by_dispatch
    # ...but once the retry time has come.
    monkeypatch.setattr(FakePowerwall, "set_grid_charging", original)
    session._grid_retry_at = 0
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _writes() == [True, False]
    assert not coordinator.grid_charging_by_dispatch


async def test_turned_off_after_a_restart(hass: HomeAssistant, fake_powerwall, hass_storage) -> None:
    """Home Assistant stopped before it could turn it off: done at the start."""
    FakePowerwall.grid_charging_disallow = False
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, minor_version=2, options=ON,
                            title="Powerwall")
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 1, "key": f"{DOMAIN}.{entry.entry_id}",
                                                  "data": {"grid_charging_by_dispatch": True}}
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _writes() == [False]
    assert not coordinator.grid_charging_by_dispatch


async def test_unloading_turns_it_off(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, ON)
    coordinator = entry.runtime_data.coordinator
    await _dispatch(hass, -2000)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _writes() == [True, False]
    assert not coordinator.grid_charging_by_dispatch


async def test_kept_on_while_a_charge_runs(hass: HomeAssistant, fake_powerwall) -> None:
    """A late turn-off (the end of a charge a new one replaced) leaves it on."""
    entry = await _setup(hass, V1R, ON)
    await _dispatch(hass, -2000)
    await entry.runtime_data.dispatch._async_release_grid_charging()
    assert _writes() == [True]
    assert entry.runtime_data.coordinator.grid_charging_by_dispatch


async def test_a_new_charge_right_after_the_end(hass: HomeAssistant, fake_powerwall) -> None:
    """The reading may still show it on after turning it off: the last write counts."""
    entry = await _setup(hass, V1R, ON)
    coordinator = entry.runtime_data.coordinator
    await _dispatch(hass, -2000)
    await _stop(hass)
    coordinator.data["grid_charging"] = True  # not read again yet
    await _dispatch(hass, -2000)
    assert _writes() == [True, False, True]
    assert coordinator.grid_charging_by_dispatch
