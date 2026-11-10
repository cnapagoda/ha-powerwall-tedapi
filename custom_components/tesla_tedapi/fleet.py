"""Cloud backup over Tesla's Fleet API, with a sign-in made here.

Two ways in, both kept in this integration's own config entry:
  * Tesla Fleet: OAuth with the user's own Tesla developer app (Home
    Assistant application credentials). The token is refreshed here.
  * Teslemetry: an access token from the Teslemetry console.

Both reach the same Fleet API energy-site endpoints. The same sign-in can
register the signed (v1r) key, so setup needs no Owner API sign-in.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
import json
import re
import threading
import time
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cloud import site_extras
from .const import (
    CONF_FLEET_PROVIDER,
    CONF_FLEET_SERVER,
    CONF_FLEET_SITE_ID,
    CONF_FLEET_TOKEN,
    CONF_OAUTH_TOKEN,
    PROVIDER_TESLA_FLEET,
    PROVIDER_TESLEMETRY,
)
from .tesla_cloud import EnergySite

AUTHORIZE_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
SCOPES = ("openid", "offline_access", "energy_device_data", "energy_cmds")
SCOPE_COMMANDS = "energy_cmds"
FLEET_SERVERS = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    "cn": "https://fleet-api.prd.cn.vn.cloud.tesla.cn",
}
TESLEMETRY_SERVER = "https://api.teslemetry.com"
# Teslemetry's regional servers: https://{region}.teslemetry.com.
TESLEMETRY_REGION = re.compile(r"[a-z]{2,8}")
LABELS = {PROVIDER_TESLEMETRY: "Teslemetry"}
CALL_TIMEOUT = 30
# How long a worker thread waits for a request on the event loop.
RUN_TIMEOUT = CALL_TIMEOUT + 5
# One site_info read serves the settings read (four values) and Storm Watch.
SITE_INFO_SECONDS = 30


class FleetError(Exception):
    """Tesla (or Teslemetry) refused a request, or it didn't get through."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class FleetAuthError(FleetError):
    """The sign-in is no longer accepted: sign in again."""


class FleetNotRegistered(FleetError):
    """HTTP 412: the developer app's domain isn't registered with Tesla in
    the account's region (Tesla's partner registration)."""


class FleetWrongRegion(FleetError):
    """HTTP 421: the account is served from another region's server."""


class FleetUnavailable(Exception):
    """No usable Fleet API sign-in (none, or without the energy_cmds scope)."""


class FleetTimeout(Exception):
    """A command got no answer in time; it may still have been applied."""


def provider_label(provider: str | None) -> str:
    return LABELS.get(provider or "", "Tesla Fleet")


def token_claims(access_token: str) -> dict[str, Any]:
    """The claims of a Tesla access token (a JWT), unverified: only used to
    pick the region and see the scopes."""
    try:
        payload = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def fleet_server(access_token: str) -> str:
    """The Fleet API server for the account's region (ou_code in the token)."""
    region = str(token_claims(access_token).get("ou_code") or "").lower()
    return FLEET_SERVERS.get(region, FLEET_SERVERS["na"])


