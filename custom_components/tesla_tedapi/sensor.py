"""Sensors for the Tesla Powerwall TEDAPI integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import phases
from .alerts import LEVELS
from .cloud import LIVE_READINGS
from .const import DOMAIN
from .coordinator import TedapiConfigEntry, TedapiCoordinator
from .daily import day_start
from .entity import AddConfigEntryEntitiesCallback, TedapiEntity
from .firmware import split_version
from .flows import FLOWS
from .outages import month_start
from .tariff import current as tariff_now, normalize as tariff_normalize
from .wear import round_trip_efficiency

# Coordinator-based: no limit on parallel updates.
PARALLEL_UPDATES = 0

# Usable energy per pack in Tesla's spec (Powerwall 2/3 and the expansion pack).
NOMINAL_PACK_KWH = 13.5
# The warranty commonly quoted for a pack: 70 % of that after 10 years.
WARRANTY_SHARE = 0.7


@dataclass(frozen=True, kw_only=True)
class TedapiSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
    last_reset_fn: Callable[[dict[str, Any]], datetime | None] | None = None
    always_available: bool = False


def _outages(d: dict[str, Any]) -> dict[str, Any]:
    return d.get("outages") or {}


def _minutes(seconds: float | None) -> float | None:
    return round(seconds / 60, 1) if isinstance(seconds, (int, float)) else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _month_start(d: dict[str, Any]) -> datetime | None:
    month = _outages(d).get("month")
    return month_start(month) if month else None


def _pct(part: float | None, whole: float | None) -> float | None:
    if not isinstance(part, (int, float)) or not isinstance(whole, (int, float)) or whole <= 0:
        return None
    return round(part / whole * 100, 1)


def _health(d: dict[str, Any]) -> float | None:
    """Full energy against the rated 13.5 kWh per pack. A new pack reads
    above 100 %: Tesla builds in more capacity than it rates.

    The system total counts every pack, also those missing from the readings
    (a follower Powerwall 3 on the signed connection); dividing it by the
    packs that are read made health read about 160 %. While some are missing,
    it is divided by the number of packs in the gateway's config. Otherwise
    the packs' own full energy is used where they report it."""
    packs = list((d.get("packs") or {}).values())
    if not packs:
        return None
    total = d.get("battery_full_kwh")
    if _pack_total(d) > len(packs) and isinstance(total, (int, float)):
        return _pct(total, NOMINAL_PACK_KWH * _pack_total(d))
    full = [p["full_kwh"] for p in packs if isinstance(p.get("full_kwh"), (int, float))]
    if full:
        return _pct(sum(full), NOMINAL_PACK_KWH * len(full))
    return _pct(d.get("battery_full_kwh"), NOMINAL_PACK_KWH * len(packs))


def _pack_total(d: dict[str, Any]) -> int:
    """Packs in the system: the gateway's config count when it lists more
    than the readings have, else the packs read."""
    read = len(d.get("packs") or {})
    listed = d.get("pack_count")
    return listed if isinstance(listed, int) and not isinstance(listed, bool) and listed > read else read


def _health_attrs(d: dict[str, Any]) -> dict[str, Any]:
    packs = list((d.get("packs") or {}).values())
    discharged = [p["discharged_kwh"] for p in packs if p.get("discharged_kwh") is not None]
    return {
        "nominal_kwh_per_pack": NOMINAL_PACK_KWH,
        "packs": _pack_total(d),
        # Fewer than "packs" while a follower Powerwall 3 is missing from the
        # readings; lifetime_discharged_kwh then covers these only.
        "packs_read": len(packs),
        "warranty_kwh": round(NOMINAL_PACK_KWH * WARRANTY_SHARE * _pack_total(d), 2),
        "lifetime_discharged_kwh": round(sum(discharged), 1) if discharged else None,
    }


GRID_STATUSES = ("up", "down", "syncing")


def _grid_status(value: Any) -> str | None:
    status = str(value).lower() if value is not None else None
    return status if status in GRID_STATUSES else None


def _firmware(d: dict[str, Any]) -> tuple[str | None, str | None]:
    return split_version(d.get("firmware_version"))


