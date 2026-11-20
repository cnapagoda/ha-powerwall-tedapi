"""2.8.0: gateways that refuse the last 5 characters of the password and take
the full password (not only a Backup Gateway 1), and a debug log that tells a
refused password's cause apart (issue #23)."""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import gateway
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, local
from .test_config_flow import SETUP

PWD = "ABCDE12345"
# The real functions (the autouse fake_gateway fixture replaces them).
REAL_LOGIN_ANY = gateway.login_any
REAL_DIN_ANY = gateway.din_any
REAL_PART_NUMBER = gateway.part_number
REAL_LOGIN = gateway.login
REAL_DIN = gateway.din


def _gateway(accepts: str, din: str = DIN):
    """A gateway whose customer login takes only `accepts`; records what was sent."""
    sent: list[str] = []

    def request(host, path, body=None, token=None):
        if path == "/api/login/Basic":
            sent.append(body["password"])
            return (200, b'{"token": "t"}') if body["password"] == accepts else (401, b"")
        if path == "/tedapi/din":
            return 200, din.encode()
        raise AssertionError(path)

    return sent, request


def _real(request):
    """The real login and DIN reads (the autouse fixture fakes them), on a fake _request."""
    return patch.multiple(gateway, _request=request, login=REAL_LOGIN, din=REAL_DIN)


# ---- the gateway module ----


def test_full_password_tried_when_the_last_5_are_refused(caplog) -> None:
    sent, request = _gateway(PWD)
    caplog.set_level(logging.DEBUG, logger="custom_components.tesla_tedapi.gateway")
    with _real(request):
        assert REAL_LOGIN_ANY("h", PWD) == ("t", True)
        assert REAL_DIN_ANY("h", PWD) == (DIN, True)
    assert sent == ["12345", PWD, "12345", PWD]
    assert "HTTP 401, the last 5 characters of a 10-character password" in caplog.text
    assert PWD not in caplog.text


def test_last_5_kept_when_they_work() -> None:
    sent, request = _gateway("12345")
    with _real(request):
        assert REAL_LOGIN_ANY("h", PWD) == ("t", False)
    assert sent == ["12345"]


@pytest.mark.parametrize(("password", "full", "expected"), [
    ("12345", False, ["12345"]),  # only 5 characters given: nothing else to try
    (PWD, True, [PWD]),  # Backup Gateway 1: already the full password
])
def test_no_second_try(password, full, expected) -> None:
    sent, request = _gateway("other")
    with _real(request), pytest.raises(gateway.GatewayPasswordRejected):
        REAL_LOGIN_ANY("h", password, full)
    assert sent == expected


def test_both_refused() -> None:
    sent, request = _gateway("other")
    with _real(request), pytest.raises(gateway.GatewayPasswordRejected) as err:
        REAL_LOGIN_ANY("h", PWD)
    assert sent == ["12345", PWD]
    assert str(err.value) == "HTTP 401, the full password"


@pytest.mark.parametrize(("answer", "expected"), [
    ((200, json.dumps({"din": "1232100-00-E--TG1234"}).encode()), "1232100"),
    ((200, json.dumps({"din": "no dashes"}).encode()), None),
    ((200, json.dumps({}).encode()), None),
    ((200, b"not json"), None),
    ((404, b""), None),
])
def test_part_number(answer, expected) -> None:
    with patch.object(gateway, "_request", return_value=answer):
        assert REAL_PART_NUMBER("h") == expected


# ---- setup, reauthentication and Reconfigure ----


async def _gateway_step(hass: HomeAssistant, password: str) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "gateway"})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.92", "gw_pwd": password, "wifi_host": ""})


