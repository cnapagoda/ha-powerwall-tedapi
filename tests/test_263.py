"""2.6.3: Backup Gateway 1 (full-password login), gateway login lockouts,
the gateway's reason for a refused dispatch, and solar inputs that never
produced (external inverters)."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import gateway
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, local
from .test_config_flow import SETUP
from .test_init import V1R, _setup

PWD = "ABCDE12345"
GW1_DIN = "1118431-00-L--TG0123456789AB"
# The real functions (the autouse fake_gateway fixture replaces them).
REAL_FULL_PASSWORD_LOGIN = gateway.full_password_login
REAL_LOGIN = gateway.login


# ---- Backup Gateway 1 and lockouts (the gateway module) ----


@pytest.mark.parametrize(("answer", "expected"), [
    ((200, json.dumps({"din": GW1_DIN}).encode()), True),
    ((200, json.dumps({"din": "1232100-00-E--TG1"}).encode()), False),
    ((200, b"not json"), False),
    ((200, b"[]"), False),
    ((404, b""), False),
])
def test_gateway_1_found_from_its_status(answer, expected) -> None:
    with patch.object(gateway, "_request", return_value=answer):
        assert REAL_FULL_PASSWORD_LOGIN("h") is expected
    with patch.object(gateway, "_request", side_effect=gateway.GatewayUnreachable("down")):
        assert REAL_FULL_PASSWORD_LOGIN("h") is False


def test_login_sends_the_full_password_on_gateway_1_and_reports_lockouts() -> None:
    sent: list[dict] = []

    def request(host, path, body=None, token=None):
        sent.append(body)
        return 200, b'{"token": "t"}'

    with patch.object(gateway, "_request", request):
        assert REAL_LOGIN("h", PWD) == "t"
        assert REAL_LOGIN("h", PWD, full=True) == "t"
    assert [b["password"] for b in sent] == ["12345", PWD]
    with patch.object(gateway, "_request", return_value=(429, b"")), pytest.raises(gateway.GatewayLocked):
        REAL_LOGIN("h", PWD)


# ---- Backup Gateway 1 and lockouts (setup) ----


async def _gateway_step(hass: HomeAssistant, password: str) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "gateway"})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.92", "gw_pwd": password, "wifi_host": ""})


async def test_gateway_1_setup_uses_the_full_password(hass: HomeAssistant, fake_gateway, fake_powerwall,
                                                      tmp_path) -> None:
    from custom_components.tesla_tedapi import config_flow  # noqa: PLC0415

    fake_gateway.gateway1 = True
    result = await _gateway_step(hass, PWD)
    assert result["step_id"] == "access"
    assert fake_gateway.logins == [PWD]  # not the last 5 characters
    key = tmp_path / "k.pem"
    key.write_text("x")
    with patch.object(config_flow, "_store_key", return_value=str(key)), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "key_upload"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"key_file": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "cloud_backup": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["full_password_login"] is True
    # The signed connection logs in with the full password too.
    assert local(fake_powerwall).kwargs["password"] == PWD


async def test_other_gateways_keep_the_last_5(hass: HomeAssistant, fake_gateway) -> None:
    result = await _gateway_step(hass, PWD)
    assert result["step_id"] == "access"
    assert fake_gateway.logins == ["12345"]


async def test_locked_gateway_says_so(hass: HomeAssistant, fake_gateway) -> None:
    fake_gateway.locked = True
    result = await _gateway_step(hass, PWD)
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": "gateway_locked"}


async def test_gateway_1_entry_connects_and_classifies_with_the_full_password(
        hass: HomeAssistant, fake_gateway, fake_powerwall) -> None:
    fake_gateway.gateway1 = True
    entry = await _setup(hass, {**V1R, "gw_pwd": PWD, "full_password_login": True})
    coordinator = entry.runtime_data.coordinator
    assert local(fake_powerwall).kwargs["password"] == PWD
    assert await hass.async_add_executor_job(coordinator._classify_failure) == "key"
    assert fake_gateway.logins[-1] == PWD


# ---- the gateway's reason for a refused dispatch ----


async def test_refused_dispatch_shows_the_gateways_reason(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)

    def refuse(power, seconds):
        # What pypowerwall logs (and returns None) when the gateway refuses.
        logging.getLogger("pypowerwall.tedapi.tedapi_v1r").error(
            "Battery dispatch rejected: %s (status code %s)", "power out of range", 11)

    local(fake_powerwall).dispatch_battery_power = refuse
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                       {"power_watts": 9000, "duration_seconds": 60}, blocking=True)
    assert err.value.translation_key == "dispatch_refused_reason"
    assert err.value.translation_placeholders == {
        "reason": "Battery dispatch rejected: power out of range (status code 11)"}
    # The handler is gone afterwards.
    assert not any(type(h).__name__ == "_PypowerwallErrors" for h in logging.getLogger("pypowerwall").handlers)


# ---- solar inputs that never produced ----


def _register_input(hass: HomeAssistant, entry: MockConfigEntry, name: str) -> dict[str, str]:
    registry = er.async_get(hass)
    return {kind: registry.async_get_or_create(
        "sensor", DOMAIN, f"{DIN}_string_{name}_{kind}", config_entry=entry,
        suggested_object_id=f"powerwall_tedapi_solar_input_{name.lower()}_{kind}").entity_id
        for kind in ("power", "voltage", "current")}


async def test_inputs_that_never_produced_are_removed(hass: HomeAssistant, fake_powerwall) -> None:
    """Fake readings: A produces, B and C are on standby. B never produced
    (at most 1 W: an external inverter's unused input) and is removed; C
    produced before (3 kW peak) and stays, provided again at standby; D has no
    statistics and is left alone."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, title="Powerwall")
    entry.add_to_hass(hass)
    ids = {name: _register_input(hass, entry, name) for name in ("B", "C", "D")}
    stats = {ids["B"]["power"]: [{"max": 1.0}], ids["C"]["power"]: [{"max": 3000.0}, {"max": None}]}
    recorder = SimpleNamespace(async_add_executor_job=hass.async_add_executor_job)
    with (patch("homeassistant.components.recorder.get_instance", return_value=recorder),
          patch("homeassistant.components.recorder.statistics.statistics_during_period",
                return_value=stats)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert all(registry.async_get(eid) is None for eid in ids["B"].values())
    assert all(registry.async_get(eid) is not None for eid in ids["C"].values())
    assert hass.states.get(ids["C"]["power"]).state == "0.0"  # standby, not unavailable
    assert registry.async_get(ids["D"]["power"]) is not None
    assert hass.states.get(_eid(hass, "string_A_power")).state == "1982.0"


async def test_cleanup_without_a_recorder_keeps_everything(hass: HomeAssistant, fake_powerwall) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, title="Powerwall")
    entry.add_to_hass(hass)
    ids = _register_input(hass, entry, "B")
    with patch("homeassistant.components.recorder.get_instance", side_effect=KeyError("recorder")):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert er.async_get(hass).async_get(ids["power"]) is not None


def test_noise_is_not_use() -> None:
    from custom_components.tesla_tedapi.sensor import _input_in_use  # noqa: PLC0415

    assert not _input_in_use({"power": 1, "voltage": 15, "state": "Pv_Standby"})
    assert _input_in_use({"power": 25, "voltage": 15, "state": "Pv_Standby"})
    assert _input_in_use({"power": 0, "voltage": 300, "state": "Pv_Standby"})
    assert _input_in_use({"power": 0, "voltage": 0, "state": "PV_Active"})


def _eid(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DIN}_{key}")
