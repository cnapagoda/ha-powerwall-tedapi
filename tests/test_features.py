"""2.2 behaviour: export rule restore, cloud site and cache, quiet status
events, dynamic packs, reauth, setup health, dispatch guards, saved alerts."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, cloud, local

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
ACCOUNT = {"owners_email": "a@b.c", "owners_authpath": "/tmp", "site_id": 42}


async def _setup(hass: HomeAssistant, data: dict, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data, options=options or {}, title="Powerwall")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _eid(hass: HomeAssistant, platform: str, key: str) -> str:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DIN}_{key}")


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_export_rule_dropped_and_restored(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    select = _eid(hass, "select", "grid_export")
    assert hass.states.get(select).state == "pv_only"

    # The gateway loses its rule: pypowerwall would say "battery_ok".
    fake_powerwall.export_rule = None
    await _refresh(hass, entry)
    state = hass.states.get(select)
    assert state.state == "pv_only"  # the rule that was set, not the default
    assert ("grid_export", "pv_only") in local(fake_powerwall).writes  # written back
    assert fake_powerwall.export_rule == "pv_only"
    assert state.attributes["restored_count"] == 1
    assert state.attributes["missing_on_powerwall"] is False


async def test_export_rule_not_restored_when_off(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, {"restore_export": False})
    fake_powerwall.export_rule = None
    await _refresh(hass, entry)
    state = hass.states.get(_eid(hass, "select", "grid_export"))
    assert state.state == "pv_only"
    assert state.attributes["missing_on_powerwall"] is True
    assert ("grid_export", "pv_only") not in local(fake_powerwall).writes


async def test_unset_export_rule_is_the_default(hass: HomeAssistant, fake_powerwall) -> None:
    # Never set on this site (and no account): Tesla's default, nothing restored.
    fake_powerwall.export_rule = None
    await _setup(hass, V1R)
    state = hass.states.get(_eid(hass, "select", "grid_export"))
    assert state.state == "battery_ok"
    assert state.attributes["missing_on_powerwall"] is False
    assert local(fake_powerwall).writes == []


async def test_export_rule_seeded_from_cloud(hass: HomeAssistant, fake_powerwall) -> None:
    # Dropped before Home Assistant ever saw it: the cloud still has it.
    fake_powerwall.export_rule = None
    await _setup(hass, {**V1R, **ACCOUNT})
    assert hass.states.get(_eid(hass, "select", "grid_export")).state == "never"
    assert ("grid_export", "never") in local(fake_powerwall).writes


async def test_grid_charging_dropped_and_restored(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    switch = _eid(hass, "switch", "grid_charging")
    assert hass.states.get(switch).state == "off"

    # The gateway loses the field: pypowerwall would say charging is allowed.
    fake_powerwall.grid_charging_disallow = None
    await _refresh(hass, entry)
    state = hass.states.get(switch)
    assert state.state == "off"  # the setting that was chosen, not the default
    assert ("grid_charging", False) in local(fake_powerwall).writes  # written back
    assert fake_powerwall.grid_charging_disallow is True
    assert state.attributes["restored_count"] == 1
    assert state.attributes["missing_on_powerwall"] is False


async def test_grid_charging_on_is_not_restored(hass: HomeAssistant, fake_powerwall) -> None:
    # Chosen "on" is the same as the default: nothing to write back.
    entry = await _setup(hass, V1R)
    await hass.services.async_call("switch", "turn_on", {"entity_id": _eid(hass, "switch", "grid_charging")},
                                   blocking=True)
    fake_powerwall.grid_charging_disallow = None
    local(fake_powerwall).writes.clear()
    await _refresh(hass, entry)
    assert local(fake_powerwall).writes == []
    assert hass.states.get(_eid(hass, "switch", "grid_charging")).state == "on"


async def test_grid_charging_not_restored_when_off(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, {"restore_export": False})
    fake_powerwall.grid_charging_disallow = None
    await _refresh(hass, entry)
    state = hass.states.get(_eid(hass, "switch", "grid_charging"))
    assert state.state == "off"
    assert state.attributes["missing_on_powerwall"] is True
    assert ("grid_charging", False) not in local(fake_powerwall).writes


async def test_cloud_checked_hourly(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    coordinator = entry.runtime_data.coordinator
    health = _eid(hass, "sensor", "setup_health")
    # Checked on the first poll, although the gateway answers everything.
    assert hass.states.get(health).attributes["cloud_last_ok"] is not None
    assert hass.states.get(health).state == "ok"
    calls = []
    cloud(fake_powerwall).get_mode = lambda: calls.append(1) or "self_consumption"
    await _refresh(hass, entry)
    assert calls == []  # not again within the hour

    coordinator._cloud_check_at -= 3601
    coordinator.cloud_last_ok -= timedelta(hours=2)
    await _refresh(hass, entry)
    assert calls == [1]


async def test_cloud_check_failure_degrades_health(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    coordinator = entry.runtime_data.coordinator
    fake_powerwall.cloud_reads_fail = True
    for _ in range(3):
        coordinator._cloud_check_at -= 3601
        coordinator.cloud_last_ok = None
        await _refresh(hass, entry)
        health = hass.states.get(_eid(hass, "sensor", "setup_health"))
        assert health.state == "degraded" and "cloud_backup" in health.attributes["problems"]
    # Three failed checks: ask to sign in again.
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"cloud_login_{entry.entry_id}") is not None

    # With the repair up, no more than hourly.
    coordinator._cloud_check_at -= 601
    reads = len([p for p in fake_powerwall.instances if p.mode == "cloud"])
    await _refresh(hass, entry)
    assert len([p for p in fake_powerwall.instances if p.mode == "cloud"]) == reads

    fake_powerwall.cloud_reads_fail = False
    coordinator._cloud_check_at -= 3601
    await _refresh(hass, entry)
    assert hass.states.get(_eid(hass, "sensor", "setup_health")).state == "ok"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"cloud_login_{entry.entry_id}") is None


async def test_failed_cloud_read_is_cached(hass: HomeAssistant, fake_powerwall) -> None:
    # Setting missing and not known yet: the cloud is asked, but a failure is
    # not retried on every poll.
    fake_powerwall.cloud_reads_fail = True
    fake_powerwall.grid_charging_disallow = None
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    logins = len([p for p in fake_powerwall.instances if p.mode == "cloud"])
    for _ in range(3):
        await _refresh(hass, entry)
    assert len([p for p in fake_powerwall.instances if p.mode == "cloud"]) == logins


async def test_cloud_pinned_to_site_and_cached(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    local(fake_powerwall).get_mode = lambda: None  # gateway can't say: cloud fills in
    await _refresh(hass, entry)
    assert cloud(fake_powerwall).kwargs["siteid"] == 42
    clouds = [p for p in fake_powerwall.instances if p.mode == "cloud"]
    calls = []
    clouds[-1].get_mode = lambda: calls.append(1) or "backup"
    await _refresh(hass, entry)
    await _refresh(hass, entry)
    assert calls == []  # served from the 5-minute cache
    assert hass.states.get(_eid(hass, "select", "operation_mode")).attributes["source"] == "mixed"


async def test_status_alerts_do_not_fire_events(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    event = _eid(hass, "event", "alert_event")
    fake_powerwall.alert_list = ["SystemConnectedToGrid", "BackfeedLimited"]
    await _refresh(hass, entry)
    assert hass.states.get(event).state == "unknown"  # status: no event
    assert "Export limited" in hass.states.get(_eid(hass, "sensor", "active_alerts")).state
    fake_powerwall.alert_list = ["SystemConnectedToGrid", "BackfeedLimited", "PVInverterComms"]
    await _refresh(hass, entry)
    assert hass.states.get(event).attributes["alert"] == "PVInverterComms"


async def test_status_alert_events_option(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, {"status_alert_events": True})
    fake_powerwall.alert_list = ["SystemConnectedToGrid", "BackfeedLimited"]
    await _refresh(hass, entry)
    assert hass.states.get(_eid(hass, "event", "alert_event")).attributes["alert"] == "BackfeedLimited"


async def test_event_follows_availability(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    event = _eid(hass, "event", "alert_event")
    fake_powerwall.fail_local = True

    def down():
        raise ConnectionError("gateway down")

    local(fake_powerwall).power = down
    await _refresh(hass, entry)
    assert hass.states.get(event).state == "unavailable"


async def test_new_pack_appears(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    assert _eid(hass, "sensor", "pack_EXP0001_capacity") is None
    fake_powerwall.extra_packs = {"TEPOD--1807000-20-B--EXP0001": {
        "POD_nom_full_pack_energy": 14000, "POD_nom_energy_remaining": 9000,
        "HVP_PackTempMax": 21.0, "HVP_PackTempMin": 20.0}}
    await _refresh(hass, entry)
    assert hass.states.get(_eid(hass, "sensor", "pack_EXP0001_capacity")).state == "14.0"


async def test_password_refused_starts_reauth(hass: HomeAssistant, fake_powerwall, fake_gateway) -> None:
    entry = await _setup(hass, V1R)
    fake_gateway.password = "XXXXXOTHER"
    fake_powerwall.fail_local = True
    local(fake_powerwall).power = lambda: (_ for _ in ()).throw(ConnectionError("refused"))
    for _ in range(3):
        await _refresh(hass, entry)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_unreachable_does_not_start_reauth(hass: HomeAssistant, fake_powerwall, fake_gateway) -> None:
    entry = await _setup(hass, V1R)
    fake_gateway.reachable = False
    fake_powerwall.fail_local = True
    local(fake_powerwall).power = lambda: (_ for _ in ()).throw(ConnectionError("down"))
    for _ in range(12):
        await _refresh(hass, entry)
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


async def test_cloud_login_repair(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    local(fake_powerwall).get_mode = lambda: None
    coordinator = entry.runtime_data.coordinator
    with patch.object(coordinator, "_init_cloud", side_effect=RuntimeError("Tesla cloud login failed")):
        for _ in range(3):
            coordinator._cloud = None
            coordinator._cloud_cache = None
            await _refresh(hass, entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"cloud_login_{entry.entry_id}")
    assert issue is not None and issue.is_fixable
    health = hass.states.get(_eid(hass, "sensor", "setup_health"))
    assert health.state == "degraded" and "cloud_backup" in health.attributes["problems"]


async def test_setup_health_ok(hass: HomeAssistant) -> None:
    await _setup(hass, V1R)
    health = hass.states.get(_eid(hass, "sensor", "setup_health"))
    assert health.state == "ok"
    assert health.attributes["checks"]["signed_connection"] is True


async def test_dispatch_floor_and_cap(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R, {"dispatch_max_watts": 5000})
    resp = await hass.services.async_call(
        DOMAIN, "dispatch_battery_power", {"power_watts": 2000, "duration_seconds": 600},
        blocking=True, return_response=True)
    assert resp["min_soc"] == 20.0  # backup reserve
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "dispatch_battery_power", {"power_watts": 6000, "duration_seconds": 600}, blocking=True)


async def test_alerts_survive_restart(hass: HomeAssistant, fake_powerwall) -> None:
    fake_powerwall.alert_list = ["SystemConnectedToGrid", "PVInverterComms"]
    entry = await _setup(hass, V1R)
    since = entry.runtime_data.coordinator.alerts.since["PVInverterComms"]
    await entry.runtime_data.coordinator._store.async_save(
        entry.runtime_data.coordinator._state_to_save())
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator.alerts.since["PVInverterComms"] == since


async def test_cloud_login_repair_flow(hass: HomeAssistant) -> None:
    from custom_components.tesla_tedapi import tesla_cloud
    from custom_components.tesla_tedapi.repairs import async_create_fix_flow

    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={**V1R, **ACCOUNT, "site_id": 1})
    entry.add_to_hass(hass)
    flow = await async_create_fix_flow(hass, "cloud_login_x", {"entry_id": entry.entry_id})
    flow.hass, flow.handler, flow.flow_id = hass, DOMAIN, "flow1"
    cloud_mod = "custom_components.tesla_tedapi.config_flow.tesla_cloud"
    with (
        patch(f"{cloud_mod}.start_login", return_value=tesla_cloud.LoginStart("https://auth", "v", "s")),
        patch(f"{cloud_mod}.exchange_code", return_value={"access_token": "at"}),
        patch(f"{cloud_mod}.token_email", return_value="new@example.com"),
        patch(f"{cloud_mod}.energy_sites", return_value=[tesla_cloud.EnergySite(42, DIN, "Home")]),
        patch(f"{cloud_mod}.save_token"),
        patch(f"{cloud_mod}.save_site"),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        result = await flow.async_step_init()
        assert result["step_id"] == "login"
        result = await flow.async_step_login({"callback": "C1"})
    assert result["type"] == "create_entry"
    assert entry.data["owners_email"] == "new@example.com" and entry.data["site_id"] == 42
    reload.assert_called_once_with(entry.entry_id)
