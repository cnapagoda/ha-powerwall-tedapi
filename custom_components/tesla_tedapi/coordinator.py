"""Data coordinator for the Powerwall TEDAPI integration.

Readings come from the gateway over the local network. Settings are written to
the gateway when the connection is signed (v1r) and to the Tesla cloud as a
backup, when a Tesla account is linked."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import gateway
from .alerts import SEVERITY_FAULT, AlertChange, AlertTracker, describe, level, summary
from .const import (
    AUTH_FILE,
    CLOUD_AUTH_FAILURES,
    CLOUD_CACHE_SECONDS,
    CLOUD_CHECK_SECONDS,
    CLOUD_RETRY_SECONDS,
    CONF_CONNECTION_MODE,
    CONF_GW_PWD,
    CONF_HOST,
    CONF_OWNERS_AUTHPATH,
    CONF_OWNERS_EMAIL,
    CONF_PROBLEM_MINUTES,
    CONF_RESTORE_EXPORT,
    CONF_RSA_KEY_PATH,
    CONF_SCAN_INTERVAL,
    CONF_SITE_ID,
    CONF_WIFI_HOST,
    DEFAULT_HOST_WIFI,
    DEFAULT_PROBLEM_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    DROPPABLE_SETTINGS,
    KEY_REFUSED_POLLS,
    MODE_V1R,
    PASSWORD_REFUSED_POLLS,
    RESTORE_MIN_SECONDS,
    app_soc,
)

if TYPE_CHECKING:
    from .dispatch import DispatchSession

_LOGGER = logging.getLogger(__name__)

CONTROL_KEYS = ("operation_mode", "reserve", "grid_charging", "grid_export")

# Lifetime meter counters (Wh) from /api/meters/aggregates: key -> (section, field).
ENERGY_COUNTERS: dict[str, tuple[str, str]] = {
    "grid_energy_imported": ("site", "energy_imported"),
    "grid_energy_exported": ("site", "energy_exported"),
    "battery_energy_charged": ("battery", "energy_imported"),
    "battery_energy_discharged": ("battery", "energy_exported"),
    "solar_energy_produced": ("solar", "energy_exported"),
    "home_energy_consumed": ("load", "energy_imported"),
}

# Battery pack part numbers -> friendly names (TEPOD vitals blocks).
PACK_NAMES = {"1707000": "Powerwall 3", "1807000": "Expansion Pack"}


def _extract_pinv_value(vitals: dict, *keys: str) -> float | None:
    """First matching key from the PINV/TEPINV inverter blocks in vitals."""
    for device_key, device_data in (vitals or {}).items():
        if ("PINV" in device_key or "TEPINV" in device_key) and isinstance(device_data, dict):
            for k in keys:
                val = device_data.get(k)
                if val is not None:
                    return round(float(val), 2)
    return None


STRING_LETTERS = "ABCDEF"


def _extract_strings(vitals: dict) -> dict[str, dict[str, Any]]:
    """Per-MPPT solar input (Powerwall 3 built-in inverter) from the PVAC vitals
    blocks: {"A": {"voltage", "current", "power", "state"}, ...}. With several
    inverters the later ones are numbered ("2A")."""

    def num(v, digits):
        return round(float(v), digits) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    strings: dict[str, dict[str, Any]] = {}
    blocks = sorted(k for k, v in (vitals or {}).items() if k.split("--")[0] == "PVAC" and isinstance(v, dict))
    for index, device_key in enumerate(blocks, start=1):
        d = vitals[device_key]
        for letter in STRING_LETTERS:
            values = {
                "voltage": num(d.get(f"PVAC_PVMeasuredVoltage_{letter}"), 1),
                "current": num(d.get(f"PVAC_PVCurrent_{letter}"), 2),
                "power": num(d.get(f"PVAC_PVMeasuredPower_{letter}"), 0),
                "state": d.get(f"PVAC_PvState_{letter}"),
            }
            if any(v is not None for v in values.values()):
                strings[letter if index == 1 else f"{index}{letter}"] = values
    return strings


def _extract_packs(vitals: dict) -> dict[str, dict[str, Any]]:
    """Per-pack capacity, charge and temperatures from TEPOD vitals blocks."""

    def kwh(v):
        return round(v / 1000, 2) if isinstance(v, (int, float)) and v > 0 else None

    def temp(v):
        return round(float(v), 1) if isinstance(v, (int, float)) else None

    packs: dict[str, dict[str, Any]] = {}
    for device_key, d in (vitals or {}).items():
        if not device_key.startswith("TEPOD--") or not isinstance(d, dict):
            continue
        parts = device_key.split("--")
        part, serial = (parts[1], parts[2]) if len(parts) >= 3 else ("", device_key)
        packs[serial] = {
            "name": PACK_NAMES.get(part.split("-")[0], f"Battery {serial[-4:]}"),
            "part_number": part,
            "full_kwh": kwh(d.get("POD_nom_full_pack_energy")),
            "remaining_kwh": kwh(d.get("POD_nom_energy_remaining")),
            "temp_max": temp(d.get("HVP_PackTempMax")),
            "temp_min": temp(d.get("HVP_PackTempMin")),
            "shunt_temp": temp(d.get("HVP_ShuntTemperature")),
            "alerts": list(d.get("alerts") or []),
        }
    return packs


@dataclass
class TedapiRuntimeData:
    """Stored on the config entry while it is loaded."""

    coordinator: TedapiCoordinator
    dispatch: DispatchSession | None = field(default=None)


type TedapiConfigEntry = ConfigEntry[TedapiRuntimeData]


class TedapiCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the gateway; the Tesla cloud backs up settings when local fails."""

    config_entry: TedapiConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TedapiConfigEntry) -> None:
        data = entry.data
        self._conn_mode: str = data.get(CONF_CONNECTION_MODE, "wifi")
        self._gw_pwd: str = data.get(CONF_GW_PWD, "")
        self._rsa_key_path: str = data.get(CONF_RSA_KEY_PATH, "")
        self._host: str = data.get(CONF_HOST, DEFAULT_HOST_WIFI)
        self._wifi_host: str = data.get(CONF_WIFI_HOST, "") or ""
        self._owners_email: str = data.get(CONF_OWNERS_EMAIL, "") or ""
        self._owners_authpath: str = data.get(CONF_OWNERS_AUTHPATH, "") or hass.config.config_dir
        self._site_id: int | None = data.get(CONF_SITE_ID)
        options = entry.options
        self.restore_settings: bool = options.get(CONF_RESTORE_EXPORT, True)
        self.problem_after = timedelta(minutes=options.get(CONF_PROBLEM_MINUTES, DEFAULT_PROBLEM_MINUTES))

        self.cloud_enabled: bool = bool(self._owners_email)
        # Settings can be changed over a signed connection, or through the cloud.
        self.control_enabled: bool = self._conn_mode == MODE_V1R or self.cloud_enabled

        self._pw = None
        self._cloud = None
        # Last good lifetime counters: a missing, zero or lower reading keeps the
        # previous value so total_increasing statistics never see a fake reset.
        self._energy_last: dict[str, float] = {}
        self.din: str | None = entry.unique_id
        self.firmware_version: str | None = None
        self.alerts = AlertTracker()
        # Alerts raised or cleared by the latest reading (for the event entity).
        self.alert_changes: list[AlertChange] = []
        self._issues: set[str] = set()

        # Cloud settings cache: (monotonic time, values).
        self._cloud_cache: tuple[float, dict[str, Any]] | None = None
        self.cloud_failures = 0
        self._cloud_issue = False
        self.cloud_last_ok: datetime | None = None
        self._cloud_check_at: float | None = None
        # Settings the gateway drops from its config now and then (export rule,
        # grid charging): the last value set or seen, written back when missing.
        self.wanted: dict[str, Any] = dict.fromkeys(DROPPABLE_SETTINGS)
        self.restores: dict[str, int] = dict.fromkeys(DROPPABLE_SETTINGS, 0)
        self.last_restored: dict[str, datetime | None] = dict.fromkeys(DROPPABLE_SETTINGS)
        self._restore_at: dict[str, float] = {}
        # Consecutive polls where the gateway refused the password or the key.
        self._password_refused = 0
        self._key_refused = 0
        self._reauth_started = False
        self._token_expiry: tuple[float, float | None] | None = None
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")

        scan = entry.options.get(CONF_SCAN_INTERVAL, data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan),
        )

    @property
    def connection_mode(self) -> str:
        return self._conn_mode

    @property
    def dispatch_supported(self) -> bool:
        return self._conn_mode == MODE_V1R

    # ------------------------------------------------------------------
    # Clients (executor)
    # ------------------------------------------------------------------

    def _init_powerwall(self):
        import pypowerwall  # noqa: PLC0415

        kwargs: dict[str, Any] = {
            "host": self._host,
            "password": "",
            "email": "",
            "timezone": str(self.hass.config.time_zone),
            "pwcacheexpire": 5,
            "auto_select": True,
            "failover": False,
            # pypowerwall's failover switches to cloud mode when it finds a token
            # cache in authpath (default: the working directory, which is the HA
            # config folder). Point it at a folder without one: this client is
            # local only, and the cloud backup is a separate client.
            "authpath": os.path.dirname(__file__),
        }
        if self._conn_mode == MODE_V1R:
            kwargs["rsa_key_path"] = self._rsa_key_path
            if self._gw_pwd:
                kwargs["gw_pwd"] = self._gw_pwd
            if self._wifi_host and self._wifi_host != self._host:
                # After 3 failures on the home-network address, use signed v1r
                # over the Powerwall's Wi-Fi hotspot; the main host is retried later.
                kwargs["wifi_host"] = self._wifi_host
                kwargs["failover"] = True
                kwargs["wifi_v1r_fallback"] = True
        else:
            kwargs["gw_pwd"] = self._gw_pwd

        pw = pypowerwall.Powerwall(**kwargs)
        if pw.mode != "local":
            raise RuntimeError(f"pypowerwall picked {pw.mode} mode, not local")
        _LOGGER.debug("pypowerwall %s connected to %s [%s, tedapi_mode=%s]",
                      getattr(pypowerwall, "version", "?"), self._host, self._conn_mode,
                      getattr(pw, "tedapi_mode", None))
        return pw

    def _init_cloud(self):
        import pypowerwall  # noqa: PLC0415

        kwargs: dict[str, Any] = {
            "host": "",
            "password": "",
            "email": self._owners_email,
            "timezone": str(self.hass.config.time_zone),
            "cloudmode": True,
            "authpath": self._owners_authpath,
            "auto_select": True,
            "failover": False,
        }
        if self._site_id:
            # Pin this entry's site; the shared .pypowerwall.site file is only a
            # fallback for entries made before 2.2.
            kwargs["siteid"] = self._site_id
        cloud = pypowerwall.Powerwall(**kwargs)
        if cloud.mode != "cloud" or cloud.client is None:
            raise RuntimeError("Tesla cloud login failed")
        return cloud

    def _get_cloud(self):
        if not self.cloud_enabled:
            raise RuntimeError("no Tesla account linked")
        if self._cloud is None:
            try:
                self._cloud = self._init_cloud()
            except Exception:
                self.cloud_failures += 1
                raise
            # A login alone doesn't reset cloud_failures: only a read that
            # returns data does (the Owner API can log in, then reject reads).
        return self._cloud

    # ------------------------------------------------------------------
    # Reads (executor)
    # ------------------------------------------------------------------

    def _fetch_all(self) -> dict[str, Any]:
        pw = self._pw
        power = pw.power() or {}
        soe = pw.level()
        grid_status = pw.grid_status()

        vitals: dict = {}
        try:
            vitals = pw.vitals() or {}
        except Exception:  # noqa: BLE001
            pass

        version: str | None = None
        try:
            version = pw.version()
        except Exception:  # noqa: BLE001
            pass

        din: str | None = None
        try:
            din = pw.din()
        except Exception:  # noqa: BLE001
            pass
        if not din:
            # A stale gateway session makes every call return empty data while
            # pypowerwall keeps the cached login. An empty DIN is the tell:
            # raise so a fresh client (new login) is built on the next poll.
            raise RuntimeError("no DIN from the gateway, reconnecting")

        control = self._read_control(pw) if self.control_enabled else {}
        missing = self._check_dropped(pw, control) if self.control_enabled else {}
        soe_value = round(soe, 1) if isinstance(soe, (int, float)) else None

        return {
            "solar_power": round(power.get("solar", 0.0), 1),
            "battery_power": round(power.get("battery", 0.0), 1),
            "grid_power": round(power.get("site", 0.0), 1),
            "home_power": round(power.get("load", 0.0), 1),
            "battery_soe": soe_value,
            "battery_level": app_soc(soe_value),
            "grid_status": grid_status,
            "grid_frequency": _extract_pinv_value(vitals, "PINV_Fout", "PINV_Fnet"),
            "grid_voltage": _extract_pinv_value(vitals, "PINV_Vout", "PINV_VSplit1"),
            "din": din,
            "firmware_version": version,
            "strings": _extract_strings(vitals),
            "tedapi_mode": getattr(pw, "tedapi_mode", None),
            **self._fetch_extras(pw, vitals),
            "local_ok": True,
            **{key: control.get(key) for key in CONTROL_KEYS},
            "control_source": control.get("source"),
            **{f"{key}_missing": missing.get(key, False) for key in DROPPABLE_SETTINGS},
        }

    @staticmethod
    def _read_control_from(pw) -> dict[str, Any]:
        readers = {
            "operation_mode": pw.get_mode,
            "reserve": pw.get_reserve,
            "grid_charging": pw.get_grid_charging,
            "grid_export": pw.get_grid_export,
        }
        values: dict[str, Any] = {}
        for key, read in readers.items():
            try:
                values[key] = read()
            except Exception:  # noqa: BLE001
                values[key] = None
        return values

    def _read_control(self, local) -> dict[str, Any]:
        """Settings from the gateway; any field it can't give comes from the
        cloud. "source" says which answered (local/cloud/mixed)."""
        values = self._read_control_from(local) if local is not None else dict.fromkeys(CONTROL_KEYS)
        missing = [key for key in CONTROL_KEYS if values.get(key) is None]
        source = "local"
        if missing and self.cloud_enabled:
            cloud_values = self._cloud_settings()
            for key in missing:
                values[key] = cloud_values.get(key)
            got = [key for key in missing if values[key] is not None]
            if got:
                source = "cloud" if len(got) == len(CONTROL_KEYS) else "mixed"
        values["source"] = source if any(values.get(k) is not None for k in CONTROL_KEYS) else None
        return values

    def _cloud_settings(self) -> dict[str, Any]:
        """Settings from the Tesla cloud, cached for CLOUD_CACHE_SECONDS."""
        now = time.monotonic()
        if self._cloud_cache and now - self._cloud_cache[0] < CLOUD_CACHE_SECONDS:
            return self._cloud_cache[1]
        try:
            values = self._read_control_from(self._get_cloud())
        except Exception as err:  # noqa: BLE001
            # Login failed (counted in _get_cloud).
            _LOGGER.debug("Cloud settings read failed: %s", err)
            self._cloud = None
            return {}
        if any(v is not None for v in values.values()):
            self._cloud_cache = (now, values)
            self.cloud_failures = 0
            self.cloud_last_ok = dt_util.utcnow()
        else:
            # Logged in but every read failed, e.g. a refreshed token the
            # Owner API rejects: log in again next time.
            _LOGGER.debug("Cloud settings read returned nothing")
            self.cloud_failures += 1
            self._cloud = None
        return values

    def _check_cloud(self) -> None:
        """A fresh cloud read (not the cache): proves the backup still works
        and lets pypowerwall refresh the Tesla token while it is idle."""
        self._cloud_cache = None
        self._cloud_settings()
        self._token_expiry = None  # re-read: the refresh rewrites the file

    def _check_dropped(self, pw, control: dict[str, Any]) -> dict[str, bool]:
        """Which settings in DROPPABLE_SETTINGS are missing from the gateway's
        own config, after writing them back where possible."""
        try:
            site_info = (pw.client.tedapi.get_config() or {}).get("site_info") or {}
        except Exception:  # noqa: BLE001
            return {}
        return {key: self._check_setting(pw, control, key, site_info) for key in DROPPABLE_SETTINGS}

    def _check_setting(self, pw, control: dict[str, Any], key: str, site_info: dict) -> bool:
        """True while a setting that was set is missing from the gateway's config.

        A missing field normally means Tesla's default, which pypowerwall
        reports ("battery_ok" export, grid charging allowed). But the Powerwall
        also drops a field that was set, now and then, while the Tesla cloud
        keeps it. Then the entity shows the value that was set and, over a
        signed connection, it is written back."""
        field, default = DROPPABLE_SETTINGS[key]
        if site_info.get(field) is not None:
            if control.get(key) is not None:
                self.wanted[key] = control[key]
            return False
        if self.wanted[key] is None and self.cloud_enabled:
            # Never seen locally since setup: the cloud keeps the value.
            self.wanted[key] = self._cloud_settings().get(key)
        wanted = self.wanted[key]
        if wanted is None or wanted == default:
            return False  # nothing set, or the same as the default
        control[key] = wanted
        name = key.replace("_", " ")
        last = self._restore_at.get(key)
        if (self.restore_settings and self._conn_mode == MODE_V1R
                and (last is None or time.monotonic() - last >= RESTORE_MIN_SECONDS)):
            self._restore_at[key] = time.monotonic()
            setter = pw.set_grid_export if key == "grid_export" else pw.set_grid_charging
            try:
                ok = self._write_ok(setter(wanted))
            except Exception as err:  # noqa: BLE001
                ok = False
                _LOGGER.debug("Restoring %s failed: %s", name, err)
            if ok:
                self.restores[key] += 1
                self.last_restored[key] = dt_util.utcnow()
                _LOGGER.warning("The Powerwall dropped its %s setting; restored it to %s", name, wanted)
                return False
            _LOGGER.warning("The Powerwall dropped its %s setting; restoring %s failed", name, wanted)
        return True

    def _fetch_cloud_only(self) -> dict[str, Any]:
        """Gateway unreachable: keep the settings entities alive from the cloud.
        Sensors read local_ok and show unavailable."""
        control = self._read_control(None)
        return {
            "local_ok": False,
            "din": self.din,
            "firmware_version": self.firmware_version,
            **{key: control.get(key) for key in CONTROL_KEYS},
            "control_source": control.get("source"),
        }

    def _fetch_extras(self, pw, vitals: dict) -> dict[str, Any]:
        """Lifetime energy, capacity, temperatures, alerts and backup time.
        Each read is independent so one failing never blocks the rest."""
        aggregates: dict = {}
        try:
            aggregates = pw.poll("/api/meters/aggregates") or {}
        except Exception:  # noqa: BLE001
            pass
        energy: dict[str, float | None] = {}
        for key, (section, fld) in ENERGY_COUNTERS.items():
            value = (aggregates.get(section) or {}).get(fld)
            last = self._energy_last.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 \
                    and (last is None or value / 1000 >= last):
                self._energy_last[key] = round(value / 1000, 3)
            energy[key] = self._energy_last.get(key)

        full_wh = remaining_wh = None
        try:
            status = pw.system_status() or {}
            full_wh = status.get("nominal_full_pack_energy")
            remaining_wh = status.get("nominal_energy_remaining")
        except Exception:  # noqa: BLE001
            pass

        gateway_started = None
        try:
            started = (pw.status() or {}).get("start_time")
            if started:
                gateway_started = datetime.strptime(started, "%Y-%m-%d %H:%M:%S %z")
        except Exception:  # noqa: BLE001
            pass

        alerts: list[str] | None = None
        try:
            alerts = sorted(pw.alerts() or [])
        except Exception:  # noqa: BLE001
            pass

        backup_hours = None
        try:
            backup_hours = pw.get_time_remaining()
        except Exception:  # noqa: BLE001
            pass

        def num(v, minimum=0):
            return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= minimum

        return {
            "energy": energy,
            "battery_full_kwh": round(full_wh / 1000, 2) if num(full_wh) and full_wh > 0 else None,
            "battery_remaining_kwh": round(remaining_wh / 1000, 2) if num(remaining_wh) else None,
            "packs": _extract_packs(vitals),
            "inverter_ambient_temp": _extract_pinv_value(vitals, "PCH_AmbientTemp"),
            "inverter_heatsink_temp": _extract_pinv_value(vitals, "PCH_heatsinkTemp"),
            "alerts": alerts,
            "backup_hours": round(backup_hours, 2) if num(backup_hours) else None,
            "gateway_started": gateway_started,
        }

    # ------------------------------------------------------------------
    # Writes (executor)
    # ------------------------------------------------------------------

    @staticmethod
    def _write_ok(result: Any) -> bool:
        """pypowerwall returns None/False on failure; some local paths return
        {"error": ...} instead."""
        return bool(result) and not (isinstance(result, dict) and "error" in result)

    def _write(self, what: str, write) -> None:
        """Run write(pw) on the gateway (signed connection only), then on the
        cloud if that fails."""
        self._cloud_cache = None
        if not self.control_enabled:
            raise HomeAssistantError("Changing settings needs a signed (v1r) connection or a Tesla account")
        # The Wi-Fi password session cannot change settings; the gateway accepts
        # the call and ignores it, so it is not tried.
        if self._conn_mode == MODE_V1R:
            if self._pw is not None:
                try:
                    if self._write_ok(write(self._pw)):
                        _LOGGER.info("%s (local)", what)
                        return
                    _LOGGER.warning("%s: local write failed", what)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("%s: local write error (%s)", what, err)
            else:
                _LOGGER.warning("%s: gateway not connected", what)
        if not self.cloud_enabled:
            raise HomeAssistantError(f"{what}: the gateway did not accept it and no Tesla account is linked")
        try:
            ok = self._write_ok(write(self._get_cloud()))
        except Exception as err:  # noqa: BLE001
            self._cloud = None
            raise HomeAssistantError(f"{what}: local and cloud writes failed ({err})") from err
        if not ok:
            raise HomeAssistantError(f"{what}: local and cloud writes failed")
        self.cloud_failures = 0
        self.cloud_last_ok = dt_util.utcnow()
        _LOGGER.info("%s (cloud)", what)

    def _set_mode(self, mode: str) -> None:
        self._write(f"Operation mode set to {mode}", lambda pw: pw.set_mode(mode))

    def _set_reserve(self, reserve: float) -> None:
        level = int(reserve)
        self._write(f"Backup reserve set to {level}%", lambda pw: pw.set_reserve(level))

    def _set_grid_charging(self, enabled: bool) -> None:
        # A real bool: the local path does bool(mode), so "off" would enable.
        self._write(f"Grid charging set to {'on' if enabled else 'off'}",
                    lambda pw: pw.set_grid_charging(bool(enabled)))
        self.wanted["grid_charging"] = bool(enabled)

    def _set_grid_export(self, mode: str) -> None:
        self._write(f"Grid export set to {mode}", lambda pw: pw.set_grid_export(mode))
        self.wanted["grid_export"] = mode

    def _send_dispatch(self, power_watts: float, duration_seconds: int) -> dict:
        """One timed battery setpoint over signed v1r (no cloud equivalent)."""
        if self._pw is None or getattr(self._pw, "tedapi_mode", None) != "v1r":
            raise HomeAssistantError("Battery dispatch needs the signed (v1r) local connection")
        try:
            result = self._pw.dispatch_battery_power(power_watts, duration_seconds)
        except ValueError as err:
            raise HomeAssistantError(f"Invalid dispatch: {err}") from err
        if not result:
            raise HomeAssistantError("The Powerwall did not accept the dispatch (see log)")
        return result

    def _send_resume(self) -> dict:
        if self._pw is None or getattr(self._pw, "tedapi_mode", None) != "v1r":
            raise HomeAssistantError("Battery resume needs the signed (v1r) local connection")
        result = self._pw.resume_battery_operation()
        if not result:
            raise HomeAssistantError("The Powerwall did not accept the resume (see log)")
        return result

    async def async_send_dispatch(self, power_watts: float, duration_seconds: int) -> dict:
        return await self.hass.async_add_executor_job(self._send_dispatch, power_watts, duration_seconds)

    async def async_send_resume(self) -> dict:
        return await self.hass.async_add_executor_job(self._send_resume)

    async def async_set_mode(self, mode: str) -> None:
        await self.hass.async_add_executor_job(self._set_mode, mode)
        await self.async_request_refresh()

    async def async_set_reserve(self, reserve: float) -> None:
        await self.hass.async_add_executor_job(self._set_reserve, reserve)
        await self.async_request_refresh()

    async def async_set_grid_charging(self, enabled: bool) -> None:
        await self.hass.async_add_executor_job(self._set_grid_charging, enabled)
        await self.async_request_refresh()

    async def async_set_grid_export(self, mode: str) -> None:
        await self.hass.async_add_executor_job(self._set_grid_export, mode)
        await self.async_request_refresh()

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def async_load_state(self) -> None:
        """Alert start times and the wanted settings survive restarts."""
        stored = await self._store.async_load() or {}
        self.wanted["grid_export"] = stored.get("export_wanted")
        self.wanted["grid_charging"] = stored.get("grid_charging_wanted")
        self.alerts.restore(stored.get("alerts") or {})

    def _state_to_save(self) -> dict[str, Any]:
        return {"export_wanted": self.wanted["grid_export"],
                "grid_charging_wanted": self.wanted["grid_charging"],
                "alerts": self.alerts.as_dict()}

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            try:
                if self._pw is None:
                    self._pw = await self.hass.async_add_executor_job(self._init_powerwall)
                data = await self.hass.async_add_executor_job(self._fetch_all)
            except Exception as err:
                self._pw = None
                await self._async_note_local_failure()
                if not (self.cloud_enabled and self.din):
                    raise
                _LOGGER.debug("Gateway read failed (%s), settings from the cloud", err)
                data = await self.hass.async_add_executor_job(self._fetch_cloud_only)
                if data.get("control_source") is None:
                    raise
            else:
                self._password_refused = self._key_refused = 0
        except Exception as err:
            self._pw = None
            self._sync_cloud_issue()
            raise UpdateFailed(f"Powerwall update failed: {err}") from err

        if data.get("din"):
            self.din = data["din"]
        if data.get("firmware_version"):
            self.firmware_version = data["firmware_version"]
        if self._cloud_check_due():
            self._cloud_check_at = time.monotonic()
            await self.hass.async_add_executor_job(self._check_cloud)
        self._process_alerts(data)
        self._sync_cloud_issue()
        for key in DROPPABLE_SETTINGS:
            data[f"{key}_restores"] = self.restores[key]
            data[f"{key}_last_restored"] = self.last_restored[key]
        data["health"] = await self._async_health(data)
        self._store.async_delay_save(self._state_to_save, 10)
        return data

    def _cloud_check_due(self) -> bool:
        if not self.cloud_enabled:
            return False
        if self._cloud_check_at is None:
            return True
        since_check = time.monotonic() - self._cloud_check_at
        if self.cloud_failures:
            return since_check >= CLOUD_RETRY_SECONDS
        # Any successful cloud read counts as a check.
        last_ok = self.cloud_last_ok
        if last_ok and (dt_util.utcnow() - last_ok).total_seconds() < CLOUD_CHECK_SECONDS:
            return False
        return since_check >= CLOUD_CHECK_SECONDS

    # ------------------------------------------------------------------
    # Password / key problems -> reauth; cloud sign-in -> repair
    # ------------------------------------------------------------------

    def _classify_failure(self) -> str:
        """Why the signed connection failed: password, key or network."""
        if not self._gw_pwd:
            return "password"
        try:
            gateway.login(self._host, self._gw_pwd)
        except gateway.GatewayPasswordRejected:
            return "password"
        except gateway.GatewayUnreachable:
            return "network"
        return "key"

    async def _async_note_local_failure(self) -> None:
        if self._conn_mode != MODE_V1R or self._reauth_started:
            return
        reason = await self.hass.async_add_executor_job(self._classify_failure)
        if reason == "password":
            self._password_refused += 1
        elif reason == "key":
            self._key_refused += 1
        if self._password_refused >= PASSWORD_REFUSED_POLLS or self._key_refused >= KEY_REFUSED_POLLS:
            _LOGGER.warning("The Powerwall keeps refusing the %s; asking to set up access again",
                            "password" if self._password_refused >= PASSWORD_REFUSED_POLLS else "key")
            self._reauth_started = True
            # Not ConfigEntryAuthFailed: that stops polling, and the cloud
            # backup should keep the settings working meanwhile.
            self.config_entry.async_start_reauth(self.hass)

    def _sync_cloud_issue(self) -> None:
        issue_id = f"cloud_login_{self.config_entry.entry_id}"
        failing = self.cloud_enabled and self.cloud_failures >= CLOUD_AUTH_FAILURES
        if failing and not self._cloud_issue:
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id,
                is_fixable=True,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="cloud_login",
                data={"entry_id": self.config_entry.entry_id},
            )
        elif not failing and self._cloud_issue:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._cloud_issue = failing

    # ------------------------------------------------------------------
    # Setup health
    # ------------------------------------------------------------------

    def _read_token_expiry(self) -> float | None:
        from .tesla_cloud import redact_token_file  # noqa: PLC0415

        info = redact_token_file(os.path.join(self._owners_authpath, AUTH_FILE))
        expiries = [e for e in info.get("expires_at") or [] if isinstance(e, (int, float))]
        return max(expiries) if expiries else None

    async def _async_health(self, data: dict[str, Any]) -> dict[str, Any]:
        checks: dict[str, bool] = {}
        if self._conn_mode == MODE_V1R:
            checks["password_set"] = bool(self._gw_pwd)
            checks["signed_connection"] = data.get("tedapi_mode") == "v1r" or not data.get("local_ok")
        checks["gateway_reachable"] = bool(data.get("local_ok"))
        checks["energy_counters"] = any(v is not None for v in (data.get("energy") or {}).values()) \
            or not data.get("local_ok")
        token_expires = None
        if self.cloud_enabled:
            # Checked by a real read at least hourly, not just "no failures yet".
            checks["cloud_backup"] = self.cloud_failures == 0 and self.cloud_last_ok is not None
            now = time.monotonic()
            if self._token_expiry is None or now - self._token_expiry[0] > 600:
                self._token_expiry = (now, await self.hass.async_add_executor_job(self._read_token_expiry))
            if self._token_expiry[1]:
                token_expires = dt_util.utc_from_timestamp(self._token_expiry[1]).isoformat()
        problems = [name for name, ok in checks.items() if not ok]
        return {
            "state": "degraded" if problems else "ok",
            "checks": checks,
            "problems": problems,
            "wifi_fallback": bool(self._wifi_host) if self._conn_mode == MODE_V1R else None,
            "cloud_linked": self.cloud_enabled,
            "cloud_token_expires": token_expires,
            "cloud_last_ok": self.cloud_last_ok.isoformat() if self.cloud_last_ok else None,
        }

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def _process_alerts(self, data: dict[str, Any]) -> None:
        now = dt_util.utcnow()
        self.alert_changes = []
        if data.get("local_ok") and data.get("alerts") is not None:
            packs = {p["name"]: p["alerts"] for p in (data.get("packs") or {}).values() if p.get("alerts")}
            self.alert_changes = self.alerts.update(data["alerts"], packs, now)
            for change in self.alert_changes:
                _LOGGER.info("Powerwall alert %s: %s (%s)", change.kind, change.name, change.description)
        active = list(self.alerts.since)
        data["alert_level"] = level(active)
        data["alert_summary"] = summary(active)
        data["alert_details"] = self.alerts.details()
        data["alert_problems"] = self.alerts.problems(now, self.problem_after)
        self._sync_issues(data["alert_problems"])

    def _sync_issues(self, problems: list[str]) -> None:
        """One repair issue per lasting warning or fault; removed when it clears."""
        wanted = {f"alert_{self.din}_{name}": name for name in problems}
        for issue_id, name in wanted.items():
            if issue_id in self._issues:
                continue
            severity, description = describe(name)
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.ERROR if severity == SEVERITY_FAULT else ir.IssueSeverity.WARNING,
                translation_key="powerwall_alert",
                translation_placeholders={
                    "alert": name,
                    "description": description,
                    "since": self.alerts.since[name].isoformat(timespec="minutes"),
                    "minutes": str(int(self.problem_after.total_seconds() // 60)),
                },
            )
        for issue_id in self._issues - wanted.keys():
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._issues = set(wanted)

    def async_clear_issues(self) -> None:
        for issue_id in self._issues:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._issues = set()
        if self._cloud_issue:
            ir.async_delete_issue(self.hass, DOMAIN, f"cloud_login_{self.config_entry.entry_id}")
            self._cloud_issue = False
