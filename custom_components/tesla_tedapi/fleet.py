"""Cloud backup through the Tesla Fleet (or Teslemetry) integration.

The Owner API refuses Powerwall access for browser sign-ins (HTTP 403 since
2026-10). Home Assistant's Tesla Fleet and Teslemetry integrations already hold
a working Fleet API session and read each energy site's site_info every minute.
This uses that: settings are read from their site data (no extra Tesla calls)
and changed through their energy site API object, so it can stand in for the
Owner API client. The same API object can register the signed (v1r) key, so
setup needs no Tesla sign-in here.

The token is never refreshed here: their own session does that, under its
lock, so using it can't log them out.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
import time
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .cloud import site_extras

FLEET_DOMAINS = ("tesla_fleet", "teslemetry")
CALL_TIMEOUT = 30
# Tesla Fleet entries in these states may still load: waiting, not failing,
# for at most STARTING_SECONDS after this integration started.
STARTING = (ConfigEntryState.NOT_LOADED, ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.SETUP_RETRY)
STARTING_SECONDS = 600
SCOPE_COMMANDS = "energy_cmds"


class FleetUnavailable(Exception):
    """No loaded Tesla Fleet energy site with this Powerwall."""


class FleetTimeout(Exception):
    """A command got no answer in time; it may still have been applied."""


class FleetRegisterError(Exception):
    """Tesla Fleet did not register the key, or its state could not be read."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def fleet_entries(hass: HomeAssistant) -> list:
    """Tesla Fleet and Teslemetry entries that are not disabled."""
    return [entry for domain in FLEET_DOMAINS for entry in hass.config_entries.async_entries(domain)
            if entry.disabled_by is None]


def _site_dins(data: dict[str, Any]) -> list[str]:
    """Every serial a site's data names: the site id (the gateway DIN), the
    gateways and the batteries (flattened site_info, or the product before
    the first refresh)."""
    dins = [data.get("id"), data.get("gateway_id")]
    for key in ("components_gateways", "components_batteries"):
        dins += [item.get("din") for item in data.get(key) or [] if isinstance(item, dict)]
    return [str(din) for din in dins if din]


def find_site(hass: HomeAssistant, din: str | None) -> tuple[Any, Any] | None:
    """(entry, energy site) for this Powerwall among the loaded Tesla Fleet
    and Teslemetry entries. A site naming this DIN wins; otherwise a single
    site is used if it names no serial at all (or the DIN isn't known)."""
    sites = [(entry, site) for entry in fleet_entries(hass) if entry.state is ConfigEntryState.LOADED
             for site in getattr(entry.runtime_data, "energysites", None) or []]
    for entry, site in sites:
        if din and din in _site_dins(_site_data(site)):
            return entry, site
    if len(sites) == 1 and (not din or not _site_dins(_site_data(sites[0][1]))):
        return sites[0]
    return None


def fleet_available(hass: HomeAssistant, din: str | None) -> bool:
    return find_site(hass, din) is not None


def fleet_starting(hass: HomeAssistant, din: str | None, since: float) -> bool:
    """Not usable yet but may still load (after a restart it can start later
    than this integration), for at most STARTING_SECONDS. No entry at all, a
    disabled or failed one, or a site with another Powerwall is not."""
    if time.monotonic() - since > STARTING_SECONDS or find_site(hass, din) is not None:
        return False
    return any(entry.state in STARTING for entry in fleet_entries(hass))


def _site_data(site: Any) -> dict[str, Any]:
    coordinator = getattr(site, "info_coordinator", None)
    return (getattr(coordinator, "data", None) or {}) if coordinator else {}


def _can_command(entry: Any) -> bool:
    scopes = getattr(entry.runtime_data, "scopes", None)
    return scopes is None or SCOPE_COMMANDS in {str(scope) for scope in scopes}


