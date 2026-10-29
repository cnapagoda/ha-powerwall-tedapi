"""Backup reserve setting."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant

from .coordinator import TedapiConfigEntry, TedapiCoordinator
from .entity import AddConfigEntryEntitiesCallback, TedapiCloudEntity, TedapiControlEntity, add_when_reported


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TedapiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator.control_enabled:
        async_add_entities([BackupReserveNumber(coordinator)])
    if coordinator.cloud_enabled:
        add_when_reported(entry, coordinator, async_add_entities,
                          [("off_grid_reserve_supported", lambda: OffGridReserveNumber(coordinator))])


class BackupReserveNumber(TedapiControlEntity, NumberEntity):
    """Backup reserve, on the Tesla app's scale."""

    _attr_translation_key = "backup_reserve"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: TedapiCoordinator) -> None:
        super().__init__(coordinator, "backup_reserve")

    @property
    def native_value(self) -> float | None:
        return self.data.get("reserve")

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_reserve(value)


class OffGridReserveNumber(TedapiCloudEntity, NumberEntity):
    """Battery kept for the house when charging a vehicle during an outage.
    Only on sites that support it; a Tesla cloud setting."""

    _attr_translation_key = "off_grid_reserve"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: TedapiCoordinator) -> None:
        super().__init__(coordinator, "off_grid_reserve", "off_grid_reserve")

    @property
    def native_value(self) -> float | None:
        return self.extras.get("off_grid_reserve")

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_off_grid_reserve(value)
