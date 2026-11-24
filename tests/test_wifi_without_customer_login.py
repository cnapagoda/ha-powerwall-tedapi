"""2.8.3 (#23): a gateway whose customer password was changed and isn't known.
Over the Powerwall's own Wi-Fi the gateway password needs no customer login,
so setup offers the Wi-Fi password connection there."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import gateway
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, local
from .test_config_flow import SETUP

PWD = "ABCDE12345"
WIFI = "192.168.91.1"
REAL_WIFI_DIN = gateway.wifi_din


async def _gateway_step(hass: HomeAssistant, host: str = WIFI, **extra):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "gateway"})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": host, "gw_pwd": PWD, "wifi_host": "", **extra})


async def test_wifi_password_connection_without_the_customer_login(
        hass: HomeAssistant, fake_gateway, fake_powerwall) -> None:
    fake_gateway.customer, fake_gateway.wifi = "SomethingElse", PWD
    result = await _gateway_step(hass)
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "wifi_only"
    assert result["menu_options"] == ["wifi", "gateway"]
    assert fake_gateway.wifi_checks == [WIFI]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "wifi"})
    assert result["step_id"] == "wifi"
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": WIFI, "cloud_backup": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == DIN
    data = result["data"]
    assert data["connection_mode"] == "wifi" and data["gw_pwd"] == PWD
    assert data["customer_password"] == "" and data["full_password_login"] is False
    # The connection check used the Wi-Fi password connection: no customer login.
    assert local(fake_powerwall).kwargs["password"] == ""
    assert local(fake_powerwall).kwargs["gw_pwd"] == PWD


async def test_back_to_enter_the_customer_password(hass: HomeAssistant, fake_gateway) -> None:
    fake_gateway.customer, fake_gateway.wifi = "Known42", PWD
    result = await _gateway_step(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "gateway"})
    assert result["step_id"] == "gateway" and not result["errors"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": WIFI, "gw_pwd": PWD, "wifi_host": "", "customer_password": "Known42"})
    assert result["step_id"] == "access"


@pytest.mark.parametrize(("wifi", "host"), [
    (None, "192.168.1.92"),  # the home-network address: no Wi-Fi password connection there
    ("OTHERPWD99", WIFI),  # the Powerwall's Wi-Fi refuses this password too
])
async def test_still_refused_when_the_wifi_doesnt_take_it(hass: HomeAssistant, fake_gateway, wifi, host) -> None:
    fake_gateway.customer, fake_gateway.wifi = "SomethingElse", wifi
    result = await _gateway_step(hass, host)
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"gw_pwd": "invalid_gw_pwd"}


async def test_no_wifi_attempt_with_a_customer_password(hass: HomeAssistant, fake_gateway) -> None:
    """A customer password that's wrong is reported as such."""
    fake_gateway.customer, fake_gateway.wifi = "Known42", PWD
    result = await _gateway_step(hass, customer_password="wrong")
    assert result["errors"] == {"customer_password": "invalid_customer_pwd"}
    assert fake_gateway.wifi_checks == []


async def test_a_known_gateway_is_not_added_twice(hass: HomeAssistant, fake_gateway) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={"host": WIFI}).add_to_hass(hass)
    fake_gateway.customer, fake_gateway.wifi = "SomethingElse", PWD
    result = await _gateway_step(hass)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


@pytest.mark.parametrize(("body", "din"), [(f"{DIN}\n".encode(), DIN), (b"", None)])
def test_wifi_din(body, din) -> None:
    with patch.object(gateway, "_request", return_value=(200, body)) as request:
        assert REAL_WIFI_DIN(WIFI, f" {PWD} ") == din
    assert request.call_args.kwargs["basic"] == ("Tesla_Energy_Device", PWD)
