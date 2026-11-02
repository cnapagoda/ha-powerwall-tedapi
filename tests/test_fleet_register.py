"""Fleet API sign-in made here: Teslemetry (access token) or Tesla Fleet
(OAuth with the user's own developer app), in setup, for registering the
signed (v1r) key, and later as a reauthentication."""
from __future__ import annotations

import base64
import json
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import config_flow, fleet as fleet_mod, tesla_cloud
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, TESLEMETRY
from .test_config_flow import CLOUD, SETUP, _gateway

PUB = b"our public key"
OTHER = b"the Tesla app key"
V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "gw_pwd": "ABCDE12345", "rsa_key_path": "/old.pem"}


def _b64(der: bytes) -> str:
    return base64.b64encode(der).decode()


def _jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def _gateway_list(*clients: tuple[bytes, int]) -> dict:
    """The Fleet API passes the gateway's message through."""
    return {"response": {"message": {"payload": {"authorization": {"message": {
        "list_authorized_clients_response": {"clients": [
            {"public_key": _b64(der), "state": state} for der, state in clients]}}}}}}}


class KeyServer:
    """Our key moves through the given states, one per list call; another
    verified key is always listed."""

    def __init__(self, fleet, states: list[int], fail: Exception | None = None) -> None:
        self.states = states
        self.fail = fail
        self.added: list[tuple[str, dict]] = []
        original = fleet.request

        async def request(api, method, path, body=None):
            if "authorized_client" in path or (path.endswith("/command") and method == "POST"):
                return await self._key_call(api, method, path, body)
            return await original(api, method, path, body)

        self.patch = patch.object(fleet, "request", request)

    async def _key_call(self, api, method, path, body):
        if method == "POST" and (path.endswith("add_authorized_client")
                                 or "add_authorized_client_request" in json.dumps(body)):
            if self.fail:
                raise self.fail
            self.added.append((path.rsplit("/", 1)[-1], body))
            return {"response": {"message": {"payload": {"authorization": {"message": {
                "add_authorized_client_response": {"client": {"state": 1}}}}}}}}
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return _gateway_list((OTHER, 3), (PUB, state))


def _key(tmp_path):
    (tmp_path / "k.pem").write_text("x")
    return patch(f"{CLOUD}.ensure_rsa_key", return_value=(str(tmp_path / "k.pem"), PUB))


async def _to_signin(hass: HomeAssistant) -> dict:
    result = await _gateway(hass)
    assert result["menu_options"] == ["register", "key_upload", "wifi"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register"})
    assert result["step_id"] == "signin"
    assert result["menu_options"] == ["login", "fleet_oauth", "teslemetry"]
    return result


async def _teslemetry(hass: HomeAssistant, result: dict, token: str = "tm-token") -> dict:
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "teslemetry"})
    assert result["step_id"] == "teslemetry"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"access_token": f" {token} "})


async def test_register_through_teslemetry(hass: HomeAssistant, tmp_path, fleet) -> None:
    keys = KeyServer(fleet, [1, 1, 3])
    with (
        keys.patch, _key(tmp_path),
        patch.object(config_flow, "VERIFY_POLL_SECONDS", 0),
        patch(f"{CLOUD}.register_key") as owner_register,
        patch(SETUP, return_value=True),
    ):
        result = await _teslemetry(hass, await _to_signin(hass))
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Teslemetry's own endpoint, with an RSA customer key.
    assert keys.added == [("add_authorized_client", {"key_type": 1, "public_key": _b64(PUB),
                                                     "authorized_client_type": 1,
                                                     "description": "Home Assistant"})]
    owner_register.assert_not_called()
    data = result["data"]
    assert data["rsa_key_path"].endswith("k.pem")
    assert "owners_email" not in data
    assert {k: data[k] for k in TESLEMETRY} == TESLEMETRY
    # The sign-in is the cloud backup too.
    assert result["options"] == {"cloud_backends": "tesla_fleet"}


async def test_teslemetry_uses_the_account_region(hass: HomeAssistant, tmp_path, fleet) -> None:
    fleet.region = "EU"
    with KeyServer(fleet, [3]).patch, _key(tmp_path), patch(SETUP, return_value=True):
        result = await _teslemetry(hass, await _to_signin(hass))
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["data"]["fleet_server"] == "https://eu.teslemetry.com"


