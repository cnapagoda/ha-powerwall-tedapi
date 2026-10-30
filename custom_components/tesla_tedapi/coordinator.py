"""Data coordinator for the Powerwall TEDAPI integration.

Readings come from the gateway over the local network. Settings are written to
the gateway when the connection is signed (v1r) and to the Tesla cloud as a
backup: a Tesla account linked here (Owner API), the Tesla Fleet integration's
sign-in (Fleet API), or both in the chosen order."""
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
from .cloud import CloudBackend, OwnerSettings
from .const import (
    AUTH_FILE,
    BACKEND_FLEET,
    BACKEND_OWNER,
    BACKUP_EVENTS_SECONDS,
    CLOUD_AUTH_FAILURES,
    CLOUD_CACHE_SECONDS,
    CLOUD_CHECK_SECONDS,
    CLOUD_CHOICES,
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
    OWNER_EXTRAS_SECONDS,
    PASSWORD_REFUSED_POLLS,
    RESTORE_MIN_SECONDS,
    app_soc,
    cloud_choice,
)
from .fleet import FleetBackup, FleetTimeout, fleet_starting

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


def _extract_fans(vitals: dict) -> dict[str, dict[str, Any]]:
    """Inverter fan speeds (RPM): fans A and B of each Powerwall 3 inverter
    (TEPINV blocks, with the drive duty in %), or one fan per Powerwall 2
    inverter (PVAC blocks, with the target speed). Fans of later inverters
    are numbered like the solar inputs ("2A", "2")."""

    def num(v):
        return round(float(v), 1) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def blocks(prefix: str) -> list[dict]:
        return [vitals[k] for k in sorted(vitals or {}) if k.split("--")[0] == prefix and isinstance(vitals[k], dict)]

    fans: dict[str, dict[str, Any]] = {}
    for index, d in enumerate(blocks("TEPINV"), start=1):
        for letter in "AB":
            rpm = num(d.get(f"PCH_FanSpeed_{letter}"))
            if rpm is not None:
                fans[letter if index == 1 else f"{index}{letter}"] = {
                    "rpm": rpm, "duty": num(d.get(f"PCH_FanDuty_{letter}"))}
    if fans:
        return fans
    for index, d in enumerate(blocks("PVAC"), start=1):
        rpm = num(d.get("PVAC_Fan_Speed_Actual_RPM"))
        if rpm is not None:
            fans[str(index)] = {"rpm": rpm, "target_rpm": num(d.get("PVAC_Fan_Speed_Target_RPM"))}
    return fans


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


