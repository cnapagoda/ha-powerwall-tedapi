"""2.9.0: freeze_battery, the battery one way only at the power the readings
call for (Predbat's freeze charging and freeze exporting)."""
from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.tesla_tedapi.const import CONF_DISPATCH_GRID_CHARGING, DOMAIN, EVENT_DISPATCH_ENDED

from .conftest import FakePowerwall, local
from .test_storm import V1R, _setup

ENTITY = "binary_sensor.powerwall_tedapi_dispatch_active"


async def _freeze(hass: HomeAssistant, direction: str, seconds: int = 600, **extra) -> dict:
    result = await hass.services.async_call(
        DOMAIN, "freeze_battery", {"direction": direction, "duration_seconds": seconds, **extra},
        blocking=True, return_response=True)
    await hass.async_block_till_done()
    return result


async def _reading(hass: HomeAssistant, entry, home: float, solar: float, level: float | None = None) -> None:
    """A fresh local reading with these values."""
    coordinator = entry.runtime_data.coordinator
    coordinator.data["home_power"] = home
    coordinator.data["solar_power"] = solar
    if level is not None:
        coordinator.data["battery_level"] = level
    coordinator.battery_level_at = dt_util.utcnow()
    coordinator.async_update_listeners()
    await hass.async_block_till_done()


async def _tick(hass: HomeAssistant, entry) -> None:
    await entry.runtime_data.dispatch._async_tick(dt_util.utcnow())
    await hass.async_block_till_done()


def _sent(fake) -> list[float]:
    return [power for power, _ in local(fake).dispatches]