async def test_teslemetry_token_errors(hass: HomeAssistant, fleet) -> None:
    result = await _to_signin(hass)
    fleet.fail = fleet_mod.FleetAuthError("invalid token", 401)
    result = await _teslemetry(hass, result)
    assert result["step_id"] == "teslemetry" and result["errors"] == {"base": "invalid_token"}
    fleet.fail = fleet_mod.FleetError("HTTP 502", 502)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"access_token": "t"})
    assert result["errors"] == {"base": "cloud_error"}


async def test_no_site_on_the_account(hass: HomeAssistant, fleet) -> None:
    fleet.sites = [{"vin": "5YJ3..."}]
    result = await _teslemetry(hass, await _to_signin(hass))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_sites"


async def test_site_chosen_among_several(hass: HomeAssistant, tmp_path, fleet) -> None:
    """The gateway doesn't give its DIN to a password login on this firmware:
    with several sites, the user picks one."""
    fleet.sites = [{"energy_site_id": 7, "gateway_id": "OTHER", "site_name": "Cabin"},
                   {"energy_site_id": 42, "gateway_id": DIN, "site_name": "Home"}]
    keys = KeyServer(fleet, [3])
    with (keys.patch, _key(tmp_path), patch.object(config_flow, "_check_gateway", return_value=None),
          patch.object(config_flow, "VERIFY_POLL_SECONDS", 0), patch(SETUP, return_value=True)):
        result = await _teslemetry(hass, await _to_signin(hass))
        assert result["step_id"] == "site"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"site": "42"})
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["fleet_site_id"] == 42


async def test_site_with_another_powerwall(hass: HomeAssistant, fleet) -> None:
    fleet.sites = [{"energy_site_id": 7, "gateway_id": "OTHER", "site_name": "Cabin"}]
    result = await _teslemetry(hass, await _to_signin(hass))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "wrong_powerwall"


async def test_window_closes_and_registers_again(hass: HomeAssistant, tmp_path, fleet) -> None:
    keys = KeyServer(fleet, [2])
    with keys.patch, _key(tmp_path), patch.object(config_flow, "VERIFY_POLL_SECONDS", 0):
        result = await _teslemetry(hass, await _to_signin(hass))
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["step_id"] == "verify_failed"
    assert len(keys.added) == 2  # once more automatically


@pytest.mark.parametrize(("error", "reason"), [
    (fleet_mod.FleetError("HTTP 500", 500), "register_failed"),
    (fleet_mod.FleetError("device offline", 408), "register_unreachable"),
    (fleet_mod.FleetTimeout("Teslemetry did not answer in time"), "register_unreachable"),
    (fleet_mod.FleetAuthError("token expired", 401), "register_token_refused"),
])
async def test_registration_fails(hass: HomeAssistant, tmp_path, fleet, error, reason) -> None:
    with KeyServer(fleet, [1], fail=error).patch, _key(tmp_path):
        result = await _teslemetry(hass, await _to_signin(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


def test_unavailable_sign_in_reason() -> None:
    assert config_flow._register_failure(fleet_mod.FleetUnavailable("no energy_cmds")) \
        == "register_fleet_unavailable"
    assert config_flow._register_failure(fleet_mod.FleetNotRegistered("412", 412)) == "fleet_not_registered"


async def test_reauth_offers_the_fleet_sign_in(hass: HomeAssistant, fleet) -> None:
    """Home Assistant starts a plain reauth when Tesla refuses the refresh
    token: with a Fleet API sign-in, its menu offers signing in again."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={**V1R, **TESLEMETRY})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        entry.async_start_reauth(hass)
        await hass.async_block_till_done()
        flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
        result = await hass.config_entries.flow.async_configure(flow["flow_id"])
        assert result["menu_options"] == ["reauth_password", "register", "key_upload", "fleet_signin"]
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"next_step_id": "fleet_signin"})
        assert result["menu_options"] == ["fleet_oauth", "teslemetry"]
        result = await _teslemetry(hass, result, "tm-new")
    assert result["reason"] == "reauth_successful"
    assert entry.data["fleet_token"] == "tm-new"


def test_key_state_only_for_our_key() -> None:
    assert fleet_mod.key_state_from(_gateway_list((OTHER, 3), (PUB, 1)), PUB) == tesla_cloud.KEY_PENDING
    assert fleet_mod.key_state_from(_gateway_list((OTHER, 3)), PUB) is None
    # Teslemetry's reshaped list, with named states.
    teslemetry = {"response": {"clients": [
        {"public_key": _b64(OTHER), "state": "AUTHORIZED_CLIENT_STATE_VERIFIED"},
        {"public_key": _b64(PUB), "state": "AUTHORIZED_CLIENT_STATE_PENDING_VERIFICATION_TIMEOUT"}]}}
    assert fleet_mod.key_state_from(teslemetry, PUB) == tesla_cloud.KEY_TIMEOUT
    teslemetry["response"]["clients"][1]["state"] = "VERIFIED"
    assert fleet_mod.key_state_from(teslemetry, PUB) == tesla_cloud.KEY_VERIFIED
    teslemetry["response"]["clients"][1]["state"] = "2"
    assert fleet_mod.key_state_from(teslemetry, PUB) == tesla_cloud.KEY_TIMEOUT
    teslemetry["response"]["clients"][1]["state"] = True
    assert fleet_mod.key_state_from(teslemetry, PUB) is None
    teslemetry["response"]["clients"][1]["state"] = "SOMETHING_ELSE"
    assert fleet_mod.key_state_from(teslemetry, PUB) is None
    assert fleet_mod.key_state_from(None, PUB) is None


async def test_new_key_from_options_through_teslemetry(hass: HomeAssistant, tmp_path, fleet) -> None:
    """Options → Register a new signed key: a reauth that keeps the entry until
    the new key is confirmed; the sign-in made there joins the cloud backup."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, options={"cloud_backends": "fleet_then_owner"},
                            data=V1R)
    entry.add_to_hass(hass)
    keys = KeyServer(fleet, [1, 3])
    with (keys.patch, _key(tmp_path), patch.object(config_flow, "VERIFY_POLL_SECONDS", 0),
          patch(SETUP, return_value=True)):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "new_key"})
        assert result["reason"] == "new_key_started"
        await hass.async_block_till_done()
        flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert len(flows) == 1 and flows[0]["step_id"] == "new_key"
        flow_id = flows[0]["flow_id"]
        result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "register"})
        result = await _teslemetry(hass, result)
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert entry.data["rsa_key_path"] == "/old.pem"  # not before it's confirmed
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data["rsa_key_path"].endswith("k.pem")
    assert entry.data["fleet_token"] == "tm-token"
    assert entry.options == {"cloud_backends": "fleet_then_owner"}


