"""2.3.1: register the signed (v1r) key through the Tesla Fleet or Teslemetry
integration instead of a Tesla sign-in here."""
from __future__ import annotations

import base64
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import config_flow, fleet, tesla_cloud
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN
from .test_config_flow import CLOUD, SETUP, _gateway
from .test_fleet import FLEET_ENTRIES, SCOPES, _fleet_entry, _site

PUB = b"our public key"
OTHER = b"the Tesla app key"


def _b64(der: bytes) -> str:
    return base64.b64encode(der).decode()


def _fleet_list(*clients: tuple[bytes, int]) -> dict:
    """Tesla Fleet passes the gateway's message through."""
    return {"response": {"message": {"payload": {"authorization": {"message": {
        "list_authorized_clients_response": {"clients": [
            {"public_key": _b64(der), "state": state} for der, state in clients]}}}}}}}


class KeyApi:
    """tesla_fleet_api EnergySite key calls: our key moves through the given
    states, one per list call; another verified key is always listed."""

    def __init__(self, states: list[int], fail: Exception | None = None) -> None:
        self.states = states
        self.fail = fail
        self.added: list[tuple[bytes, str]] = []

    async def add_authorized_client(self, public_key, description="Powerwall LAN Client"):
        if self.fail:
            raise self.fail
        self.added.append((public_key, description))
        return {"response": {"message": {"payload": {"authorization": {"message": {
            "add_authorized_client_response": {"client": {"state": 1}}}}}}}}

    async def list_authorized_clients(self):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return _fleet_list((OTHER, 3), (PUB, state))


def _fleet_with(api: KeyApi, scopes=SCOPES):
    site = _site()
    site.api = api
    return patch(FLEET_ENTRIES, return_value=[_fleet_entry(site, scopes=scopes)])


def _key(tmp_path):
    (tmp_path / "k.pem").write_text("x")
    return patch(f"{CLOUD}.ensure_rsa_key", return_value=(str(tmp_path / "k.pem"), PUB))


async def test_menu_offers_fleet_only_when_it_can_register(hass: HomeAssistant) -> None:
    with _fleet_with(KeyApi([1])):
        result = await _gateway(hass)
        assert result["menu_options"] == ["register_fleet", "register", "key_upload", "wifi"]
    with _fleet_with(KeyApi([1]), scopes=["energy_device_data"]):
        result = await _gateway(hass)
        assert "register_fleet" not in result["menu_options"]
    with patch(FLEET_ENTRIES, return_value=[]):
        result = await _gateway(hass)
        assert "register_fleet" not in result["menu_options"]


async def test_register_through_fleet(hass: HomeAssistant, tmp_path) -> None:
    api = KeyApi([1, 1, 3])
    with (
        _fleet_with(api), _key(tmp_path),
        patch.object(config_flow, "VERIFY_POLL_SECONDS", 0),
        patch(f"{CLOUD}.register_key") as owner_register,
        patch(SETUP, return_value=True),
    ):
        result = await _gateway(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register_fleet"})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert api.added == [(PUB, "Home Assistant")]
    owner_register.assert_not_called()
    assert result["data"]["rsa_key_path"].endswith("k.pem")
    assert "owners_email" not in result["data"]
    # Tesla Fleet becomes the cloud backup too.
    assert result["options"] == {"cloud_backends": "tesla_fleet"}


async def test_fleet_window_closes_and_registers_again(hass: HomeAssistant, tmp_path) -> None:
    api = KeyApi([2])
    with _fleet_with(api), _key(tmp_path), patch.object(config_flow, "VERIFY_POLL_SECONDS", 0):
        result = await _gateway(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register_fleet"})
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["step_id"] == "verify_failed"
    assert len(api.added) == 2  # once more automatically


@pytest.mark.parametrize(("error", "reason"), [
    (RuntimeError("boom"), "register_failed"),
    (type("Offline", (Exception,), {"status": 408})("device offline"), "register_unreachable"),
])
async def test_fleet_registration_fails(hass: HomeAssistant, tmp_path, error, reason) -> None:
    with _fleet_with(KeyApi([1], fail=error)), _key(tmp_path):
        result = await _gateway(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register_fleet"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_fleet_gone_before_registering(hass: HomeAssistant, tmp_path) -> None:
    with _fleet_with(KeyApi([1])), _key(tmp_path):
        result = await _gateway(hass)
        with patch(FLEET_ENTRIES, return_value=[]):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"next_step_id": "register_fleet"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "register_fleet_unavailable"


def test_key_state_only_for_our_key() -> None:
    assert fleet.key_state_from(_fleet_list((OTHER, 3), (PUB, 1)), PUB) == tesla_cloud.KEY_PENDING
    assert fleet.key_state_from(_fleet_list((OTHER, 3)), PUB) is None
    # Teslemetry's reshaped list, with named states.
    teslemetry = {"response": {"clients": [
        {"public_key": _b64(OTHER), "state": "AUTHORIZED_CLIENT_STATE_VERIFIED"},
        {"public_key": _b64(PUB), "state": "AUTHORIZED_CLIENT_STATE_PENDING_VERIFICATION_TIMEOUT"}]}}
    assert fleet.key_state_from(teslemetry, PUB) == tesla_cloud.KEY_TIMEOUT
    teslemetry["response"]["clients"][1]["state"] = "VERIFIED"
    assert fleet.key_state_from(teslemetry, PUB) == tesla_cloud.KEY_VERIFIED
    teslemetry["response"]["clients"][1]["state"] = "2"
    assert fleet.key_state_from(teslemetry, PUB) == tesla_cloud.KEY_TIMEOUT
    assert fleet.key_state_from(None, PUB) is None


async def test_new_key_from_options_through_fleet(hass: HomeAssistant, tmp_path) -> None:
    """Options → Register a new signed key opens a reauth that keeps the entry
    (and its options) until the new key is confirmed, then switches to it."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, options={"cloud_backends": "fleet_then_owner"}, data={
        "connection_mode": "v1r", "host": "192.168.1.50", "gw_pwd": "ABCDE12345", "rsa_key_path": "/old.pem"})
    entry.add_to_hass(hass)
    api = KeyApi([1, 3])
    with (
        _fleet_with(api), _key(tmp_path),
        patch.object(config_flow, "VERIFY_POLL_SECONDS", 0),
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert "new_key" in result["menu_options"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "new_key"})
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "new_key_started"
        await hass.async_block_till_done()
        flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert len(flows) == 1 and flows[0]["step_id"] == "new_key"
        flow_id = flows[0]["flow_id"]
        result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "register_fleet"})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert entry.data["rsa_key_path"] == "/old.pem"  # not before it's confirmed
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["rsa_key_path"].endswith("k.pem")
    assert entry.options == {"cloud_backends": "fleet_then_owner"}
    assert api.added == [(PUB, "Home Assistant")]


async def test_new_key_not_offered_on_wifi_entries(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="x", data={"connection_mode": "wifi", "host": "192.168.91.1"})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "new_key" not in result["menu_options"]
