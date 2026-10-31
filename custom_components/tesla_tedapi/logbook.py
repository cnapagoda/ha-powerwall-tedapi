"""Logbook entries for the integration's events."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import (
    DOMAIN,
    EVENT_DISPATCH_ENDED,
    EVENT_FIRMWARE_UPDATED,
    EVENT_GRID_OUTAGE,
    EVENT_OFF_GRID_ENDED,
)


def _minutes(seconds: Any) -> str:
    return f"{round(seconds / 60)} min" if isinstance(seconds, (int, float)) else "?"


def _firmware(data: dict[str, Any]) -> str:
    return f"firmware updated from {data.get('previous_version')} to {data.get('version')}"


def _outage(data: dict[str, Any]) -> str:
    if data.get("phase") == "started":
        return "grid outage started"
    return f"grid back after an outage of {_minutes(data.get('duration_seconds'))}"


def _dispatch(data: dict[str, Any]) -> str:
    return f"battery dispatch ended ({data.get('reason')})"


def _off_grid(data: dict[str, Any]) -> str:
    return f"off-grid run ended ({data.get('reason')})"


MESSAGES: dict[str, Callable[[dict[str, Any]], str]] = {
    EVENT_FIRMWARE_UPDATED: _firmware,
    EVENT_GRID_OUTAGE: _outage,
    EVENT_DISPATCH_ENDED: _dispatch,
    EVENT_OFF_GRID_ENDED: _off_grid,
}


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    def name(event: Event) -> str:
        entry = hass.config_entries.async_get_entry(event.data.get("config_entry_id", ""))
        return entry.title if entry else "Powerwall"

    for event_type, message in MESSAGES.items():

        @callback
        def describe(event: Event, message: Callable[[dict[str, Any]], str] = message) -> dict[str, str]:
            return {LOGBOOK_ENTRY_NAME: name(event), LOGBOOK_ENTRY_MESSAGE: message(event.data)}

        async_describe_event(DOMAIN, event_type, describe)
