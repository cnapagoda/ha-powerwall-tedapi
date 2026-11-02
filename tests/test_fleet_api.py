"""The Fleet API client (HTTP) and the sign-in kept in the entry."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import fleet as fleet_mod
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import TESLEMETRY
from .test_fleet_register import _jwt

SERVER = "https://fleet.example"
SITE = f"{SERVER}/api/1/energy_sites/42"


def _api(hass: HomeAssistant, provider: str = "tesla_fleet", token: str = "tok") -> fleet_mod.FleetApi:
    return fleet_mod.FleetApi(async_get_clientsession(hass), fleet_mod.static_token(token), SERVER + "/", provider)


async def test_requests_and_answers(hass: HomeAssistant, aioclient_mock) -> None:
    api = _api(hass)
    aioclient_mock.get(f"{SERVER}/api/1/products", json={"response": [
        {"energy_site_id": 42, "gateway_id": "DIN1", "site_name": "Home"},
        {"energy_site_id": 7},
        {"vin": "5YJ3"}, "junk"]})
    sites = await api.energy_sites()
    assert [(s.site_id, s.din, s.name) for s in sites] == [(42, "DIN1", "Home"), (7, "", "7")]
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer tok"

    aioclient_mock.get(f"{SITE}/site_info", json={"response": {"default_real_mode": "backup"}})
    assert await api.site_info(42) == {"default_real_mode": "backup"}
    aioclient_mock.get(f"{SITE}/live_status", json={"response": None})
    assert await api.live_status(42) == {}
    aioclient_mock.post(f"{SITE}/backup", json={"response": {"code": 201}})
    assert await api.post(42, "backup", {"backup_reserve_percent": 20}) == {"response": {"code": 201}}
    assert aioclient_mock.mock_calls[-1][2] == {"backup_reserve_percent": 20}


async def test_unexpected_bodies(hass: HomeAssistant, aioclient_mock) -> None:
    api = _api(hass)
    aioclient_mock.get(f"{SERVER}/api/1/products", text="not json")
    assert await api.energy_sites() == []
    aioclient_mock.get(f"{SITE}/site_info", json=["list"])
    assert await api.site_info(42) == {}


@pytest.mark.parametrize(("kwargs", "error", "status"), [
    ({"status": 401, "json": {"error": "invalid bearer token"}}, fleet_mod.FleetAuthError, 401),
    ({"status": 401, "text": ""}, fleet_mod.FleetAuthError, 401),
    ({"status": 500, "json": {"error": "internal"}}, fleet_mod.FleetError, 500),
    ({"status": 408, "text": "offline"}, fleet_mod.FleetError, 408),
    ({"status": 200, "json": {"error": "vehicle unavailable"}}, fleet_mod.FleetError, 200),
])
async def test_errors(hass: HomeAssistant, aioclient_mock, kwargs, error, status) -> None:
    aioclient_mock.get(f"{SITE}/site_info", **kwargs)
    with pytest.raises(error) as err:
        await _api(hass).site_info(42)
    assert err.value.status == status


async def test_no_answer(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{SITE}/site_info", exc=TimeoutError())
    with pytest.raises(fleet_mod.FleetTimeout, match="Teslemetry"):
        await _api(hass, "teslemetry").site_info(42)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{SITE}/site_info", exc=aiohttp.ClientError())
    with pytest.raises(fleet_mod.FleetError, match="ClientError"):
        await _api(hass).site_info(42)


async def test_scopes(hass: HomeAssistant, aioclient_mock) -> None:
    # Tesla Fleet: from the token; Teslemetry: from its metadata.
    assert await _api(hass, token=_jwt({"scp": ["openid", "energy_cmds"]})).scopes() == {"openid", "energy_cmds"}
    assert await _api(hass, token="not-a-jwt").scopes() is None
    aioclient_mock.get(f"{SERVER}/api/metadata", json={"scopes": ["energy_device_data"], "region": "na"})
    assert await _api(hass, "teslemetry").scopes() == {"energy_device_data"}


async def test_authorized_clients(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(f"{SITE}/command", json={"response": {}})
    await _api(hass).add_authorized_client(42, b"pub")
    await _api(hass).list_authorized_clients(42)
    bodies = [call[2] for call in aioclient_mock.mock_calls]
    assert [list(b["command_properties"]["message"]["authorization"]) for b in bodies] == [
        ["add_authorized_client_request"], ["list_authorized_clients_request"]]
    assert all(b["command_type"] == "grpc_command" for b in bodies)
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{SITE}/command/add_authorized_client", json={"response": {}})
    aioclient_mock.get(f"{SITE}/command/authorized_clients", json={"response": {"clients": []}})
    await _api(hass, "teslemetry").add_authorized_client(42, b"pub")
    assert await _api(hass, "teslemetry").list_authorized_clients(42) == {"response": {"clients": []}}


def test_token_claims_and_region() -> None:
    assert fleet_mod.token_claims(_jwt({"ou_code": "EU"})) == {"ou_code": "EU"}
    assert fleet_mod.token_claims("a.!!!.c") == {}
    assert fleet_mod.token_claims(_jwt(["list"])) == {}  # type: ignore[arg-type]
    assert fleet_mod.fleet_server(_jwt({"ou_code": "CN"})) == fleet_mod.FLEET_SERVERS["cn"]
    assert fleet_mod.fleet_server("opaque") == fleet_mod.FLEET_SERVERS["na"]
    assert fleet_mod.provider_label("teslemetry") == "Teslemetry"
    assert fleet_mod.provider_label(None) == "Tesla Fleet"


def test_signed_in() -> None:
    assert fleet_mod.signed_in(TESLEMETRY)
    assert not fleet_mod.signed_in({**TESLEMETRY, "fleet_token": ""})
    assert not fleet_mod.signed_in({**TESLEMETRY, "fleet_site_id": None})
    assert fleet_mod.signed_in({"fleet_provider": "tesla_fleet", "fleet_site_id": 1, "token": {}})
    assert not fleet_mod.signed_in({"fleet_provider": "tesla_fleet", "fleet_site_id": 1})
    assert not fleet_mod.signed_in({})


async def test_entry_api(hass: HomeAssistant) -> None:
    assert fleet_mod.entry_api(hass, MockConfigEntry(domain=DOMAIN, data={})) is None
    teslemetry = fleet_mod.entry_api(hass, MockConfigEntry(domain=DOMAIN, data={**TESLEMETRY, "fleet_server": ""}))
    assert teslemetry.server == fleet_mod.TESLEMETRY_SERVER and teslemetry.teslemetry
    assert await teslemetry._token() == "tm-token"


async def test_entry_api_refreshes_the_tesla_fleet_token(hass: HomeAssistant) -> None:
    token = _jwt({"ou_code": "EU"})
    entry = MockConfigEntry(domain=DOMAIN, data={"fleet_provider": "tesla_fleet", "fleet_site_id": 42,
                                                 "auth_implementation": "tesla_tedapi_x",
                                                 "token": {"access_token": token}})
    api = fleet_mod.entry_api(hass, entry)
    assert api.server == fleet_mod.FLEET_SERVERS["eu"]  # from the token when not stored
    session = AsyncMock()
    session.token = {"access_token": "fresh"}
    oauth2 = "homeassistant.helpers.config_entry_oauth2_flow"
    with (patch(f"{oauth2}.async_get_config_entry_implementation", return_value=object()) as impl,
          patch(f"{oauth2}.OAuth2Session", return_value=session)):
        assert await api._token() == "fresh"
        assert await api._token() == "fresh"
    impl.assert_called_once()  # the session is kept
    assert session.async_ensure_token_valid.await_count == 2


@pytest.mark.parametrize(("error", "expected"), [
    (ValueError("Implementation not available"), fleet_mod.FleetAuthError),
    (type("ImplementationUnavailableError", (Exception,), {})(), fleet_mod.FleetAuthError),
    (type("OAuth2TokenRequestReauthError", (Exception,), {})(), fleet_mod.FleetAuthError),
    (type("Refused", (Exception,), {"status": 400})(), fleet_mod.FleetAuthError),
    (type("Busy", (Exception,), {"status": 503})(), fleet_mod.FleetError),
])
async def test_entry_api_token_errors(hass: HomeAssistant, error, expected) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"fleet_provider": "tesla_fleet", "fleet_site_id": 42,
                                                 "fleet_server": SERVER, "token": {"access_token": "x"}})
    api = fleet_mod.entry_api(hass, entry)
    assert api.server == SERVER
    with (patch("homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
                side_effect=error),
          pytest.raises(expected) as err):
        await api._token()
    assert type(err.value) is expected