class FleetBackup:
    """pypowerwall-like settings client over Tesla Fleet or Teslemetry.
    Called from executor threads: reads use their stored site data, commands
    run on the event loop."""

    mode = "cloud"

    def __init__(self, hass: HomeAssistant, din: Callable[[], str | None]) -> None:
        self._hass = hass
        self._din = din  # the gateway DIN, read when needed (known after setup)
        self.client = self  # pypowerwall exposes .client; checked for None

    def _site(self) -> tuple[Any, Any]:
        found = find_site(self._hass, self._din())
        if found is None:
            raise FleetUnavailable("no loaded Tesla Fleet or Teslemetry energy site has this Powerwall")
        return found

    @property
    def can_write(self) -> bool:
        """The sign-in includes the energy_cmds scope (False without a site)."""
        found = find_site(self._hass, self._din())
        return found is not None and _can_command(found[0])

    def _site_and_data(self) -> tuple[Any, dict[str, Any]]:
        _, site = self._site()
        coordinator = site.info_coordinator
        if not coordinator.last_update_success:
            raise FleetUnavailable("the Tesla Fleet site data is not updating")
        data = coordinator.data or {}
        # Before its first site_info read the coordinator holds the product
        # summary, which has no settings.
        return site, (data if "default_real_mode" in data else {})

    def _data(self) -> dict[str, Any]:
        return self._site_and_data()[1]

    def get_extras(self) -> dict[str, Any]:
        """Storm Watch and other cloud-only values from their site data
        (flattened site_info) and live status."""
        site, data = self._site_and_data()
        if not data:
            return {}
        live_coordinator = getattr(site, "live_coordinator", None)
        live = (live_coordinator.data or {}) if live_coordinator and live_coordinator.last_update_success else {}

        def section(prefix: str) -> dict[str, Any]:
            return {k[len(prefix):]: v for k, v in data.items() if k.startswith(prefix)}

        return site_extras(data, section("user_settings_"), section("components_"), live)

    # -- reads (pypowerwall names) ---------------------------------------

    def get_mode(self) -> str | None:
        return self._data().get("default_real_mode")

    def get_reserve(self) -> float | None:
        value = self._data().get("backup_reserve_percent")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def get_grid_charging(self) -> bool | None:
        data = self._data()
        if not data:
            return None
        return not data.get("components_disallow_charge_from_grid_with_solar_installed", False)

    def get_grid_export(self) -> str | None:
        data = self._data()
        if not data:
            return None
        # components_non_export_configured is not used: it stays true on sites
        # that do export with "battery_ok".
        return data.get("components_customer_preferred_export_rule") or "battery_ok"

    # -- writes ----------------------------------------------------------

    def _command(self, call: Callable[[Any], Any]) -> dict[str, Any]:
        entry, site = self._site()
        if not _can_command(entry):
            raise FleetUnavailable("the Tesla Fleet sign-in has no energy_cmds scope")

        async def send() -> Any:
            result = await call(site.api)
            # Pick up the change on their next refresh instead of in a minute.
            await site.info_coordinator.async_request_refresh()
            return result

        future = asyncio.run_coroutine_threadsafe(send(), self._hass.loop)
        try:
            result = future.result(CALL_TIMEOUT)
        except TimeoutError as err:
            future.cancel()
            raise FleetTimeout("Tesla did not answer in time") from err
        return result if isinstance(result, dict) and result else {"ok": True}

    def set_mode(self, mode: str) -> dict[str, Any]:
        return self._command(lambda api: api.operation(mode))

    def set_reserve(self, level: int) -> dict[str, Any]:
        return self._command(lambda api: api.backup(int(level)))

    def set_grid_charging(self, enabled: bool) -> dict[str, Any]:
        return self._command(lambda api: api.grid_import_export(
            disallow_charge_from_grid_with_solar_installed=not enabled))

    def set_grid_export(self, mode: str) -> dict[str, Any]:
        return self._command(lambda api: api.grid_import_export(customer_preferred_export_rule=mode))

    def set_storm_watch(self, enabled: bool) -> dict[str, Any]:
        return self._command(lambda api: api.storm_mode(bool(enabled)))

    def set_off_grid_reserve(self, percent: int) -> dict[str, Any]:
        return self._command(lambda api: api.off_grid_vehicle_charging_reserve(int(percent)))


# -- key registration --------------------------------------------------

# Gateway key states (tesla_fleet_api AuthorizedClientState), also when a
# response names them instead of numbering them.
_STATE_NAMES = (("TIMEOUT", 2), ("REMOVED", 4), ("PENDING_VERIFICATION", 1), ("VERIFIED", 3))


def fleet_can_register(hass: HomeAssistant, din: str | None) -> bool:
    """A loaded Tesla Fleet or Teslemetry site with this Powerwall, signed in
    with the energy_cmds scope."""
    found = find_site(hass, din)
    return found is not None and _can_command(found[0])


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
    (Tesla Fleet passes the gateway's message through; Teslemetry reshapes it)."""
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


async def _async_site_call(hass: HomeAssistant, din: str | None,
                           call: Callable[[Any], Awaitable[Any]]) -> Any:
    found = find_site(hass, din)
    if found is None:
        raise FleetUnavailable("no loaded Tesla Fleet or Teslemetry energy site has this Powerwall")
    entry, site = found
    if not _can_command(entry):
        raise FleetUnavailable("the Tesla Fleet sign-in has no energy_cmds scope")
    try:
        async with asyncio.timeout(CALL_TIMEOUT):
            return await call(site.api)
    except TimeoutError as err:
        raise FleetRegisterError("Tesla did not answer in time", 408) from err
    except Exception as err:  # noqa: BLE001
        raise FleetRegisterError(str(err) or type(err).__name__, getattr(err, "status", None)) from err


async def async_register_key(hass: HomeAssistant, din: str | None, public_der: bytes) -> int | None:
    """Ask the gateway, through Tesla Fleet, to trust this key. Returns the key
    state if reported. Registering the same key again reopens an expired
    confirmation window."""
    resp = await _async_site_call(hass, din, lambda api: api.add_authorized_client(
        public_der, description="Home Assistant"))
    return key_state_from(resp, public_der)


async def async_key_state(hass: HomeAssistant, din: str | None, public_der: bytes) -> int | None:
    resp = await _async_site_call(hass, din, lambda api: api.list_authorized_clients())
    return key_state_from(resp, public_der)
