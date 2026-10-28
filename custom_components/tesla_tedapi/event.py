"""Alert events: one event each time the Powerwall raises or clears an alert."""
from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback

from .alerts import SEVERITY_STATUS
from .const import CONF_STATUS_EVENTS
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
    automations trigger on them. Status alerts (normal operation, such as
    "Export limited") are left out unless the option says otherwise: some come
    and go every few minutes."""

    _attr_translation_key = "alert"
    _attr_event_types = ["raised", "cleared"]

    def __init__(self, coordinator: TedapiCoordinator) -> None:
        super().__init__(coordinator, "alert_event")
        self._include_status = coordinator.config_entry.options.get(CONF_STATUS_EVENTS, False)

    @callback
    def _handle_coordinator_update(self) -> None:
        for change in self.coordinator.alert_changes:
            if change.severity == SEVERITY_STATUS and not self._include_status:
                continue
            self._trigger_event(change.kind, {
                "alert": change.name,
                "severity": change.severity,
                "description": change.description,
                "sources": change.sources,
            })
        # Also writes availability when there was nothing to report.
        super()._handle_coordinator_update()