async def test_setup_falls_back_to_the_full_password(hass: HomeAssistant, fake_gateway, fake_powerwall,
                                                     tmp_path) -> None:
    from custom_components.tesla_tedapi import config_flow  # noqa: PLC0415

    fake_gateway.full_only = True
    result = await _gateway_step(hass, PWD)
    assert result["step_id"] == "access"
    assert fake_gateway.logins == ["12345", PWD]
    key = tmp_path / "k.pem"
    key.write_text("x")
    with patch.object(config_flow, "_store_key", return_value=str(key)), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "key_upload"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"key_file": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "cloud_backup": False})
    # Remembered, so polling and the signed connection use the full password.
    assert result["data"]["full_password_login"] is True


async def test_setup_refused_logs_why(hass: HomeAssistant, fake_gateway, caplog) -> None:
    fake_gateway.password = "OTHER99999"
    fake_gateway.part_number = "1232100"
    caplog.set_level(logging.DEBUG, logger="custom_components.tesla_tedapi.config_flow")
    result = await _gateway_step(hass, PWD)
    assert result["errors"] == {"gw_pwd": "invalid_gw_pwd"}
    assert fake_gateway.logins == ["12345", PWD]
    assert "part number 1232100, 10 characters given" in caplog.text
    assert PWD not in caplog.text


async def test_setup_refused_without_a_part_number(hass: HomeAssistant, fake_gateway, caplog) -> None:
    fake_gateway.password = "OTHER99999"
    caplog.set_level(logging.DEBUG, logger="custom_components.tesla_tedapi.config_flow")
    await _gateway_step(hass, "12345")
    assert fake_gateway.logins == ["12345"]
    assert "part number not reported, 5 characters given" in caplog.text


async def test_reauth_remembers_the_full_password(hass: HomeAssistant, fake_gateway) -> None:
    fake_gateway.full_only = True
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN,
                            data={"connection_mode": "v1r", "host": "192.168.1.92", "gw_pwd": "old",
                                  "rsa_key_path": "/k.pem"})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "reauth_password"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"gw_pwd": PWD})
    assert result["reason"] == "reauth_successful"
    assert entry.data["full_password_login"] is True


@pytest.mark.parametrize("case", [
    (True, PWD, True),
    (False, PWD, False),
    (True, "OTHER99999", False),  # refused: left to the connection check
])
async def test_reconfigure_finds_the_login(hass: HomeAssistant, fake_gateway, fake_powerwall, tmp_path,
                                           case) -> None:
    full_only, password, expected = case
    fake_gateway.full_only = full_only
    key = tmp_path / "key.pem"
    key.write_text("x")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN,
                            data={"connection_mode": "v1r", "host": "192.168.1.92", "gw_pwd": PWD,
                                  "rsa_key_path": str(key)})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.92", "rsa_key_path": str(key), "gw_pwd": password})
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["full_password_login"] is expected
    assert local(fake_powerwall).kwargs["password"] == (password if expected else "")


async def test_reconfigure_without_a_password(hass: HomeAssistant, fake_gateway) -> None:
    from custom_components.tesla_tedapi import config_flow  # noqa: PLC0415

    assert config_flow._login_mode("h", "") is False
    fake_gateway.reachable = False
    assert config_flow._login_mode("h", PWD) is False


# ---- 2.8.2 (#23): a customer password that differs from the sticker ----

CUSTOMER = "MyOwnPw42"


async def test_setup_with_a_customer_password(hass: HomeAssistant, fake_gateway, fake_powerwall,
                                              tmp_path) -> None:
    from custom_components.tesla_tedapi import config_flow  # noqa: PLC0415

    fake_gateway.customer = CUSTOMER
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "gateway"})
    # The sticker password alone is refused (both forms)...
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.92", "gw_pwd": PWD, "wifi_host": ""})
    assert result["errors"] == {"gw_pwd": "invalid_gw_pwd"}
    # ...the customer password logs in, as it is and only it.
    fake_gateway.logins.clear()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.92", "gw_pwd": PWD, "wifi_host": "", "customer_password": CUSTOMER})
    assert result["step_id"] == "access"
    assert fake_gateway.logins == [CUSTOMER]
    key = tmp_path / "k.pem"
    key.write_text("x")
    with patch.object(config_flow, "_store_key", return_value=str(key)), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "key_upload"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"key_file": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "cloud_backup": False})
    data = result["data"]
    assert data["customer_password"] == CUSTOMER and data["gw_pwd"] == PWD
    assert data["full_password_login"] is False
    # The signed connection logs in with it; the sticker password stays for Wi-Fi.
    assert local(fake_powerwall).kwargs["password"] == CUSTOMER
    assert local(fake_powerwall).kwargs["gw_pwd"] == PWD