def _power(key: str) -> TedapiSensorEntityDescription:
    return TedapiSensorEntityDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, k=key: d.get(k),
    )


def _split_power(key: str, source: str, sign: int) -> TedapiSensorEntityDescription:
    """One direction of a signed power reading, never negative (0 when the
    power flows the other way)."""
    return TedapiSensorEntityDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, s=source: round(max(sign * d[s], 0.0), 1) if d.get(s) is not None else None,
    )


def _energy(key: str) -> TedapiSensorEntityDescription:
    return TedapiSensorEntityDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        value_fn=lambda d, k=key: (d.get("energy") or {}).get(k),
    )


def _today_start(d: dict[str, Any]) -> datetime | None:
    day = d.get("energy_day")
    return day_start(day) if day else None


def _energy_today(key: str) -> TedapiSensorEntityDescription:
    """Today's energy (reset at local midnight), like Tesla Fleet's daily sensors."""
    return TedapiSensorEntityDescription(
        key=f"{key}_today",
        translation_key=f"{key}_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda d, k=key: (d.get("energy_today") or {}).get(k),
        last_reset_fn=_today_start,
    )


def _flow(name: str) -> TedapiSensorEntityDescription:
    """Lifetime energy of one flow (solar to home, grid to battery, ...),
    attributed from the power readings (see flows.py)."""
    return TedapiSensorEntityDescription(
        key=f"flow_{name}",
        translation_key=f"flow_{name}",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda d, n=name: (d.get("flows") or {}).get(n),
    )


def _trend(d: dict[str, Any]) -> dict[str, Any]:
    return d.get("capacity_trend") or {}


def _efficiency(d: dict[str, Any]) -> float | None:
    energy = d.get("energy") or {}
    return round_trip_efficiency(energy.get("battery_energy_charged"), energy.get("battery_energy_discharged"))


# Sensors the cloud backup's live status can stand in for while the gateway
# can't be reached (the cloud readings option): the readings themselves and
# the ones split from them.
CLOUD_READING_SENSORS = frozenset({
    *LIVE_READINGS, "grid_import_power", "grid_export_power", "battery_discharge_power", "battery_charge_power"})


