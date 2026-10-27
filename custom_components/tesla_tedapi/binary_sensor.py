"""Dispatch session state."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant

from .coordinator import TedapiConfigEntry, TedapiCoordinator
from .dispatch import DispatchSession
from .entity import AddConfigEntryEntitiesCallback, TedapiEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TedapiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    dispatch = entry.runtime_data.dispatch
    if dispatch is not None:
        async_add_entities([DispatchActiveSensor(entry.runtime_data.coordinator, dispatch)])


class DispatchActiveSensor(TedapiEntity, BinarySensorEntity):
    """On while a dispatch session started by this integration runs."""

    _attr_translation_key = "dispatch_active"

    def __init__(self, coordinator: TedapiCoordinator, dispatch: DispatchSession) -> None:
        super().__init__(coordinator, "dispatch_active")
        self._dispatch = dispatch

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._dispatch.async_add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self._dispatch.active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = self._dispatch.as_dict()
        attrs.pop("active", None)
        return attrs
