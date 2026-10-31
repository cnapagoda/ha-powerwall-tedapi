"""2.4.0 (experimental): Tesla's on-grid solar curtailment flag, read and
written over the signed connection. Never tested on a real Powerwall."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest

from .conftest import FakePowerwall, local
from .test_storm import V1R, WIFI, _eid, _refresh, _setup


async def _enabled(hass: HomeAssistant, entry) -> str:
    """The switch is disabled by default: enable it like a user would."""
    eid = _eid(hass, "switch", "solar_curtailment")
    assert er.async_get(hass).async_get(eid).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    er.async_get(hass).async_update_entity(eid, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return eid


async def _switch(hass: HomeAssistant, eid: str, on: bool) -> None:
    await hass.services.async_call("switch", "turn_on" if on else "turn_off", {"entity_id": eid}, blocking=True)


async def test_read_and_write(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    eid = await _enabled(hass, entry)
    assert hass.states.get(eid).state == "off"  # the key is absent

    await _switch(hass, eid, True)
    pw = local(fake_powerwall)
    assert ("config", {"site_info.on_grid_solar_curtailment_enabled": True}) in pw.writes
    assert hass.states.get(eid).state == "on"  # read back from the gateway's config

    await _switch(hass, eid, False)
    await _refresh(hass, entry)  # the refresh after a change is debounced
    assert hass.states.get(eid).state == "off"
    assert ("config", {"site_info.on_grid_solar_curtailment_enabled": False}) in pw.writes


async def test_refused_write(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    eid = await _enabled(hass, entry)
    FakePowerwall.config_writes_ok = False
    with pytest.raises(HomeAssistantError) as err:
        await _switch(hass, eid, True)
    assert err.value.translation_key == "write_failed"

    def boom(_updates):
        raise ConnectionError("lost")

    FakePowerwall.config_writes_ok = True
    local(fake_powerwall).client.tedapi._write_config = boom
    with pytest.raises(HomeAssistantError):
        await _switch(hass, eid, True)
    assert hass.states.get(eid).state == "off"


async def test_needs_signed_connection(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    coordinator._pw.tedapi_mode = "full"
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_solar_curtailment(True)
    assert err.value.translation_key == "curtailment_needs_v1r"
    assert not any(w[0] == "config" for w in local(fake_powerwall).writes)


async def test_not_on_wifi_entries(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, WIFI)
    assert _eid(hass, "switch", "solar_curtailment") is None


async def test_unavailable_without_the_config(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    eid = await _enabled(hass, entry)

    def broken(force=False):
        raise ConnectionError("no config")

    local(fake_powerwall).client.tedapi.get_config = broken
    await _refresh(hass, entry)
    assert hass.states.get(eid).state == "unavailable"
