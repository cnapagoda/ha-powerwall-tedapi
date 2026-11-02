"""Logbook entries for the integration's events, in Home Assistant's language."""
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
from .texts import DISPATCH_END_REASON, LOGBOOK, OFF_GRID_END_REASON, Texts, texts as shared_texts

# English texts; the translations are in strings.json under selector.logbook,
# selector.dispatch_end_reason and selector.off_grid_end_reason.
FIRMWARE_UPDATED = "firmware updated from {previous} to {version}"
OUTAGE_STARTED = "grid outage started"
OUTAGE_ENDED = "grid back after an outage of {minutes} min"
OUTAGE_ENDED_UNKNOWN = "grid back after an outage"
DISPATCH_ENDED = "battery dispatch ended ({reason})"
OFF_GRID_ENDED = "off-grid run ended ({reason})"
DISPATCH_REASONS = {
    "completed": "time up",
    "target_reached": "target reached",
    "min_soc": "lower limit reached",
    "max_soc": "upper limit reached",
    "stopped": "stopped",
    "connection_lost": "connection lost",
    "unloaded": "integration unloaded",
}
OFF_GRID_REASONS = {
    "completed": "time up",
    "min_soc": "battery at the reconnect level",
    "requested": "reconnect requested",
    "reconnected_elsewhere": "reconnected elsewhere",
}


def _reason(t: Texts, group: str, known: dict[str, str], reason: Any) -> str:
    """A known reason in the user's language; anything else as it is."""
    reason = str(reason)
    return t.get(group, reason, known[reason]) if reason in known else reason


def _firmware(t: Texts, data: dict[str, Any]) -> str:
    return t.get(LOGBOOK, "firmware_updated", FIRMWARE_UPDATED,
                 previous=data.get("previous_version"), version=data.get("version"))


def _outage(t: Texts, data: dict[str, Any]) -> str:
    if data.get("phase") == "started":
        return t.get(LOGBOOK, "outage_started", OUTAGE_STARTED)
    seconds = data.get("duration_seconds")
    if isinstance(seconds, (int, float)):
        return t.get(LOGBOOK, "outage_ended", OUTAGE_ENDED, minutes=round(seconds / 60))
    return t.get(LOGBOOK, "outage_ended_unknown", OUTAGE_ENDED_UNKNOWN)


def _dispatch(t: Texts, data: dict[str, Any]) -> str:
    return t.get(LOGBOOK, "dispatch_ended", DISPATCH_ENDED,
                 reason=_reason(t, DISPATCH_END_REASON, DISPATCH_REASONS, data.get("reason")))


def _off_grid(t: Texts, data: dict[str, Any]) -> str:
    return t.get(LOGBOOK, "off_grid_ended", OFF_GRID_ENDED,
                 reason=_reason(t, OFF_GRID_END_REASON, OFF_GRID_REASONS, data.get("reason")))


MESSAGES: dict[str, Callable[[Texts, dict[str, Any]], str]] = {
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
        def describe(event: Event,
                     message: Callable[[Texts, dict[str, Any]], str] = message) -> dict[str, str]:
            return {LOGBOOK_ENTRY_NAME: name(event),
                    LOGBOOK_ENTRY_MESSAGE: message(shared_texts(hass), event.data)}

        async_describe_event(DOMAIN, event_type, describe)
