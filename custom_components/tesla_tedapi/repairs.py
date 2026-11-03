"""Fixable repairs: sign in to Tesla again when a cloud backup stops working."""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
import voluptuous as vol

from . import tesla_cloud
from .config_flow import FLEET_SIGNIN, TeslaLoginMixin


class CloudLoginRepairFlow(TeslaLoginMixin, RepairsFlow):
    """Paste a new Tesla callback address; the token cache is replaced and the
    entry reloaded."""

    def __init__(self, entry_id: str) -> None:
        self._init_login()
        self._entry_id = entry_id
        self._din: str | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_gone")
        self._din = entry.unique_id
        return await self.async_step_login()

    async def _async_logged_in(self, site: tesla_cloud.EnergySite) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is not None:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, **self._account_data(site)})
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data={})


class FleetSigninRepairFlow(RepairsFlow):
    """Sign in to the Fleet API (Tesla Fleet or Teslemetry). The sign-in runs
    as a reauthentication of the entry: Tesla Fleet's OAuth can only finish
    in a config flow. Ends with an abort, so the repair stays until the
    sign-in has worked (the reload after it clears the repair)."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_gone")
        if user_input is not None:
            entry.async_start_reauth(self.hass, data={**entry.data, FLEET_SIGNIN: True})
            return self.async_abort(reason="fleet_signin_started")
        return self.async_show_form(step_id="init", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    entry_id = (data or {})["entry_id"]
    if issue_id.startswith("cloud_login"):
        return CloudLoginRepairFlow(entry_id)
    return FleetSigninRepairFlow(entry_id)
