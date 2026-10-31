"""Base entity for the Tesla Powerwall TEDAPI integration."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

try:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
except ImportError:  # Home Assistant < 2025.2
    from homeassistant.helpers.entity_platform import (  # type: ignore[assignment]
        AddEntitiesCallback as AddConfigEntryEntitiesCallback,
    )

from .const import DOMAIN, DROPPABLE_SETTINGS
from .coordinator import TedapiCoordinator

__all__ = ["AddConfigEntryEntitiesCallback", "TedapiCloudEntity", "TedapiControlEntity", "TedapiEntity",
           "add_when_reported"]


class TedapiEntity(CoordinatorEntity[TedapiCoordinator]):
    """One device per gateway (DIN); unique ids are "<DIN>_<key>"."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TedapiCoordinator, key: str) -> None:
        super().__init__(coordinator)
        din = coordinator.din or coordinator.config_entry.entry_id
        self._attr_unique_id = f"{din}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, din)},
            name="Powerwall TEDAPI",
            manufacturer="Tesla",
            model="Powerwall",
            serial_number=din,
            sw_version=coordinator.firmware_version,
        )

    @property
    def data(self) -> dict[str, Any]:
        return self.coordinator.data or {}


class TedapiControlEntity(TedapiEntity):
    """A Powerwall setting. Stays available while the gateway is down as long
    as the cloud still answers."""

    @property
    def available(self) -> bool:
        return super().available and self.data.get("control_source") is not None

    def __init__(self, coordinator: TedapiCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._setting_key = key

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Where the value came from (local, cloud or mixed) and which cloud
        backup answered, and for settings the Powerwall drops now and then,
        whether it is missing and how often it was restored (see the
        coordinator)."""
        attrs: dict[str, Any] = {"source": self.data.get("control_source"),
                                 "cloud_backend": self.data.get("control_backend")}
        key = self._setting_key
        if key in DROPPABLE_SETTINGS:
            attrs["missing_on_powerwall"] = bool(self.data.get(f"{key}_missing"))
            attrs["restored_count"] = self.data.get(f"{key}_restores", 0)
            last = self.data.get(f"{key}_last_restored")
            attrs["last_restored"] = last.isoformat() if last else None
        return attrs


class TedapiCloudEntity(TedapiEntity):
    """A setting or state that only the Tesla cloud has (Storm Watch and
    similar), read and changed through the cloud backup."""

    def __init__(self, coordinator: TedapiCoordinator, key: str, value_key: str) -> None:
        super().__init__(coordinator, key)
        self._value_key = value_key

    @property
    def extras(self) -> dict[str, Any]:
        return self.data.get("cloud_extras") or {}

    @property
    def available(self) -> bool:
        return super().available and self.extras.get(self._value_key) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"cloud_backend": self.extras.get("backend")}


def add_when_reported(
    entry: ConfigEntry,
    coordinator: TedapiCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    candidates: list[tuple[str, Callable[[], Entity]]],
) -> None:
    """Add each (flag, factory) entity once the cloud backup reports the
    flag (e.g. the site is Storm Watch capable); sites without it never get
    the entity. Also catches a cloud backup that answers after setup."""
    known: set[str] = set()

    @callback
    def add_new() -> None:
        extras = (coordinator.data or {}).get("cloud_extras") or {}
        new = []
        for flag, factory in candidates:
            if flag not in known and extras.get(flag):
                known.add(flag)
                new.append(factory())
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))