async def _fleet_signin_flow(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entry.async_start_reauth(hass, data={**entry.data, config_flow.FLEET_SIGNIN: True})
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1 and flows[0]["step_id"] == "fleet_signin"
    return flows[0]["flow_id"]


async def test_fleet_sign_in_later_joins_the_owner_backup(hass: HomeAssistant, fleet) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data={
        **V1R, "owners_email": "a@b.c", "owners_authpath": "/tmp", "site_id": 42,
        "fleet_provider": "tesla_fleet", "token": {"access_token": "old"}})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        flow_id = await _fleet_signin_flow(hass, entry)
        result = await _teslemetry(hass, {"flow_id": flow_id})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    # The earlier sign-in's keys are replaced, the account kept.
    assert "token" not in entry.data and entry.data["fleet_provider"] == "teslemetry"
    assert entry.data["owners_email"] == "a@b.c"
    assert entry.options["cloud_backends"] == "owner_then_fleet"


async def test_fleet_sign_in_to_another_powerwall(hass: HomeAssistant, fleet) -> None:
    fleet.sites = [{"energy_site_id": 7, "gateway_id": "OTHER", "site_name": "Cabin"}]
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R)
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result = await _teslemetry(hass, {"flow_id": await _fleet_signin_flow(hass, entry)})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "wrong_powerwall"
    assert "fleet_provider" not in entry.data


# ---- Tesla Fleet: OAuth with the user's own developer app ----


TOKEN = {"access_token": _jwt({"ou_code": "EU", "scp": ["openid", "energy_device_data", "energy_cmds"]}),
         "refresh_token": "rt", "expires_in": 28800, "token_type": "Bearer"}


async def _add_app(hass: HomeAssistant) -> None:
    """The developer app, as Home Assistant's Add application credentials
    dialog saves it."""
    from homeassistant.components.application_credentials import (  # noqa: PLC0415
        ClientCredential,
        async_import_client_credential,
    )
    from homeassistant.setup import async_setup_component  # noqa: PLC0415

    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(hass, DOMAIN, ClientCredential("my-app", "s3cret", name="Tesla Fleet"))


