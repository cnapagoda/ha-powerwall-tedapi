"""2.9.0: local only. After the key is registered, the Tesla sign-in can be
forgotten (in setup, or later in the options); the entities that need the
cloud are then removed, and nothing the integration does reaches Tesla."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import config_flow, fleet as fleet_mod, tesla_cloud
from custom_components.tesla_tedapi.const import AUTH_FILE, DOMAIN, SITE_FILE, STORAGE_FOLDER
from custom_components.tesla_tedapi.diagnostics import async_get_config_entry_diagnostics

from .conftest import DIN, TESLEMETRY, cloud, local
from .test_config_flow import CLOUD, SETUP, _gateway, _keep_signin
from .test_fleet_register import KeyServer, _key, _teslemetry, _to_signin

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
WIFI = {"connection_mode": "wifi", "host": "192.168.91.1", "gw_pwd": "ABCDE12345", "scan_interval": 30}
TOKEN = {"access_token": "at", "refresh_token": "rt"}
OWNER = "owner@example.com"


def _owner_patches(tmp_path: Path) -> list:
    """An Owner API sign-in and key registration. The token is saved for
    real, in the integration's folder."""
    login = tesla_cloud.LoginStart("https://auth.tesla.com/x", "verifier", "state1")
    (tmp_path / "k.pem").write_text("x")
    return [
        patch.object(config_flow, "VERIFY_POLL_SECONDS", 0),
        patch(f"{CLOUD}.start_login", return_value=login),
        patch(f"{CLOUD}.exchange_code", return_value=TOKEN),
        patch(f"{CLOUD}.token_email", return_value=OWNER),
        patch(f"{CLOUD}.energy_sites", return_value=[tesla_cloud.EnergySite(42, DIN, "Home")]),
        patch(f"{CLOUD}.ensure_rsa_key", return_value=(str(tmp_path / "k.pem"), b"pub")),
        patch(f"{CLOUD}.register_key", return_value=tesla_cloud.KEY_VERIFIED),
        patch(SETUP, return_value=True),
    ]


