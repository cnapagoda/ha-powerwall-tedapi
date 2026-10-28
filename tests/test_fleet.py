"""Cloud backup through the core Tesla Fleet integration's sign-in."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, local

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}


class FakeFleetSite:
    """tesla_fleet_api EnergySpecific: site_info + the settings commands."""

    def __init__(self) -> None:
        self.info = {"default_real_mode": "self_consumption", "backup_reserve_percent": 20,
                     "components": {"customer_preferred_export_rule": "never",
                                    "disallow_charge_from_grid_with_solar_installed": True,
                                    "gateways": [{"din": DIN}]}}
        self.calls: list[tuple[str, dict]] = []
        self.fail = False

    async def site_info(self):
        self.calls.append(("site_info", {}))
        if self.fail:
            raise RuntimeError("401 token expired")
        return {"response": self.info}

    async def operation(self, default_real_mode):
        self.calls.append(("operation", {"default_real_mode": default_real_mode}))
        return {"response": {"code": 201}}

    async def backup(self, backup_reserve_percent):
        self.calls.append(("backup", {"backup_reserve_percent": backup_reserve_percent}))
        return {"response": {"code": 201}}

    async def grid_import_export(self, **kwargs):
        self.calls.append(("grid_import_export", kwargs))
        return {"response": {"code": 204}}


@pytest.fixture
def fleet():
    """A loaded Tesla Fleet entry with one energy site (the core integration
    itself is not loaded in tests)."""
    site = FakeFleetSite()
    entry = SimpleNamespace(state=ConfigEntryState.LOADED,
                            runtime_data=SimpleNamespace(energysites=[SimpleNamespace(api=site)]))
    with patch("custom_components.tesla_tedapi.fleet.fleet_entries", return_value=[entry]):
        yield site


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, options={"fleet_backup": True},
                            title="Powerwall")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _eid(hass: HomeAssistant, platform: str, key: str) -> str:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DIN}_{key}")


async def test_fleet_backup_checked_and_used_for_writes(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    site = fleet
    await _setup(hass)
    health = hass.states.get(_eid(hass, "sensor", "setup_health"))
    assert health.state == "ok"
    assert health.attributes["cloud_source"] == "tesla_fleet"
    assert health.attributes["cloud_last_ok"] is not None
    assert ("site_info", {}) in site.calls
    # No pypowerwall cloud client (Owner API) is made.
    assert [p for p in fake_powerwall.instances if p.mode == "cloud"] == []

    # The gateway refuses the write: it goes through Tesla Fleet.
    local(fake_powerwall).set_grid_export = lambda mode: None
    await hass.services.async_call("select", "select_option", {
        "entity_id": _eid(hass, "select", "grid_export"), "option": "pv_only"}, blocking=True)
    assert ("grid_import_export", {"customer_preferred_export_rule": "pv_only"}) in site.calls

    local(fake_powerwall).set_grid_charging = lambda mode: None
    await hass.services.async_call("switch", "turn_on", {
        "entity_id": _eid(hass, "switch", "grid_charging")}, blocking=True)
    assert ("grid_import_export", {"disallow_charge_from_grid_with_solar_installed": False}) in site.calls


async def test_fleet_backup_fills_in_settings(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    fleet.info["default_real_mode"] = "autonomous"
    entry = await _setup(hass)
    local(fake_powerwall).get_mode = lambda: None  # gateway can't say
    entry.runtime_data.coordinator._cloud_cache = None
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(_eid(hass, "select", "operation_mode"))
    assert state.state == "autonomous"
    assert state.attributes["source"] == "mixed"


async def test_fleet_backup_failing(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    fleet.fail = True
    await _setup(hass)
    health = hass.states.get(_eid(hass, "sensor", "setup_health"))
    assert health.state == "degraded" and "cloud_backup" in health.attributes["problems"]


async def test_fleet_backup_without_fleet(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass)
    health = hass.states.get(_eid(hass, "sensor", "setup_health"))
    assert health.state == "degraded" and "cloud_backup" in health.attributes["problems"]
