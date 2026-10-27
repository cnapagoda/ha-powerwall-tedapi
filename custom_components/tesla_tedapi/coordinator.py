"""Data coordinator for the Powerwall TEDAPI integration.

Readings come from the gateway over the local network. Settings are written to
the gateway when the connection is signed (v1r) and to the Tesla cloud as a
backup, when a Tesla account is linked."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import os
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .alerts import SEVERITY_FAULT, AlertChange, AlertTracker, describe, level, summary
from .const import (
    CONF_CONNECTION_MODE,
    CONF_GW_PWD,
    CONF_HOST,
    CONF_OWNERS_AUTHPATH,
    CONF_OWNERS_EMAIL,
    CONF_RSA_KEY_PATH,
    CONF_SCAN_INTERVAL,
    CONF_WIFI_HOST,
    DEFAULT_HOST_WIFI,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MODE_V1R,
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


def _extract_string_voltages(vitals: dict) -> dict[str, float]:
    """Per-string PV voltages from the PVAC vitals blocks."""
    strings: dict[str, float] = {}
    for device_key, device_data in (vitals or {}).items():
        if ("PVAC" in device_key or "TEPVAC" in device_key) and isinstance(device_data, dict):
            for i in range(1, 5):
                val = device_data.get(f"PVAC_PVVoltage_Array{i}")
                if val is not None and float(val) > 0:
                    strings[f"string_{i}_voltage"] = round(float(val), 1)
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

        cloud = pypowerwall.Powerwall(
            host="",
            password="",
            email=self._owners_email,
            timezone=str(self.hass.config.time_zone),
            cloudmode=True,
            authpath=self._owners_authpath,
            auto_select=True,
            failover=False,
        )
        if cloud.mode != "cloud" or cloud.client is None:
            raise RuntimeError("Tesla cloud login failed")
        return cloud

    def _get_cloud(self):
        if not self.cloud_enabled:
            raise RuntimeError("no Tesla account linked")
        if self._cloud is None:
            self._cloud = self._init_cloud()
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
            "strings": _extract_string_voltages(vitals),
            "tedapi_mode": getattr(pw, "tedapi_mode", None),
            **self._fetch_extras(pw, vitals),
            "local_ok": True,
            **{key: control.get(key) for key in CONTROL_KEYS},
            "control_source": control.get("source"),
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
            try:
                cloud_values = self._read_control_from(self._get_cloud())
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Cloud settings read failed: %s", err)
                self._cloud = None
                cloud_values = {}
            for key in missing:
                values[key] = cloud_values.get(key)
            got = [key for key in missing if values[key] is not None]
            if got:
                source = "cloud" if len(got) == len(CONTROL_KEYS) else "mixed"
        values["source"] = source if any(values.get(k) is not None for k in CONTROL_KEYS) else None
        return values

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

    def _set_grid_export(self, mode: str) -> None:
        self._write(f"Grid export set to {mode}", lambda pw: pw.set_grid_export(mode))

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

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            try:
                if self._pw is None:
                    self._pw = await self.hass.async_add_executor_job(self._init_powerwall)
                data = await self.hass.async_add_executor_job(self._fetch_all)
            except Exception as err:
                self._pw = None
                if not (self.cloud_enabled and self.din):
                    raise
                _LOGGER.debug("Gateway read failed (%s), settings from the cloud", err)
                data = await self.hass.async_add_executor_job(self._fetch_cloud_only)
                if data.get("control_source") is None:
                    raise
        except Exception as err:
            self._pw = None
            raise UpdateFailed(f"Powerwall update failed: {err}") from err

        if data.get("din"):
            self.din = data["din"]
        if data.get("firmware_version"):
            self.firmware_version = data["firmware_version"]
        self._process_alerts(data)
        return data

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
        data["alert_problems"] = self.alerts.problems(now)
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
                },
            )
        for issue_id in self._issues - wanted.keys():
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._issues = set(wanted)

    def async_clear_issues(self) -> None:
        for issue_id in self._issues:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._issues = set()
