"""Config flow paths not covered by test_config_flow.py: connection check
failures, the site choice, discovery edge cases, reauth through a new key,
reconfigure errors and the cloud backup choice."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import config_flow, gateway, tesla_cloud
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, FakePowerwall
from .test_config_flow import CLOUD, PWD, SETUP, DhcpServiceInfo, _gateway

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "gw_pwd": PWD, "rsa_key_path": "/k"}
LOGIN = tesla_cloud.LoginStart("https://auth.tesla.com/x", "verifier", "state1")
HOME = tesla_cloud.EnergySite(42, DIN, "Home")


def _login_patches(sites=None, **extra):
    """Patches for a Tesla sign-in that returns these sites."""
    patches = {
        "start_login": {"return_value": LOGIN},
        "exchange_code": {"return_value": {"access_token": "at"}},
        "token_email": {"return_value": "owner@example.com"},
        "energy_sites": {"return_value": [HOME] if sites is None else sites},
        "save_token": {},
        "save_site": {},
        **extra,
    }
    return [patch(f"{CLOUD}.{name}", **kwargs) for name, kwargs in patches.items()]


class _Patched:
    def __init__(self, patches):
        self._patches = patches
        self.mocks = {}

    def __enter__(self):
        for p in self._patches:
            self.mocks[p.attribute] = p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


# ---------------------------------------------------------------------------
# The blocking connection check
# ---------------------------------------------------------------------------


def test_probe(tmp_path) -> None:
    key = tmp_path / "k.pem"
    key.write_text("x")
    with pytest.raises(config_flow.PasswordRequired):
        config_flow._probe("v1r", "h", "", str(key), "UTC")
    with pytest.raises(config_flow.InvalidRsaKey):
        config_flow._probe("v1r", "h", PWD, str(tmp_path / "missing.pem"), "UTC")
    assert config_flow._probe("v1r", "h", PWD, str(key), "UTC") == DIN
    assert config_flow._probe("wifi", "h", PWD, "", "UTC") == DIN

    # The gateway answered without the signed session: the key isn't verified.
    with patch.object(FakePowerwall, "__init__", _init_with_mode("wifi")), \
            pytest.raises(config_flow.KeyNotAccepted):
        config_flow._probe("v1r", "h", PWD, str(key), "UTC")
    FakePowerwall.fail_local = True
    with pytest.raises(config_flow.CannotConnect):
        config_flow._probe("wifi", "h", PWD, "", "UTC")
    FakePowerwall.fail_local = False
    with patch.object(FakePowerwall, "din", lambda self: None):
        with pytest.raises(config_flow.KeyNotAccepted):
            config_flow._probe("v1r", "h", PWD, str(key), "UTC")
        with pytest.raises(config_flow.CannotConnect):
            config_flow._probe("wifi", "h", PWD, "", "UTC")


def _init_with_mode(mode):
    original = FakePowerwall.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.tedapi_mode = mode

    return init


def test_check_gateway_and_scan() -> None:
    assert config_flow._check_gateway("h", PWD) == DIN
    with patch("pypowerwall.scan.scan", return_value=[{"ip": "1.2.3.4"}]) as scan:
        assert config_flow._scan() == [{"ip": "1.2.3.4"}]
    scan.assert_called_once_with(json_output=True, timeout=1.0)


def test_store_key_rejects_non_pem_and_keeps_a_backup(hass: HomeAssistant, tmp_path) -> None:
    from contextlib import contextmanager

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    upload = tmp_path / "upload.pem"

    @contextmanager
    def uploaded(_hass, _file_id):
        yield upload

    store = tmp_path / "store"
    with patch("homeassistant.components.file_upload.process_uploaded_file", uploaded):
        upload.write_bytes(b"not a key")
        with pytest.raises(config_flow.InvalidRsaKey):
            config_flow._store_key(hass, "id", str(store))
        upload.write_bytes(rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        config_flow._store_key(hass, "id", str(store))
        config_flow._store_key(hass, "id", str(store))
    assert (store / f"{config_flow.RSA_KEY_FILE}.bak").is_file()


@pytest.mark.parametrize(("err", "errors"), [
    (gateway.GatewayPasswordRejected(), {"gw_pwd": "invalid_gw_pwd"}),
    (config_flow.PasswordRequired(), {"gw_pwd": "password_required"}),
    (config_flow.InvalidRsaKey(), {"base": "invalid_rsa_key"}),
    (config_flow.KeyNotAccepted(), {"base": "key_not_accepted"}),
    (config_flow.CannotConnect(), {"base": "cannot_connect"}),
])
def test_gateway_errors(err, errors) -> None:
    assert config_flow._gateway_errors(err) == errors


async def test_login_mixin_needs_a_finish(hass: HomeAssistant) -> None:
    with pytest.raises(NotImplementedError):
        await config_flow.TeslaLoginMixin()._async_logged_in(HOME)


# ---------------------------------------------------------------------------
# Sign-in and site choice
# ---------------------------------------------------------------------------


async def _key_upload_with_cloud(hass: HomeAssistant, tmp_path):
    """Gateway step, then an uploaded key with the cloud backup ticked: the
    flow goes on to the Tesla sign-in."""
    key = tmp_path / "k.pem"
    key.write_text("x")
    result = await _gateway(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "key_upload"})
    with patch.object(config_flow, "_store_key", return_value=str(key)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"key_file": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "cloud_backup": True})
    assert result["step_id"] == "login"
    return result


async def test_key_upload_then_sign_in_with_site_choice(hass: HomeAssistant, tmp_path) -> None:
    other = tesla_cloud.EnergySite(7, "", "Cabin")
    with _Patched(_login_patches(sites=[other, tesla_cloud.EnergySite(8, "", "Shed")])), \
            patch(SETUP, return_value=True):
        result = await _key_upload_with_cloud(hass, tmp_path)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"callback": "C1"})
        # No site names this gateway: ask.
        assert result["step_id"] == "site"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"site": "7"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["site_id"] == 7
    assert result["data"]["connection_mode"] == "v1r"


async def test_sign_in_single_site_used(hass: HomeAssistant, tmp_path) -> None:
    with _Patched(_login_patches(sites=[tesla_cloud.EnergySite(9, "", "Only")])), patch(SETUP, return_value=True):
        result = await _key_upload_with_cloud(hass, tmp_path)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"callback": "C1"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["site_id"] == 9


async def test_sign_in_errors(hass: HomeAssistant, tmp_path) -> None:
    with _Patched(_login_patches(energy_sites={"side_effect": tesla_cloud.TeslaCloudError("HTTP 500", 500)})):
        result = await _key_upload_with_cloud(hass, tmp_path)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"callback": "C1"})
    assert result["errors"] == {"base": "cloud_error"}

    with _Patched(_login_patches(sites=[])):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"callback": "C1"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_sites"


async def test_sign_in_to_another_powerwall(hass: HomeAssistant, tmp_path) -> None:
    with _Patched(_login_patches(sites=[tesla_cloud.EnergySite(7, "OTHER-DIN", "Cabin")])):
        result = await _key_upload_with_cloud(hass, tmp_path)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"callback": "C1"})
    assert result["reason"] == "wrong_powerwall"


# ---------------------------------------------------------------------------
# Key registration and confirmation
# ---------------------------------------------------------------------------


async def _register(hass: HomeAssistant, tmp_path, **extra):
    patches = _login_patches(ensure_rsa_key={"return_value": (str(tmp_path / "k.pem"), b"pub")}, **extra)
    (tmp_path / "k.pem").write_text("x")
    patched = _Patched([*patches, patch.object(config_flow, "VERIFY_POLL_SECONDS", 0)])
    patched.__enter__()
    result = await _gateway(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"callback": "C1"})
    return patched, result


async def test_register_already_verified(hass: HomeAssistant, tmp_path) -> None:
    patched, result = await _register(hass, tmp_path, register_key={"return_value": tesla_cloud.KEY_VERIFIED})
    patched.__exit__()
    # No confirmation wait: straight to the connection check and the entry.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["site_id"] == 42


async def test_confirmation_survives_a_failed_check(hass: HomeAssistant, tmp_path) -> None:
    patched, result = await _register(
        hass, tmp_path, register_key={"return_value": tesla_cloud.KEY_PENDING},
        key_state={"side_effect": [tesla_cloud.TeslaCloudError("offline", 408), tesla_cloud.KEY_VERIFIED]})
    with patch(SETUP, return_value=True):
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    patched.__exit__()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_confirmation_error_ends_in_verify_failed(hass: HomeAssistant, tmp_path) -> None:
    async def boom(self):
        await asyncio.sleep(0)  # still running when the progress step is shown
        raise RuntimeError("lost")

    with patch.object(config_flow.TedapiConfigFlow, "_async_wait_verified", boom):
        patched, result = await _register(hass, tmp_path, register_key={"return_value": tesla_cloud.KEY_PENDING})
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    patched.__exit__()
    assert result["step_id"] == "verify_failed"


async def test_wait_verified_window_ends(hass: HomeAssistant) -> None:
    flow = config_flow.TedapiConfigFlow()
    flow.hass = hass
    flow._site, flow._token, flow._public_der = HOME, {"access_token": "at"}, b"pub"
    flow._key_state = tesla_cloud.KEY_PENDING
    with patch.object(config_flow, "VERIFY_WINDOW_SECONDS", 0):
        assert await flow._async_wait_verified() == tesla_cloud.KEY_PENDING


# ---------------------------------------------------------------------------
# The final connection check
# ---------------------------------------------------------------------------


async def test_connect_failed_goes_back_to_gateway(hass: HomeAssistant) -> None:
    result = await _gateway(hass, host="192.168.91.1")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "wifi"})
    FakePowerwall.fail_local = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.91.1", "cloud_backup": False})
    assert result["step_id"] == "connect_failed"
    assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "gateway"


async def test_connect_finds_another_powerwall(hass: HomeAssistant, tmp_path) -> None:
    with patch.object(FakePowerwall, "din", lambda self: "OTHER-DIN"):
        patched, result = await _register(hass, tmp_path, register_key={"return_value": tesla_cloud.KEY_VERIFIED})
    patched.__exit__()
    assert result["reason"] == "wrong_powerwall"


async def test_finish_without_din_checks_the_connection(hass: HomeAssistant) -> None:
    # Older firmware: the gateway step can't read the DIN, the final check does.
    with patch.object(config_flow, "_check_gateway", return_value=None), patch(SETUP, return_value=True):
        result = await _gateway(hass, host="192.168.91.1")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "wifi"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.91.1", "cloud_backup": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == DIN

    flow = config_flow.TedapiConfigFlow()
    flow.hass = hass
    flow.context = {"source": config_entries.SOURCE_USER}
    with patch.object(flow, "_async_connect", return_value={"type": "checked"}) as connect:
        assert await flow._async_finish() == {"type": "checked"}
    connect.assert_called_once()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def test_dhcp_known_or_other_gateways(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="WIFI-DIN", data={
        "connection_mode": "wifi", "host": "192.168.91.1", "gw_pwd": PWD}).add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, unique_id="NO-PWD", data={
        "connection_mode": "v1r", "host": "192.168.1.40"}).add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R).add_to_hass(hass)

    # Already at that address.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP},
        data=DhcpServiceInfo(ip="192.168.1.50", hostname="teslapw_x", macaddress="aabbccddee02"))
    assert result["reason"] == "already_configured"

    # A gateway the known entries can't log in to: offered as new.
    with patch.object(config_flow, "_check_gateway", side_effect=gateway.GatewayPasswordRejected):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_DHCP},
            data=DhcpServiceInfo(ip="192.168.1.62", hostname="teslapw_y", macaddress="aabbccddee03"))
    assert result["step_id"] == "gateway"


# ---------------------------------------------------------------------------
# Reauth through a new key, and reconfigure errors
# ---------------------------------------------------------------------------


async def test_reauth_with_uploaded_key(hass: HomeAssistant, tmp_path) -> None:
    key = tmp_path / "new.pem"
    key.write_text("x")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "key_upload"})
    assert "cloud_backup" not in result["data_schema"].schema
    with patch.object(config_flow, "_store_key", return_value=str(key)), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"key_file": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"})
    assert result["reason"] == "reauth_successful"
    assert entry.data["rsa_key_path"] == str(key)


async def test_reconfigure_errors(hass: HomeAssistant, tmp_path) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R)
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    form = {"host": "192.168.1.50", "gw_pwd": PWD, "rsa_key_path": str(tmp_path / "missing.pem"), "wifi_host": ""}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form)
    assert result["errors"] == {"rsa_key_path": "invalid_rsa_key"}
    FakePowerwall.fail_local = True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**form, "rsa_key_path": ""})
    assert result["errors"] == {"base": "cannot_connect"}


# ---------------------------------------------------------------------------
# Options: cloud backup choice
# ---------------------------------------------------------------------------


async def test_cloud_backup_keeps_a_choice_no_longer_offered(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, options={"cloud_backends": "tesla_fleet"})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "cloud_backup"})
    # Tesla Fleet isn't set up any more, but the current choice stays listed.
    options = result["data_schema"].schema["cloud_backends"].config["options"]
    assert "tesla_fleet" in options
    assert result["description_placeholders"] == {"owner": "not linked", "fleet": "not set up"}


async def test_scan_error_falls_back_to_manual(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch.object(config_flow, "_scan", side_effect=OSError("no network")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "scan"})
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": "none_found"}
