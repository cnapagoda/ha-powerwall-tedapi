"""Failure paths: gateway reads that fail one by one, restores that fail,
backup events that can't be read, and signed-only commands on a Wi-Fi entry."""
from __future__ import annotations

import logging
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, FakePowerwall, local

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
WIFI = {"connection_mode": "wifi", "host": "192.168.91.1", "gw_pwd": "pw", "scan_interval": 30}


async def _setup(hass: HomeAssistant, data: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data, title="Powerwall")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _eid(hass: HomeAssistant, platform: str, key: str) -> str:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DIN}_{key}")


def _raise(*args, **kwargs):
    raise ConnectionError("gateway hiccup")


async def test_optional_reads_fail_one_by_one(hass: HomeAssistant, fake_powerwall) -> None:
    """Vitals, version, meters, status, alerts and backup time can each fail
    without taking the readings that did work with them."""
    failing = ("vitals", "version", "poll", "system_status", "status", "alerts", "get_time_remaining")
    with patch.multiple(FakePowerwall, **{name: _raise for name in failing}):
        entry = await _setup(hass, V1R)
        data = entry.runtime_data.coordinator.data
    assert entry.state is ConfigEntryState.LOADED
    assert data["din"] == DIN
    assert data["firmware_version"] is None
    assert all(value is None for value in data["energy"].values())
    assert hass.states.get(_eid(hass, "sensor", "battery_level")).state == "50.0"


async def test_din_read_failing_retries_setup(hass: HomeAssistant, fake_powerwall) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, title="Powerwall")
    entry.add_to_hass(hass)
    with patch.object(FakePowerwall, "din", _raise):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize("setter", [_raise, lambda self, mode: None, lambda self, mode: {"error": "refused"}])
async def test_restore_that_fails_is_logged(hass: HomeAssistant, fake_powerwall, caplog, setter) -> None:
    """The gateway drops the export rule and writing it back fails (an error,
    or the gateway refuses): logged, not counted as restored, still shown."""
    entry = await _setup(hass, V1R)
    fake_powerwall.export_rule = None
    with patch.object(FakePowerwall, "set_grid_export", setter), caplog.at_level(logging.WARNING):
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()
    assert "dropped its grid export setting; restoring pv_only failed" in caplog.text
    state = hass.states.get(_eid(hass, "select", "grid_export"))
    assert state.state == "pv_only"
    assert state.attributes["restored_count"] == 0
    assert state.attributes["missing_on_powerwall"] is True


@pytest.mark.parametrize("read", [_raise, lambda self: None])
async def test_backup_events_unreadable_from_the_start(hass: HomeAssistant, fake_powerwall, read) -> None:
    with patch.object(FakePowerwall, "get_backup_events", read):
        entry = await _setup(hass, V1R)
    assert entry.runtime_data.coordinator.data["backup_event"] is None


async def test_signed_only_commands_refused_on_wifi(hass: HomeAssistant, fake_powerwall) -> None:
    """The services check this first; the coordinator refuses too."""
    entry = await _setup(hass, WIFI)
    coordinator = entry.runtime_data.coordinator
    for call, args in ((coordinator._max_backup, (60,)), (coordinator._send_island, (True,)),
                       (coordinator._send_dispatch, (1000.0, 60)), (coordinator._send_resume, ())):
        with pytest.raises(HomeAssistantError):
            call(*args)
    assert not local(fake_powerwall).dispatches


async def test_max_backup_refused_by_gateway(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    with patch.object(FakePowerwall, "schedule_max_backup", lambda self, duration_seconds=0: False), \
            pytest.raises(HomeAssistantError):
        await entry.runtime_data.coordinator.async_max_backup(30)


async def test_grid_status_read_fails_quietly(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    with patch.object(FakePowerwall, "grid_status", _raise):
        assert await coordinator.async_read_grid_status() is None
    coordinator._pw = None  # no client (gateway down)
    assert await coordinator.async_read_grid_status() is None


async def test_no_password_counts_as_password_problem(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    coordinator._gw_pwd = ""
    assert coordinator._classify_failure() == "password"