async def _register_with_owner_api(hass: HomeAssistant, tmp_path: Path, choice: str) -> dict:
    patches = _owner_patches(tmp_path)
    for p in patches:
        p.start()
    try:
        result = await _gateway(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "login"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": "tesla://auth/callback?code=abc&state=state1"})
        return await _keep_signin(hass, result, choice)
    finally:
        for p in reversed(patches):
            p.stop()


def _folder(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(STORAGE_FOLDER))


# ---------------------------------------------------------------------------
# Setup: keep the sign-in, or local only
# ---------------------------------------------------------------------------


async def test_setup_keeps_the_sign_in(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    result = await _register_with_owner_api(hass, tmp_path, "keep_cloud")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["owners_email"] == OWNER
    assert OWNER in json.loads((_folder(hass) / AUTH_FILE).read_text())


async def test_setup_local_only_forgets_the_owner_api_sign_in(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    result = await _register_with_owner_api(hass, tmp_path, "local_only")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["connection_mode"] == "v1r" and data["rsa_key_path"].endswith("k.pem")
    assert not {"owners_email", "owners_authpath", "site_id"} & data.keys()
    assert result["options"] == {}
    assert not fleet_mod.has_signin(data)
    # The token saved at the sign-in is gone, and the site pin with it.
    assert not (_folder(hass) / AUTH_FILE).exists()
    assert not (_folder(hass) / SITE_FILE).exists()


async def test_setup_local_only_keeps_an_account_another_powerwall_uses(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    MockConfigEntry(domain=DOMAIN, unique_id="1707000-11-J--TG0000000000OTHER", data={
        **V1R, "owners_email": OWNER, "owners_authpath": str(_folder(hass)), "site_id": 7}).add_to_hass(hass)
    result = await _register_with_owner_api(hass, tmp_path, "local_only")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "owners_email" not in result["data"]
    assert OWNER in json.loads((_folder(hass) / AUTH_FILE).read_text())
    assert (_folder(hass) / SITE_FILE).exists()


async def test_setup_local_only_forgets_a_fleet_api_sign_in(hass: HomeAssistant, tmp_path: Path, fleet) -> None:
    with KeyServer(fleet, [3]).patch, _key(tmp_path), patch(SETUP, return_value=True):
        result = await _teslemetry(hass, await _to_signin(hass))
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        result = await _keep_signin(hass, result, "local_only")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not set(TESLEMETRY) & result["data"].keys()
    # Not the Fleet API backup a kept sign-in gets.
    assert result["options"] == {}


async def test_key_upload_and_wifi_setups_arent_asked(hass: HomeAssistant, tmp_path: Path) -> None:
    """No sign-in was made only to register a key: nothing to keep or forget."""
    key = tmp_path / "k.pem"
    key.write_text("x")
    result = await _gateway(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "key_upload"})
    with patch.object(config_flow, "_store_key", return_value=str(key)), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"key_file": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "cloud_backup": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not fleet_mod.has_signin(result["data"])


# ---------------------------------------------------------------------------
# Options: Local only
# ---------------------------------------------------------------------------


async def _menu(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    hass.config_entries.options.async_abort(result["flow_id"])
    return result["menu_options"]


@pytest.mark.parametrize(("data", "offered"), [
    ({**V1R, "owners_email": OWNER, "site_id": 42}, True),
    ({**V1R, **TESLEMETRY}, True),
    (V1R, False),  # already local only
    ({**WIFI, "owners_email": OWNER, "site_id": 42}, False),  # the cloud is its only way to change settings
])
async def test_local_only_offered_for_signed_entries_with_a_sign_in(hass: HomeAssistant, data, offered) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data)
    entry.add_to_hass(hass)
    assert ("local_only" in await _menu(hass, entry)) is offered


def _registry_keys(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    return {e.unique_id.removeprefix(f"{DIN}_")
            for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


async def test_options_local_only(hass: HomeAssistant, tmp_path: Path, fake_powerwall, fleet, caplog) -> None:
    """Both backups and the Tesla tariff on: Local only forgets both sign-ins
    (and the saved tokens), turns the backup off, and the cloud's entities go."""
    hass.config.config_dir = str(tmp_path)
    folder = _folder(hass)
    await hass.async_add_executor_job(tesla_cloud.save_token, TOKEN, str(folder), OWNER)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, minor_version=2, title="Powerwall", data={
        **V1R, "owners_email": OWNER, "owners_authpath": str(folder), "site_id": 42, **TESLEMETRY},
        options={"cloud_backends": "owner_then_fleet", "tesla_tariff": True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    # A Wall Connector seen before (the fake site has none now).
    registry.async_get_or_create("sensor", DOMAIN, f"{DIN}_wall_connector_WC1_power", config_entry=entry)
    before = _registry_keys(hass, entry)
    assert {"storm_watch", "storm_watch_active", "tesla_tariff", "tariff_buy_price", "buy_tariff",
            "wall_connector_WC1_power"} <= before

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "local_only"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "local_only"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert not fleet_mod.has_signin(entry.data)
    assert not {"owners_email", "owners_authpath", "site_id", *TESLEMETRY} & entry.data.keys()
    assert entry.options["cloud_backends"] == "none"
    assert not (folder / AUTH_FILE).exists()
    coordinator = entry.runtime_data.coordinator  # reloaded
    assert not coordinator.cloud_enabled and coordinator.control_enabled
    after = _registry_keys(hass, entry)
    assert not after & {"storm_watch", "storm_watch_active", "grid_services_active", "off_grid_reserve",
                        "tesla_tariff", "tariff_buy_price", "tariff_sell_price", "buy_tariff", "sell_tariff",
                        "wall_connector_WC1_power"}
    # The local entities stay.
    assert {"backup_reserve", "grid_charging", "operation_mode", "grid_export", "battery_level"} <= after
    assert "Removed" in caplog.text and OWNER not in caplog.text and DIN not in caplog.text
    assert (await async_get_config_entry_diagnostics(hass, entry))["local_only"] is True
    assert "local_only" not in await _menu(hass, entry)


async def test_options_local_only_keeps_working_when_tokens_cant_go(hass: HomeAssistant, tmp_path: Path,
                                                                    fake_powerwall, caplog) -> None:
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, minor_version=2, title="Powerwall", data={
        **V1R, "owners_email": OWNER, "owners_authpath": str(_folder(hass)), "site_id": 42})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True), \
            patch.object(tesla_cloud, "forget_account", side_effect=PermissionError(13, "denied")):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "local_only"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "owners_email" not in entry.data
    assert "saved tokens couldn't be removed" in caplog.text


# ---------------------------------------------------------------------------
# Entities the entry no longer makes
# ---------------------------------------------------------------------------


async def _setup(hass: HomeAssistant, data: dict, options: dict | None = None,
                 stale: tuple[tuple[str, str], ...] = ()) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data, minor_version=2,
                            options=options or {}, title="Powerwall")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for platform, unique_id in stale:
        registry.async_get_or_create(platform, DOMAIN, unique_id, config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_tariff_entities_go_with_the_option(hass: HomeAssistant, fake_powerwall) -> None:
    """Cloud backup on, Tesla tariff off: only the tariff's entities go."""
    entry = await _setup(hass, {**V1R, "owners_email": OWNER, "owners_authpath": "/tmp", "site_id": 42}, stale=(
        ("sensor", f"{DIN}_tesla_tariff"), ("calendar", f"{DIN}_sell_tariff")))
    keys = _registry_keys(hass, entry)
    assert "storm_watch" in keys
    assert not {"tesla_tariff", "sell_tariff"} & keys


async def test_wifi_without_a_cloud_backup_loses_the_settings(hass: HomeAssistant, fake_powerwall) -> None:
    """Over the Wi-Fi password connection, settings need a cloud backup."""
    entry = await _setup(hass, WIFI, stale=(
        ("number", f"{DIN}_backup_reserve"), ("select", f"{DIN}_operation_mode"),
        ("switch", f"{DIN}_storm_watch"), ("sensor", f"{DIN}_battery_level")))
    keys = _registry_keys(hass, entry)
    assert not {"backup_reserve", "operation_mode", "storm_watch"} & keys
    assert "battery_level" in keys


async def test_entities_of_other_entries_and_unknown_keys_stay(hass: HomeAssistant, fake_powerwall) -> None:
    other = MockConfigEntry(domain=DOMAIN, unique_id="OTHER")
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create("switch", DOMAIN, f"{DIN}_storm_watch_x", config_entry=other)
    entry = await _setup(hass, V1R, stale=(
        ("switch", f"{DIN}_storm_watch_extra"), ("sensor", "unrelated_storm_watch"),
        ("sensor", "ENTRYID_tesla_tariff")))
    keys = {e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert {f"{DIN}_storm_watch_extra", "unrelated_storm_watch", "ENTRYID_tesla_tariff"} <= keys
    assert er.async_entries_for_config_entry(registry, other.entry_id)


async def test_an_entity_keyed_by_the_entry_id_goes_too(hass: HomeAssistant, fake_powerwall) -> None:
    """Entities made before the DIN was known are keyed by the entry id."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, minor_version=2, title="Powerwall")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create("switch", DOMAIN, f"{entry.entry_id}_storm_watch", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_storm_watch") is None


# ---------------------------------------------------------------------------
# Nothing reaches Tesla
# ---------------------------------------------------------------------------


async def test_local_only_never_reaches_tesla(hass: HomeAssistant, tmp_path: Path, fake_powerwall, cloud_net,
                                              aioclient_mock) -> None:
    """Setup, full and fast polls, every local write, the options and a
    reload: no cloud client is made and no Tesla server is called."""
    hass.config.config_dir = str(tmp_path)
    calls: list[str] = []

    def record(name):
        def called(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} called in local-only mode")
        return called

    async def fleet_request(*args, **kwargs):
        calls.append("fleet")
        raise AssertionError("Fleet API called in local-only mode")

    with (
        patch("custom_components.tesla_tedapi.coordinator.TedapiCoordinator._init_cloud", record("owner_api")),
        patch.object(tesla_cloud, "_call", record("tesla_cloud")),
        patch.object(fleet_mod.FleetApi, "request", fleet_request),
    ):
        entry = await _setup(hass, V1R, {"power_interval": 5})
        coordinator = entry.runtime_data.coordinator
        assert coordinator.backends == [] and not coordinator.cloud_enabled and coordinator.control_enabled
        await coordinator.async_refresh()  # a fast poll
        await hass.async_block_till_done()
        coordinator._full_at = None
        await coordinator.async_refresh()  # a full read
        await hass.async_block_till_done()

        def eid(platform: str, key: str) -> str:
            entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DIN}_{key}")
            assert entity_id, key
            return entity_id

        await hass.services.async_call("select", "select_option",
                                       {"entity_id": eid("select", "operation_mode"), "option": "backup"},
                                       blocking=True)
        await hass.services.async_call("number", "set_value",
                                       {"entity_id": eid("number", "backup_reserve"), "value": 30}, blocking=True)
        await hass.services.async_call("switch", "turn_on", {"entity_id": eid("switch", "grid_charging")},
                                       blocking=True)
        await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                       {"power_watts": 1000, "duration_seconds": 60}, blocking=True)
        await hass.services.async_call(DOMAIN, "resume_battery_operation", {}, blocking=True)
        await hass.services.async_call(DOMAIN, "start_max_backup", {"duration_minutes": 60}, blocking=True)
        await hass.services.async_call(DOMAIN, "cancel_max_backup", {}, blocking=True)
        # Cloud-only actions are refused, without trying the cloud.
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(DOMAIN, "get_tariff", {}, blocking=True, return_response=True)
        await hass.async_block_till_done()
        writes = [name for name, *_ in local(fake_powerwall).writes]

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert "local_only" not in result["menu_options"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
        assert "tesla_tariff" not in str(result["data_schema"].schema)
        hass.config_entries.options.async_abort(result["flow_id"])
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert {"max_backup"} <= set(writes) and len(writes) >= 4
    assert calls == []
    assert cloud(fake_powerwall) is None
    assert cloud_net.checked == []
    assert aioclient_mock.call_count == 0
    assert diagnostics["local_only"] is True and diagnostics["token_cache"] is None