SENSORS: tuple[TedapiSensorEntityDescription, ...] = (
    # Power: battery + = discharging; grid + = importing
    _power("solar_power"),
    _power("battery_power"),
    _power("grid_power"),
    _power("home_power"),
    # The same, split by direction
    _split_power("grid_import_power", "grid_power", 1),
    _split_power("grid_export_power", "grid_power", -1),
    _split_power("battery_discharge_power", "battery_power", 1),
    _split_power("battery_charge_power", "battery_power", -1),
    # Charge
    TedapiSensorEntityDescription(
        key="battery_level",
        translation_key="battery_level",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("battery_level"),
    ),
    TedapiSensorEntityDescription(
        key="battery_soe",
        translation_key="battery_soe",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("battery_soe"),
    ),
    # Grid
    TedapiSensorEntityDescription(
        key="grid_status",
        translation_key="grid_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(GRID_STATUSES),
        # The gateway reports UP, DOWN or SYNCING; the states are translated.
        value_fn=lambda d: _grid_status(d.get("grid_status")),
    ),
    TedapiSensorEntityDescription(
        key="grid_frequency",
        translation_key="grid_frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("grid_frequency"),
    ),
    TedapiSensorEntityDescription(
        key="grid_voltage",
        translation_key="grid_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("grid_voltage"),
    ),
    # Lifetime energy (gateway meters)
    _energy("grid_energy_imported"),
    _energy("grid_energy_exported"),
    _energy("battery_energy_charged"),
    _energy("battery_energy_discharged"),
    _energy("solar_energy_produced"),
    _energy("home_energy_consumed"),
    # Today's grid counters against the grid power readings
    TedapiSensorEntityDescription(
        key="grid_counter_check",
        translation_key="grid_counter_check",
        device_class=SensorDeviceClass.ENUM,
        options=["ok", "mismatch", "unknown"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (d.get("grid_check") or {}).get("state"),
        attrs_fn=lambda d: {k: v for k, v in (d.get("grid_check") or {}).items() if k != "state"},
    ),
    # Today's energy
    _energy_today("grid_energy_imported"),
    _energy_today("grid_energy_exported"),
    _energy_today("battery_energy_charged"),
    _energy_today("battery_energy_discharged"),
    _energy_today("solar_energy_produced"),
    _energy_today("home_energy_consumed"),
    # Energy flows (attributed from the power readings)
    *(_flow(name) for name in FLOWS),
    # Capacity
    TedapiSensorEntityDescription(
        key="battery_full_capacity",
        translation_key="battery_full_capacity",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("battery_full_kwh"),
        # Each pack's capacity. Not its remaining energy, which changes on
        # every reading: that is on each pack's capacity sensor and in
        # Battery energy remaining.
        attrs_fn=lambda d: {
            p["name"]: {"serial": sn, "full_kwh": p["full_kwh"]}
            for sn, p in (d.get("packs") or {}).items()
        },
    ),
    TedapiSensorEntityDescription(
        key="battery_energy_remaining",
        translation_key="battery_energy_remaining",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("battery_remaining_kwh"),
    ),
    TedapiSensorEntityDescription(
        key="battery_health",
        translation_key="battery_health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_health,
        attrs_fn=_health_attrs,
    ),
    # Battery economics
    TedapiSensorEntityDescription(
        key="battery_round_trip_efficiency",
        translation_key="battery_round_trip_efficiency",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Lifetime energy out of the battery over lifetime energy in.
        value_fn=_efficiency,
    ),
    TedapiSensorEntityDescription(
        key="battery_capacity_fade",
        translation_key="battery_capacity_fade",
        native_unit_of_measurement=f"{PERCENTAGE}/y",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        # Of the rated capacity, per year: negative while the capacity falls.
        # Unknown until there are enough daily samples (see wear.py).
        value_fn=lambda d: _trend(d).get("pct_per_year"),
        attrs_fn=lambda d: {key: _trend(d).get(key) for key in ("kwh_per_year", "fit_kwh", "samples", "days")},
    ),
    TedapiSensorEntityDescription(
        key="battery_warranty_date",
        translation_key="battery_warranty_date",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        # When the capacity trend reaches the warranty level; unknown while
        # the trend is flat or rising, or there isn't enough history.
        value_fn=lambda d: _trend(d).get("warranty_date") if isinstance(_trend(d).get("warranty_date"), date) else None,
    ),
    TedapiSensorEntityDescription(
        key="backup_time_remaining",
        translation_key="backup_time_remaining",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.get("backup_hours"),
    ),
    # Temperatures
    TedapiSensorEntityDescription(
        key="inverter_ambient_temperature",
        translation_key="inverter_ambient_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("inverter_ambient_temp"),
    ),
    TedapiSensorEntityDescription(
        key="inverter_heatsink_temperature",
        translation_key="inverter_heatsink_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("inverter_heatsink_temp"),
    ),
    # Diagnostics
    TedapiSensorEntityDescription(
        key="alerts",
        translation_key="alerts",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Number of alerts the gateway reports, including informational ones
        # such as SystemConnectedToGrid; the list is in the attributes.
        value_fn=lambda d: len(d["alerts"]) if d.get("alerts") is not None else None,
        attrs_fn=lambda d: {
            "alerts": d.get("alerts") or [],
            "pack_alerts": {p["name"]: p["alerts"] for p in (d.get("packs") or {}).values()},
            # Each alert with severity, plain-language description and start time.
            "details": d.get("alert_details") or [],
        },
    ),
    TedapiSensorEntityDescription(
        key="alert_level",
        translation_key="alert_level",
        device_class=SensorDeviceClass.ENUM,
        options=list(LEVELS),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("alert_level"),
    ),
    TedapiSensorEntityDescription(
        key="active_alerts",
        translation_key="active_alerts",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("alert_summary"),
        attrs_fn=lambda d: {"details": d.get("alert_details") or []},
    ),
    TedapiSensorEntityDescription(
        key="gateway_started",
        translation_key="gateway_started",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("gateway_started"),
    ),
    TedapiSensorEntityDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        # The gateway reports "26.34.1 dcee474d": version, then build hash.
        value_fn=lambda d: _firmware(d)[0],
        attrs_fn=lambda d: {"build": _firmware(d)[1], **(d.get("firmware") or {})},
    ),
    TedapiSensorEntityDescription(
        key="setup_health",
        translation_key="setup_health",
        device_class=SensorDeviceClass.ENUM,
        options=["ok", "degraded"],
        entity_category=EntityCategory.DIAGNOSTIC,
        # Settings and cloud checks still mean something while the gateway is down.
        always_available=True,
        value_fn=lambda d: (d.get("health") or {}).get("state"),
        attrs_fn=lambda d: {k: v for k, v in (d.get("health") or {}).items() if k != "state"},
    ),
    TedapiSensorEntityDescription(
        key="connection",
        translation_key="connection",
        device_class=SensorDeviceClass.ENUM,
        options=["v1r", "wifi"],
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: "v1r" if d.get("tedapi_mode") == "v1r" else ("wifi" if d.get("tedapi_mode") else None),
    ),
    # Grid outages (kept while the gateway is unreachable; counts per month)
    TedapiSensorEntityDescription(
        key="last_grid_outage",
        translation_key="last_grid_outage",
        device_class=SensorDeviceClass.TIMESTAMP,
        always_available=True,
        value_fn=lambda d: _outages(d).get("last_start"),
        attrs_fn=lambda d: {"ended": _iso(_outages(d).get("last_end")), "ongoing": _outages(d).get("ongoing")},
    ),
    TedapiSensorEntityDescription(
        key="last_grid_outage_duration",
        translation_key="last_grid_outage_duration",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        suggested_display_precision=0,
        always_available=True,
        value_fn=lambda d: _minutes(_outages(d).get("last_duration")),
    ),
    TedapiSensorEntityDescription(
        key="grid_outages_month",
        translation_key="grid_outages_month",
        state_class=SensorStateClass.TOTAL,
        always_available=True,
        value_fn=lambda d: _outages(d).get("month_count"),
        last_reset_fn=_month_start,
    ),
    TedapiSensorEntityDescription(
        key="grid_outage_time_month",
        translation_key="grid_outage_time_month",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        always_available=True,
        value_fn=lambda d: _minutes(_outages(d).get("month_seconds")),
        last_reset_fn=_month_start,
    ),
)


