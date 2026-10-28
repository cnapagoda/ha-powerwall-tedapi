"""Alert events: one event each time the Powerwall raises or clears an alert."""
from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback

from .coordinator import TedapiConfigEntry, TedapiCoordinator
from .entity import AddConfigEntryEntitiesCallback, TedapiEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TedapiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([AlertEvent(entry.runtime_data.coordinator)])


class AlertEvent(TedapiEntity, EventEntity):
    """Shows alert changes in the device's activity and the logbook, and lets
    automations trigger on them."""

    _attr_translation_key = "alert"
    _attr_event_types = ["raised", "cleared"]

    def __init__(self, coordinator: TedapiCoordinator) -> None:
        super().__init__(coordinator, "alert_event")

    @callback
    def _handle_coordinator_update(self) -> None:
        for change in self.coordinator.alert_changes:
            self._trigger_event(change.kind, {
                "alert": change.name,
                "severity": change.severity,
                "description": change.description,
                "sources": change.sources,
            })
            self.async_write_ha_state()
