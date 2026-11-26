"""2.9.0: every full read checks that the gateway at the address is this
entry's Powerwall (its DIN); another one is never read from or written to."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, FakePowerwall

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
ACCOUNT = {"owners_email": "a@b.c", "owners_authpath": "/tmp", "site_id": 42}
OTHER = "1707000-11-J--TG0000000000OTHER"


def _entry(hass: HomeAssistant, data: dict, unique_id: str = DIN) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=unique_id, data=data, minor_version=2,
                            title=f"Powerwall {unique_id}")
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, data: dict, unique_id: str = DIN) -> MockConfigEntry:
    entry = _entry(hass, data, unique_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    coordinator = entry.runtime_data.coordinator
    coordinator._pw = None
    coordinator._full_at = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, entry: MockConfigEntry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"wrong_gateway_{entry.entry_id}")


async def test_another_powerwall_at_the_address(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    fake_powerwall.export_rule = None
    coordinator.wanted["grid_export"] = "pv_only"
    with patch.object(FakePowerwall, "din", lambda self: OTHER):
        await _refresh(hass, entry)
        await _refresh(hass, entry)
        await _refresh(hass, entry)
    assert not coordinator.last_update_success
    battery = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DIN}_battery_level")
    assert hass.states.get(battery).state == "unavailable"
    issue = _issue(hass, entry)
    assert issue is not None and issue.translation_key == "wrong_gateway"
    assert DIN not in str(issue.translation_placeholders) and OTHER not in str(issue.translation_placeholders)
    assert caplog.text.count("nothing is read from it") == 1
    assert DIN not in caplog.text and OTHER not in caplog.text
    # Its answers don't count against our password or key, and nothing was
    # written back to it.
    assert coordinator._password_refused == 0 and coordinator._key_refused == 0
    assert all(not p.writes for p in fake_powerwall.instances[1:])
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    await _refresh(hass, entry)
    assert coordinator.last_update_success
    assert _issue(hass, entry) is None


async def test_settings_go_through_the_cloud_meanwhile(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    with patch.object(FakePowerwall, "din", lambda self: OTHER):
        await _refresh(hass, entry)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.last_update_success and not coordinator.data["local_ok"]
    assert coordinator.data["control_source"] == "cloud"
    assert _issue(hass, entry) is not None


async def test_setup_waits_and_says_why(hass: HomeAssistant, fake_powerwall) -> None:
    entry = _entry(hass, V1R)
    with patch.object(FakePowerwall, "din", lambda self: OTHER):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert _issue(hass, entry) is not None


async def test_the_dins_case_and_spaces_dont_matter(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    with patch.object(FakePowerwall, "din", lambda self: f" {DIN.lower()} "):
        await _refresh(hass, entry)
    assert entry.runtime_data.coordinator.last_update_success
    assert _issue(hass, entry) is None


async def test_an_entry_id_that_isnt_a_din_is_not_compared(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, unique_id="Powerwall-3")
    assert entry.runtime_data.coordinator.last_update_success
    assert _issue(hass, entry) is None


async def test_unload_removes_the_repair(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    with patch.object(FakePowerwall, "din", lambda self: OTHER):
        await _refresh(hass, entry)
    assert _issue(hass, entry) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, entry) is None