def _string(d: dict[str, Any], name: str) -> dict[str, Any]:
    return (d.get("strings") or {}).get(name) or {}


_LOGGER = logging.getLogger(__name__)

# An unused input reports a small standing voltage (15 V on a Powerwall 3) in
# Pv_Standby, and now and then 1 W of noise; a connected string reads far more
# in daylight.
IN_USE_VOLTS = 60
IN_USE_WATTS = 20
STRING_KINDS = ("power", "voltage", "current")


def _input_in_use(values: dict[str, Any]) -> bool:
    return bool((values.get("power") or 0) >= IN_USE_WATTS or (values.get("voltage") or 0) >= IN_USE_VOLTS
                or values.get("state") not in (None, "Pv_Standby", "PV_Standby"))


async def _async_remove_unused_inputs(hass: HomeAssistant, entry: TedapiConfigEntry, din: str) -> None:
    """Remove the solar input sensors of inputs that never produced: their
    power never reached IN_USE_WATTS in the recorder's statistics. They were
    created by older versions (or by noise) for inputs with nothing
    connected, e.g. with an external inverter, and showed as unavailable.
    Inputs without statistics are left alone."""
    registry = er.async_get(hass)
    prefix = f"{din}_string_"
    names = {e.unique_id[len(prefix):-len("_power")]: e.entity_id
             for e in er.async_entries_for_config_entry(registry, entry.entry_id)
             if e.domain == "sensor" and e.unique_id.startswith(prefix) and e.unique_id.endswith("_power")}
    if not names:
        return
    try:
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        stats = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, datetime(2000, 1, 1, tzinfo=UTC), None,
            set(names.values()), "month", None, {"max"})
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Solar input cleanup skipped: %s", err)
        return
    for name, entity_id in names.items():
        rows = stats.get(entity_id) or []
        peaks = [row.get("max") for row in rows if isinstance(row.get("max"), (int, float))]
        if not peaks or max(peaks) >= IN_USE_WATTS:
            continue
        _LOGGER.info("Solar input %s never produced (at most %s W): removing its sensors", name, max(peaks))
        for kind in STRING_KINDS:
            if eid := registry.async_get_entity_id("sensor", DOMAIN, f"{prefix}{name}_{kind}"):
                registry.async_remove(eid)