class FleetApi:
    """The few Fleet API calls used here (async)."""

    def __init__(self, session: aiohttp.ClientSession, token: Callable[[], Awaitable[str]],
                 server: str, provider: str, on_server: Callable[[str], None] | None = None) -> None:
        self._session = session
        self._token = token
        self.server = server.rstrip("/")
        self.provider = provider
        # Told when another region's server turns out to be the right one.
        self._on_server = on_server

    @property
    def teslemetry(self) -> bool:
        return self.provider == PROVIDER_TESLEMETRY

    async def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """One call. Tesla Fleet answers 421 from the wrong region's server:
        then the account's region is looked up once and the call repeated."""
        try:
            return await self._send(method, path, body)
        except FleetWrongRegion:
            if self.teslemetry:
                raise
            await self.find_server()
            return await self._send(method, path, body)

    async def find_server(self) -> None:
        """The account's own server, from Tesla's region endpoint (asked on
        each regional server until one answers, as Home Assistant's Tesla
        Fleet integration does)."""
        tried = self.server
        for server in [tried, *(s for s in FLEET_SERVERS.values() if s != tried)]:
            self.server = server
            try:
                resp = await self._send("GET", "api/1/users/region")
            except FleetWrongRegion:
                continue
            found = (resp.get("response") or {}).get("fleet_api_base_url") if isinstance(resp, dict) else None
            self.server = str(found or server).rstrip("/")
            if self.server != tried and self._on_server is not None:
                self._on_server(self.server)
            return
        self.server = tried
        raise FleetWrongRegion("no Tesla region serves this account", 421)

    async def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        token = await self._token()
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                async with self._session.request(
                        method, f"{self.server}/{path}", json=body,
                        headers={"Authorization": f"Bearer {token}"}) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except ValueError:
                        data = None
                    status = resp.status
        except TimeoutError as err:
            raise FleetTimeout(f"{provider_label(self.provider)} did not answer in time") from err
        except aiohttp.ClientError as err:
            raise FleetError(str(err) or type(err).__name__) from err
        error = data.get("error") if isinstance(data, dict) else None
        if status == 401:
            raise FleetAuthError(str(error or "token not accepted"), status)
        if status == 412:
            raise FleetNotRegistered(str(error or "developer app not registered in this region"), status)
        if status == 421:
            raise FleetWrongRegion(str(error or "wrong region"), status)
        if status >= 400 or error:
            raise FleetError(str(error or f"HTTP {status}"), status)
        return data

    async def energy_sites(self) -> list[EnergySite]:
        resp = await self.request("GET", "api/1/products")
        products = resp.get("response") if isinstance(resp, dict) else None
        return [EnergySite(int(p["energy_site_id"]), str(p.get("gateway_id") or ""),
                           str(p.get("site_name") or p["energy_site_id"]))
                for p in products or [] if isinstance(p, dict) and "energy_site_id" in p]

    async def site_info(self, site_id: int) -> dict[str, Any]:
        resp = await self.request("GET", f"api/1/energy_sites/{site_id}/site_info")
        info = resp.get("response") if isinstance(resp, dict) else None
        return info if isinstance(info, dict) else {}

    async def live_status(self, site_id: int) -> dict[str, Any]:
        resp = await self.request("GET", f"api/1/energy_sites/{site_id}/live_status")
        live = resp.get("response") if isinstance(resp, dict) else None
        return live if isinstance(live, dict) else {}

    async def post(self, site_id: int, endpoint: str, body: dict[str, Any]) -> Any:
        return await self.request("POST", f"api/1/energy_sites/{site_id}/{endpoint}", body)

    async def scopes(self) -> set[str] | None:
        """The sign-in's scopes, or None when they can't be told."""
        if self.teslemetry:
            resp = await self.request("GET", "api/metadata")
            scopes = resp.get("scopes") if isinstance(resp, dict) else None
        else:
            scopes = token_claims(await self._token()).get("scp")
        return {str(scope) for scope in scopes} if isinstance(scopes, list) else None

    async def use_teslemetry_region(self) -> None:
        """Teslemetry: switch to the account's regional server, as named by
        its metadata (read once, at sign-in). Also checks the token."""
        resp = await self.request("GET", "api/metadata")
        region = str(resp.get("region") or "").lower() if isinstance(resp, dict) else ""
        if TESLEMETRY_REGION.fullmatch(region):
            self.server = f"https://{region}.teslemetry.com"

    async def add_authorized_client(self, site_id: int, public_der: bytes) -> Any:
        message = {"key_type": 1, "public_key": base64.b64encode(public_der).decode(),
                   "authorized_client_type": 1, "description": "Home Assistant"}
        if self.teslemetry:
            # Teslemetry's own endpoint for the gateway's authorization commands.
            return await self.post(site_id, "command/add_authorized_client", message)
        return await self._grpc(site_id, {"add_authorized_client_request": message})

    async def list_authorized_clients(self, site_id: int) -> Any:
        if self.teslemetry:
            return await self.request("GET", f"api/1/energy_sites/{site_id}/command/authorized_clients")
        return await self._grpc(site_id, {"list_authorized_clients_request": {}})

    async def _grpc(self, site_id: int, message: dict[str, Any]) -> Any:
        return await self.post(site_id, "command", {
            "command_type": "grpc_command",
            "command_properties": {"message": {"authorization": message}, "identifier_type": 1},
        })


def signed_in(data: Any) -> bool:
    """The entry has a Fleet API sign-in made here."""
    provider = data.get(CONF_FLEET_PROVIDER)
    if not data.get(CONF_FLEET_SITE_ID):
        return False
    if provider == PROVIDER_TESLEMETRY:
        return bool(data.get(CONF_FLEET_TOKEN))
    return provider == PROVIDER_TESLA_FLEET and isinstance(data.get(CONF_OAUTH_TOKEN), dict)


def static_token(token: str) -> Callable[[], Awaitable[str]]:
    async def get() -> str:
        return token
    return get


