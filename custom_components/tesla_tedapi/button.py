"""Buttons for the local commands that end something: a battery dispatch, a
max backup, and an off-grid run (reconnect to the grid).

They do what the matching actions do, so they're on dashboards without a
script. Signed connection only, like those actions. There is deliberately no
button to go off-grid: that needs a reconnect plan (go_off_grid action)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant

from .coordinator import TedapiConfigEntry, TedapiRuntimeData
from .entity import AddConfigEntryEntitiesCallback, TedapiEntity

# Commands to the Powerwall go one at a time.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class TedapiButtonEntityDescription(ButtonEntityDescription):
    press_fn: Callable[[TedapiRuntimeData], Awaitable[object]]


BUTTONS: tuple[TedapiButtonEntityDescription, ...] = (
    TedapiButtonEntityDescription(
        key="stop_dispatch",
        translation_key="stop_dispatch",
        # Also clears a dispatch left over from before a restart.
        press_fn=lambda rt: rt.dispatch.async_stop(),  # type: ignore[union-attr]
    ),
    TedapiButtonEntityDescription(
        key="cancel_max_backup",
        translation_key="cancel_max_backup",
        press_fn=lambda rt: rt.coordinator.async_max_backup(None),
    ),
    TedapiButtonEntityDescription(
        key="reconnect_grid",
        translation_key="reconnect_grid",
        press_fn=lambda rt: rt.off_grid.async_reconnect(),  # type: ignore[union-attr]
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TedapiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    if runtime.coordinator.dispatch_supported:
        async_add_entities(TedapiButton(runtime, description) for description in BUTTONS)


class TedapiButton(TedapiEntity, ButtonEntity):
    """A local command button; unavailable while the gateway can't be reached
    (the command would fail)."""

    entity_description: TedapiButtonEntityDescription

    def __init__(self, runtime: TedapiRuntimeData, description: TedapiButtonEntityDescription) -> None:
        super().__init__(runtime.coordinator, description.key)
        self.entity_description = description
        self._runtime = runtime

    @property
    def available(self) -> bool:
        return super().available and bool(self.data.get("local_ok"))

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self._runtime)
