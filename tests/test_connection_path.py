"""2.9.0: the Connection path sensor, the settings the gateway last reported,
and readings over the Powerwall's Wi-Fi (read-only) while it refuses the key."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi.const import DOMAIN
from custom_components.tesla_tedapi.diagnostics import async_get_config_entry_diagnostics

from .conftest import DIN

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
FALLBACK = {**V1R, "wifi_host": "192.168.91.1"}
WIFI = {"connection_mode": "wifi", "host": "192.168.91.1", "gw_pwd": "ABCDE12345", "scan_interval": 30}
ACCOUNT = {"owners_email": "a@b.c", "owners_authpath": "/tmp", "site_id": 42}


async def _setup(hass: HomeAssistant, data: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data, minor_version=2, title="Powerwall")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _eid(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DIN}_{key}")
    assert entity_id, key
    return entity_id


def _path(hass: HomeAssistant):
    return hass.states.get(_eid(hass, "sensor", "connection_path"))


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A full read with a new client (the fake fails, or fails over, when
    a client is made)."""
    coordinator = entry.runtime_data.coordinator
    coordinator._pw = None
    coordinator._full_at = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Connection path
# ---------------------------------------------------------------------------


async def test_signed_then_wifi_fallback(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, FALLBACK)
    state = _path(hass)
    assert state.state == "signed"
    # The gateway answers: its entities show everything, nothing extra here.
    assert "last_known_settings" not in state.attributes
    assert state.attributes["options"] == ["signed", "wifi_fallback", "wifi_password", "cloud", "none"]
    fake_powerwall.lan_failed = True
    await _refresh(hass, entry)
    assert _path(hass).state == "wifi_fallback"
    assert (await async_get_config_entry_diagnostics(hass, entry))["connection_path"] == "wifi_fallback"


async def test_wifi_password_connection(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, WIFI)
    assert _path(hass).state == "wifi_password"