async def test_charge_only_follows_surplus_solar(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    """Solar 3000 W, home 2600 W: charges at 400 W, never from the grid."""
    entry = await _setup(hass, V1R, {CONF_DISPATCH_GRID_CHARGING: True})
    with caplog.at_level(logging.WARNING):
        result = await _freeze(hass, "charge_only")
    assert result["freeze"] == "charge_only" and result["power_watts"] == -400
    assert local(fake_powerwall).dispatches[-1] == (-400, 120)
    # Neither turns Grid charging on nor warns that it's off.
    assert not [w for pw in FakePowerwall.instances for w in pw.writes if w[0] == "grid_charging"]
    assert "charges from solar only" not in caplog.text
    assert "charge_only, following the readings up to 10000 W" in caplog.text
    attrs = hass.states.get(ENTITY).attributes
    assert attrs["freeze"] == "charge_only" and attrs["max_power_watts"] == 10000
    # More solar than the home uses beyond the cap: the cap.
    await _reading(hass, entry, home=500, solar=20000)
    assert _sent(fake_powerwall)[-1] == -10000
    # The home uses all the solar: a hold, not a discharge.
    await _reading(hass, entry, home=3000, solar=1000)
    assert _sent(fake_powerwall)[-1] == 0


async def test_discharge_only_covers_the_home(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    await _freeze(hass, "discharge_only", max_power_watts=3000)
    assert _sent(fake_powerwall) == [0]  # surplus solar: exported, not charged
    await _reading(hass, entry, home=3500, solar=1000.4)
    assert _sent(fake_powerwall)[-1] == 2500
    # A small change waits for the renewal...
    await _reading(hass, entry, home=3550, solar=1000)
    assert _sent(fake_powerwall)[-1] == 2500
    await _tick(hass, entry)
    assert _sent(fake_powerwall)[-1] == 2550
    assert hass.states.get(ENTITY).attributes["power_watts"] == 2550
    # ...a large one doesn't; and the cap holds.
    await _reading(hass, entry, home=9000, solar=0)
    assert _sent(fake_powerwall)[-1] == 3000
    # A change to a hold is sent however small.
    await _reading(hass, entry, home=3040, solar=3000)
    await _reading(hass, entry, home=3000, solar=3000)
    assert _sent(fake_powerwall)[-2:] == [40, 0]


async def test_battery_limits_hold_without_ending(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    session = entry.runtime_data.dispatch
    await _freeze(hass, "discharge_only")
    assert session.state.min_soc == 20.0  # the backup reserve (option on)
    await _reading(hass, entry, home=2000, solar=0, level=20)
    assert _sent(fake_powerwall)[-1] == 0
    assert session.active
    await _reading(hass, entry, home=2000, solar=0, level=21)
    assert _sent(fake_powerwall)[-1] == 2000
    await _freeze(hass, "charge_only", max_soc=90)
    await _reading(hass, entry, home=0, solar=2000, level=89)
    assert _sent(fake_powerwall)[-1] == -2000
    await _reading(hass, entry, home=0, solar=2000, level=90)
    assert _sent(fake_powerwall)[-1] == 0
    assert session.active


async def test_power_limit(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    with pytest.raises(ServiceValidationError) as err:
        await _freeze(hass, "charge_only", max_power_watts=12000)
    assert err.value.translation_key == "dispatch_power_cap"
    with pytest.raises(Exception):  # noqa: B017, PT011 - the schema: one way only
        await _freeze(hass, "both")


async def test_holds_without_a_fresh_reading(hass: HomeAssistant, fake_powerwall, freezer, caplog) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    await _freeze(hass, "discharge_only")
    await _reading(hass, entry, home=3000, solar=0)  # after the poll the start asks for
    assert _sent(fake_powerwall)[-1] == 3000
    freezer.tick(timedelta(seconds=300))  # no reading since

    async def no_level(timeout: float) -> bool:
        return False

    with patch.object(coordinator, "async_read_battery_level", no_level), caplog.at_level(logging.WARNING):
        await _tick(hass, entry)
        await _tick(hass, entry)
    assert _sent(fake_powerwall)[-2:] == [0, 0]  # kept as a hold, not ended
    assert entry.runtime_data.dispatch.active
    assert caplog.text.count("holds the battery: no fresh reading") == 1
    # Cloud readings (local read failing) don't count either.
    await _reading(hass, entry, home=3000, solar=0)
    assert _sent(fake_powerwall)[-1] == 3000
    coordinator.data["local_ok"] = False
    await _tick(hass, entry)
    assert _sent(fake_powerwall)[-1] == 0


async def test_stale_start_tries_one_read_then_holds(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    freezer.tick(timedelta(seconds=300))
    calls: list[float] = []

    async def no_level(timeout: float) -> bool:
        calls.append(timeout)
        return False

    with patch.object(coordinator, "async_read_battery_level", no_level):
        await _freeze(hass, "discharge_only")
    assert calls == [20]
    assert entry.runtime_data.dispatch.active and _sent(fake_powerwall) == [0]


async def test_repeat_switch_and_end(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    entry = await _setup(hass, V1R, {CONF_DISPATCH_GRID_CHARGING: True})
    coordinator = entry.runtime_data.coordinator
    events = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    # A charge turned Grid charging on; a charge freeze turns it off again.
    await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                   {"power_watts": -2000, "duration_seconds": 600}, blocking=True)
    await hass.async_block_till_done()
    assert coordinator.grid_charging_by_dispatch
    with caplog.at_level(logging.WARNING):
        await _freeze(hass, "charge_only")
        assert not coordinator.grid_charging_by_dispatch
        caplog.clear()
        await _freeze(hass, "charge_only")  # Predbat re-sends it every cycle
    assert "Battery dispatch started" not in caplog.text
    await hass.services.async_call(DOMAIN, "resume_battery_operation", {}, blocking=True)
    await hass.async_block_till_done()
    assert [(e.data["reason"], e.data["freeze"]) for e in events] == [("stopped", "charge_only")]
    assert hass.states.get(ENTITY).state == "off"


async def test_renewal_failures_end_it(hass: HomeAssistant, fake_powerwall, monkeypatch) -> None:
    entry = await _setup(hass, V1R)
    await _freeze(hass, "charge_only")

    async def fail(power: float, seconds: int) -> None:
        raise HomeAssistantError("timed out")

    monkeypatch.setattr(entry.runtime_data.coordinator, "async_send_dispatch", fail)
    for _ in range(3):
        await _tick(hass, entry)
    assert not entry.runtime_data.dispatch.active
    assert entry.runtime_data.dispatch.last_end_reason == "connection_lost"


async def test_a_step_waits_for_a_command_on_its_way(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    session = entry.runtime_data.dispatch
    await _freeze(hass, "discharge_only")
    sent = len(_sent(fake_powerwall))
    async with session._send_lock:
        await _reading(hass, entry, home=5000, solar=0)
    assert len(_sent(fake_powerwall)) == sent  # the renewal catches up
    await _tick(hass, entry)
    assert _sent(fake_powerwall)[-1] == 5000
    # Ended while a step waited: nothing more is sent.
    await _reading(hass, entry, home=5000, solar=0)
    state = session.state
    session._end("stopped")
    await session._async_send_freeze(state)
    assert _sent(fake_powerwall)[-1] == 5000


async def test_ended_during_the_extra_read(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    entry = await _setup(hass, V1R)
    session = entry.runtime_data.dispatch
    await _freeze(hass, "discharge_only")
    await _reading(hass, entry, home=3000, solar=0)
    sent = len(_sent(fake_powerwall))
    freezer.tick(timedelta(seconds=300))

    async def stopped_meanwhile(timeout: float) -> bool:
        session._end("stopped")
        return False

    with patch.object(entry.runtime_data.coordinator, "async_read_battery_level", stopped_meanwhile):
        await _tick(hass, entry)
    assert len(_sent(fake_powerwall)) == sent