def _string_sensors(name: str) -> list[TedapiSensorEntityDescription]:
    """Solar input (MPPT) `name` of the Powerwall 3's built-in inverter."""
    placeholders = {"string": name}
    return [
        TedapiSensorEntityDescription(
            key=f"string_{name}_power",
            translation_key="string_power",
            translation_placeholders=placeholders,
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda d, n=name: _string(d, n).get("power"),
            attrs_fn=lambda d, n=name: {"state": _string(d, n).get("state")},
        ),
        TedapiSensorEntityDescription(
            key=f"string_{name}_voltage",
            translation_key="string_voltage",
            translation_placeholders=placeholders,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_registry_enabled_default=False,
            value_fn=lambda d, n=name: _string(d, n).get("voltage"),
        ),
        TedapiSensorEntityDescription(
            key=f"string_{name}_current",
            translation_key="string_current",
            translation_placeholders=placeholders,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            entity_registry_enabled_default=False,
            value_fn=lambda d, n=name: _string(d, n).get("current"),
        ),
    ]


def _phases(d: dict[str, Any]) -> dict[str, Any]:
    return d.get("phases") or {}


def _phase_voltage_sensor(side: str, phase: str) -> TedapiSensorEntityDescription:
    """The voltage of one phase on the grid side ("grid") or the home side
    ("home") of the gateway. The home side is disabled by default: it reads
    the same as the grid side except during an outage."""
    return TedapiSensorEntityDescription(
        key=f"{side}_voltage_l{phase}",
        translation_key=f"phase_{side}_voltage",
        translation_placeholders={"phase": phase},
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=side == "grid",
        value_fn=lambda d, s=side, p=phase: (_phases(d).get(f"{s}_voltage") or {}).get(p),
    )


def _ct(d: dict[str, Any], meter: str, ct: str) -> dict[str, Any]:
    return ((_phases(d).get("meters") or {}).get(meter) or {}).get(ct) or {}


def _ct_key(meter: str, ct: str, kind: str) -> str:
    return f"meter_{meter.lower()}_ct_{ct.lower()}_{kind}"


def _ct_sensors(meter: str, ct: str) -> list[TedapiSensorEntityDescription]:
    """One current transformer of one of the gateway's meters: real power,
    and (disabled by default) current and reactive power."""
    placeholders = {"meter": meter, "ct": ct}
    return [
        TedapiSensorEntityDescription(
            key=_ct_key(meter, ct, "power"),
            translation_key="meter_ct_power",
            translation_placeholders=placeholders,
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda d, m=meter, c=ct: _ct(d, m, c).get("power"),
        ),
        TedapiSensorEntityDescription(
            key=_ct_key(meter, ct, "current"),
            translation_key="meter_ct_current",
            translation_placeholders=placeholders,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            entity_registry_enabled_default=False,
            value_fn=lambda d, m=meter, c=ct: _ct(d, m, c).get("current"),
        ),
        TedapiSensorEntityDescription(
            key=_ct_key(meter, ct, "reactive_power"),
            translation_key="meter_ct_reactive_power",
            translation_placeholders=placeholders,
            native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
            device_class=SensorDeviceClass.REACTIVE_POWER,
            state_class=SensorStateClass.MEASUREMENT,
            entity_registry_enabled_default=False,
            value_fn=lambda d, m=meter, c=ct: _ct(d, m, c).get("reactive_power"),
        ),
    ]


def _fan(d: dict[str, Any], name: str) -> dict[str, Any]:
    return (d.get("fans") or {}).get(name) or {}


