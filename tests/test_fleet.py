"""2.3 cloud backups: the Tesla Fleet / Teslemetry integration's site data and
API, alone or with the Owner API in either order; migration and the Cloud
backup options step."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import fleet as fleet_mod, tesla_cloud
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, cloud, local

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
ACCOUNT = {"owners_email": "a@b.c", "owners_authpath": "/tmp", "site_id": 42}
FLEET_ENTRIES = "custom_components.tesla_tedapi.fleet.fleet_entries"
SCOPES = ["energy_cmds", "energy_device_data", "offline_access", "openid", "user_data"]

# Tesla Fleet's info coordinator data for a Powerwall 3 with an expansion pack
# (flattened site_info, shape from a real site, serials replaced).
SITE_INFO = {
    "id": DIN,
    "default_real_mode": "self_consumption",
    "backup_reserve_percent": 5,
    "components_battery": True,
    "components_gateway": "teg",
    "components_customer_preferred_export_rule": "never",
    "components_disallow_charge_from_grid_with_solar_installed": True,
    "components_non_export_configured": True,
    "components_storm_mode_capable": True,
    "components_grid_services_enabled": False,
    "components_off_grid_vehicle_charging_reserve_supported": False,
    "user_settings_storm_mode_enabled": False,
    "components_gateways": [{"din": DIN, "part_number": "1707000-11-J", "part_type": 4,
                             "part_name": "Powerwall 3", "is_active": True}],
    "components_batteries": [{"din": "1807000-20-B--TG0000000000EX", "part_number": "1807000-20-B",
                              "part_name": "Unknown", "is_active": True}],
}


class FakeFleetApi:
    """tesla_fleet_api EnergySite: the settings commands (reads come from the
    coordinator)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = False
        self.hang = False

    async def _call(self, name: str, **kwargs):
        self.calls.append((name, kwargs))
        if self.hang:
            await asyncio.sleep(30)
        if self.fail:
            raise RuntimeError("401 token expired")
        return {"response": {"code": 201}}

    async def operation(self, default_real_mode):
        return await self._call("operation", default_real_mode=default_real_mode)

    async def backup(self, backup_reserve_percent):
        return await self._call("backup", backup_reserve_percent=backup_reserve_percent)

    async def grid_import_export(self, **kwargs):
        return await self._call("grid_import_export", **kwargs)

    async def storm_mode(self, enabled):
        return await self._call("storm_mode", enabled=enabled)

    async def off_grid_vehicle_charging_reserve(self, off_grid_vehicle_charging_reserve_percent):
        return await self._call("off_grid_vehicle_charging_reserve",
                                off_grid_vehicle_charging_reserve_percent=off_grid_vehicle_charging_reserve_percent)


class FakeInfoCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.last_update_success = True
        self.refreshes = 0

    async def async_request_refresh(self) -> None:
        self.refreshes += 1


