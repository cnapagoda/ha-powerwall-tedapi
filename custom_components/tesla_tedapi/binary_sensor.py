"""Grid connection, alerts, dispatch sessions, backup events and cloud states."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant

from .alerts import describe
from .coordinator import TedapiConfigEntry, TedapiCoordinator
from .dispatch import DispatchSession
from .entity import AddConfigEntryEntitiesCallback, TedapiCloudEntity, TedapiEntity, add_when_reported
from .off_grid import OffGridSession

# Coordinator-based: no limit on parallel updates.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TedapiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities: list[BinarySensorEntity] = [GridConnectedSensor(coordinator, entry.runtime_data.off_grid),
                                          AlertProblemSensor(coordinator)]
    dispatch = entry.runtime_data.dispatch
    if dispatch is not None:
        entities.append(DispatchActiveSensor(coordinator, dispatch))
    if coordinator.dispatch_supported:
        entities.append(BackupEventSensor(coordinator))
    async_add_entities(entities)
    if coordinator.cloud_enabled:
        add_when_reported(entry, coordinator, async_add_entities, [
            ("storm_watch_capable", lambda: CloudStateSensor(coordinator, "storm_watch_active", "storm_active")),
            ("grid_services_enabled",
             lambda: CloudStateSensor(coordinator, "grid_services_active", "grid_services_active")),
        ])


class GridConnectedSensor(TedapiEntity, BinarySensorEntity):
    """On while the Powerwall is connected to the grid; off while it runs off
    grid (an outage, or "Go off-grid") and while it syncs to reconnect. An
    off-grid run started here is shown in the attributes."""

    _attr_translation_key = "grid_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: TedapiCoordinator, off_grid: OffGridSession | None) -> None:
        super().__init__(coordinator, "grid_connected")
        self._off_grid = off_grid

    @property
    def available(self) -> bool:
        return super().available and bool(self.data.get("local_ok")) and self.data.get("grid_status") is not None

    @property
    def is_on(self) -> bool:
        return self.data.get("grid_status") == "UP"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"grid_status": self.data.get("grid_status")}
        if self._off_grid is not None:
            attrs.update(self._off_grid.as_dict())
        return attrs


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


class BackupEventSensor(TedapiEntity, BinarySensorEntity):
    """On while a backup event runs on the gateway: a max backup started
    here or in the Tesla app, or one Storm Watch scheduled. The Powerwall
    then charges to full and holds it. Read locally over signed v1r."""

    _attr_translation_key = "backup_event"

    def __init__(self, coordinator: TedapiCoordinator) -> None:
        super().__init__(coordinator, "backup_event")

    @property
    def event(self) -> dict[str, Any] | None:
        return self.data.get("backup_event")

    @property
    def available(self) -> bool:
        return super().available and bool(self.data.get("local_ok")) and self.event is not None

    @property
    def is_on(self) -> bool | None:
        return bool(self.event["active"]) if self.event else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        event = self.event or {}
        return {
            "kind": event.get("kind"),
            "start": event["start"].isoformat() if event.get("start") else None,
            "end": event["end"].isoformat() if event.get("end") else None,
        }


class CloudStateSensor(TedapiCloudEntity, BinarySensorEntity):
    """Storm Watch or grid services active, as the Tesla cloud reports it."""

    def __init__(self, coordinator: TedapiCoordinator, key: str, value_key: str) -> None:
        super().__init__(coordinator, key, value_key)
        self._attr_translation_key = key

    @property
    def is_on(self) -> bool | None:
        value = self.extras.get(self._value_key)
        return bool(value) if value is not None else None
