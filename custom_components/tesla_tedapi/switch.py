"""Grid charging setting."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant

from .coordinator import TedapiConfigEntry, TedapiCoordinator
from .entity import AddConfigEntryEntitiesCallback, TedapiControlEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TedapiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator.control_enabled:
        async_add_entities([GridChargingSwitch(coordinator)])


class GridChargingSwitch(TedapiControlEntity, SwitchEntity):
    """Allow the Powerwall to charge from the grid."""

    _attr_translation_key = "grid_charging"

    def __init__(self, coordinator: TedapiCoordinator) -> None:
        super().__init__(coordinator, "grid_charging")

    @property
    def is_on(self) -> bool | None:
        value = self.data.get("grid_charging")
        return bool(value) if value is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_grid_charging(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_grid_charging(False)
