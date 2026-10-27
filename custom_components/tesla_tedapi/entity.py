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

from .const import DOMAIN
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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Where the value came from: local, cloud or mixed."""
        return {"source": self.data.get("control_source")}
