"""2.4.0: charge_to / discharge_to, dispatch sessions that end at a target
battery level or a time limit."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed
import voluptuous as vol

from custom_components.tesla_tedapi.const import DOMAIN, EVENT_DISPATCH_ENDED

from .conftest import local
from .test_storm import V1R, WIFI, _eid, _refresh, _setup


def _soe(app_percent: float) -> float:
    """Raw gateway level for a battery level on the Tesla app scale."""
    return app_percent * 0.95 + 5


async def _call(hass: HomeAssistant, action: str, **data):
    return await hass.services.async_call(DOMAIN, action, data, blocking=True, return_response=True)


async def _tick(hass: HomeAssistant, seconds: int) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_charge_to_target(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)  # battery 50 %
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    resp = await _call(hass, "charge_to", target_soc=80, power_watts=3000, max_minutes=120)
    assert resp["active"] is True
    assert resp["power_watts"] == -3000
    assert resp["target_soc"] == 80
    assert resp["max_soc"] == 80 and resp["min_soc"] is None
    pw = local(fake_powerwall)
    assert pw.dispatches == [(-3000.0, 300)]
    sensor = hass.states.get(_eid(hass, "binary_sensor", "dispatch_active"))
    assert sensor.state == "on" and sensor.attributes["target_soc"] == 80

    fake_powerwall.soe = _soe(80)
    await _refresh(hass, entry)
    await _tick(hass, 61)
    assert pw.resumes == 1
    assert ended[-1].data["reason"] == "target_reached"
    assert hass.states.get(_eid(hass, "binary_sensor", "dispatch_active")).state == "off"


async def test_discharge_to_target(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    resp = await _call(hass, "discharge_to", target_soc=30, power_watts=5000, max_minutes=60)
    assert resp["power_watts"] == 5000
    assert resp["min_soc"] == 30 and resp["max_soc"] is None and resp["target_soc"] == 30
    assert local(fake_powerwall).dispatches == [(5000.0, 300)]

    # Not yet: renewed.
    fake_powerwall.soe = _soe(40)
    await _refresh(hass, entry)
    await _tick(hass, 61)
    assert len(local(fake_powerwall).dispatches) == 2 and not ended

    fake_powerwall.soe = _soe(29.5)
    await _refresh(hass, entry)
    await _tick(hass, 122)
    assert ended[-1].data["reason"] == "target_reached"


async def test_time_limit_ends_it(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    await _call(hass, "charge_to", target_soc=100, power_watts=1000, max_minutes=2)
    assert local(fake_powerwall).dispatches == [(-1000.0, 120)]  # chunk = time left
    await _tick(hass, 121)
    assert ended[-1].data["reason"] == "completed"
    assert local(fake_powerwall).resumes == 1


@pytest.mark.parametrize(("action", "target"), [("charge_to", 50), ("charge_to", 40),
                                                ("discharge_to", 50), ("discharge_to", 60)])
async def test_already_at_target(hass: HomeAssistant, fake_powerwall, action, target) -> None:
    await _setup(hass, V1R)  # battery 50 %
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, action, target_soc=target, power_watts=2000, max_minutes=30)
    assert err.value.translation_key == "target_reached"
    assert local(fake_powerwall).dispatches == []


async def test_unknown_level_still_starts(hass: HomeAssistant, fake_powerwall) -> None:
    """Without a reading the gateway can't take the command either; if it
    does, the session stops at the target once readings return."""
    entry = await _setup(hass, V1R)
    entry.runtime_data.coordinator.data["battery_level"] = None
    await _call(hass, "charge_to", target_soc=90, power_watts=2000, max_minutes=30)
    assert local(fake_powerwall).dispatches == [(-2000.0, 300)]


@pytest.mark.parametrize("data", [
    {"target_soc": 0, "power_watts": 2000, "max_minutes": 30},
    {"target_soc": 101, "power_watts": 2000, "max_minutes": 30},
    {"target_soc": 80, "power_watts": 50, "max_minutes": 30},
    {"target_soc": 80, "power_watts": 20000, "max_minutes": 30},
    {"target_soc": 80, "power_watts": 2000, "max_minutes": 0},
    {"target_soc": 80, "power_watts": 2000, "max_minutes": 1441},
    {"target_soc": 80, "power_watts": 2000},
])
async def test_schema(hass: HomeAssistant, fake_powerwall, data) -> None:
    await _setup(hass, V1R)
    with pytest.raises(vol.Invalid):
        await _call(hass, "charge_to", **data)


async def test_power_cap_option(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R, {"dispatch_max_watts": 5000})
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "discharge_to", target_soc=20, power_watts=6000, max_minutes=30)
    assert err.value.translation_key == "dispatch_power_cap"


async def test_needs_signed_connection(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, WIFI)
    for action in ("charge_to", "discharge_to"):
        with pytest.raises(ServiceValidationError) as err:
            await _call(hass, action, target_soc=60, power_watts=2000, max_minutes=30)
        assert err.value.translation_key == "dispatch_needs_v1r"


async def test_plain_dispatch_has_no_target(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    resp = await _call(hass, "dispatch_battery_power", power_watts=2000, duration_seconds=600, min_soc=45)
    assert resp["target_soc"] is None
    fake_powerwall.soe = _soe(44)
    await _refresh(hass, entry)
    await _tick(hass, 61)
    assert ended[-1].data["reason"] == "min_soc"  # unchanged for plain dispatches
