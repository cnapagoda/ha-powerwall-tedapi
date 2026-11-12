"""Diagnostics for the Tesla Powerwall TEDAPI integration."""
from __future__ import annotations

import os
import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import tesla_cloud
from .const import (
    AUTH_FILE,
    CONF_DIAGNOSTICS_GATEWAY,
    CONF_FLEET_SITE_ID,
    CONF_FLEET_TOKEN,
    CONF_GW_PWD,
    CONF_MACS,
    CONF_OAUTH_IMPLEMENTATION,
    CONF_OAUTH_TOKEN,
    CONF_OWNERS_AUTHPATH,
    CONF_OWNERS_EMAIL,
    CONF_SITE_ID,
)
from .coordinator import TedapiConfigEntry

TO_REDACT = {CONF_GW_PWD, CONF_OWNERS_EMAIL, CONF_SITE_ID, CONF_FLEET_SITE_ID, CONF_FLEET_TOKEN,
             CONF_OAUTH_TOKEN, CONF_OAUTH_IMPLEMENTATION, CONF_MACS, "din", "serial", "unique_id", "title"}
REDACTED = "**REDACTED**"
# Gateway config keys that identify the site, a person or a device, or hold
# credentials: any key containing one of these, or equal to one of the short ones.
SENSITIVE_PARTS = ("serial", "password", "passwd", "secret", "token", "ssid", "psk", "latitude",
                   "longitude", "address", "email", "phone", "customer", "installer", "vin", "site_name",
                   "street", "postcode", "postal", "mac_", "macaddr", "cert", "private", "key_",
                   # A meter's TLS name is its MAC address; short_id and device_id name a device.
                   "server_name", "short_id", "device_id", "hostname",
                   # Together these narrow the site down to one network area.
                   "distributor", "utility", "retailer", "commission")
SENSITIVE_KEYS = {"din", "ip", "mac", "lat", "lng", "lon", "zip", "city", "key", "pwd", "name_of_site",
                  "gateway_din", "teg_din", "vin", "pin"}
# Sensitive only under a given parent: "state" is the site's state or
# province in site_info, and a harmless status everywhere else.
SENSITIVE_UNDER = {"site_info": {"state"}}
# In a tariff, the plan and retailer names; periods and prices stay (they
# are what a tariff bug report needs).
TARIFF_NAMES = {"name", "utility", "code", "plan"}
# A MAC address anywhere in a string, whatever its key is called.
MAC_ADDRESS = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}(?![0-9A-Fa-f])")
# Shorter strings are too likely to match something unrelated.
MIN_SECRET_LENGTH = 6


def _sensitive(key: str, parent: str = "") -> bool:
    key = key.lower()
    return (key in SENSITIVE_KEYS or key in SENSITIVE_UNDER.get(parent, ())
            or any(part in key for part in SENSITIVE_PARTS))


def _secret_value(value: str, secrets: tuple[str, ...]) -> bool:
    return bool(MAC_ADDRESS.search(value)) or any(s and s in value for s in secrets)