def _fan_sensor(name: str) -> TedapiSensorEntityDescription:
    """Inverter fan `name`: measured speed, with the drive duty (Powerwall 3)
    or target speed (Powerwall 2) in the attributes."""
    return TedapiSensorEntityDescription(
        key=f"fan_{name}_speed",
        translation_key="fan_speed",
        translation_placeholders={"fan": name},
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=lambda d, n=name: _fan(d, n).get("rpm"),
        attrs_fn=lambda d, n=name: {k: v for k, v in _fan(d, n).items() if k != "rpm"},
    )


def _pack(d: dict[str, Any], serial: str) -> dict[str, Any]:
    return (d.get("packs") or {}).get(serial) or {}


def _pack_wear_sensors(serial: str, name: str) -> list[TedapiSensorEntityDescription]:
    """Powerwall 3 battery counters (pypowerwall 0.23+); not on every model."""
    return [
        TedapiSensorEntityDescription(
            key=f"pack_{serial}_discharged",
            translation_key="pack_discharged",
            translation_placeholders={"pack": name},
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda d, sn=serial: _pack(d, sn).get("discharged_kwh"),
        ),
        TedapiSensorEntityDescription(
            key=f"pack_{serial}_cycles",
            translation_key="pack_cycles",
            translation_placeholders={"pack": name},
            state_class=SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=0,
            entity_category=EntityCategory.DIAGNOSTIC,
            # Equivalent full cycles: lifetime discharge over the usable 13.5 kWh.
            value_fn=lambda d, sn=serial: _cycles(_pack(d, sn).get("discharged_kwh")),
            attrs_fn=lambda d, sn=serial: {
                "bms_full_cycles": _pack(d, sn).get("bms_cycles"),
                "over_discharges": _pack(d, sn).get("over_discharges"),
            },
        ),
    ]


def _cycles(discharged: float | None) -> float | None:
    return round(discharged / NOMINAL_PACK_KWH, 1) if isinstance(discharged, (int, float)) else None