async def test_wrong_customer_password_is_refused(hass: HomeAssistant, fake_gateway, caplog) -> None:
    fake_gateway.customer = CUSTOMER
    caplog.set_level(logging.DEBUG, logger="custom_components.tesla_tedapi.config_flow")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "gateway"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.92", "gw_pwd": PWD, "wifi_host": "", "customer_password": "wrong"})
    assert result["errors"] == {"gw_pwd": "invalid_gw_pwd"}
    assert fake_gateway.logins == ["wrong"]  # no sticker fallback once one is given
    assert "refused the customer password" in caplog.text and "wrong" not in caplog.text


async def test_customer_password_in_polling_reauth_reconfigure_and_diagnostics(
        hass: HomeAssistant, fake_gateway, fake_powerwall, tmp_path) -> None:
    from custom_components.tesla_tedapi.diagnostics import async_get_config_entry_diagnostics  # noqa: PLC0415

    from .test_init import V1R, _setup  # noqa: PLC0415

    fake_gateway.customer = CUSTOMER
    entry = await _setup(hass, {**V1R, "gw_pwd": PWD, "customer_password": CUSTOMER})
    coordinator = entry.runtime_data.coordinator
    assert local(fake_powerwall).kwargs["password"] == CUSTOMER
    assert coordinator.extras._customer_pwd == CUSTOMER
    # Telling a refused key from a refused password logs in with it too.
    assert await hass.async_add_executor_job(coordinator._classify_failure) == "key"
    assert fake_gateway.logins[-1] == CUSTOMER
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert CUSTOMER not in str(diagnostics)

    with patch(SETUP, return_value=True):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "reauth_password"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"gw_pwd": PWD, "customer_password": CUSTOMER})
    assert result["reason"] == "reauth_successful"
    assert entry.data["customer_password"] == CUSTOMER

    key = tmp_path / "key.pem"
    key.write_text("x")
    with patch(SETUP, return_value=True):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.92", "rsa_key_path": str(key), "gw_pwd": PWD,
                                "customer_password": CUSTOMER})
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["customer_password"] == CUSTOMER and entry.data["full_password_login"] is False


def test_extras_signed_client_uses_the_customer_password() -> None:
    from custom_components.tesla_tedapi import client  # noqa: PLC0415

    kwargs = client.local_kwargs("v1r", "h", PWD, "/k", "UTC", customer_pwd=CUSTOMER)
    assert kwargs["password"] == CUSTOMER and kwargs["gw_pwd"] == PWD
    assert client.local_kwargs("wifi", "h", PWD, "", "UTC", customer_pwd=CUSTOMER)["password"] == ""
    assert client.local_kwargs("v1r", "h", PWD, "/k", "UTC")["password"] == ""


async def test_reauth_keeps_and_diagnostics_flag_the_customer_password(hass: HomeAssistant, fake_gateway,
                                                                      fake_powerwall) -> None:
    from custom_components.tesla_tedapi.diagnostics import async_get_config_entry_diagnostics  # noqa: PLC0415

    from .test_init import V1R, _setup  # noqa: PLC0415

    fake_gateway.customer = CUSTOMER
    entry = await _setup(hass, {**V1R, "gw_pwd": PWD, "customer_password": CUSTOMER})
    assert (await async_get_config_entry_diagnostics(hass, entry))["customer_password_set"] is True
    with patch(SETUP, return_value=True):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "reauth_password"})
        field = next(k for k in result["data_schema"].schema if k == "customer_password")
        assert field.description == {"suggested_value": CUSTOMER}  # shown again, so it's kept
