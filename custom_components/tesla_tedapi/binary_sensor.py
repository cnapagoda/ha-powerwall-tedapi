"""Dispatch session state."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant

from .alerts import describe
from .coordinator import TedapiConfigEntry, TedapiCoordinator
from .dispatch import DispatchSession
from .entity import AddConfigEntryEntitiesCallback, TedapiEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TedapiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities: list[BinarySensorEntity] = [AlertProblemSensor(coordinator)]
    dispatch = entry.runtime_data.dispatch
    if dispatch is not None:
        entities.append(DispatchActiveSensor(coordinator, dispatch))
    async_add_entities(entities)


class AlertProblemSensor(TedapiEntity, BinarySensorEntity):
    """On while a Powerwall warning or fault has lasted the problem delay
    (10 minutes unless changed in the options)."""

    _attr_translation_key = "alert_problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: TedapiCoordinator) -> None:
        super().__init__(coordinator, "alert_problem")

    @property
    def is_on(self) -> bool:
        return bool(self.data.get("alert_problems"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "problems": [
                {"alert": name, "description": describe(name)[1], "severity": describe(name)[0]}
                for name in self.data.get("alert_problems") or []
            ],
            "minutes_before_problem": int(self.coordinator.problem_after.total_seconds() // 60),
        }


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
