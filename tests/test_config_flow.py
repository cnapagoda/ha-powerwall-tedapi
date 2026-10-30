"""Config, reauth, options and repair flow tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import config_flow, tesla_cloud
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN

CLOUD = "custom_components.tesla_tedapi.config_flow.tesla_cloud"
PWD = "ABCDE12345"
SETUP = "custom_components.tesla_tedapi.async_setup_entry"


async def _gateway(hass: HomeAssistant, host: str = "192.168.1.50", pwd: str = PWD, wifi_host: str = ""):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "gateway"})
    assert result["step_id"] == "gateway"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": host, "gw_pwd": pwd, "wifi_host": wifi_host})


async def test_gateway_password_checked_first(hass: HomeAssistant, fake_gateway) -> None:
    result = await _gateway(hass, pwd="WRONGWRONG")
    assert result["errors"] == {"gw_pwd": "invalid_gw_pwd"}
    fake_gateway.reachable = False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.50", "gw_pwd": PWD, "wifi_host": ""})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_already_configured_at_gateway_step(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={}).add_to_hass(hass)
    result = await _gateway(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_wifi(hass: HomeAssistant) -> None:
    result = await _gateway(hass, host="192.168.91.1")
    assert result["step_id"] == "access"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "wifi"})
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.91.1", "cloud_backup": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == DIN
    assert result["data"]["connection_mode"] == "wifi"
    assert "owners_email" not in result["data"]


async def test_key_upload(hass: HomeAssistant, tmp_path) -> None:
    key = tmp_path / "k.pem"
    key.write_text("x")
    result = await _gateway(hass, wifi_host="192.168.91.1")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "key_upload"})
    with patch.object(config_flow, "_store_key", side_effect=config_flow.InvalidRsaKey):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"key_file": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "cloud_backup": False})
    assert result["errors"] == {"key_file": "invalid_rsa_key"}
    with patch.object(config_flow, "_store_key", return_value=str(key)), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"key_file": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "cloud_backup": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["connection_mode"] == "v1r"
    assert data["gw_pwd"] == PWD
    assert data["wifi_host"] == "192.168.91.1"
    assert data["rsa_key_path"] == str(key)


def test_store_key(hass: HomeAssistant, tmp_path) -> None:
    from contextlib import contextmanager

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    def pem(key):
        return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())

    upload = tmp_path / "upload.pem"

    @contextmanager
    def uploaded(_hass, _file_id):
        yield upload

    with patch("homeassistant.components.file_upload.process_uploaded_file", uploaded):
        upload.write_bytes(pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)))
        target = config_flow._store_key(hass, "id", str(tmp_path / "store"))
        assert oct(__import__("os").stat(target).st_mode & 0o777) == "0o600"
        upload.write_bytes(pem(ec.generate_private_key(ec.SECP256R1())))
        try:
            config_flow._store_key(hass, "id", str(tmp_path / "store"))
            raise AssertionError("EC key accepted")
        except config_flow.InvalidRsaKey:
            pass


async def test_register_new_key(hass: HomeAssistant, tmp_path) -> None:
    login = tesla_cloud.LoginStart("https://auth.tesla.com/x", "verifier", "state1")
    (tmp_path / "k.pem").write_text("x")
    with (
        patch.object(config_flow, "VERIFY_POLL_SECONDS", 0),
        patch(f"{CLOUD}.start_login", return_value=login),
        patch(f"{CLOUD}.exchange_code", return_value={"access_token": "at", "refresh_token": "rt"}) as exchange,
        patch(f"{CLOUD}.token_email", return_value="owner@example.com"),
        patch(f"{CLOUD}.energy_sites", return_value=[tesla_cloud.EnergySite(7, "OTHER-DIN", "Cabin"),
                                                     tesla_cloud.EnergySite(42, DIN, "Home")]),
        patch(f"{CLOUD}.save_token"),
        patch(f"{CLOUD}.save_site"),
        patch(f"{CLOUD}.ensure_rsa_key", return_value=(str(tmp_path / "k.pem"), b"pub")),
        patch(f"{CLOUD}.register_key", return_value=tesla_cloud.KEY_PENDING),
        patch(f"{CLOUD}.key_state", side_effect=[tesla_cloud.KEY_PENDING, tesla_cloud.KEY_VERIFIED]),
        patch(SETUP, return_value=True),
    ):
        result = await _gateway(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register"})
        assert result["step_id"] == "login"
        assert "auth.tesla.com" in result["description_placeholders"]["url"]

        # wrong state -> rejected before reaching Tesla; the link stays
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": "tesla://auth/callback?code=abc&state=other"})
        assert result["errors"] == {"base": "invalid_callback"}
        exchange.assert_not_called()

        # Two sites: the one with this gateway's DIN is picked without asking.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": "tesla://auth/callback?code=abc&state=state1"})
        exchange.assert_called_once_with("abc", "verifier")
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["owners_email"] == "owner@example.com"
    assert data["site_id"] == 42
    assert data["rsa_key_path"].endswith("k.pem")
    assert data["owners_authpath"] == hass.config.path("tesla_tedapi")


async def test_register_window_closes(hass: HomeAssistant, tmp_path) -> None:
    login = tesla_cloud.LoginStart("https://auth.tesla.com/x", "verifier", "state1")
    with (
        patch.object(config_flow, "VERIFY_POLL_SECONDS", 0),
        patch(f"{CLOUD}.start_login", return_value=login),
        patch(f"{CLOUD}.exchange_code", return_value={"access_token": "at"}),
        patch(f"{CLOUD}.token_email", return_value="owner@example.com"),
        patch(f"{CLOUD}.energy_sites", return_value=[tesla_cloud.EnergySite(42, DIN, "Home")]),
        patch(f"{CLOUD}.save_token"),
        patch(f"{CLOUD}.save_site"),
        patch(f"{CLOUD}.ensure_rsa_key", return_value=(str(tmp_path / "k.pem"), b"pub")),
        patch(f"{CLOUD}.register_key", return_value=tesla_cloud.KEY_PENDING) as register,
        patch(f"{CLOUD}.key_state", return_value=tesla_cloud.KEY_TIMEOUT),
    ):
        result = await _gateway(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": "C1"})
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["step_id"] == "verify_failed"
        assert register.call_count == 2  # once more automatically
        # Submit: register again -> a new progress round
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()


async def test_scan(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch.object(config_flow, "_scan", return_value=[{"ip": "192.168.1.77", "din": "Powerwall-3"}]):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "scan"})
    assert result["step_id"] == "scan"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"found": "192.168.1.77"})
    assert result["step_id"] == "gateway"
    schema_host = next(k for k in result["data_schema"].schema if k == "host")
    assert schema_host.default() == "192.168.1.77"

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch.object(config_flow, "_scan", return_value=[]):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "scan"})
    assert result["step_id"] == "gateway" and result["errors"] == {"base": "none_found"}


async def test_dhcp_follows_new_address(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={
        "connection_mode": "v1r", "host": "192.168.1.50", "gw_pwd": PWD, "rsa_key_path": "/k"})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_DHCP},
            data=DhcpServiceInfo(ip="192.168.1.60", hostname="teslapw_abcdef", macaddress="aabbccddeeff"))
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "192.168.1.60"


async def test_dhcp_new_gateway(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP},
        data=DhcpServiceInfo(ip="192.168.1.61", hostname="teslapw_abcdef", macaddress="aabbccddee01"))
    assert result["step_id"] == "gateway"


async def test_reauth_password(hass: HomeAssistant, fake_gateway) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={
        "connection_mode": "v1r", "host": "192.168.1.50", "gw_pwd": "OLDOLDOLD1", "rsa_key_path": "/k"})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "reauth_password"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"gw_pwd": "WRONGWRONG"})
    assert result["errors"] == {"gw_pwd": "invalid_gw_pwd"}
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"gw_pwd": PWD})
    assert result["reason"] == "reauth_successful"
    assert entry.data["gw_pwd"] == PWD


async def test_reconfigure_to_v1r(hass: HomeAssistant, tmp_path) -> None:
    key = tmp_path / "key.pem"
    key.write_text("x")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN,
                            data={"connection_mode": "wifi", "host": "192.168.91.1", "gw_pwd": PWD})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "rsa_key_path": str(key),
                                "wifi_host": "192.168.91.1", "gw_pwd": PWD})
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["connection_mode"] == "v1r"
    assert entry.data["host"] == "192.168.1.50"


async def test_options_settings_and_unlink(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={
        "connection_mode": "v1r", "host": "192.168.1.50", "gw_pwd": PWD, "rsa_key_path": "/k",
        "owners_email": "a@b.c", "owners_authpath": "/tmp", "site_id": 42})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.MENU
        assert "unlink_account" in result["menu_options"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {
            "scan_interval": 45, "restore_export": False, "status_alert_events": True,
            "problem_minutes": 20, "dispatch_floor_reserve": True, "dispatch_max_watts": 5000})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options["problem_minutes"] == 20
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "unlink_account"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert "owners_email" not in entry.data
    assert entry.options["scan_interval"] == 45


async def test_options_link_account(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={
        "connection_mode": "v1r", "host": "192.168.1.50", "gw_pwd": PWD, "rsa_key_path": "/k"})
    entry.add_to_hass(hass)
    login = tesla_cloud.LoginStart("https://auth.tesla.com/x", "verifier", "s")
    with (
        patch(f"{CLOUD}.start_login", return_value=login),
        patch(f"{CLOUD}.exchange_code", return_value={"access_token": "at"}),
        patch(f"{CLOUD}.token_email", return_value="owner@example.com"),
        patch(f"{CLOUD}.energy_sites", return_value=[tesla_cloud.EnergySite(42, DIN, "Home")]),
        patch(f"{CLOUD}.save_token"),
        patch(f"{CLOUD}.save_site"),
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "link_account"})
        assert result["step_id"] == "login"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"callback": "C1"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data["owners_email"] == "owner@example.com"
    assert entry.data["site_id"] == 42


RT = "eyJ" + "a" * 600  # shape of a Tesla refresh token


async def test_options_link_account_with_refresh_token(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={
        "connection_mode": "v1r", "host": "192.168.1.50", "gw_pwd": PWD, "rsa_key_path": "/k"})
    entry.add_to_hass(hass)
    login = tesla_cloud.LoginStart("https://auth.tesla.com/x", "verifier", "s")
    with (
        patch(f"{CLOUD}.start_login", return_value=login),
        patch(f"{CLOUD}.exchange_code") as exchange,
        patch("pypowerwall.tesla_auth._refresh_access_token",
              return_value={"access_token": "at", "id_token": ""}) as refresh,
        patch(f"{CLOUD}.token_email", return_value="owner@example.com"),
        patch(f"{CLOUD}.energy_sites", return_value=[tesla_cloud.EnergySite(42, DIN, "Home")]),
        patch(f"{CLOUD}.save_token") as save,
        patch(f"{CLOUD}.save_site"),
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "link_account"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"callback": f"  {RT}\n"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    refresh.assert_called_once_with(RT)
    exchange.assert_not_called()
    # The pasted refresh token is kept when Tesla doesn't send a new one.
    assert save.call_args.args[0]["refresh_token"] == RT
    assert entry.data["site_id"] == 42


async def test_login_refused_by_owner_api(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={
        "connection_mode": "v1r", "host": "192.168.1.50", "gw_pwd": PWD, "rsa_key_path": "/k"})
    entry.add_to_hass(hass)
    login = tesla_cloud.LoginStart("https://auth.tesla.com/x", "verifier", "s")
    with (
        patch(f"{CLOUD}.start_login", return_value=login),
        patch(f"{CLOUD}.exchange_code", return_value={"access_token": "at"}),
        patch(f"{CLOUD}.token_email", return_value="owner@example.com"),
        patch(f"{CLOUD}.energy_sites", side_effect=tesla_cloud.TeslaRefused("HTTP 403")),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "link_account"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"callback": "tesla://auth/callback?code=NA_x&state=s"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "owner_api_refused"}


def test_is_refresh_token() -> None:
    assert tesla_cloud.is_refresh_token(RT)
    assert not tesla_cloud.is_refresh_token("tesla://auth/callback?code=NA_abc&state=x")
    assert not tesla_cloud.is_refresh_token("NA_abcdef")


def DhcpServiceInfo(ip: str, hostname: str, macaddress: str):  # noqa: N802
    """The discovery object (its module needs aiodhcpwatcher on older HA)."""
    try:
        from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo as Info
    except ImportError:
        return SimpleNamespace(ip=ip, hostname=hostname, macaddress=macaddress)
    return Info(ip=ip, hostname=hostname, macaddress=macaddress)


def test_parse_callback() -> None:
    assert tesla_cloud.parse_callback("tesla://auth/callback?code=C1&state=S", "S") == "C1"
    assert tesla_cloud.parse_callback("  C2  ", "S") == "C2"


@pytest.mark.parametrize(("error", "reason"), [
    (tesla_cloud.TeslaRefused("refused", 403), "register_token_refused"),
    (tesla_cloud.TeslaAuthError("rejected", 401), "register_token_refused"),
    (tesla_cloud.TeslaCloudError("not found", 404), "register_wrong_site"),
    (tesla_cloud.TeslaCloudError("Owner API HTTP 408: device offline", 408), "register_unreachable"),
    (tesla_cloud.TeslaCloudError("Owner API HTTP 500", 500), "register_failed"),
])
async def test_register_failure_reasons(hass: HomeAssistant, tmp_path, error, reason) -> None:
    login = tesla_cloud.LoginStart("https://auth.tesla.com/x", "verifier", "state1")
    with (
        patch(f"{CLOUD}.start_login", return_value=login),
        patch(f"{CLOUD}.exchange_code", return_value={"access_token": "at", "refresh_token": "rt"}),
        patch(f"{CLOUD}.token_email", return_value="owner@example.com"),
        patch(f"{CLOUD}.energy_sites", return_value=[tesla_cloud.EnergySite(42, DIN, "Home")]),
        patch(f"{CLOUD}.save_token"),
        patch(f"{CLOUD}.save_site"),
        patch(f"{CLOUD}.ensure_rsa_key", return_value=(str(tmp_path / "k.pem"), b"pub")),
        patch(f"{CLOUD}.register_key", side_effect=error),
    ):
        result = await _gateway(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": "tesla://auth/callback?code=abc&state=state1"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_sign_in_link_changes_only_after_a_code_reached_tesla(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={
        "connection_mode": "v1r", "host": "192.168.1.50", "gw_pwd": PWD, "rsa_key_path": "/k"})
    entry.add_to_hass(hass)
    first = tesla_cloud.LoginStart("https://auth.tesla.com/first", "v1", "s1")
    second = tesla_cloud.LoginStart("https://auth.tesla.com/second", "v2", "s2")
    with (
        patch(f"{CLOUD}.start_login", side_effect=[first, second]) as start,
        patch(f"{CLOUD}.exchange_code", side_effect=tesla_cloud.TeslaAuthError("expired")) as exchange,
        patch("pypowerwall.tesla_auth._refresh_access_token", side_effect=RuntimeError("revoked")),
        patch(f"{CLOUD}.energy_sites", side_effect=tesla_cloud.TeslaRefused("HTTP 403")),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "link_account"})
        flow_id = result["flow_id"]

        # Not from this link, or nothing usable: Tesla isn't asked, the link stays.
        for pasted in ("tesla://auth/callback?code=old&state=s0", "not a code"):
            result = await hass.config_entries.options.async_configure(flow_id, {"callback": pasted})
            assert result["errors"] == {"base": "invalid_callback"}
            assert result["description_placeholders"]["url"] == first.url
        # A refresh token Tesla rejects: no code was used, the link stays.
        result = await hass.config_entries.options.async_configure(flow_id, {"callback": RT})
        assert result["errors"] == {"base": "invalid_token"}
        assert result["description_placeholders"]["url"] == first.url
        exchange.assert_not_called()
        assert start.call_count == 1

        # A code from this link that Tesla rejects: it can't be used again, new link.
        result = await hass.config_entries.options.async_configure(
            flow_id, {"callback": "tesla://auth/callback?code=c1&state=s1"})
        assert result["errors"] == {"base": "invalid_auth"}
        assert result["description_placeholders"]["url"] == second.url
        exchange.assert_called_once_with("c1", "v1")


async def test_refresh_token_refused_keeps_the_link(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={
        "connection_mode": "v1r", "host": "192.168.1.50", "gw_pwd": PWD, "rsa_key_path": "/k"})
    entry.add_to_hass(hass)
    login = tesla_cloud.LoginStart("https://auth.tesla.com/x", "verifier", "s")
    with (
        patch(f"{CLOUD}.start_login", return_value=login) as start,
        patch("pypowerwall.tesla_auth._refresh_access_token", return_value={"access_token": "at"}),
        patch(f"{CLOUD}.token_email", return_value="owner@example.com"),
        patch(f"{CLOUD}.energy_sites", side_effect=tesla_cloud.TeslaRefused("HTTP 403")),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "link_account"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"callback": RT})
    assert result["errors"] == {"base": "owner_api_refused"}
    assert start.call_count == 1