async def _oauth(hass: HomeAssistant, aioclient_mock, flow_id: str, *, credentials: bool = True) -> dict:
    """fleet_oauth → pick the app → Tesla's sign-in page → back."""
    hass.config.components.add("my")  # redirect through my.home-assistant.io
    if credentials:
        await _add_app(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "fleet_oauth"})
    if result["step_id"] == "pick_implementation":
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"implementation": next(iter(result["data_schema"].schema["implementation"].container))})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"].startswith(fleet_mod.AUTHORIZE_URL)
    assert "energy_cmds" in result["url"] and "client_id=my-app" in result["url"]
    aioclient_mock.post(fleet_mod.TOKEN_URL, json=TOKEN)
    result = await hass.config_entries.flow.async_configure(flow_id, {
        "code": "abcd", "state": {"flow_id": flow_id, "redirect_uri": "https://my.home-assistant.io/redirect/oauth"}})
    assert result["type"] is FlowResultType.EXTERNAL_STEP_DONE
    return await hass.config_entries.flow.async_configure(flow_id)


async def test_register_through_tesla_fleet(hass: HomeAssistant, tmp_path, fleet, aioclient_mock) -> None:
    keys = KeyServer(fleet, [3])
    with (keys.patch, _key(tmp_path), patch.object(config_flow, "VERIFY_POLL_SECONDS", 0),
          patch(SETUP, return_value=True)):
        result = await _to_signin(hass)
        result = await _oauth(hass, aioclient_mock, result["flow_id"])
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["fleet_provider"] == "tesla_fleet" and data["fleet_site_id"] == 42
    assert data["fleet_server"] == fleet_mod.FLEET_SERVERS["eu"]  # the account's region
    assert data["token"]["refresh_token"] == "rt" and data["auth_implementation"]
    # The Fleet API's gateway command (passed through to the gateway).
    assert [name for name, _ in keys.added] == ["command"]
    message = keys.added[0][1]["command_properties"]["message"]["authorization"]
    assert message["add_authorized_client_request"]["public_key"] == _b64(PUB)


async def test_without_credentials_home_assistant_asks_for_them(hass: HomeAssistant) -> None:
    """No app added yet: the standard step stops with missing_credentials,
    which makes the frontend open its Add application credentials dialog."""
    from homeassistant.setup import async_setup_component  # noqa: PLC0415

    assert await async_setup_component(hass, "application_credentials", {})
    result = await _to_signin(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "fleet_oauth"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "missing_credentials"


async def test_tesla_fleet_sign_in_later(hass: HomeAssistant, tmp_path, fleet, aioclient_mock) -> None:
    await _add_app(hass)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R)
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        flow_id = await _fleet_signin_flow(hass, entry)
        result = await _oauth(hass, aioclient_mock, flow_id, credentials=False)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data["fleet_provider"] == "tesla_fleet"
    assert entry.options["cloud_backends"] == "tesla_fleet"


@pytest.mark.parametrize(("error", "reason"), [
    (fleet_mod.FleetAuthError("missing scopes", 401), "fleet_refused"),
    (fleet_mod.FleetNotRegistered("not registered", 412), "fleet_not_registered"),
    (fleet_mod.FleetError("HTTP 503", 503), "fleet_error"),
])
async def test_tesla_fleet_sign_in_refused(hass: HomeAssistant, fleet, aioclient_mock, error, reason) -> None:
    result = await _to_signin(hass)
    fleet.fail = error
    result = await _oauth(hass, aioclient_mock, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT and result["reason"] == reason


async def test_wifi_with_teslemetry_for_settings(hass: HomeAssistant, fleet) -> None:
    """Password only over the Powerwall's Wi-Fi, with a Fleet API sign-in so
    settings can be changed through the cloud: no key registration."""
    with patch(SETUP, return_value=True):
        result = await _gateway(hass, host="192.168.91.1")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "wifi"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.91.1", "cloud_backup": True})
        assert result["step_id"] == "signin"
        result = await _teslemetry(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["connection_mode"] == "wifi" and result["data"]["fleet_site_id"] == 42
    assert result["options"] == {"cloud_backends": "tesla_fleet"}
    assert not [call for call in fleet.calls if "authorized_client" in str(call)]


async def test_credentials_description(hass: HomeAssistant) -> None:
    from custom_components.tesla_tedapi import application_credentials  # noqa: PLC0415

    placeholders = await application_credentials.async_get_description_placeholders(hass)
    assert placeholders["redirect_url"] == "https://my.home-assistant.io/redirect/oauth"


async def test_new_key_not_offered_on_wifi_entries(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="x", data={"connection_mode": "wifi", "host": "192.168.91.1"})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "new_key" not in result["menu_options"]