def entry_api(hass: HomeAssistant, entry: ConfigEntry) -> FleetApi | None:
    """The Fleet API client for an entry's sign-in, or None without one. A
    Tesla Fleet token is refreshed here (and saved in the entry) when needed."""
    data = entry.data
    if not signed_in(data):
        return None
    session = async_get_clientsession(hass)
    provider = data[CONF_FLEET_PROVIDER]
    if provider == PROVIDER_TESLEMETRY:
        return FleetApi(session, static_token(data[CONF_FLEET_TOKEN]),
                        data.get(CONF_FLEET_SERVER) or TESLEMETRY_SERVER, provider)
    oauth: dict[str, Any] = {}

    async def token() -> str:
        from homeassistant.helpers import config_entry_oauth2_flow as oauth2  # noqa: PLC0415

        # A credentials provider that is down for now (newer Home Assistant;
        # looked up so older releases without it still work).
        unavailable = getattr(oauth2, "ImplementationUnavailableError", None) or ()
        if "session" not in oauth:
            try:
                implementation = await oauth2.async_get_config_entry_implementation(hass, entry)
            except unavailable as err:
                raise FleetError("the application credentials are not available yet") from err
            except ValueError as err:
                # The application credentials were removed (Home Assistant's
                # UnknownImplementationError is a ValueError too): sign in again.
                raise FleetAuthError("the Tesla developer app's credentials are gone") from err
            oauth["session"] = oauth2.OAuth2Session(hass, entry, implementation)
        try:
            await oauth["session"].async_ensure_token_valid()
        except aiohttp.ClientResponseError as err:
            # Tesla refused the refresh token: sign in again. Other statuses
            # (busy, server errors) pass.
            if err.status in (400, 401):
                raise FleetAuthError("Tesla refused the refresh token", err.status) from err
            raise FleetError(f"token refresh failed: HTTP {err.status}", err.status) from err
        except aiohttp.ClientError as err:
            raise FleetError(f"token refresh failed: {err or type(err).__name__}") from err
        return str(oauth["session"].token["access_token"])

    def on_server(server: str) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_FLEET_SERVER: server})

    server = data.get(CONF_FLEET_SERVER) or fleet_server(str(data[CONF_OAUTH_TOKEN].get("access_token") or ""))
    return FleetApi(session, token, server, provider, on_server)