def _pack_sensors(serial: str, name: str) -> list[TedapiSensorEntityDescription]:
    return [
        TedapiSensorEntityDescription(
            key=f"pack_{serial}_health",
            translation_key="pack_health",
            translation_placeholders={"pack": name},
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda d, sn=serial: _pct(_pack(d, sn).get("full_kwh"), NOMINAL_PACK_KWH),
            attrs_fn=lambda d, sn=serial: {
                "nominal_kwh": NOMINAL_PACK_KWH,
                "warranty_kwh": round(NOMINAL_PACK_KWH * WARRANTY_SHARE, 2),
            },
        ),
        TedapiSensorEntityDescription(
            key=f"pack_{serial}_capacity",
            translation_key="pack_capacity",
            translation_placeholders={"pack": name},
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY_STORAGE,
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda d, sn=serial: _pack(d, sn).get("full_kwh"),
            attrs_fn=lambda d, sn=serial: {
                "remaining_kwh": _pack(d, sn).get("remaining_kwh"),
                "part_number": _pack(d, sn).get("part_number"),
                "serial": sn,
            },
        ),
        TedapiSensorEntityDescription(
            key=f"pack_{serial}_temperature",
            translation_key="pack_temperature",
            translation_placeholders={"pack": name},
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            # Hottest cell reading; min and shunt in the attributes.
            value_fn=lambda d, sn=serial: _pack(d, sn).get("temp_max"),
            attrs_fn=lambda d, sn=serial: {
                "temp_min": _pack(d, sn).get("temp_min"),
                "shunt_temp": _pack(d, sn).get("shunt_temp"),
                "serial": sn,
            },
        ),
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TedapiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(TedapiSensor(coordinator, d) for d in SENSORS)
    if coordinator.din:
        await _async_remove_unused_inputs(hass, entry, coordinator.din)
    registry = er.async_get(hass)

    # Solar inputs, inverter fans and battery packs come from the readings: add
    # them when they first appear (also after a start with the gateway
    # unreachable, or when an expansion pack is installed later).
    known: set[str] = set()

    def _registered(key: str) -> bool:
        return registry.async_get_entity_id("sensor", DOMAIN, f"{coordinator.din}_{key}") is not None

    @callback
    def add_new() -> None:
        data = coordinator.data or {}
        if not data.get("local_ok"):
            return
        new: list[TedapiSensorEntityDescription] = []
        for name, values in (data.get("strings") or {}).items():
            # In use now, or set up before (an input in use reads as standby
            # at night: keep providing its sensors after a restart then).
            registered = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{coordinator.din}_string_{name}_power") is not None
            if f"string_{name}" not in known and (_input_in_use(values) or registered):
                known.add(f"string_{name}")
                new += _string_sensors(name)
        # Phases and current transformers in use now, or set up before (like
        # the solar inputs: a circuit that is idle now keeps its sensors).
        for side in ("grid", "home"):
            for phase, volts in (_phases(data).get(f"{side}_voltage") or {}).items():
                key = f"{side}_voltage_l{phase}"
                if key not in known and (phases.voltage_in_use(volts) or _registered(key)):
                    known.add(key)
                    new.append(_phase_voltage_sensor(side, phase))
        for meter, cts in (_phases(data).get("meters") or {}).items():
            for ct, values in cts.items():
                key = _ct_key(meter, ct, "power")
                if key not in known and (phases.ct_in_use(values) or _registered(key)):
                    known.add(key)
                    new += _ct_sensors(meter, ct)
        for name in data.get("fans") or {}:
            if f"fan_{name}" not in known:
                known.add(f"fan_{name}")
                new.append(_fan_sensor(name))
        for serial, pack in (data.get("packs") or {}).items():
            if f"pack_{serial}" not in known:
                known.add(f"pack_{serial}")
                new += _pack_sensors(serial, pack["name"])
            if f"pack_{serial}_wear" not in known and pack.get("discharged_kwh") is not None:
                known.add(f"pack_{serial}_wear")
                new += _pack_wear_sensors(serial, pack["name"])
        if new:
            async_add_entities(TedapiSensor(coordinator, d) for d in new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))

    if coordinator.tariff_enabled:
        async_add_entities([TeslaTariffSensor(coordinator), TariffPriceSensor(coordinator, "buy"),
                            TariffPriceSensor(coordinator, "sell")])

    if coordinator.cloud_enabled:
        # Wall Connectors come from the cloud backup's live status: added when
        # one first shows up, also while the gateway can't be reached.
        connectors: set[str] = set()

        @callback
        def add_wall_connectors() -> None:
            found = [din for din in (coordinator.data or {}).get("wall_connectors") or {} if din not in connectors]
            connectors.update(found)
            if found:
                async_add_entities(WallConnectorSensor(coordinator, din, description)
                                   for din in found for description in WALL_CONNECTOR_SENSORS)

        add_wall_connectors()
        entry.async_on_unload(coordinator.async_add_listener(add_wall_connectors))


class TedapiSensor(TedapiEntity, SensorEntity):
    entity_description: TedapiSensorEntityDescription

    # Attributes that change on (nearly) every reading while the state
    # doesn't, or that are only diagnostic detail: shown live, but kept out
    # of the recorder so they don't fill the database.
    _unrecorded_attributes = frozenset({
        # pack capacity / temperature
        "remaining_kwh", "temp_min", "shunt_temp", "part_number", "serial",
        # grid counter check
        "counter_import_kwh", "counter_export_kwh", "power_import_kwh", "power_export_kwh",
        "import_difference_pct", "export_difference_pct", "covered_minutes", "gaps",
        # setup health
        "checks", "cloud_backends", "cloud_token_expires", "cloud_last_ok", "wifi_fallback", "cloud_linked",
        # fans (drive duty, target speed)
        "duty", "target_rpm",
        # alert details (the alert codes themselves stay recorded)
        "details", "pack_alerts",
    })

    def __init__(self, coordinator: TedapiCoordinator, description: TedapiSensorEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Unavailable while the gateway is unreachable, even though the
        coordinator keeps running on cloud data for the settings."""
        if self.entity_description.always_available:
            return super().available
        return super().available and (bool(self.data.get("local_ok", False)) or self._from_cloud)

    @property
    def _from_cloud(self) -> bool:
        """The gateway can't be reached and this reading comes from the cloud
        backup's live status instead (the cloud readings option)."""
        return (self.data.get("readings_source") == "cloud"
                and self.entity_description.key in CLOUD_READING_SENSORS
                and self.entity_description.value_fn(self.data) is not None)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.data)

    @property
    def last_reset(self) -> datetime | None:
        if self.entity_description.last_reset_fn and self.data:
            return self.entity_description.last_reset_fn(self.data)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn and self.data:
            return self.entity_description.attrs_fn(self.data)
        if self._from_cloud:
            # Says so while the reading isn't the gateway's own.
            return {"source": "cloud"}
        return None


