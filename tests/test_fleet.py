"""Cloud backups: the Fleet API through a sign-in made here (Tesla Fleet or
Teslemetry), alone or with the Owner API in either order; the Cloud backup
options step and the sign-in repair."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import fleet as fleet_mod, tesla_cloud
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, SCOPES, TESLEMETRY, cloud, local

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
ACCOUNT = {"owners_email": "a@b.c", "owners_authpath": "/tmp", "site_id": 42}
async def _setup(hass: HomeAssistant, data: dict, choice: str | None = "tesla_fleet",
                 options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data, minor_version=2,
                            options={**({"cloud_backends": choice} if choice else {}), **(options or {})},
                            title="Powerwall")
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


async def test_reads_and_writes_go_through_the_sign_in_here(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **TESLEMETRY})
    health = _health(hass)
    assert health.state == "ok"
    status = health.attributes["cloud_backends"]["tesla_fleet"]
    assert status["ok"] is True and status["can_change_settings"] is True
    assert health.attributes["cloud_token_expires"] is None
    assert cloud(fake_powerwall) is None
    assert "site_info" in fleet.reads and "metadata" in fleet.reads

    local(fake_powerwall).get_mode = lambda: None  # gateway can't say
    backend = entry.runtime_data.coordinator.backends[0]
    assert backend.label == "Teslemetry"
    backend.cache = None
    backend.get_client()._info = None
    fleet.info["default_real_mode"] = "autonomous"
    await _refresh(hass, entry)
    state = hass.states.get(_eid(hass, "select", "operation_mode"))
    assert state.state == "autonomous"
    assert state.attributes["source"] == "mixed"
    assert state.attributes["cloud_backend"] == "tesla_fleet"

    # The gateway refuses writes: they go through the Fleet API.
    local(fake_powerwall).set_grid_export = lambda mode: None
    await _select(hass, "grid_export", "pv_only")
    assert ("grid_import_export", {"customer_preferred_export_rule": "pv_only"}) in fleet.posts
    local(fake_powerwall).set_grid_charging = lambda mode: None
    await hass.services.async_call("switch", "turn_on", {
        "entity_id": _eid(hass, "switch", "grid_charging")}, blocking=True)
    assert ("grid_import_export", {"disallow_charge_from_grid_with_solar_installed": False}) in fleet.posts
    local(fake_powerwall).set_reserve = lambda level: None
    await hass.services.async_call("number", "set_value", {
        "entity_id": _eid(hass, "number", "backup_reserve"), "value": 30}, blocking=True)
    assert ("backup", {"backup_reserve_percent": 30}) in fleet.posts
    # A write drops the cached site data, so the change is read next time.
    assert backend.get_client()._info is None


async def test_mode_and_errors_fall_back_to_fleet(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **TESLEMETRY})
    local(fake_powerwall).set_mode = lambda mode: None
    await _select(hass, "operation_mode", "backup")
    assert ("operation", {"default_real_mode": "backup"}) in fleet.posts

    def boom(level):
        raise ConnectionError("gateway down")

    local(fake_powerwall).set_reserve = boom
    await hass.services.async_call("number", "set_value", {
        "entity_id": _eid(hass, "number", "backup_reserve"), "value": 95}, blocking=True)
    assert ("backup", {"backup_reserve_percent": 95}) in fleet.posts

    entry.runtime_data.coordinator._pw = None
    await _select(hass, "grid_export", "never")
    assert ("grid_import_export", {"customer_preferred_export_rule": "never"}) in fleet.posts


async def test_site_data_is_read_once_for_all_settings(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **TESLEMETRY})
    client = entry.runtime_data.coordinator.backends[0].get_client()
    client._info = None
    fleet.calls.clear()
    values = await hass.async_add_executor_job(
        lambda: (client.get_mode(), client.get_reserve(), client.get_grid_charging(), client.get_grid_export()))
    assert values == ("self_consumption", 5.0, False, "never")
    assert fleet.reads == ["site_info"]


async def test_export_rule_ignores_non_export_configured(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    # Real sites keep non_export_configured while exporting with battery_ok.
    fleet.info["components"]["customer_preferred_export_rule"] = "battery_ok"
    entry = await _setup(hass, {**V1R, **TESLEMETRY})
    client = entry.runtime_data.coordinator.backends[0].get_client()
    client._info = None
    assert await hass.async_add_executor_job(client.get_grid_export) == "battery_ok"
    del fleet.info["components"]["customer_preferred_export_rule"]
    client._info = None
    assert await hass.async_add_executor_job(client.get_grid_export) == "battery_ok"  # Tesla's default
    # A site_info without settings: nothing to report.
    fleet.info = {}
    client._info = None
    assert await hass.async_add_executor_job(client.get_grid_export) is None
    assert await hass.async_add_executor_job(client.get_grid_charging) is None
    assert await hass.async_add_executor_job(client.get_reserve) is None
    assert await hass.async_add_executor_job(client.get_extras) == {}


async def test_owner_api_export_rule_ignores_non_export_configured(hass: HomeAssistant, fake_powerwall) -> None:
    fake_powerwall.non_export_configured = True
    entry = await _setup(hass, {**V1R, **ACCOUNT}, None)
    owner = entry.runtime_data.coordinator.backends[0].get_client()
    owner.set_grid_export("battery_ok")
    assert owner.get_grid_export() == "battery_ok"  # pypowerwall alone says "never"


async def test_failing_sign_in_gets_a_fixable_repair(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    fleet.fail = fleet_mod.FleetAuthError("token not accepted", 401)
    entry = await _setup(hass, {**V1R, **TESLEMETRY})
    health = _health(hass)
    assert health.state == "degraded" and "cloud_backup" in health.attributes["problems"]
    await _check(hass, entry, "tesla_fleet", 3)
    issue = _issue(hass, "fleet_backup", entry)
    assert issue is not None and issue.is_fixable and issue.translation_key == "fleet_backup"
    assert _issue(hass, "cloud_login", entry) is None

    fleet.fail = None
    await _check(hass, entry, "tesla_fleet")
    assert _health(hass).state == "ok"
    assert _issue(hass, "fleet_backup", entry) is None


async def test_choice_without_a_sign_in_here_asks_for_one(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    """Set up through the Tesla Fleet integration before 2.6: the choice is
    kept, but nothing is used until there is a sign-in here."""
    entry = await _setup(hass, {**V1R, **ACCOUNT}, "fleet_then_owner")
    assert [b.name for b in entry.runtime_data.coordinator.backends] == ["owner_api"]
    issue = _issue(hass, "fleet_signin", entry)
    assert issue is not None and issue.is_fixable and issue.translation_key == "fleet_signin"
    assert fleet.calls == []
    # The repair starts the sign-in as a reauthentication.
    from custom_components.tesla_tedapi.repairs import async_create_fix_flow  # noqa: PLC0415

    flow = await async_create_fix_flow(hass, issue.issue_id, issue.data)
    flow.hass, flow.handler, flow.flow_id = hass, DOMAIN, "flow1"
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    result = await flow.async_step_init({})
    # An abort, not a finished fix: the repair stays until the sign-in works.
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "fleet_signin_started"
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["step_id"] for f in flows] == ["fleet_signin"]
    # The card's title has the gateway address ("Powerwall ({host})").
    assert flows[0]["context"]["title_placeholders"] == {"name": "Powerwall", "host": V1R["host"]}
    assert _issue(hass, "fleet_signin", entry) is not None
    # Unloading clears it.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, "fleet_signin", entry) is None


async def test_repair_for_a_removed_entry(hass: HomeAssistant) -> None:
    from custom_components.tesla_tedapi.repairs import FleetSigninRepairFlow, async_create_fix_flow  # noqa: PLC0415

    flow = await async_create_fix_flow(hass, "fleet_signin_x", {"entry_id": "gone"})
    assert isinstance(flow, FleetSigninRepairFlow)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "entry_gone"


async def test_read_only_sign_in(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    fleet.scopes = ["energy_device_data"]
    entry = await _setup(hass, {**V1R, **ACCOUNT, **TESLEMETRY}, "fleet_then_owner")
    health = _health(hass)
    assert health.attributes["cloud_backends"]["tesla_fleet"]["can_change_settings"] is False
    assert health.state == "ok"  # the Owner API can still change settings
    issue = _issue(hass, "fleet_backup", entry)
    assert issue is not None and issue.translation_key == "fleet_read_only" and issue.is_fixable
    # A change goes to the Owner API without a Fleet API command.
    local(fake_powerwall).set_mode = lambda mode: None
    await _select(hass, "operation_mode", "backup")
    assert fleet.posts == []
    assert ("mode", "backup") in cloud(fake_powerwall).writes
    client = entry.runtime_data.coordinator.backend("tesla_fleet").get_client()
    with pytest.raises(fleet_mod.FleetUnavailable):
        client.set_mode("backup")


async def test_read_only_sign_in_alone_degrades(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    fleet.scopes = ["energy_device_data"]
    await _setup(hass, {**V1R, **TESLEMETRY})
    assert _health(hass).state == "degraded"


async def test_scopes_read_again_after_a_failure(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **TESLEMETRY}, None)
    client = fleet_mod.FleetBackup(hass, fleet_mod.entry_api(hass, entry), 42)
    calls = 0
    original = fleet.request

    async def flaky(api, method, path, body=None):
        nonlocal calls
        if path == "api/metadata":
            calls += 1
            if calls == 1:
                raise fleet_mod.FleetError("busy", 503)
        return await original(api, method, path, body)

    with patch.object(fleet, "request", flaky):
        await hass.async_add_executor_job(client.get_mode)
        assert client.can_write is True and client._scopes is None
        client._info = None
        await hass.async_add_executor_job(client.get_mode)
    assert calls == 2 and client._scopes == set(SCOPES)


async def test_timeout_is_not_sent_elsewhere(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    await _setup(hass, {**V1R, **ACCOUNT, **TESLEMETRY}, "fleet_then_owner")
    fleet.hang = True
    local(fake_powerwall).set_mode = lambda mode: None
    with patch.object(fleet_mod, "RUN_TIMEOUT", 0.2), pytest.raises(HomeAssistantError) as err:
        await _select(hass, "operation_mode", "backup")
    assert err.value.translation_key == "write_cloud_timeout"
    assert err.value.translation_placeholders == {"backend": "Teslemetry"}
    assert ("operation", {"default_real_mode": "backup"}) in fleet.posts
    assert ("mode", "backup") not in cloud(fake_powerwall).writes


async def test_fleet_first_falls_back_to_owner(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    fleet.fail = fleet_mod.FleetError("HTTP 500", 500)
    entry = await _setup(hass, {**V1R, **ACCOUNT, **TESLEMETRY}, "fleet_then_owner")
    coordinator = entry.runtime_data.coordinator
    assert [b.name for b in coordinator.backends] == ["tesla_fleet", "owner_api"]
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

    # A write the Fleet API refuses goes to the Owner API.
    local(fake_powerwall).set_grid_export = lambda mode: None
    await _select(hass, "grid_export", "pv_only")
    assert ("grid_import_export", {"customer_preferred_export_rule": "pv_only"}) in fleet.posts
    assert ("grid_export", "pv_only") in cloud(fake_powerwall).writes


async def test_owner_first_writes_once(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    await _setup(hass, {**V1R, **ACCOUNT, **TESLEMETRY}, "owner_then_fleet")
    local(fake_powerwall).set_mode = lambda mode: None
    await _select(hass, "operation_mode", "backup")
    assert ("mode", "backup") in cloud(fake_powerwall).writes
    assert fleet.posts == []


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
    entry = await _setup(hass, {**V1R, **ACCOUNT, **TESLEMETRY}, None)
    result = await _cloud_step(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["fleet"] == "Teslemetry"
    selector = result["data_schema"].schema["cloud_backends"]
    assert selector.config["options"] == ["none", "owner_api", "tesla_fleet", "fleet_then_owner", "owner_then_fleet"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"cloud_backends": "fleet_then_owner"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["cloud_backends"] == "fleet_then_owner"
    assert [b.name for b in entry.runtime_data.coordinator.backends] == ["tesla_fleet", "owner_api"]


async def test_options_offer_fleet_only_with_a_sign_in_here(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, None)
    result = await _cloud_step(hass, entry)
    assert result["data_schema"].schema["cloud_backends"].config["options"] == ["none"]
    assert result["description_placeholders"]["fleet"] == "not signed in"


async def test_options_keep_showing_a_choice_without_its_sign_in(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, "tesla_fleet")
    result = await _cloud_step(hass, entry)
    assert result["data_schema"].schema["cloud_backends"].config["options"] == ["none", "tesla_fleet"]


async def test_options_start_the_fleet_sign_in(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, None)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "fleet_signin" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "fleet_signin"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "fleet_signin_started"
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["step_id"] for f in flows] == ["fleet_signin"]


async def test_unlink_keeps_fleet(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT, **TESLEMETRY}, "owner_then_fleet")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "unlink_account"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["cloud_backends"] == "tesla_fleet"
    assert "owners_email" not in entry.data
    assert entry.data["fleet_token"] == "tm-token"


async def test_linking_an_account_loads_the_owner_backup(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **TESLEMETRY})
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


async def test_unlink_fleet_removes_its_sign_in_and_keeps_the_account(
        hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT, **TESLEMETRY}, "fleet_then_owner")
    result = await _unlink(hass, entry, "tesla_fleet")
    assert "unlink_account" in result["menu"] and "link_account" not in result["menu"]
    assert result["offered"] == ["tesla_fleet", "owner_api"]
    assert entry.options["cloud_backends"] == "owner_api"
    assert entry.data["owners_email"] == ACCOUNT["owners_email"]
    assert not any(key in entry.data for key in TESLEMETRY)
    assert [b.name for b in entry.runtime_data.coordinator.backends] == ["owner_api"]


async def test_unlink_owner_api_keeps_fleet(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT, **TESLEMETRY}, "owner_then_fleet")
    result = await _unlink(hass, entry, "owner_api")
    assert result["offered"] == ["owner_api", "tesla_fleet"]
    assert entry.options["cloud_backends"] == "tesla_fleet"
    assert "owners_email" not in entry.data
    assert [b.name for b in entry.runtime_data.coordinator.backends] == ["tesla_fleet"]


async def test_menu_offers_link_and_unlink_with_only_fleet(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **TESLEMETRY})
    result = await _unlink(hass, entry, None)
    assert result["menu_options"] == ["settings", "cloud_backup", "link_account", "fleet_signin",
                                      "unlink_account", "new_key"]
    result = await _unlink(hass, entry, "tesla_fleet")
    assert result["offered"] == ["tesla_fleet"]
    assert entry.options["cloud_backends"] == "none"
    result = await _unlink(hass, entry, None)
    assert result["menu_options"] == ["settings", "cloud_backup", "link_account", "fleet_signin", "new_key"]


async def test_token_refresh_does_not_reload(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    entry = await _setup(hass, {**V1R, **TESLEMETRY, "token": {"access_token": "a"}})
    coordinator = entry.runtime_data.coordinator
    hass.config_entries.async_update_entry(entry, data={**entry.data, "token": {"access_token": "b"}})
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator is coordinator
    assert entry.runtime_data.applied_data["token"] == {"access_token": "b"}
    # So does the account's region server, once found.
    hass.config_entries.async_update_entry(entry, data={**entry.data, "fleet_server": "https://eu.example"})
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator is coordinator
    # Any other data change still reloads.
    hass.config_entries.async_update_entry(entry, data={**entry.data, "fleet_token": "new"})
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator is not coordinator


async def test_tesla_tariff_through_fleet(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    """The Fleet API: the tariff from site_info, written with time_of_use_settings."""
    from .test_tariff import V2  # noqa: PLC0415

    fleet.tariff = V2
    entry = await _setup(hass, {**V1R, **TESLEMETRY}, options={"tesla_tariff": True})
    sensor = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DIN}_tesla_tariff")
    assert hass.states.get(sensor).state == "ZeroHero"
    assert hass.states.get(sensor).attributes["backend"] == "Teslemetry"

    new = {**V2, "name": "ZeroHero 2"}
    resp = await hass.services.async_call(DOMAIN, "set_tariff", {"tou_settings": {"tariff_content_v2": new},
                                                                 "dry_run": False},
                                          blocking=True, return_response=True)
    assert resp == {"dry_run": False, "backend": "Teslemetry", "confirmed": True, "status": "confirmed",
                    "warnings": []}
    # Tesla gets the tariff wrapped once, not tariff_content_v2.tariff_content_v2.
    assert ("time_of_use_settings", {"tou_settings": {"tariff_content_v2": new}}) in fleet.posts
    with pytest.raises(ValueError, match="non-empty"):
        await hass.async_add_executor_job(
            entry.runtime_data.coordinator.backends[0].get_client().set_tariff, {"tariff_content_v2": {}})

    # The Fleet API shows a new tariff at once: other rates are a mismatch and a repair.
    original = fleet.request

    async def keeps_old(api, method, path, body=None):
        result = await original(api, method, path, body)
        if path.endswith("/time_of_use_settings"):
            fleet.tariff = V2
        return result

    issue_id = f"tariff_write_{entry.entry_id}"
    with patch.object(fleet, "request", keeps_old):
        resp = await hass.services.async_call(DOMAIN, "set_tariff", {
            "tou_settings": {"tariff_content_v2": {**V2, "energy_charges": {"Year": {"rates": {"ON_PEAK": 0.7}}}}},
            "dry_run": False}, blocking=True, return_response=True)
    assert resp["status"] == "mismatch"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    fleet.tariff = {**V2, "energy_charges": {"Year": {"rates": {"ON_PEAK": 0.7}}}}
    entry.runtime_data.coordinator._tariff_at = None
    await entry.runtime_data.coordinator.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

    # A site_info without a tariff, and a read that hangs.
    fleet.tariff = None
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "get_tariff", {}, blocking=True, return_response=True)
    client = entry.runtime_data.coordinator.backends[0].get_client()
    fleet.hang = True
    with patch.object(fleet_mod, "RUN_TIMEOUT", 0.05), pytest.raises(fleet_mod.FleetTimeout):
        await hass.async_add_executor_job(client.get_tariff)


async def test_flows_leave_the_reload_to_the_update_listener(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    """Home Assistant (2026.12+) wants an entry with an update listener to be
    reloaded by that listener, not by the flow. Each flow still reloads once:
    through the listener, or scheduled when the listener wouldn't."""
    from custom_components.tesla_tedapi import config_flow  # noqa: PLC0415

    entry = await _setup(hass, {**V1R, **TESLEMETRY})
    reload, schedule = hass.config_entries.async_reload, hass.config_entries.async_schedule_reload
    reloads: list[str] = []
    scheduled: list[str] = []

    async def counted_reload(entry_id):
        reloads.append(entry_id)
        return await reload(entry_id)

    def counted_schedule(entry_id):
        scheduled.append(entry_id)
        schedule(entry_id)

    async def reauth(*steps):
        entry.async_start_reauth(hass, data={**entry.data, **steps[0]})
        await hass.async_block_till_done()
        flow_id = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]["flow_id"]
        result = None
        for step in steps[1:]:
            result = await hass.config_entries.flow.async_configure(flow_id, step)
        await hass.async_block_till_done()
        return result

    with (patch.object(hass.config_entries, "async_reload", counted_reload),
          patch.object(hass.config_entries, "async_schedule_reload", counted_schedule),
          patch.object(config_flow, "_check_gateway", return_value=DIN)):
        # A new gateway password: the listener reloads.
        result = await reauth({}, {"next_step_id": "reauth_password"}, {"gw_pwd": "NEW12345"})
        assert result["reason"] == "reauth_successful" and entry.data["gw_pwd"] == "NEW12345"
        assert (len(reloads), scheduled) == (1, [])
        # The same Teslemetry sign-in again: nothing changes, so the flow
        # schedules the reload itself.
        reloads.clear()
        result = await reauth({config_flow.FLEET_SIGNIN: True}, {"next_step_id": "teslemetry"},
                              {"access_token": "tm-token"})
        assert result["reason"] == "reauth_successful"
        assert scheduled == [entry.entry_id] and len(reloads) == 1
