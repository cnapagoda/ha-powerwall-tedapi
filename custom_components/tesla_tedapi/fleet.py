"""Cloud backup through the Tesla Fleet integration's sign-in.

The Owner API refuses Powerwall access for browser sign-ins (HTTP 403 since
2026-10). When the core Tesla Fleet integration is set up, its energy site API
object already holds a working Fleet API session. This wraps it with the
pypowerwall methods the coordinator uses for settings, so it can stand in for
the Owner API client.

The token is never refreshed here: Tesla Fleet's own session does that (a
refresh from a second place would invalidate its refresh token).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

FLEET_DOMAIN = "tesla_fleet"
# The four getters share one site_info request.
SITE_INFO_SECONDS = 10
CALL_TIMEOUT = 30
# Tesla Fleet entries in these states may still load: waiting, not failing.
STARTING = (ConfigEntryState.NOT_LOADED, ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.SETUP_RETRY)


def fleet_entries(hass: HomeAssistant) -> list:
    return hass.config_entries.async_entries(FLEET_DOMAIN)


def fleet_ready(hass: HomeAssistant) -> bool:
    """Tesla Fleet has finished loading."""
    return any(entry.state is ConfigEntryState.LOADED for entry in fleet_entries(hass))


def fleet_starting(hass: HomeAssistant) -> bool:
    """Not loaded yet but may still load (after a restart it can start later
    than this integration). No entry at all, or one that failed, is not."""
    return not fleet_ready(hass) and any(entry.state in STARTING for entry in fleet_entries(hass))


def _gateway_dins(info: dict[str, Any]) -> list[str]:
    gateways = (info.get("components") or {}).get("gateways") or []
    return [str(g.get("din", "")) for g in gateways if isinstance(g, dict)]


class FleetBackup:
    """pypowerwall-like settings client over the Tesla Fleet integration.
    Called from executor threads; the calls run on the event loop."""

    mode = "cloud"

    def __init__(self, hass: HomeAssistant, din: str | None) -> None:
        self._hass = hass
        self._din = din
        self._site = None
        self._info: tuple[float, dict[str, Any]] | None = None
        self.client = self  # pypowerwall exposes .client; checked for None

    # -- plumbing ------------------------------------------------------

    def _run(self, coro) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._hass.loop).result(CALL_TIMEOUT)

    async def _async_site(self):
        """The energy site with this gateway. A site whose info lists other
        gateways is skipped; one that lists none is accepted."""
        if self._site is not None:
            return self._site
        sites = []
        for entry in fleet_entries(self._hass):
            if entry.state is ConfigEntryState.LOADED:
                sites.extend(entry.runtime_data.energysites)
        if not sites:
            raise RuntimeError("the Tesla Fleet integration has no energy site loaded")
        for site in sites:
            if not self._din:
                break
            info = (await site.api.site_info()).get("response") or {}
            dins = _gateway_dins(info)
            if not dins or any(self._din in din for din in dins):
                break
        else:
            raise RuntimeError("no Tesla Fleet energy site has this Powerwall")
        self._site = site
        return site

    def _site_info(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._info and now - self._info[0] < SITE_INFO_SECONDS:
            return self._info[1]

        async def read() -> dict[str, Any]:
            site = await self._async_site()
            return (await site.api.site_info()).get("response") or {}

        info = self._run(read())
        self._info = (now, info)
        return info

    def _post(self, call) -> dict[str, Any]:
        async def write() -> dict[str, Any]:
            return await call((await self._async_site()).api)

        self._info = None
        result = self._run(write())
        return result if isinstance(result, dict) and result else {"ok": True}

    # -- reads (pypowerwall names) ---------------------------------------

    def get_mode(self) -> str | None:
        return self._site_info().get("default_real_mode")

    def get_reserve(self) -> float | None:
        value = self._site_info().get("backup_reserve_percent")
        return float(value) if isinstance(value, (int, float)) else None

    def get_grid_charging(self) -> bool | None:
        components = self._site_info().get("components") or {}
        if not components:
            return None
        return not components.get("disallow_charge_from_grid_with_solar_installed", False)

    def get_grid_export(self) -> str | None:
        components = self._site_info().get("components") or {}
        if not components:
            return None
        return components.get("customer_preferred_export_rule") or "battery_ok"

    # -- writes ----------------------------------------------------------

    def set_mode(self, mode: str) -> dict[str, Any]:
        return self._post(lambda api: api.operation(mode))

    def set_reserve(self, level: int) -> dict[str, Any]:
        return self._post(lambda api: api.backup(int(level)))

    def set_grid_charging(self, enabled: bool) -> dict[str, Any]:
        return self._post(lambda api: api.grid_import_export(
            disallow_charge_from_grid_with_solar_installed=not enabled))

    def set_grid_export(self, mode: str) -> dict[str, Any]:
        return self._post(lambda api: api.grid_import_export(customer_preferred_export_rule=mode))
