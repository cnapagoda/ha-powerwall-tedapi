"""Diagnostics for the Powerwall TEDAPI integration."""
from __future__ import annotations

import os
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import tesla_cloud
from .const import AUTH_FILE, CONF_GW_PWD, CONF_OWNERS_AUTHPATH, CONF_OWNERS_EMAIL, CONF_SITE_ID
from .coordinator import TedapiConfigEntry

TO_REDACT = {CONF_GW_PWD, CONF_OWNERS_EMAIL, CONF_SITE_ID, "din", "serial", "unique_id", "title"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: TedapiConfigEntry) -> dict[str, Any]:
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    data = dict(coordinator.data or {})
    # Pack serials are dictionary keys; replace them with an index.
    data["packs"] = list((data.get("packs") or {}).values())
    authpath = entry.data.get(CONF_OWNERS_AUTHPATH) or ""
    token = (await hass.async_add_executor_job(
        tesla_cloud.redact_token_file, os.path.join(authpath, AUTH_FILE))) if authpath else None
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "connection_mode": coordinator.connection_mode,
        "control_enabled": coordinator.control_enabled,
        "cloud_enabled": coordinator.cloud_enabled,
        "token_cache": token,
        "dispatch": runtime.dispatch.as_dict() if runtime.dispatch else None,
        "data": async_redact_data(data, TO_REDACT),
    }
