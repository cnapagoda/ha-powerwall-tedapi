"""Grid charging, Storm Watch and (experimental) on-grid solar curtailment."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .coordinator import TedapiConfigEntry, TedapiCoordinator
from .entity import (
    AddConfigEntryEntitiesCallback,
    TedapiCloudEntity,
    TedapiControlEntity,
    TedapiEntity,
    add_when_reported,
)

# One setting change at a time per platform.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TedapiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator.control_enabled:
        async_add_entities([GridChargingSwitch(coordinator)])
    if coordinator.dispatch_supported:
        async_add_entities([SolarCurtailmentSwitch(coordinator)])
    if coordinator.cloud_enabled:
        add_when_reported(entry, coordinator, async_add_entities,
                          [("storm_watch_capable", lambda: StormWatchSwitch(coordinator))])


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


class StormWatchSwitch(TedapiCloudEntity, SwitchEntity):
    """Tesla's Storm Watch service: the cloud charges the Powerwall to full
    before severe weather. A Tesla cloud setting (through the cloud backup)."""

    _attr_translation_key = "storm_watch"

    def __init__(self, coordinator: TedapiCoordinator) -> None:
        super().__init__(coordinator, "storm_watch", "storm_watch")

    @property
    def is_on(self) -> bool | None:
        value = self.extras.get("storm_watch")
        return bool(value) if value is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_storm_watch(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_storm_watch(False)


class SolarCurtailmentSwitch(TedapiEntity, SwitchEntity):
    """Experimental: Tesla's on-grid solar curtailment flag in the gateway's
    config, read and written over the signed local connection. Disabled by
    default: what the Powerwall does with it is not documented."""

    _attr_translation_key = "solar_curtailment"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: TedapiCoordinator) -> None:
        super().__init__(coordinator, "solar_curtailment")

    @property
    def available(self) -> bool:
        return super().available and bool(self.data.get("local_ok")) and self.data.get("solar_curtailment") is not None

    @property
    def is_on(self) -> bool | None:
        return self.data.get("solar_curtailment")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_solar_curtailment(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_solar_curtailment(False)
