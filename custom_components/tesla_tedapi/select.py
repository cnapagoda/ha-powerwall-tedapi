"""Operation mode and grid export settings."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant

from .const import GRID_EXPORT_MODES, OPERATION_MODES
from .coordinator import TedapiConfigEntry, TedapiCoordinator
from .entity import AddConfigEntryEntitiesCallback, TedapiControlEntity


@dataclass(frozen=True, kw_only=True)
class TedapiSelectEntityDescription(SelectEntityDescription):
    data_key: str
    set_fn: Callable[[TedapiCoordinator, str], Awaitable[None]]


SELECTS = (
    TedapiSelectEntityDescription(
        key="operation_mode",
        translation_key="operation_mode",
        options=list(OPERATION_MODES),
        data_key="operation_mode",
        set_fn=lambda c, v: c.async_set_mode(v),
    ),
    TedapiSelectEntityDescription(
        key="grid_export",
        translation_key="grid_export",
        options=list(GRID_EXPORT_MODES),
        data_key="grid_export",
        set_fn=lambda c, v: c.async_set_grid_export(v),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TedapiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator.control_enabled:
        async_add_entities(TedapiSelect(coordinator, d) for d in SELECTS)


class TedapiSelect(TedapiControlEntity, SelectEntity):
    entity_description: TedapiSelectEntityDescription

    def __init__(self, coordinator: TedapiCoordinator, description: TedapiSelectEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def current_option(self) -> str | None:
        value = self.data.get(self.entity_description.data_key)
        return value if value in self.options else None

    async def async_select_option(self, option: str) -> None:
        await self.entity_description.set_fn(self.coordinator, option)