async def test_cloud_only_while_the_gateway_is_down(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    fake_powerwall.fail_local = True
    await _refresh(hass, entry)
    state = _path(hass)
    assert state.state == "cloud"
    assert state.attributes["last_local_read"]
    assert state.attributes["last_known_settings"]["operation_mode"] == "self_consumption"


async def test_none_keeps_the_last_known_settings(hass: HomeAssistant, fake_powerwall) -> None:
    """Without a cloud backup, the other entities go unavailable; this one
    says so, with what the gateway last reported."""
    entry = await _setup(hass, V1R)
    reserve = hass.states.get(_eid(hass, "number", "backup_reserve")).state
    fake_powerwall.fail_local = True
    await _refresh(hass, entry)
    assert hass.states.get(_eid(hass, "sensor", "battery_level")).state == "unavailable"
    state = _path(hass)
    assert state.state == "none"
    settings = state.attributes["last_known_settings"]
    assert settings["operation_mode"] == "self_consumption"
    assert float(settings["backup_reserve"]) == float(reserve)
    assert {"grid_charging", "grid_export", "read_at"} <= settings.keys()
    assert state.attributes["last_local_read"] == settings["read_at"]

    fake_powerwall.fail_local = False
    await _refresh(hass, entry)
    assert _path(hass).state == "signed"
    assert "last_known_settings" not in _path(hass).attributes


# ---------------------------------------------------------------------------
# Read-only over the Powerwall's Wi-Fi while the key is refused
# ---------------------------------------------------------------------------


async def test_key_refused_reads_over_wifi_read_only(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    entry = await _setup(hass, FALLBACK)
    coordinator = entry.runtime_data.coordinator
    # A dropped export rule is not written back over the read-only client.
    fake_powerwall.export_rule = None
    coordinator.wanted["grid_export"] = "pv_only"
    fake_powerwall.fail_signed = True
    with caplog.at_level(logging.WARNING):
        await _refresh(hass, entry)
        await _refresh(hass, entry)
    assert _path(hass).state == "wifi_password"
    assert hass.states.get(_eid(hass, "sensor", "battery_level")).state == "50.0"
    readonly = [p for p in fake_powerwall.instances if p.tedapi_mode == "wifi"]
    assert len(readonly) == 1  # kept between polls
    assert readonly[0].kwargs["host"] == "192.168.91.1"
    assert "rsa_key_path" not in readonly[0].kwargs
    assert caplog.text.count("refuses the key") == 1
    health = hass.states.get(_eid(hass, "sensor", "setup_health"))
    assert health.state == "degraded" and "signed_connection" in health.attributes["problems"]

    # Settings can't be changed over it (and there is no cloud backup).
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("number", "set_value",
                                       {"entity_id": _eid(hass, "number", "backup_reserve"), "value": 40},
                                       blocking=True)
    assert readonly[0].writes == []

    fake_powerwall.fail_signed = False
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await _refresh(hass, entry)
    assert _path(hass).state == "signed"
    assert "accepts the key again" in caplog.text
    assert coordinator._readonly_pw is None


async def test_no_read_only_without_an_address_on_the_powerwalls_wifi(hass: HomeAssistant,
                                                                        fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    fake_powerwall.fail_signed = True
    await _refresh(hass, entry)
    assert _path(hass).state == "none"
    assert not [p for p in fake_powerwall.instances if p.tedapi_mode == "wifi"]


async def test_no_read_only_when_the_password_is_refused(hass: HomeAssistant, fake_powerwall,
                                                         fake_gateway) -> None:
    entry = await _setup(hass, FALLBACK)
    fake_powerwall.fail_signed = True
    fake_gateway.password = "ABCDE99999"
    await _refresh(hass, entry)
    assert _path(hass).state == "none"
    assert not [p for p in fake_powerwall.instances if p.tedapi_mode == "wifi"]


async def test_read_only_that_fails_too(hass: HomeAssistant, fake_powerwall) -> None:
    """The Wi-Fi client fails as well: nothing to show, and it's tried anew
    next time."""
    entry = await _setup(hass, FALLBACK)
    fake_powerwall.fail_signed = True
    fake_powerwall.fail_local = True
    await _refresh(hass, entry)
    assert _path(hass).state == "none"
    assert entry.runtime_data.coordinator._readonly_pw is None


async def test_read_only_keeps_going_once_reauth_has_started(hass: HomeAssistant, fake_powerwall) -> None:
    """After the repeated refusals start a reauthentication, readings still
    come over the Wi-Fi meanwhile."""
    entry = await _setup(hass, FALLBACK)
    coordinator = entry.runtime_data.coordinator
    fake_powerwall.fail_signed = True
    await _refresh(hass, entry)
    coordinator._reauth_started = True
    await _refresh(hass, entry)
    assert _path(hass).state == "wifi_password"


async def test_last_known_settings_survive_a_restart(hass: HomeAssistant, fake_powerwall, hass_storage) -> None:
    """With a cloud backup the entry starts while the gateway is down: the
    Connection path still says when it last answered, and what it reported."""
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    coordinator = entry.runtime_data.coordinator
    read_at, settings = coordinator.last_local_read, coordinator.last_known_settings
    assert read_at is not None and settings["operation_mode"] == "self_consumption"
    fake_powerwall.fail_local = True
    assert await hass.config_entries.async_reload(entry.entry_id)  # saves, then starts again
    await hass.async_block_till_done()
    state = _path(hass)
    assert state.state == "cloud"
    assert state.attributes["last_local_read"] == read_at.isoformat()
    assert state.attributes["last_known_settings"] == settings


async def test_stored_last_known_values_that_cant_be_used(hass: HomeAssistant, fake_powerwall,
                                                          hass_storage) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={**V1R, **ACCOUNT}, minor_version=2,
                            title="Powerwall")
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 1, "key": f"{DOMAIN}.{entry.entry_id}", "data": {
        "last_local_read": "2026-11-26T09:00:00", "last_known_settings": ["not", "a", "dict"]}}
    entry.add_to_hass(hass)
    fake_powerwall.fail_local = True
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = _path(hass)
    assert state.state == "cloud"
    assert state.attributes["last_local_read"] is None and state.attributes["last_known_settings"] is None