def redact_values(value: Any, secrets: tuple[str, ...]) -> Any:
    """Redact any string that holds a MAC address or one of `secrets`,
    whatever its key: for data whose keys are known to be safe."""
    if isinstance(value, dict):
        return {k: redact_values(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_values(v, secrets) for v in value]
    if isinstance(value, str) and _secret_value(value, secrets):
        return REDACTED
    return value


def redact_config(value: Any, secrets: tuple[str, ...], parent: str = "", tariff: bool = False) -> Any:
    """Redact sensitive keys anywhere in the gateway config or a tariff, and
    any string that holds a MAC address or one of `secrets` (the DIN, the
    site id, a serial number)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            name = k.lower() if isinstance(k, str) else ""
            in_tariff = tariff or "tariff" in name
            if name and (_sensitive(name, parent) or (tariff and name in TARIFF_NAMES)):
                out[k] = REDACTED
            else:
                out[k] = redact_config(v, secrets, name, in_tariff)
        return out
    if isinstance(value, list):
        return [redact_config(v, secrets, parent, tariff) for v in value]
    if isinstance(value, str) and _secret_value(value, secrets):
        return REDACTED
    return value


def redact_tariff(tariff: Any, secrets: tuple[str, ...] = ()) -> Any:
    """The Tesla tariff without the plan and retailer names."""
    return redact_config(tariff, secrets, tariff=True)


def _rename(value: Any, names: dict[str, str]) -> Any:
    """Replace strings (values and dictionary keys) by `names`."""
    if isinstance(value, dict):
        return {names.get(k, k) if isinstance(k, str) else k: _rename(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [_rename(v, names) for v in value]
    if isinstance(value, str):
        return names.get(value, value)
    return value


def number_packs(data: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Packs as a list, without their serials: a pack named after the end
    of its serial ("Battery 06TN") becomes "Battery 1", here and wherever
    else the name appears (alert sources). Returns the data and the serials."""
    packs = data.get("packs") or {}
    if not isinstance(packs, dict):
        return data, ()
    names: dict[str, str] = {}
    for index, (serial, pack) in enumerate(packs.items(), start=1):
        name = pack.get("name") if isinstance(pack, dict) else None
        if isinstance(name, str) and isinstance(serial, str) and serial and name.endswith(serial[-4:]):
            names[name] = f"Battery {index}"
    serials = tuple(s for s in packs if isinstance(s, str))
    data = {**data, "packs": list(packs.values())}
    return (_rename(data, names) if names else data), serials


def _secrets(*values: Any) -> tuple[str, ...]:
    """Strings to redact wherever they appear; for a DIN also its serial part."""
    found: list[str] = []
    for value in values:
        text = str(value) if value else ""
        for part in (text, text.rpartition("--")[2]):
            if len(part) >= MIN_SECRET_LENGTH and part not in found:
                found.append(part)
    return tuple(found)


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: TedapiConfigEntry) -> dict[str, Any]:
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    # Pack serials are dictionary keys, and the end of one can be in the
    # pack's name: a list, numbered.
    data, pack_serials = number_packs(dict(coordinator.data or {}))
    # So are the Wall Connectors' serials.
    data["wall_connectors"] = list((data.get("wall_connectors") or {}).values())
    # Serials of Powerwall 3s missing from the readings: only how many.
    if isinstance(data.get("missing_powerwalls"), list):
        data["missing_powerwalls"] = len(data["missing_powerwalls"])
    authpath = entry.data.get(CONF_OWNERS_AUTHPATH) or ""
    token = (await hass.async_add_executor_job(
        tesla_cloud.redact_token_file, os.path.join(authpath, AUTH_FILE))) if authpath else None
    secrets = _secrets(coordinator.din, entry.unique_id, entry.data.get(CONF_SITE_ID),
                       entry.data.get(CONF_FLEET_SITE_ID), *pack_serials)
    result: dict[str, Any] = {
        "entry": redact_values(async_redact_data(entry.as_dict(), TO_REDACT), secrets),
        "connection_mode": coordinator.connection_mode,
        "control_enabled": coordinator.control_enabled,
        "cloud_enabled": coordinator.cloud_enabled,
        "cloud_backends": [backend.name for backend in coordinator.backends],
        "token_cache": token,
        "dispatch": runtime.dispatch.as_dict() if runtime.dispatch else None,
        "data": redact_values(async_redact_data(data, TO_REDACT), secrets),
        "tesla_tariff": redact_tariff(coordinator.tariff, secrets) if coordinator.tariff_enabled else None,
    }
    if entry.options.get(CONF_DIAGNOSTICS_GATEWAY):
        try:
            config = await hass.async_add_executor_job(coordinator.read_gateway_config)
        except Exception as err:  # noqa: BLE001
            result["gateway_config"] = {"error": str(err)}
        else:
            result["gateway_config"] = redact_config(config, secrets)
    return result