class FleetBackup:
    """pypowerwall-like settings client over the Fleet API. Called from
    executor threads; the requests run on the event loop."""

    mode = "cloud"

    def __init__(self, hass: HomeAssistant, api: FleetApi, site_id: int) -> None:
        self._hass = hass
        self._api = api
        self._site_id = site_id
        self.client = self  # pypowerwall exposes .client; checked for None
        self._info: tuple[float, dict[str, Any]] | None = None
        self._scopes: set[str] | None = None
        self._scopes_read = False

    def _run(self, call: Callable[[FleetApi], Awaitable[Any]]) -> Any:
        """Run a request on the event loop and wait for it, from a worker
        thread only: waiting on the loop's own thread would freeze it."""
        if threading.get_ident() == self._hass.loop_thread_id:
            raise RuntimeError("FleetBackup must be called from a worker thread, not the event loop")
        future = asyncio.run_coroutine_threadsafe(call(self._api), self._hass.loop)
        try:
            return future.result(RUN_TIMEOUT)
        except TimeoutError as err:
            future.cancel()
            raise FleetTimeout(f"{provider_label(self._api.provider)} did not answer in time") from err

    @property
    def can_write(self) -> bool:
        """The sign-in includes the energy_cmds scope (assumed until read)."""
        return self._scopes is None or SCOPE_COMMANDS in self._scopes

    def _site_info(self, fresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not fresh and self._info is not None and now - self._info[0] < SITE_INFO_SECONDS:
            return self._info[1]
        info = self._run(lambda api: api.site_info(self._site_id))
        self._info = (now, info)
        if not self._scopes_read:
            self._scopes_read = True
            try:
                self._scopes = self._run(lambda api: api.scopes())
            except (FleetError, FleetTimeout):
                self._scopes_read = False  # asked again with the next read
        return info

    # -- reads (pypowerwall names) ---------------------------------------

    def get_mode(self) -> str | None:
        return self._site_info().get("default_real_mode")

    def get_reserve(self) -> float | None:
        value = self._site_info().get("backup_reserve_percent")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def get_grid_charging(self) -> bool | None:
        info = self._site_info()
        if not info:
            return None
        return not (info.get("components") or {}).get("disallow_charge_from_grid_with_solar_installed", False)

    def get_grid_export(self) -> str | None:
        info = self._site_info()
        if not info:
            return None
        # non_export_configured is not used: it stays true on sites that do
        # export with "battery_ok".
        return (info.get("components") or {}).get("customer_preferred_export_rule") or "battery_ok"

    def get_extras(self) -> dict[str, Any]:
        """Storm Watch and other cloud-only values."""
        info = self._site_info()
        if not info:
            return {}
        live = self._run(lambda api: api.live_status(self._site_id))
        return site_extras(info, info.get("user_settings") or {}, info.get("components") or {}, live)

    def get_live(self) -> dict[str, Any]:
        """The site's live status (power, battery level, Wall Connectors)."""
        return self._run(lambda api: api.live_status(self._site_id))

    def get_tariff(self) -> dict[str, Any] | None:
        """The site's tariff (tariff_content, else tariff_content_v2), read fresh."""
        info = self._site_info(fresh=True)
        tariff = info.get("tariff_content") or info.get("tariff_content_v2")
        return tariff if isinstance(tariff, dict) and tariff else None

    # -- writes ----------------------------------------------------------

    def _command(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.can_write:
            raise FleetUnavailable(f"the {provider_label(self._api.provider)} sign-in has no energy_cmds scope")
        result = self._run(lambda api: api.post(self._site_id, endpoint, body))
        self._info = None  # read the change next time
        return result if isinstance(result, dict) and result else {"ok": True}

    def set_tariff(self, tou_settings: dict[str, Any]) -> dict[str, Any]:
        """Write {"tariff_content_v2": ...}. Never send an empty one: Tesla has
        been seen to accept that and delete the site's time-of-use settings."""
        content = tou_settings.get("tariff_content_v2")
        if not isinstance(content, dict) or not content:
            raise ValueError("tariff_content_v2 must be a non-empty object")
        return self._command("time_of_use_settings", {"tou_settings": {"tariff_content_v2": content}})

    def set_mode(self, mode: str) -> dict[str, Any]:
        return self._command("operation", {"default_real_mode": mode})

    def set_reserve(self, level: int) -> dict[str, Any]:
        return self._command("backup", {"backup_reserve_percent": int(level)})

    def set_grid_charging(self, enabled: bool) -> dict[str, Any]:
        return self._command("grid_import_export",
                             {"disallow_charge_from_grid_with_solar_installed": not enabled})

    def set_grid_export(self, mode: str) -> dict[str, Any]:
        return self._command("grid_import_export", {"customer_preferred_export_rule": mode})

    def set_storm_watch(self, enabled: bool) -> dict[str, Any]:
        return self._command("storm_mode", {"enabled": bool(enabled)})

    def set_off_grid_reserve(self, percent: int) -> dict[str, Any]:
        return self._command("off_grid_vehicle_charging_reserve",
                             {"off_grid_vehicle_charging_reserve_percent": int(percent)})


# -- key registration --------------------------------------------------

# Gateway key states, also when a response names them instead of numbering them.
_STATE_NAMES = (("TIMEOUT", 2), ("REMOVED", 4), ("PENDING_VERIFICATION", 1), ("VERIFIED", 3))


def _state(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value.isdigit():
            return int(value)
        for name, state in _STATE_NAMES:
            if value.upper().endswith(name):
                return state
    return None


def _find_client(node: Any, key_b64: str) -> int | None:
    """The state of the client with this public key, anywhere in a response
    (the Fleet API passes the gateway's message through; Teslemetry reshapes it)."""
    if isinstance(node, dict):
        key = node.get("public_key") or node.get("PublicKey") or node.get("publicKey")
        if key == key_b64:
            return _state(node.get("state") if "state" in node else node.get("State"))
        for value in node.values():
            found = _find_client(value, key_b64)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_client(value, key_b64)
            if found is not None:
                return found
    return None


def key_state_from(resp: Any, public_der: bytes) -> int | None:
    """This key's state in an add or list response, never another key's."""
    found = _find_client(resp, base64.b64encode(public_der).decode())
    if found is not None:
        return found
    from pypowerwall.v1r_register import _check_key_state  # noqa: PLC0415

    # An add response may name the new client without its key.
    return _check_key_state(resp, pubkey_der=public_der) if isinstance(resp, dict) else None


async def async_register_key(api: FleetApi, site_id: int, public_der: bytes) -> int | None:
    """Ask the gateway to trust this key. Returns the key state if reported.
    Registering the same key again reopens an expired confirmation window."""
    return key_state_from(await api.add_authorized_client(site_id, public_der), public_der)


async def async_key_state(api: FleetApi, site_id: int, public_der: bytes) -> int | None:
    return key_state_from(await api.list_authorized_clients(site_id), public_der)