def _site(data: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(api=FakeFleetApi(),
                           info_coordinator=FakeInfoCoordinator(copy.deepcopy(SITE_INFO if data is None else data)),
                           live_coordinator=FakeInfoCoordinator({"island_status": "on_grid",
                                                                 "storm_mode_active": False}))


def _fleet_entry(*sites, state=ConfigEntryState.LOADED, scopes=SCOPES) -> SimpleNamespace:
    return SimpleNamespace(state=state, disabled_by=None,
                           runtime_data=SimpleNamespace(energysites=list(sites), scopes=scopes))


def _other_site() -> SimpleNamespace:
    return _site({**SITE_INFO, "id": "1707000-11-J--TGOTHER",
                  "components_gateways": [{"din": "1707000-11-J--TGOTHER"}], "components_batteries": []})


@pytest.fixture
def fleet():
    """A loaded Tesla Fleet entry with this Powerwall's site (the core
    integration itself is not loaded in tests)."""
    site = _site()
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


def _issue(hass: HomeAssistant, prefix: str, entry: MockConfigEntry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{prefix}_{entry.entry_id}")


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


async def _check(hass: HomeAssistant, entry: MockConfigEntry, name: str, times: int = 1) -> None:
    """Make a backup's hourly check due and poll."""
    backend = entry.runtime_data.coordinator.backend(name)
    for _ in range(times):
        backend.check_at -= 3601
        backend.last_ok = None
        await _refresh(hass, entry)


async def _select(hass: HomeAssistant, key: str, option: str) -> None:
    await hass.services.async_call("select", "select_option", {
        "entity_id": _eid(hass, "select", key), "option": option}, blocking=True)


async def test_reads_use_fleet_data_and_writes_use_its_api(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, V1R)
    health = _health(hass)
    assert health.state == "ok"
    status = health.attributes["cloud_backends"]["tesla_fleet"]
    assert status["ok"] is True and status["can_change_settings"] is True
    assert health.attributes["cloud_token_expires"] is None
    # Checked from Tesla Fleet's own data: no Tesla call, no Owner API client.
    assert fleet.api.calls == []
    assert cloud(fake_powerwall) is None

    local(fake_powerwall).get_mode = lambda: None  # gateway can't say
    entry.runtime_data.coordinator.backends[0].cache = None
    fleet.info_coordinator.data["default_real_mode"] = "autonomous"
    await _refresh(hass, entry)
    state = hass.states.get(_eid(hass, "select", "operation_mode"))
    assert state.state == "autonomous"
    assert state.attributes["source"] == "mixed"
    assert state.attributes["cloud_backend"] == "tesla_fleet"
    assert fleet.api.calls == []

    # The gateway refuses writes: they go through Tesla Fleet, which then
    # refreshes its data.
    local(fake_powerwall).set_grid_export = lambda mode: None
    await _select(hass, "grid_export", "pv_only")
    assert ("grid_import_export", {"customer_preferred_export_rule": "pv_only"}) in fleet.api.calls
    local(fake_powerwall).set_grid_charging = lambda mode: None
    await hass.services.async_call("switch", "turn_on", {
        "entity_id": _eid(hass, "switch", "grid_charging")}, blocking=True)
    assert ("grid_import_export", {"disallow_charge_from_grid_with_solar_installed": False}) in fleet.api.calls
    local(fake_powerwall).set_reserve = lambda level: None
    await hass.services.async_call("number", "set_value", {
        "entity_id": _eid(hass, "number", "backup_reserve"), "value": 30}, blocking=True)
    assert ("backup", {"backup_reserve_percent": 30}) in fleet.api.calls
    assert fleet.info_coordinator.refreshes == 3


async def test_mode_and_errors_fall_back_to_fleet(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, V1R)
    # The gateway refuses the mode: it goes through Tesla Fleet.
    local(fake_powerwall).set_mode = lambda mode: None
    await _select(hass, "operation_mode", "backup")
    assert ("operation", {"default_real_mode": "backup"}) in fleet.api.calls

    # A local write that raises goes there too.
    def boom(level):
        raise ConnectionError("gateway down")

    local(fake_powerwall).set_reserve = boom
    await hass.services.async_call("number", "set_value", {
        "entity_id": _eid(hass, "number", "backup_reserve"), "value": 95}, blocking=True)
    assert ("backup", {"backup_reserve_percent": 95}) in fleet.api.calls

    # So does any write while the gateway is not connected.
    entry.runtime_data.coordinator._pw = None
    await _select(hass, "grid_export", "never")
    assert ("grid_import_export", {"customer_preferred_export_rule": "never"}) in fleet.api.calls
    assert fleet.info_coordinator.refreshes == 3


async def test_export_rule_ignores_non_export_configured(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    # Real sites keep non_export_configured while exporting with battery_ok.
    fleet.info_coordinator.data["components_customer_preferred_export_rule"] = "battery_ok"
    entry = await _setup(hass, V1R)
    client = entry.runtime_data.coordinator.backends[0].get_client()
    assert client.get_grid_export() == "battery_ok"
    del fleet.info_coordinator.data["components_customer_preferred_export_rule"]
    assert client.get_grid_export() == "battery_ok"  # not set: Tesla's default


async def test_owner_api_export_rule_ignores_non_export_configured(hass: HomeAssistant, fake_powerwall) -> None:
    fake_powerwall.non_export_configured = True
    entry = await _setup(hass, {**V1R, **ACCOUNT}, None)
    owner = entry.runtime_data.coordinator.backends[0].get_client()
    owner.set_grid_export("battery_ok")
    assert owner.get_grid_export() == "battery_ok"  # pypowerwall alone says "never"


async def test_fleet_data_not_updating(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    fleet.info_coordinator.last_update_success = False
    entry = await _setup(hass, V1R)
    health = _health(hass)
    assert health.state == "degraded" and "cloud_backup" in health.attributes["problems"]
    await _check(hass, entry, "tesla_fleet", 3)
    issue = _issue(hass, "fleet_backup", entry)
    assert issue is not None and not issue.is_fixable and issue.translation_key == "fleet_backup"
    assert _issue(hass, "cloud_login", entry) is None

    fleet.info_coordinator.last_update_success = True
    await _check(hass, entry, "tesla_fleet")
    assert _health(hass).state == "ok"
    assert _issue(hass, "fleet_backup", entry) is None


async def test_waits_while_fleet_loads(hass: HomeAssistant, fake_powerwall) -> None:
    fleet_entry = _fleet_entry(_site(), state=ConfigEntryState.SETUP_IN_PROGRESS)
    with patch(FLEET_ENTRIES, return_value=[fleet_entry]):
        entry = await _setup(hass, V1R)
        backend = entry.runtime_data.coordinator.backends[0]
        assert backend.failures == 0 and backend.check_at is None
        health = _health(hass)
        assert health.attributes["cloud_backends"]["tesla_fleet"]["starting"] is True
        assert health.state == "ok"  # not degraded while it loads
        fleet_entry.state = ConfigEntryState.LOADED
        await _refresh(hass, entry)
        assert backend.ok
        assert _health(hass).attributes["cloud_backends"]["tesla_fleet"]["starting"] is False


async def test_waiting_for_fleet_is_limited(hass: HomeAssistant, fake_powerwall) -> None:
    # Retrying for longer than the startup window counts as not working.
    with (patch(FLEET_ENTRIES, return_value=[_fleet_entry(_site(), state=ConfigEntryState.SETUP_RETRY)]),
          patch.object(fleet_mod, "STARTING_SECONDS", -1)):
        await _setup(hass, V1R)
        health = _health(hass)
    assert health.attributes["cloud_backends"]["tesla_fleet"]["starting"] is False
    assert health.state == "degraded"


async def test_disabled_and_other_domains(hass: HomeAssistant) -> None:
    MockConfigEntry(domain="tesla_fleet", disabled_by=ConfigEntryDisabler.USER).add_to_hass(hass)
    teslemetry = MockConfigEntry(domain="teslemetry")
    teslemetry.add_to_hass(hass)
    MockConfigEntry(domain="tesla_custom").add_to_hass(hass)
    assert fleet_mod.fleet_entries(hass) == [teslemetry]


async def test_disabled_fleet_is_not_waited_for(hass: HomeAssistant, fake_powerwall) -> None:
    MockConfigEntry(domain="tesla_fleet", disabled_by=ConfigEntryDisabler.USER).add_to_hass(hass)
    await _setup(hass, V1R)
    health = _health(hass)
    assert health.attributes["cloud_backends"]["tesla_fleet"]["starting"] is False
    assert health.state == "degraded"


async def test_without_fleet(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    assert _health(hass).state == "degraded"


async def test_site_matching(hass: HomeAssistant) -> None:
    other = _other_site()
    ours = _site({**SITE_INFO, "id": "", "components_gateways": [], "components_batteries": [{"din": DIN}]})
    bare = _site({"default_real_mode": "backup"})
    assert fleet_mod._site_dins(other.info_coordinator.data) == ["1707000-11-J--TGOTHER", "1707000-11-J--TGOTHER"]
    with patch(FLEET_ENTRIES, return_value=[_fleet_entry(other), _fleet_entry(ours)]):
        assert fleet_mod.find_site(hass, DIN)[1] is ours  # named as a battery only
    # A site naming no serial is used only when it is the only one.
    with patch(FLEET_ENTRIES, return_value=[_fleet_entry(bare)]):
        assert fleet_mod.find_site(hass, DIN)[1] is bare
    with patch(FLEET_ENTRIES, return_value=[_fleet_entry(bare, other)]):
        assert fleet_mod.find_site(hass, DIN) is None
    with patch(FLEET_ENTRIES, return_value=[_fleet_entry(other)]):
        assert fleet_mod.find_site(hass, DIN) is None


async def test_site_with_another_powerwall_is_not_used(hass: HomeAssistant, fake_powerwall) -> None:
    with patch(FLEET_ENTRIES, return_value=[_fleet_entry(_other_site())]):
        await _setup(hass, V1R)
        assert _health(hass).state == "degraded"


async def test_read_only_sign_in(hass: HomeAssistant, fake_powerwall) -> None:
    site = _site()
    with patch(FLEET_ENTRIES, return_value=[_fleet_entry(site, scopes=["energy_device_data"])]):
        entry = await _setup(hass, {**V1R, **ACCOUNT}, "fleet_then_owner")
        health = _health(hass)
        assert health.attributes["cloud_backends"]["tesla_fleet"]["can_change_settings"] is False
        assert health.state == "ok"  # the Owner API can still change settings
        issue = _issue(hass, "fleet_backup", entry)
        assert issue is not None and issue.translation_key == "fleet_read_only"
        # A change goes to the Owner API without a Tesla Fleet command.
        local(fake_powerwall).set_mode = lambda mode: None
        await _select(hass, "operation_mode", "backup")
    assert site.api.calls == []
    assert ("mode", "backup") in cloud(fake_powerwall).writes


async def test_read_only_sign_in_alone_degrades(hass: HomeAssistant, fake_powerwall) -> None:
    with patch(FLEET_ENTRIES, return_value=[_fleet_entry(_site(), scopes=["energy_device_data"])]):
        await _setup(hass, V1R)
        assert _health(hass).state == "degraded"


async def test_timeout_is_not_sent_elsewhere(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    await _setup(hass, {**V1R, **ACCOUNT}, "fleet_then_owner")
    fleet.api.hang = True
    local(fake_powerwall).set_mode = lambda mode: None
    with patch.object(fleet_mod, "CALL_TIMEOUT", 0.2), pytest.raises(HomeAssistantError) as err:
        await _select(hass, "operation_mode", "backup")
    assert err.value.translation_key == "write_cloud_timeout"
    assert err.value.translation_placeholders == {"backend": "Tesla Fleet"}
    assert ("operation", {"default_real_mode": "backup"}) in fleet.api.calls
    assert ("mode", "backup") not in cloud(fake_powerwall).writes


async def test_fleet_first_falls_back_to_owner(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    fleet.info_coordinator.last_update_success = False
    fleet.api.fail = True
    entry = await _setup(hass, {**V1R, **ACCOUNT}, "fleet_then_owner")
    coordinator = entry.runtime_data.coordinator
    assert [b.name for b in coordinator.backends] == ["tesla_fleet", "owner_api"]
    # One backup still works: not degraded, but the failing one is reported.
    health = _health(hass)
    assert health.state == "ok"
    assert health.attributes["cloud_backends"]["tesla_fleet"]["ok"] is False
    assert health.attributes["cloud_backends"]["owner_api"]["ok"] is True
    await _check(hass, entry, "tesla_fleet", 3)
    assert _issue(hass, "fleet_backup", entry) is not None
    assert _issue(hass, "cloud_login", entry) is None

    local(fake_powerwall).get_mode = lambda: None
    coordinator.backend("owner_api").cache = None
    await _refresh(hass, entry)
    assert hass.states.get(_eid(hass, "select", "operation_mode")).attributes["cloud_backend"] == "owner_api"

    # A write Tesla Fleet refuses goes to the Owner API.
    local(fake_powerwall).set_grid_export = lambda mode: None
    await _select(hass, "grid_export", "pv_only")
    assert ("grid_import_export", {"customer_preferred_export_rule": "pv_only"}) in fleet.api.calls
    assert ("grid_export", "pv_only") in cloud(fake_powerwall).writes


async def test_owner_first_writes_once(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    await _setup(hass, {**V1R, **ACCOUNT}, "owner_then_fleet")
    local(fake_powerwall).set_mode = lambda mode: None
    await _select(hass, "operation_mode", "backup")
    assert ("mode", "backup") in cloud(fake_powerwall).writes
    assert fleet.api.calls == []


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


async def _cloud_step(hass: HomeAssistant, entry: MockConfigEntry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "cloud_backup" in result["menu_options"]
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "cloud_backup"})


async def test_options_cloud_backup_choices(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT}, None)
    result = await _cloud_step(hass, entry)
    assert result["type"] is FlowResultType.FORM
    selector = result["data_schema"].schema["cloud_backends"]
    assert selector.config["options"] == ["none", "owner_api", "tesla_fleet", "fleet_then_owner", "owner_then_fleet"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"cloud_backends": "fleet_then_owner"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["cloud_backends"] == "fleet_then_owner"
    assert [b.name for b in entry.runtime_data.coordinator.backends] == ["tesla_fleet", "owner_api"]


async def test_options_offer_fleet_only_with_this_powerwall(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, None)
    for entries in ([], [_fleet_entry(_other_site())], [_fleet_entry(_site(), state=ConfigEntryState.SETUP_ERROR)]):
        with patch(FLEET_ENTRIES, return_value=entries):
            result = await _cloud_step(hass, entry)
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


async def test_linking_an_account_loads_the_owner_backup(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, V1R)
    assert [b.name for b in entry.runtime_data.coordinator.backends] == ["tesla_fleet"]
    login = tesla_cloud.LoginStart("https://auth.tesla.com/x", "verifier", "s")
    cloud_mod = "custom_components.tesla_tedapi.config_flow.tesla_cloud"
    with (
        patch(f"{cloud_mod}.start_login", return_value=login),
        patch(f"{cloud_mod}.refresh_login", return_value={"access_token": "at", "refresh_token": "rt"}),
        patch(f"{cloud_mod}.token_email", return_value="owner@example.com"),
        patch(f"{cloud_mod}.energy_sites", return_value=[tesla_cloud.EnergySite(42, DIN, "Home")]),
        patch(f"{cloud_mod}.save_token"),
        patch(f"{cloud_mod}.save_site"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "link_account"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"callback": "eyJ" + "a" * 600})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["cloud_backends"] == "fleet_then_owner"
    # The reload after linking sets up both backups, without another reload.
    assert [b.name for b in entry.runtime_data.coordinator.backends] == ["tesla_fleet", "owner_api"]


async def _unlink(hass: HomeAssistant, entry: MockConfigEntry, backup: str | None) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    menu = result["menu_options"]
    if backup is None:
        return result
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "unlink_account"})
    offered = result["data_schema"].schema["backup"].config["options"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"backup": backup})
    await hass.async_block_till_done()
    return {"menu": menu, "offered": offered, **result}


async def test_unlink_tesla_fleet_keeps_the_account(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT}, "fleet_then_owner")
    result = await _unlink(hass, entry, "tesla_fleet")
    assert "unlink_account" in result["menu"] and "link_account" not in result["menu"]
    assert result["offered"] == ["tesla_fleet", "owner_api"]
    assert entry.options["cloud_backends"] == "owner_api"
    assert entry.data["owners_email"] == ACCOUNT["owners_email"]
    assert [b.name for b in entry.runtime_data.coordinator.backends] == ["owner_api"]


async def test_unlink_owner_api_keeps_tesla_fleet(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT}, "owner_then_fleet")
    result = await _unlink(hass, entry, "owner_api")
    assert result["offered"] == ["owner_api", "tesla_fleet"]
    assert entry.options["cloud_backends"] == "tesla_fleet"
    assert "owners_email" not in entry.data
    assert [b.name for b in entry.runtime_data.coordinator.backends] == ["tesla_fleet"]


async def test_menu_offers_link_and_unlink_with_only_tesla_fleet(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, V1R)
    result = await _unlink(hass, entry, None)
    assert result["menu_options"] == ["settings", "cloud_backup", "link_account", "unlink_account"]
    result = await _unlink(hass, entry, "tesla_fleet")
    assert result["offered"] == ["tesla_fleet"]
    assert entry.options["cloud_backends"] == "none"
    result = await _unlink(hass, entry, None)
    assert result["menu_options"] == ["settings", "cloud_backup", "link_account"]
