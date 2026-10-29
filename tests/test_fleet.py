"""2.3 cloud backups: the Tesla Fleet integration's sign-in, alone or with the
Owner API in either order; migration and the Cloud backup options step."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, cloud, local

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
ACCOUNT = {"owners_email": "a@b.c", "owners_authpath": "/tmp", "site_id": 42}
FLEET_ENTRIES = "custom_components.tesla_tedapi.fleet.fleet_entries"


class FakeFleetSite:
    """tesla_fleet_api EnergySpecific: site_info + the settings commands."""

    def __init__(self, din: str = DIN) -> None:
        self.info = {"default_real_mode": "self_consumption", "backup_reserve_percent": 20,
                     "components": {"customer_preferred_export_rule": "never",
                                    "disallow_charge_from_grid_with_solar_installed": True,
                                    "gateways": [{"din": din}]}}
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
        if self.fail:
            raise RuntimeError("401 token expired")
        return {"response": {"code": 204}}


def _fleet_entry(*sites: FakeFleetSite, state=ConfigEntryState.LOADED) -> SimpleNamespace:
    return SimpleNamespace(state=state, runtime_data=SimpleNamespace(
        energysites=[SimpleNamespace(api=site) for site in sites]))


@pytest.fixture
def fleet():
    """A loaded Tesla Fleet entry with one energy site (the core integration
    itself is not loaded in tests)."""
    site = FakeFleetSite()
    with patch(FLEET_ENTRIES, return_value=[_fleet_entry(site)]):
        yield site


async def _setup(hass: HomeAssistant, data: dict, choice: str | None = "tesla_fleet") -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data, minor_version=2,
                            options={"cloud_backends": choice} if choice else {}, title="Powerwall")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _eid(hass: HomeAssistant, platform: str, key: str) -> str:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DIN}_{key}")


def _health(hass: HomeAssistant):
    return hass.states.get(_eid(hass, "sensor", "setup_health"))


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


async def _fail_checks(hass: HomeAssistant, entry: MockConfigEntry, name: str) -> None:
    """Three failed hourly checks of one backup."""
    backend = entry.runtime_data.coordinator.backend(name)
    for _ in range(3):
        backend.check_at -= 3601
        backend.last_ok = None
        await _refresh(hass, entry)


async def test_fleet_backup_checked_and_used_for_writes(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    site = fleet
    await _setup(hass, V1R)
    health = _health(hass)
    assert health.state == "ok"
    assert list(health.attributes["cloud_backends"]) == ["tesla_fleet"]
    assert health.attributes["cloud_backends"]["tesla_fleet"]["ok"] is True
    assert health.attributes["cloud_last_ok"] is not None
    assert health.attributes["cloud_token_expires"] is None
    assert ("site_info", {}) in site.calls
    # No pypowerwall cloud client (Owner API) is made.
    assert cloud(fake_powerwall) is None

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
    entry = await _setup(hass, V1R)
    local(fake_powerwall).get_mode = lambda: None  # gateway can't say
    entry.runtime_data.coordinator.backends[0].cache = None
    await _refresh(hass, entry)
    state = hass.states.get(_eid(hass, "select", "operation_mode"))
    assert state.state == "autonomous"
    assert state.attributes["source"] == "mixed"
    assert state.attributes["cloud_backend"] == "tesla_fleet"


async def test_fleet_backup_failing(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    fleet.fail = True
    entry = await _setup(hass, V1R)
    health = _health(hass)
    assert health.state == "degraded" and "cloud_backup" in health.attributes["problems"]
    await _fail_checks(hass, entry, "tesla_fleet")
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"fleet_backup_{entry.entry_id}")
    assert issue is not None and not issue.is_fixable
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"cloud_login_{entry.entry_id}") is None

    fleet.fail = False
    entry.runtime_data.coordinator.backends[0].check_at -= 3601
    await _refresh(hass, entry)
    assert _health(hass).state == "ok"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"fleet_backup_{entry.entry_id}") is None


async def test_fleet_backup_waits_for_fleet_to_load(hass: HomeAssistant, fake_powerwall) -> None:
    site = FakeFleetSite()
    fleet_entry = _fleet_entry(site, state=ConfigEntryState.SETUP_IN_PROGRESS)
    with patch(FLEET_ENTRIES, return_value=[fleet_entry]):
        entry = await _setup(hass, V1R)
        backend = entry.runtime_data.coordinator.backends[0]
        assert site.calls == [] and backend.failures == 0
        assert _health(hass).attributes["cloud_backends"]["tesla_fleet"]["starting"] is True
        assert _health(hass).state == "ok"  # not degraded while it loads
        fleet_entry.state = ConfigEntryState.LOADED
        await _refresh(hass, entry)
    assert ("site_info", {}) in site.calls
    assert _health(hass).state == "ok"


async def test_fleet_backup_without_fleet(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    health = _health(hass)
    assert health.state == "degraded" and "cloud_backup" in health.attributes["problems"]


async def test_fleet_site_matched_by_din(hass: HomeAssistant, fake_powerwall) -> None:
    other, ours = FakeFleetSite("1707000-11-J--TGOTHER"), FakeFleetSite()
    ours.info["default_real_mode"] = "backup"
    with patch(FLEET_ENTRIES, return_value=[_fleet_entry(other, ours)]):
        entry = await _setup(hass, V1R)
        local(fake_powerwall).get_mode = lambda: None
        entry.runtime_data.coordinator.backends[0].cache = None
        await _refresh(hass, entry)
    assert hass.states.get(_eid(hass, "select", "operation_mode")).state == "backup"


async def test_fleet_site_with_another_powerwall_is_not_used(hass: HomeAssistant, fake_powerwall) -> None:
    with patch(FLEET_ENTRIES, return_value=[_fleet_entry(FakeFleetSite("1707000-11-J--TGOTHER"))]):
        await _setup(hass, V1R)
    assert _health(hass).state == "degraded"


async def test_fleet_first_falls_back_to_owner(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    fleet.fail = True
    entry = await _setup(hass, {**V1R, **ACCOUNT}, "fleet_then_owner")
    coordinator = entry.runtime_data.coordinator
    assert [b.name for b in coordinator.backends] == ["tesla_fleet", "owner_api"]
    # One backup still works: not degraded, but the failing one is reported.
    health = _health(hass)
    assert health.state == "ok"
    assert health.attributes["cloud_backends"]["tesla_fleet"]["ok"] is False
    assert health.attributes["cloud_backends"]["owner_api"]["ok"] is True
    await _fail_checks(hass, entry, "tesla_fleet")
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"fleet_backup_{entry.entry_id}") is not None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"cloud_login_{entry.entry_id}") is None

    # Reads come from the Owner API.
    local(fake_powerwall).get_mode = lambda: None
    coordinator.backend("owner_api").cache = None
    await _refresh(hass, entry)
    assert hass.states.get(_eid(hass, "select", "operation_mode")).attributes["cloud_backend"] == "owner_api"

    # A write Tesla Fleet refuses goes to the Owner API.
    local(fake_powerwall).set_grid_export = lambda mode: None
    await hass.services.async_call("select", "select_option", {
        "entity_id": _eid(hass, "select", "grid_export"), "option": "pv_only"}, blocking=True)
    assert ("grid_export", "pv_only") in cloud(fake_powerwall).writes


async def test_owner_first_writes_once(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    await _setup(hass, {**V1R, **ACCOUNT}, "owner_then_fleet")
    local(fake_powerwall).set_mode = lambda mode: None
    await hass.services.async_call("select", "select_option", {
        "entity_id": _eid(hass, "select", "operation_mode"), "option": "backup"}, blocking=True)
    assert ("mode", "backup") in cloud(fake_powerwall).writes
    assert not [c for c in fleet.calls if c[0] == "operation"]


async def test_owner_choice_without_account_has_no_backup(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, "owner_api")
    assert entry.runtime_data.coordinator.backends == []
    assert _health(hass).attributes["cloud_linked"] is False


async def test_default_is_the_linked_account(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT}, None)
    assert [b.name for b in entry.runtime_data.coordinator.backends] == ["owner_api"]


async def test_migration_drops_test_build_option(hass: HomeAssistant, fake_powerwall) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, version=1, minor_version=1,
                            options={"fleet_backup": True, "scan_interval": 20}, title="Powerwall")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 2
    assert entry.options == {"scan_interval": 20}
    assert entry.runtime_data.coordinator.backends == []


async def test_options_cloud_backup_choices(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT}, None)
    with patch("custom_components.tesla_tedapi.config_flow.fleet_entries", return_value=[object()]):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert "cloud_backup" in result["menu_options"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "cloud_backup"})
    assert result["type"] is FlowResultType.FORM
    selector = result["data_schema"].schema["cloud_backends"]
    assert selector.config["options"] == ["none", "owner_api", "tesla_fleet", "fleet_then_owner", "owner_then_fleet"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"cloud_backends": "fleet_then_owner"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["cloud_backends"] == "fleet_then_owner"
    assert [b.name for b in entry.runtime_data.coordinator.backends] == ["tesla_fleet", "owner_api"]


async def test_options_cloud_backup_without_fleet(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, None)
    with patch("custom_components.tesla_tedapi.config_flow.fleet_entries", return_value=[]):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "cloud_backup"})
    assert result["data_schema"].schema["cloud_backends"].config["options"] == ["none"]


async def test_unlink_keeps_fleet(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT}, "owner_then_fleet")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "unlink_account"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["cloud_backends"] == "tesla_fleet"
    assert "owners_email" not in entry.data
