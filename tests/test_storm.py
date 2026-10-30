"""Storm Watch (cloud setting), backup events and max backup (local, signed
v1r), grid services and the off-grid vehicle charging reserve."""
from __future__ import annotations

import time
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, cloud, local
from .test_fleet import FLEET_ENTRIES, SITE_INFO, _fleet_entry, _site

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
WIFI = {"connection_mode": "wifi", "host": "192.168.91.1", "gw_pwd": "ABCDE12345", "scan_interval": 30}
ACCOUNT = {"owners_email": "a@b.c", "owners_authpath": "/tmp", "site_id": 42}


async def _setup(hass: HomeAssistant, data: dict, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data, minor_version=2,
                            options=options or {}, title="Powerwall")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _eid(hass: HomeAssistant, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DIN}_{key}")


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_max_backup_start_and_cancel(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    sensor = _eid(hass, "binary_sensor", "backup_event")
    assert hass.states.get(sensor).state == "off"

    await hass.services.async_call(DOMAIN, "start_max_backup", {"duration_minutes": 120}, blocking=True)
    await hass.async_block_till_done()
    assert ("max_backup", 7200) in local(fake_powerwall).writes
    state = hass.states.get(sensor)
    assert state.state == "on"
    assert state.attributes["kind"] == "manual"
    assert state.attributes["end"] is not None

    await hass.services.async_call(DOMAIN, "cancel_max_backup", {}, blocking=True)
    await hass.async_block_till_done()
    assert ("max_backup", None) in local(fake_powerwall).writes
    assert hass.states.get(sensor).state == "off"


async def test_storm_watch_event_from_tesla(hass: HomeAssistant, fake_powerwall) -> None:
    now = int(time.time())
    fake_powerwall.backup_events = {"manual_backup": None, "backup_events": [
        {"id": "1", "name": "Storm Watch", "start_time": now - 60, "duration_seconds": 3600}]}
    await _setup(hass, V1R)
    state = hass.states.get(_eid(hass, "binary_sensor", "backup_event"))
    assert state.state == "on" and state.attributes["kind"] == "Storm Watch"


async def test_upcoming_event_is_not_active(hass: HomeAssistant, fake_powerwall) -> None:
    now = int(time.time())
    fake_powerwall.backup_events = {"manual_backup": None, "backup_events": [
        {"id": "1", "name": "Storm Watch", "start_time": now + 3600, "duration_seconds": 3600}]}
    await _setup(hass, V1R)
    state = hass.states.get(_eid(hass, "binary_sensor", "backup_event"))
    assert state.state == "off" and state.attributes["start"] is not None


async def test_backup_events_read_every_five_minutes(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    calls = []
    pw = local(fake_powerwall)
    pw.get_backup_events = lambda: calls.append(1) or {"manual_backup": None, "backup_events": []}
    await _refresh(hass, entry)
    assert calls == []
    entry.runtime_data.coordinator._backup_events = (time.monotonic() - 301,
                                                     entry.runtime_data.coordinator._backup_events[1])
    await _refresh(hass, entry)
    assert calls == [1]


async def test_max_backup_needs_v1r(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, WIFI)
    assert _eid(hass, "binary_sensor", "backup_event") is None
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "start_max_backup", {"duration_minutes": 60}, blocking=True)


async def test_storm_watch_through_owner_api(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, {**V1R, **ACCOUNT})
    switch = _eid(hass, "switch", "storm_watch")
    assert hass.states.get(switch).state == "off"
    assert hass.states.get(switch).attributes["cloud_backend"] == "owner_api"
    assert hass.states.get(_eid(hass, "binary_sensor", "storm_watch_active")).state == "off"
    # Not reported by this site: no entity.
    assert _eid(hass, "binary_sensor", "grid_services_active") is None
    assert _eid(hass, "number", "off_grid_reserve") is None

    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done()
    assert ("STORM_MODE_SETTINGS", {"enabled": True}) in cloud(fake_powerwall).writes
    # A cloud setting: never sent to the gateway.
    assert not [w for w in local(fake_powerwall).writes if "storm" in str(w).lower()]
    assert hass.states.get(switch).state == "on"


async def test_owner_api_extras_read_half_hourly(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    calls = []
    client = cloud(fake_powerwall).client
    client.get_site_power = lambda force=False: calls.append(1) or {"response": {"storm_mode_active": True}}
    await _refresh(hass, entry)
    assert calls == []
    owner = entry.runtime_data.coordinator.backends[0]
    owner.extras = (owner.extras[0] - 1801, owner.extras[1])
    await _refresh(hass, entry)
    assert calls == [1]
    assert hass.states.get(_eid(hass, "binary_sensor", "storm_watch_active")).state == "on"


async def test_no_storm_watch_when_not_capable(hass: HomeAssistant, fake_powerwall) -> None:
    fake_powerwall.storm_capable = False
    await _setup(hass, {**V1R, **ACCOUNT})
    assert _eid(hass, "switch", "storm_watch") is None


async def test_no_cloud_no_storm_watch(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    assert _eid(hass, "switch", "storm_watch") is None
    assert _eid(hass, "binary_sensor", "storm_watch_active") is None


async def test_storm_watch_and_off_grid_reserve_through_fleet(hass: HomeAssistant, fake_powerwall) -> None:
    site = _site({**SITE_INFO, "user_settings_storm_mode_enabled": True,
                  "components_off_grid_vehicle_charging_reserve_supported": True,
                  "off_grid_vehicle_charging_reserve_percent": 20})
    site.live_coordinator.data["storm_mode_active"] = True
    with patch(FLEET_ENTRIES, return_value=[_fleet_entry(site)]):
        await _setup(hass, V1R, {"cloud_backends": "tesla_fleet"})
        switch = _eid(hass, "switch", "storm_watch")
        assert hass.states.get(switch).state == "on"
        assert hass.states.get(switch).attributes["cloud_backend"] == "tesla_fleet"
        assert hass.states.get(_eid(hass, "binary_sensor", "storm_watch_active")).state == "on"
        number = _eid(hass, "number", "off_grid_reserve")
        assert hass.states.get(number).state == "20"

        await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
        await hass.services.async_call("number", "set_value", {"entity_id": number, "value": 35}, blocking=True)
    assert ("storm_mode", {"enabled": False}) in site.api.calls
    assert ("off_grid_vehicle_charging_reserve", {"off_grid_vehicle_charging_reserve_percent": 35}) in site.api.calls
    assert fake_powerwall.instances and cloud(fake_powerwall) is None


async def test_storm_watch_appears_when_cloud_answers_later(hass: HomeAssistant, fake_powerwall) -> None:
    site = _site()
    fleet_entry = _fleet_entry(site)
    with patch(FLEET_ENTRIES, return_value=[]):
        entry = await _setup(hass, V1R, {"cloud_backends": "tesla_fleet"})
        assert _eid(hass, "switch", "storm_watch") is None
    with patch(FLEET_ENTRIES, return_value=[fleet_entry]):
        await _refresh(hass, entry)
        assert hass.states.get(_eid(hass, "switch", "storm_watch")).state == "off"
