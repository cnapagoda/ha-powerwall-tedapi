"""Fixable repair: sign in to Tesla again when the cloud backup stops working."""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from . import tesla_cloud
from .config_flow import TeslaLoginMixin


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


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    return CloudLoginRepairFlow((data or {})["entry_id"])
