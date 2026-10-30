"""Setup, entities, settings and dispatch tests."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.tesla_tedapi import coordinator as coordinator_mod
from custom_components.tesla_tedapi.const import DOMAIN
from custom_components.tesla_tedapi.diagnostics import async_get_config_entry_diagnostics

from .conftest import DIN, cloud, local

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "192.168.91.1",
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


async def test_sensors(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    assert hass.states.get(_eid(hass, "sensor", "solar_power")).state == "3000.0"
    assert hass.states.get(_eid(hass, "sensor", "battery_soe")).state == "52.5"
    assert hass.states.get(_eid(hass, "sensor", "battery_level")).state == "50.0"
    assert hass.states.get(_eid(hass, "sensor", "grid_energy_imported")).state == "1000.0"
    # Solar inputs by MPPT letter; unused inputs (B at 0 V, C at a standby 15 V) get no sensors.
    assert hass.states.get(_eid(hass, "sensor", "string_A_power")).state == "1982.0"
    assert _eid(hass, "sensor", "string_A_voltage") is not None
    assert _eid(hass, "sensor", "string_B_power") is None
    assert _eid(hass, "sensor", "string_C_power") is None
    assert hass.states.get(_eid(hass, "sensor", "battery_health")).state == "103.0"
    assert _eid(hass, "sensor", "pack_TG0123456789AB_capacity")
    firmware = hass.states.get(_eid(hass, "sensor", "firmware_version"))
    assert firmware.state == "26.34.1"
    assert firmware.attributes["build"] == "dcee474d"
    # Local client: hotspot fallback configured, never cloud mode.
    kw = local(fake_powerwall).kwargs
    assert kw["wifi_host"] == "192.168.91.1" and kw["failover"] and kw["wifi_v1r_fallback"]
    assert kw["authpath"].endswith("tesla_tedapi")


async def test_settings_local(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    select = _eid(hass, "select", "operation_mode")
    assert hass.states.get(select).state == "self_consumption"
    assert hass.states.get(select).attributes["source"] == "local"
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": select, "option": "autonomous"}, blocking=True)
    await hass.services.async_call("switch", "turn_off",
                                   {"entity_id": _eid(hass, "switch", "grid_charging")}, blocking=True)
    await hass.services.async_call("number", "set_value",
                                   {"entity_id": _eid(hass, "number", "backup_reserve"), "value": 35},
                                   blocking=True)
    pw = local(fake_powerwall)
    assert ("mode", "autonomous") in pw.writes
    assert ("grid_charging", False) in pw.writes  # a real bool, never "off"
    assert ("reserve", 35) in pw.writes


async def test_wifi_read_only_without_account(hass: HomeAssistant) -> None:
    await _setup(hass, WIFI)
    assert _eid(hass, "select", "operation_mode") is None
    assert _eid(hass, "binary_sensor", "dispatch_active") is None
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                       {"power_watts": 100, "duration_seconds": 60}, blocking=True)


async def test_wifi_writes_go_to_cloud(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, {**WIFI, "owners_email": "a@b.c", "owners_authpath": "/tmp"})
    await hass.services.async_call("switch", "turn_on",
                                   {"entity_id": _eid(hass, "switch", "grid_charging")}, blocking=True)
    assert local(fake_powerwall).writes == []
    assert ("grid_charging", True) in cloud(fake_powerwall).writes


async def test_v1r_local_failure_falls_back_to_cloud(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, "owners_email": "a@b.c", "owners_authpath": "/tmp"})
    fake_powerwall.fail_local = True

    def down():
        raise ConnectionError("gateway down")

    local(fake_powerwall).power = down
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_eid(hass, "sensor", "solar_power")).state == "unavailable"
    select = hass.states.get(_eid(hass, "select", "operation_mode"))
    assert select.state == "self_consumption"
    assert select.attributes["source"] == "cloud"
    await hass.services.async_call("number", "set_value",
                                   {"entity_id": _eid(hass, "number", "backup_reserve"), "value": 40},
                                   blocking=True)
    assert ("reserve", 40) in cloud(fake_powerwall).writes


async def test_v1r_without_account_write_failure(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    local(fake_powerwall).set_mode = lambda mode: None  # gateway refuses
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call("select", "select_option",
                                       {"entity_id": _eid(hass, "select", "operation_mode"),
                                        "option": "backup"}, blocking=True)
    assert err.value.translation_key == "write_refused_no_cloud"
    assert entry.runtime_data.coordinator.control_enabled


async def test_gateway_down_logged_once(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, "owners_email": "a@b.c", "owners_authpath": "/tmp"})
    coordinator = entry.runtime_data.coordinator
    fake_powerwall.fail_local = True

    def down():
        raise ConnectionError("gateway down")

    local(fake_powerwall).power = down
    with patch.object(coordinator_mod, "_LOGGER") as logger:
        for _ in range(3):
            await coordinator.async_refresh()
        # Settings still come from the cloud: one warning, not one per poll.
        assert coordinator.last_update_success
        down_logs = [c for c in logger.warning.call_args_list if "can't be reached" in c.args[0]]
        assert len(down_logs) == 1
        fake_powerwall.fail_local = False
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        back_logs = [c for c in logger.info.call_args_list if "answers again" in c.args[0]]
        assert len(back_logs) == 1


def test_parallel_updates() -> None:
    from custom_components.tesla_tedapi import binary_sensor, event, number, select, sensor, switch

    assert sensor.PARALLEL_UPDATES == binary_sensor.PARALLEL_UPDATES == event.PARALLEL_UPDATES == 0
    assert select.PARALLEL_UPDATES == number.PARALLEL_UPDATES == switch.PARALLEL_UPDATES == 1


async def test_dispatch_session(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    active = _eid(hass, "binary_sensor", "dispatch_active")
    ended = async_capture_events(hass, f"{DOMAIN}_dispatch_ended")

    resp = await hass.services.async_call(
        DOMAIN, "dispatch_battery_power",
        {"power_watts": 2000, "duration_seconds": 1800, "min_soc": 20},
        blocking=True, return_response=True)
    assert resp["active"] is True
    pw = local(fake_powerwall)
    assert pw.dispatches == [(2000.0, 300)]  # short chunk, renewed
    assert hass.states.get(active).state == "on"

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(pw.dispatches) == 2

    # SOC falls to the limit -> stop and resume
    fake_powerwall.soe = 23.0  # app scale 19.0
    await hass.config_entries.async_entries(DOMAIN)[0].runtime_data.coordinator.async_refresh()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=122))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert pw.resumes == 1
    assert hass.states.get(active).state == "off"
    assert ended[-1].data["reason"] == "min_soc"


async def test_dispatch_refused_past_limit(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "dispatch_battery_power",
            {"power_watts": -3000, "duration_seconds": 600, "max_soc": 40}, blocking=True)
    assert local(fake_powerwall).dispatches == []


async def test_dispatch_completes(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    ended = async_capture_events(hass, f"{DOMAIN}_dispatch_ended")
    await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                   {"power_watts": 0, "duration_seconds": 90}, blocking=True)
    assert local(fake_powerwall).dispatches == [(0.0, 90)]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=91))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert local(fake_powerwall).resumes == 1
    assert ended[-1].data["reason"] == "completed"


async def test_resume_and_unload(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                   {"power_watts": 500, "duration_seconds": 600}, blocking=True)
    await hass.services.async_call(DOMAIN, "resume_battery_operation", {}, blocking=True)
    pw = local(fake_powerwall)
    assert pw.resumes == 1
    await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                   {"power_watts": 500, "duration_seconds": 600}, blocking=True)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert pw.resumes == 2


async def test_diagnostics(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {**V1R, "gw_pwd": "secret", "owners_email": "a@b.c",
                                "owners_authpath": "/nonexistent"})
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert "secret" not in text and "a@b.c" not in text and DIN not in text
    assert diag["data"]["battery_level"] == 50.0
