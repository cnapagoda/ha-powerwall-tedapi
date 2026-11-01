"""Diagnostics for the Tesla Powerwall TEDAPI integration."""
from __future__ import annotations

import os
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import tesla_cloud
from .const import (
    AUTH_FILE,
    CONF_DIAGNOSTICS_GATEWAY,
    CONF_GW_PWD,
    CONF_OWNERS_AUTHPATH,
    CONF_OWNERS_EMAIL,
    CONF_SITE_ID,
)
from .coordinator import TedapiConfigEntry

TO_REDACT = {CONF_GW_PWD, CONF_OWNERS_EMAIL, CONF_SITE_ID, "din", "serial", "unique_id", "title"}
REDACTED = "**REDACTED**"
# Gateway config keys that identify the site, a person or a device, or hold
# credentials: any key containing one of these, or equal to one of the short ones.
SENSITIVE_PARTS = ("serial", "password", "passwd", "secret", "token", "ssid", "psk", "latitude",
                   "longitude", "address", "email", "phone", "customer", "installer", "vin", "site_name",
                   "street", "postcode", "postal", "mac_", "macaddr", "cert", "private", "key_")
SENSITIVE_KEYS = {"din", "ip", "mac", "lat", "lng", "lon", "zip", "city", "key", "pwd", "name_of_site",
                  "gateway_din", "teg_din", "vin", "pin"}


def _sensitive(key: str) -> bool:
    key = key.lower()
    return key in SENSITIVE_KEYS or any(part in key for part in SENSITIVE_PARTS)


def redact_config(value: Any, secrets: tuple[str, ...]) -> Any:
    """Redact sensitive keys anywhere in the gateway config, and any string
    that contains one of `secrets` (the DIN, the site id)."""
    if isinstance(value, dict):
        return {k: REDACTED if isinstance(k, str) and _sensitive(k) else redact_config(v, secrets)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_config(v, secrets) for v in value]
    if isinstance(value, str) and any(s and s in value for s in secrets):
        return REDACTED
    return value


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: TedapiConfigEntry) -> dict[str, Any]:
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    data = dict(coordinator.data or {})
    # Pack serials are dictionary keys; replace them with an index.
    data["packs"] = list((data.get("packs") or {}).values())
    authpath = entry.data.get(CONF_OWNERS_AUTHPATH) or ""
    token = (await hass.async_add_executor_job(
        tesla_cloud.redact_token_file, os.path.join(authpath, AUTH_FILE))) if authpath else None
    result: dict[str, Any] = {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "connection_mode": coordinator.connection_mode,
        "control_enabled": coordinator.control_enabled,
        "cloud_enabled": coordinator.cloud_enabled,
        "cloud_backends": [backend.name for backend in coordinator.backends],
        "token_cache": token,
        "dispatch": runtime.dispatch.as_dict() if runtime.dispatch else None,
        "data": async_redact_data(data, TO_REDACT),
        "tesla_tariff": coordinator.tariff if coordinator.tariff_enabled else None,
    }
    if entry.options.get(CONF_DIAGNOSTICS_GATEWAY):
        secrets = tuple(str(s) for s in (coordinator.din, entry.unique_id, entry.data.get(CONF_SITE_ID)) if s)
        try:
            config = await hass.async_add_executor_job(coordinator.read_gateway_config)
        except Exception as err:  # noqa: BLE001
            result["gateway_config"] = {"error": str(err)}
        else:
            result["gateway_config"] = redact_config(config, secrets)
    return result