def _current_backup_event(events: dict[str, Any], now: float) -> dict[str, Any]:
    """{"active", "kind", "start", "end"} for the backup event running at
    now (epoch seconds): the manual one, or a scheduled one such as Storm
    Watch. Not active: the next scheduled one, if any."""
    candidates = []
    manual = events.get("manual_backup")
    if manual:
        candidates.append(("manual", manual.get("start_time"), manual.get("duration_seconds")))
    for event in events.get("backup_events") or []:
        candidates.append((event.get("name") or "scheduled", event.get("start_time"), event.get("duration_seconds")))
    found: dict[str, Any] = {"active": False, "kind": None, "start": None, "end": None}
    for kind, start, duration in sorted(candidates, key=lambda c: c[1] or 0):
        if not isinstance(start, (int, float)) or not isinstance(duration, (int, float)):
            continue
        end = start + duration
        if end <= now:
            continue
        active = start <= now
        if active or found["kind"] is None:
            found = {"active": active, "kind": kind,
                     "start": dt_util.utc_from_timestamp(start), "end": dt_util.utc_from_timestamp(end)}
        if active:
            break
    return found


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

        self.backends: list[CloudBackend] = self._make_backends(hass, data, options)
        self.cloud_enabled: bool = bool(self.backends)
        # Settings can be changed over a signed connection, or through the cloud.
        self.control_enabled: bool = self._conn_mode == MODE_V1R or self.cloud_enabled

        self._pw = None
        # Last good lifetime counters: a missing, zero or lower reading keeps the
        # previous value so total_increasing statistics never see a fake reset.
        self._energy_last: dict[str, float] = {}
        self.din: str | None = entry.unique_id
        self.firmware_version: str | None = None
        self.alerts = AlertTracker()
        # Alerts raised or cleared by the latest reading (for the event entity).
        self.alert_changes: list[AlertChange] = []
        self._issues: set[str] = set()

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
        # Gateway down while the cloud backup keeps the settings: logged once.
        self._gateway_down = False
        self._token_expiry: tuple[float, float | None] | None = None
        # Backup events from the gateway: (monotonic time read, raw result).
        self._backup_events: tuple[float, dict[str, Any]] | None = None
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")

        scan = entry.options.get(CONF_SCAN_INTERVAL, data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan),
        )

    def _make_backends(self, hass: HomeAssistant, data: Any, options: Any) -> list[CloudBackend]:
        """The cloud backups in the order chosen in the options."""
        backends: list[CloudBackend] = []
        started = time.monotonic()
        for name in CLOUD_CHOICES.get(cloud_choice(data, options), ()):
            if name == BACKEND_OWNER and self._owners_email:
                backends.append(CloudBackend(name, self._init_cloud, extras_seconds=OWNER_EXTRAS_SECONDS))
            elif name == BACKEND_FLEET:
                fleet = FleetBackup(hass, lambda: self.din)
                backends.append(CloudBackend(
                    name, lambda fleet=fleet: fleet,
                    starting=lambda: fleet_starting(hass, self.din, started),
                    writable=lambda fleet=fleet: fleet.can_write))
        return backends

    def backend(self, name: str) -> CloudBackend | None:
        return next((b for b in self.backends if b.name == name), None)

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
        return OwnerSettings(cloud)

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
            "fans": _extract_fans(vitals),
            "tedapi_mode": getattr(pw, "tedapi_mode", None),
            "backup_event": self._read_backup_event(pw),
            **self._fetch_extras(pw, vitals),
            "local_ok": True,
            **{key: control.get(key) for key in CONTROL_KEYS},
            "control_source": control.get("source"),
            "control_backend": control.get("backend"),
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
        backend = None
        if missing and self.cloud_enabled:
            cloud_values = self._cloud_settings()
            for key in missing:
                values[key] = cloud_values.get(key)
            got = [key for key in missing if values[key] is not None]
            if got:
                source = "cloud" if len(got) == len(CONTROL_KEYS) else "mixed"
                backend = cloud_values.get("backend")
        values["source"] = source if any(values.get(k) is not None for k in CONTROL_KEYS) else None
        values["backend"] = backend
        return values

    def _cloud_settings(self) -> dict[str, Any]:
        """Settings from the first cloud backup (in order) that gives them;
        "backend" names it."""
        for backend in self.backends:
            values = self._backend_settings(backend)
            if any(v is not None for v in values.values()):
                return {**values, "backend": backend.name}
        return {}

    def _backend_settings(self, backend: CloudBackend, check: bool = False) -> dict[str, Any]:
        """Settings from one cloud backup, cached for CLOUD_CACHE_SECONDS.
        Once its repair is up, only the hourly check (check=True) tries again."""
        now = time.monotonic()
        if backend.cache and now - backend.cache[0] < CLOUD_CACHE_SECONDS:
            return backend.cache[1]
        if (backend.failing and not check) or backend.starting():
            return {}  # Tesla Fleet still starting: not a failure, asked again later
        try:
            values = self._read_control_from(backend.get_client())
        except Exception as err:  # noqa: BLE001
            # Login failed (counted in get_client). Cached like a good read:
            # retrying every poll would hammer the Tesla API.
            _LOGGER.debug("%s settings read failed: %s", backend.label, err)
            backend.drop()
            backend.cache = (now, {})
            return {}
        if any(v is not None for v in values.values()):
            backend.cache = (now, values)
            backend.failures = 0
            backend.last_ok = dt_util.utcnow()
        else:
            # Logged in but every read failed, e.g. a refreshed token the
            # Owner API rejects: log in again next time.
            _LOGGER.debug("%s settings read returned nothing", backend.label)
            backend.failures += 1
            backend.drop()
            backend.cache = (now, {})
        return values

    def _check_cloud(self, backends: list[CloudBackend]) -> None:
        """A fresh read of each backup (not the cache): proves it still works
        and lets pypowerwall refresh the Owner API token while it is idle."""
        for backend in backends:
            backend.cache = None
            self._backend_settings(backend, check=True)
            if backend.name == BACKEND_OWNER:
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

    def _read_backup_event(self, pw) -> dict[str, Any] | None:
        """The backup event running now (manual "max backup" or one the Tesla
        cloud scheduled, such as Storm Watch), from the gateway over signed
        v1r. None when it can't be read."""
        if getattr(pw, "tedapi_mode", None) != "v1r":
            return None
        now = time.monotonic()
        if self._backup_events is None or now - self._backup_events[0] >= BACKUP_EVENTS_SECONDS:
            try:
                events = pw.get_backup_events()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Reading backup events failed: %s", err)
                events = None
            if events is None:
                if self._backup_events is None:
                    return None
            else:
                self._backup_events = (now, events)
        return _current_backup_event(self._backup_events[1], time.time())

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
            "control_backend": control.get("backend"),
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

    def _write(self, what: str, write, local: bool = True) -> None:
        """Run write(pw) on the gateway (signed connection only; local=False
        for cloud-only settings), then on each cloud backup in order until one
        accepts it."""
        for backend in self.backends:
            backend.cache = backend.extras = None
        if not (self.control_enabled if local else self.cloud_enabled):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="settings_need_control")
        # The Wi-Fi password session cannot change settings; the gateway accepts
        # the call and ignores it, so it is not tried.
        if local and self._conn_mode == MODE_V1R:
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
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="write_refused_no_cloud")
        errors: list[str] = []
        for backend in self.backends:
            if backend.starting():
                errors.append(f"{backend.label} is still starting")
                continue
            try:
                ok = self._write_ok(write(backend.get_client()))
            except FleetTimeout as err:
                # It may still have been applied: don't send it elsewhere too.
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="write_cloud_timeout",
                                         translation_placeholders={"backend": backend.label}) from err
            except Exception as err:  # noqa: BLE001
                backend.drop()
                errors.append(f"{backend.label}: {err}")
                continue
            if ok:
                backend.failures = 0
                backend.last_ok = dt_util.utcnow()
                _LOGGER.info("%s (cloud, %s)", what, backend.label)
                return
            errors.append(f"{backend.label} did not accept it")
        _LOGGER.warning("%s: local and cloud writes failed (%s)", what, "; ".join(errors))
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="write_failed")

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

    def _set_storm_watch(self, enabled: bool) -> None:
        # Tesla's Storm Watch service is a cloud setting: no local write.
        self._write(f"Storm Watch set to {'on' if enabled else 'off'}",
                    lambda pw: pw.set_storm_watch(bool(enabled)), local=False)

    def _set_off_grid_reserve(self, percent: float) -> None:
        self._write(f"Off-grid vehicle charging reserve set to {int(percent)}%",
                    lambda pw: pw.set_off_grid_reserve(int(percent)), local=False)

    def _cloud_extras(self) -> dict[str, Any]:
        """Storm Watch and other cloud-only values from the first backup (in
        order) that gives them; "backend" names it."""
        for backend in self.backends:
            if backend.failing or backend.starting():
                continue
            now = time.monotonic()
            if backend.extras is not None and now - backend.extras[0] < backend.extras_seconds:
                values = backend.extras[1]
            else:
                try:
                    values = backend.get_client().get_extras() or {}
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("%s Storm Watch read failed: %s", backend.label, err)
                    values = {}
                backend.extras = (now, values)
            if any(v is not None for v in values.values()):
                return {**values, "backend": backend.name}
        return {}

    def _max_backup(self, minutes: int | None) -> None:
        """Start (minutes) or cancel (None) a manual backup event over signed
        v1r: the Powerwall charges to full and holds it, as in Storm Watch."""
        if self._pw is None or getattr(self._pw, "tedapi_mode", None) != "v1r":
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="max_backup_needs_v1r")
        if minutes is None:
            ok = self._pw.cancel_max_backup()
        else:
            ok = self._pw.schedule_max_backup(duration_seconds=int(minutes) * 60)
        if not ok:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="backup_event_refused")
        _LOGGER.info("Max backup %s", f"started for {minutes} min" if minutes else "cancelled")
        self._backup_events = None  # read it back on the next poll

    async def async_set_storm_watch(self, enabled: bool) -> None:
        await self.hass.async_add_executor_job(self._set_storm_watch, enabled)
        await self.async_request_refresh()

    async def async_set_off_grid_reserve(self, percent: float) -> None:
        await self.hass.async_add_executor_job(self._set_off_grid_reserve, percent)
        await self.async_request_refresh()

    async def async_max_backup(self, minutes: int | None) -> None:
        await self.hass.async_add_executor_job(self._max_backup, minutes)
        await self.async_refresh()

    def _send_dispatch(self, power_watts: float, duration_seconds: int) -> dict:
        """One timed battery setpoint over signed v1r (no cloud equivalent)."""
        if self._pw is None or getattr(self._pw, "tedapi_mode", None) != "v1r":
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="dispatch_needs_v1r")
        try:
            result = self._pw.dispatch_battery_power(power_watts, duration_seconds)
        except ValueError as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="dispatch_invalid",
                                     translation_placeholders={"error": str(err)}) from err
        if not result:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="dispatch_refused")
        return result

    def _send_resume(self) -> dict:
        if self._pw is None or getattr(self._pw, "tedapi_mode", None) != "v1r":
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="dispatch_needs_v1r")
        result = self._pw.resume_battery_operation()
        if not result:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="resume_refused")
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
                data = await self.hass.async_add_executor_job(self._fetch_cloud_only)
                if data.get("control_source") is None:
                    raise
                if not self._gateway_down:
                    self._gateway_down = True
                    _LOGGER.warning("The Powerwall gateway at %s can't be reached (%s); readings are "
                                    "unavailable and settings use the cloud backup", self._host, err)
            else:
                self._password_refused = self._key_refused = 0
                if self._gateway_down:
                    self._gateway_down = False
                    _LOGGER.info("The Powerwall gateway at %s answers again", self._host)
        except Exception as err:
            self._pw = None
            self._sync_cloud_issue()
            raise UpdateFailed(f"Powerwall update failed: {err}") from err

        if data.get("din"):
            self.din = data["din"]
        if data.get("firmware_version"):
            self.firmware_version = data["firmware_version"]
        due = [backend for backend in self.backends if self._cloud_check_due(backend)]
        if due:
            for backend in due:
                backend.check_at = time.monotonic()
            await self.hass.async_add_executor_job(self._check_cloud, due)
        data["cloud_extras"] = await self.hass.async_add_executor_job(self._cloud_extras) if self.backends else {}
        self._process_alerts(data)
        self._sync_cloud_issue()
        for key in DROPPABLE_SETTINGS:
            data[f"{key}_restores"] = self.restores[key]
            data[f"{key}_last_restored"] = self.last_restored[key]
        data["health"] = await self._async_health(data)
        self._store.async_delay_save(self._state_to_save, 10)
        return data

    @staticmethod
    def _cloud_check_due(backend: CloudBackend) -> bool:
        if backend.starting():
            return False  # checked once Tesla Fleet has loaded
        if backend.check_at is None:
            return True
        since_check = time.monotonic() - backend.check_at
        if 0 < backend.failures < CLOUD_AUTH_FAILURES:
            return since_check >= CLOUD_RETRY_SECONDS
        # Working, or its repair is up: hourly is enough.
        # Any successful read counts as a check.
        last_ok = backend.last_ok
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

    def _cloud_issue_id(self, backend: CloudBackend) -> str:
        prefix = "cloud_login" if backend.name == BACKEND_OWNER else "fleet_backup"
        return f"{prefix}_{self.config_entry.entry_id}"

    @staticmethod
    def _cloud_issue_key(backend: CloudBackend) -> str | None:
        """The repair a backup needs: sign in again (Owner API), check Tesla
        Fleet, or give Tesla Fleet the energy_cmds scope."""
        if backend.failing:
            return "cloud_login" if backend.name == BACKEND_OWNER else "fleet_backup"
        if backend.ok and not backend.writable():
            return "fleet_read_only"
        return None

    def _sync_cloud_issue(self) -> None:
        """One repair per backup that needs one; replaced when the reason
        changes, removed when it is fixed."""
        for backend in self.backends:
            issue_id = self._cloud_issue_id(backend)
            key = self._cloud_issue_key(backend)
            if key == backend.issue:
                continue
            if backend.issue:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            if key:
                ir.async_create_issue(
                    self.hass, DOMAIN, issue_id,
                    is_fixable=key == "cloud_login",
                    is_persistent=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=key,
                    data={"entry_id": self.config_entry.entry_id},
                )
            backend.issue = key

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
            # One working backup is enough; a failing one has its own repair.
            # Tesla Fleet still loading after a restart is not a problem yet.
            checks["cloud_backup"] = any((backend.ok and backend.writable()) or backend.starting()
                                         for backend in self.backends)
        if self.backend(BACKEND_OWNER):
            # Only an Owner API sign-in here has a token file; Tesla Fleet
            # keeps its own.
            now = time.monotonic()
            if self._token_expiry is None or now - self._token_expiry[0] > 600:
                self._token_expiry = (now, await self.hass.async_add_executor_job(self._read_token_expiry))
            if self._token_expiry[1]:
                token_expires = dt_util.utc_from_timestamp(self._token_expiry[1]).isoformat()
        problems = [name for name, ok in checks.items() if not ok]
        last_oks = [backend.last_ok for backend in self.backends if backend.last_ok]
        return {
            "state": "degraded" if problems else "ok",
            "checks": checks,
            "problems": problems,
            "wifi_fallback": bool(self._wifi_host) if self._conn_mode == MODE_V1R else None,
            "cloud_linked": self.cloud_enabled,
            "cloud_backends": {
                backend.name: {
                    "ok": backend.ok,
                    "starting": backend.starting(),
                    "can_change_settings": backend.writable(),
                    "failures": backend.failures,
                    "last_ok": backend.last_ok.isoformat() if backend.last_ok else None,
                }
                for backend in self.backends
            },
            "cloud_token_expires": token_expires,
            "cloud_last_ok": max(last_oks).isoformat() if last_oks else None,
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
        for backend in self.backends:
            if backend.issue:
                ir.async_delete_issue(self.hass, DOMAIN, self._cloud_issue_id(backend))
                backend.issue = None