@dataclass(frozen=True, kw_only=True)
class WallConnectorSensorEntityDescription(SensorEntityDescription):
    value_key: str


WALL_CONNECTOR_SENSORS = (
    WallConnectorSensorEntityDescription(
        key="power",
        translation_key="wall_connector_power",
        value_key="power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # Tesla's own state numbers, as the cloud reports them.
    WallConnectorSensorEntityDescription(
        key="state",
        translation_key="wall_connector_state",
        value_key="state",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    WallConnectorSensorEntityDescription(
        key="fault_state",
        translation_key="wall_connector_fault_state",
        value_key="fault_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


class WallConnectorSensor(TedapiEntity, SensorEntity):
    """A Wall Connector reading from the cloud backup's live status (the
    gateway doesn't report Wall Connectors). Named after the last characters
    of the Wall Connector's serial number."""

    entity_description: WallConnectorSensorEntityDescription

    def __init__(self, coordinator: TedapiCoordinator, din: str,
                 description: WallConnectorSensorEntityDescription) -> None:
        super().__init__(coordinator, f"wall_connector_{din}_{description.key}")
        self.entity_description = description
        self._din = din
        self._attr_translation_placeholders = {"connector": din[-4:]}

    @property
    def _reading(self) -> dict[str, Any]:
        return (self.data.get("wall_connectors") or {}).get(self._din) or {}

    @property
    def available(self) -> bool:
        return super().available and self._reading.get(self.entity_description.value_key) is not None

    @property
    def native_value(self) -> Any:
        return self._reading.get(self.entity_description.value_key)


class TariffPriceSensor(TedapiEntity, SensorEntity):
    """The Tesla tariff's price now, to buy ("buy") or for feed-in ("sell"),
    per kWh in Home Assistant's currency. Follows the tariff's periods."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: TedapiCoordinator, side: str) -> None:
        super().__init__(coordinator, f"tariff_{side}_price")
        self._side = side
        self._attr_translation_key = f"tariff_{side}_price"
        self._attr_native_unit_of_measurement = f"{coordinator.hass.config.currency}/{UnitOfEnergy.KILO_WATT_HOUR}"

    @property
    def _now(self) -> dict[str, Any]:
        return tariff_now(tariff_normalize(self.coordinator.tariff), dt_util.now())

    @property
    def available(self) -> bool:
        return self.coordinator.tariff is not None and self._now[f"{self._side}_rate"] is not None

    @property
    def native_value(self) -> float | None:
        return self._now[f"{self._side}_rate"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = self._now
        return {"season": now["season"], "period": now["period"]}


class TeslaTariffSensor(TedapiEntity, SensorEntity):
    """Tesla tariff (optional, cloud only): the name as state, the current
    period and rates as attributes, and the raw tariff kept out of the
    recorder."""

    _attr_translation_key = "tesla_tariff"
    _attr_icon = "mdi:cash-clock"
    _unrecorded_attributes = frozenset({"tariff"})

    def __init__(self, coordinator: TedapiCoordinator) -> None:
        super().__init__(coordinator, "tesla_tariff")

    @property
    def available(self) -> bool:
        return self.coordinator.tariff is not None

    @property
    def native_value(self) -> str | None:
        norm = tariff_normalize(self.coordinator.tariff)
        return str(norm["name"])[:255] if norm and norm["name"] else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        tariff = self.coordinator.tariff
        norm = tariff_normalize(tariff)
        if norm is None:
            return None
        return {
            "utility": norm["utility"],
            **tariff_now(norm, dt_util.now()),
            "backend": self.coordinator.tariff_backend,
            "tariff": tariff,
        }
