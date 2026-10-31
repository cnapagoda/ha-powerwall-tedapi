"""Sensors for the Tesla Powerwall TEDAPI integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .alerts import LEVELS
from .coordinator import TedapiConfigEntry, TedapiCoordinator
from .entity import AddConfigEntryEntitiesCallback, TedapiEntity
from .firmware import split_version
from .outages import month_start
from .tariff import current as tariff_now, normalize as tariff_normalize

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
    above 100 %: Tesla builds in more capacity than it rates."""
    packs = d.get("packs") or {}
    if not packs:
        return None
    return _pct(d.get("battery_full_kwh"), NOMINAL_PACK_KWH * len(packs))


def _health_attrs(d: dict[str, Any]) -> dict[str, Any]:
    packs = list((d.get("packs") or {}).values())
    discharged = [p["discharged_kwh"] for p in packs if p.get("discharged_kwh") is not None]
    return {
        "nominal_kwh_per_pack": NOMINAL_PACK_KWH,
        "packs": len(packs),
        "warranty_kwh": round(NOMINAL_PACK_KWH * WARRANTY_SHARE * len(packs), 2),
        "lifetime_discharged_kwh": round(sum(discharged), 1) if discharged else None,
    }


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
        value_fn=lambda d: d.get("grid_status"),
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
    # Capacity
    TedapiSensorEntityDescription(
        key="battery_full_capacity",
        translation_key="battery_full_capacity",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("battery_full_kwh"),
        attrs_fn=lambda d: {
            p["name"]: {"serial": sn, "full_kwh": p["full_kwh"], "remaining_kwh": p["remaining_kwh"]}
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


# An unused input reports a small standing voltage (15 V on a Powerwall 3) in
# Pv_Standby; a connected string reads far more in daylight.
IN_USE_VOLTS = 60


def _input_in_use(values: dict[str, Any]) -> bool:
    return bool((values.get("power") or 0) > 0 or (values.get("voltage") or 0) >= IN_USE_VOLTS
                or values.get("state") not in (None, "Pv_Standby", "PV_Standby"))


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

    # Solar inputs, inverter fans and battery packs come from the readings: add
    # them when they first appear (also after a start with the gateway
    # unreachable, or when an expansion pack is installed later).
    known: set[str] = set()

    @callback
    def add_new() -> None:
        data = coordinator.data or {}
        if not data.get("local_ok"):
            return
        new: list[TedapiSensorEntityDescription] = []
        for name, values in (data.get("strings") or {}).items():
            if f"string_{name}" not in known and _input_in_use(values):
                known.add(f"string_{name}")
                new += _string_sensors(name)
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
        async_add_entities([TeslaTariffSensor(coordinator)])


class TedapiSensor(TedapiEntity, SensorEntity):
    entity_description: TedapiSensorEntityDescription

    def __init__(self, coordinator: TedapiCoordinator, description: TedapiSensorEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Unavailable while the gateway is unreachable, even though the
        coordinator keeps running on cloud data for the settings."""
        if self.entity_description.always_available:
            return super().available
        return super().available and bool(self.data.get("local_ok", False))

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
        return None


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
