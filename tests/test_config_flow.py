"""Config flow tests."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import tesla_cloud
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN

CLOUD = "custom_components.tesla_tedapi.config_flow.tesla_cloud"


async def _start(hass: HomeAssistant, option: str):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": option})


async def test_wifi(hass: HomeAssistant) -> None:
    result = await _start(hass, "wifi")
    assert result["step_id"] == "connect_wifi"
    with patch("custom_components.tesla_tedapi.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.91.1", "gw_pwd": "ABCDEFGHIJ", "cloud_backup": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == DIN
    assert result["data"]["connection_mode"] == "wifi"
    assert "owners_email" not in result["data"]


async def test_existing_key(hass: HomeAssistant, tmp_path) -> None:
    key = tmp_path / "key.pem"
    result = await _start(hass, "existing_key")
    assert result["step_id"] == "connect_v1r"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.50", "rsa_key_path": str(key), "cloud_backup": False})
    assert result["errors"] == {"rsa_key_path": "invalid_rsa_key"}

    key.write_text("x")
    with patch("custom_components.tesla_tedapi.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "wifi_host": "192.168.91.1",
                                "rsa_key_path": str(key), "cloud_backup": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["connection_mode"] == "v1r"
    assert result["data"]["wifi_host"] == "192.168.91.1"


async def test_register_new_key(hass: HomeAssistant, tmp_path) -> None:
    login = tesla_cloud.LoginStart("https://auth.tesla.com/x", "verifier", "state1")
    with (
        patch(f"{CLOUD}.start_login", return_value=login),
        patch(f"{CLOUD}.exchange_code", return_value={"access_token": "at", "refresh_token": "rt"}) as exchange,
        patch(f"{CLOUD}.token_email", return_value="owner@example.com"),
        patch(f"{CLOUD}.energy_sites", return_value=[tesla_cloud.EnergySite(42, DIN, "Home")]),
        patch(f"{CLOUD}.save_token"),
        patch(f"{CLOUD}.save_site"),
        patch(f"{CLOUD}.ensure_rsa_key", return_value=(str(tmp_path / "k.pem"), b"pub")),
        patch(f"{CLOUD}.register_key", return_value=tesla_cloud.KEY_PENDING),
        patch(f"{CLOUD}.wait_for_key_state", side_effect=[tesla_cloud.KEY_PENDING, tesla_cloud.KEY_VERIFIED]),
        patch("custom_components.tesla_tedapi.async_setup_entry", return_value=True),
    ):
        (tmp_path / "k.pem").write_text("x")
        result = await _start(hass, "register")
        assert result["step_id"] == "login"
        assert "auth.tesla.com" in result["description_placeholders"]["url"]

        # wrong state -> rejected, new link
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": "tesla://auth/callback?code=abc&state=other"})
        assert result["errors"] == {"base": "invalid_auth"}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": "tesla://auth/callback?code=abc&state=state1"})
        exchange.assert_called_once_with("abc", "verifier")
        assert result["step_id"] == "verify"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["errors"] == {"base": "not_verified"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "connect_v1r"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "wifi_host": ""})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["owners_email"] == "owner@example.com"
    assert data["site_id"] == 42
    assert data["rsa_key_path"].endswith("k.pem")
    assert data["owners_authpath"] == hass.config.path("tesla_tedapi")


async def test_already_configured(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={}).add_to_hass(hass)
    result = await _start(hass, "wifi")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.91.1", "gw_pwd": "pw", "cloud_backup": False})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_cannot_connect(hass: HomeAssistant, fake_powerwall) -> None:
    fake_powerwall.fail_local = True
    result = await _start(hass, "wifi")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.91.1", "gw_pwd": "pw", "cloud_backup": False})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_to_v1r(hass: HomeAssistant, tmp_path) -> None:
    key = tmp_path / "key.pem"
    key.write_text("x")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN,
                            data={"connection_mode": "wifi", "host": "192.168.91.1", "gw_pwd": "pw"})
    entry.add_to_hass(hass)
    with patch("custom_components.tesla_tedapi.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "rsa_key_path": str(key),
                                "wifi_host": "192.168.91.1", "gw_pwd": "pw"})
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["connection_mode"] == "v1r"
    assert entry.data["host"] == "192.168.1.50"


def test_parse_callback() -> None:
    assert tesla_cloud.parse_callback("tesla://auth/callback?code=C1&state=S", "S") == "C1"
    assert tesla_cloud.parse_callback("  C2  ", "S") == "C2"
