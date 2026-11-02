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


def _refused(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(request_info=None, history=(), status=status)  # type: ignore[arg-type]


@pytest.mark.parametrize(("error", "expected"), [
    (_refused(400), fleet_mod.FleetAuthError),
    (_refused(401), fleet_mod.FleetAuthError),
    (_refused(503), fleet_mod.FleetError),
    (aiohttp.ClientConnectionError("no route"), fleet_mod.FleetError),
])
async def test_token_refresh_errors(hass: HomeAssistant, error, expected) -> None:
    """Refused (400/401): sign in again. Anything else: temporary."""
    entry = MockConfigEntry(domain=DOMAIN, data={"fleet_provider": "tesla_fleet", "fleet_site_id": 42,
                                                 "fleet_server": SERVER, "token": {"access_token": "x"}})
    api = fleet_mod.entry_api(hass, entry)
    assert api.server == SERVER
    session = AsyncMock()
    session.async_ensure_token_valid.side_effect = error
    oauth2 = "homeassistant.helpers.config_entry_oauth2_flow"
    with (patch(f"{oauth2}.async_get_config_entry_implementation", return_value=object()),
          patch(f"{oauth2}.OAuth2Session", return_value=session),
          pytest.raises(expected) as err):
        await api._token()
    assert type(err.value) is expected


async def test_missing_or_unavailable_credentials(hass: HomeAssistant) -> None:
    from homeassistant.helpers import config_entry_oauth2_flow as oauth2  # noqa: PLC0415

    entry = MockConfigEntry(domain=DOMAIN, data={"fleet_provider": "tesla_fleet", "fleet_site_id": 42,
                                                 "auth_implementation": "gone", "token": {"access_token": "x"}})
    api = fleet_mod.entry_api(hass, entry)
    # The application credentials were removed: sign in again.
    with pytest.raises(fleet_mod.FleetAuthError):
        await api._token()
    # A credentials provider that is down (Home Assistant 2026.2+): temporary.
    with (patch(f"{oauth2.__name__}.async_get_config_entry_implementation",
                side_effect=oauth2.ImplementationUnavailableError("cloud down")),
          pytest.raises(fleet_mod.FleetError) as err):
        await api._token()
    assert type(err.value) is fleet_mod.FleetError
    # Older Home Assistant without that error: only a missing app counts.
    with (patch.object(oauth2, "ImplementationUnavailableError", None, create=False),
          patch(f"{oauth2.__name__}.async_get_config_entry_implementation", side_effect=ValueError("gone")),
          pytest.raises(fleet_mod.FleetAuthError)):
        await api._token()


async def test_app_not_registered_in_the_region(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{SERVER}/api/1/products", status=412, json={"error": "must be registered"})
    with pytest.raises(fleet_mod.FleetNotRegistered) as err:
        await _api(hass).energy_sites()
    assert err.value.status == 412


async def test_wrong_region_finds_the_right_one(hass: HomeAssistant, aioclient_mock) -> None:
    na, eu = fleet_mod.FLEET_SERVERS["na"], fleet_mod.FLEET_SERVERS["eu"]
    aioclient_mock.get(f"{na}/api/1/products", status=421, json={"error": "user out of region"})
    aioclient_mock.get(f"{na}/api/1/users/region", status=421)
    aioclient_mock.get(f"{eu}/api/1/users/region", json={"response": {"region": "eu", "fleet_api_base_url": eu}})
    aioclient_mock.get(f"{eu}/api/1/products", json={"response": [{"energy_site_id": 42}]})
    found: list[str] = []
    api = fleet_mod.FleetApi(async_get_clientsession(hass), fleet_mod.static_token("t"), na, "tesla_fleet",
                             found.append)
    assert [s.site_id for s in await api.energy_sites()] == [42]
    assert api.server == eu and found == [eu]


async def test_no_region_serves_the_account(hass: HomeAssistant, aioclient_mock) -> None:
    for server in fleet_mod.FLEET_SERVERS.values():
        aioclient_mock.get(f"{server}/api/1/users/region", status=421)
    aioclient_mock.get(f"{SERVER}/api/1/products", status=421)
    aioclient_mock.get(f"{SERVER}/api/1/users/region", status=421)
    api = _api(hass)
    with pytest.raises(fleet_mod.FleetWrongRegion):
        await api.energy_sites()
    assert api.server == SERVER
    # The right server answering without a URL keeps it.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{SERVER}/api/1/users/region", json={"response": {}})
    await api.find_server()
    assert api.server == SERVER
    # Teslemetry routes itself: no region lookup.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{SERVER}/api/1/products", status=421)
    with pytest.raises(fleet_mod.FleetWrongRegion):
        await _api(hass, "teslemetry").energy_sites()
    assert len(aioclient_mock.mock_calls) == 1


async def test_found_region_is_saved_without_a_reload(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"fleet_provider": "tesla_fleet", "fleet_site_id": 42,
                                                 "fleet_server": SERVER, "token": {"access_token": "x"}})
    entry.add_to_hass(hass)
    api = fleet_mod.entry_api(hass, entry)
    api._on_server("https://other.example")
    assert entry.data["fleet_server"] == "https://other.example"


async def test_never_waits_on_the_event_loop(hass: HomeAssistant) -> None:
    """Called from the event loop itself, it refuses at once instead of
    freezing Home Assistant while it waits for the loop."""
    client = fleet_mod.FleetBackup(hass, _api(hass), 42)
    with pytest.raises(RuntimeError, match="worker thread"):
        client.get_mode()


@pytest.mark.parametrize(("metadata", "server"), [
    ({"region": "na", "scopes": []}, "https://na.teslemetry.com"),
    ({"region": "../evil"}, SERVER),
    ({}, SERVER),
    (["list"], SERVER),
])
async def test_teslemetry_region(hass: HomeAssistant, aioclient_mock, metadata, server) -> None:
    aioclient_mock.get(f"{SERVER}/api/metadata", json=metadata)
    api = _api(hass, "teslemetry")
    await api.use_teslemetry_region()
    assert api.server == server
