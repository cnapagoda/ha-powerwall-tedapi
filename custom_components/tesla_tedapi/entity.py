"""Base entity for the Powerwall TEDAPI integration."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

try:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
except ImportError:  # Home Assistant < 2025.2
    from homeassistant.helpers.entity_platform import (  # type: ignore[assignment]
        AddEntitiesCallback as AddConfigEntryEntitiesCallback,
    )

from .const import DOMAIN, DROPPABLE_SETTINGS
from .coordinator import TedapiCoordinator

__all__ = ["AddConfigEntryEntitiesCallback", "TedapiControlEntity", "TedapiEntity"]


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
        """Where the value came from (local, cloud or mixed), and for settings
        the Powerwall drops now and then, whether it is missing and how often
        it was restored (see the coordinator)."""
        attrs: dict[str, Any] = {"source": self.data.get("control_source")}
        key = self._setting_key
        if key in DROPPABLE_SETTINGS:
            attrs["missing_on_powerwall"] = bool(self.data.get(f"{key}_missing"))
            attrs["restored_count"] = self.data.get(f"{key}_restores", 0)
            last = self.data.get(f"{key}_last_restored")
            attrs["last_restored"] = last.isoformat() if last else None
        return attrs
