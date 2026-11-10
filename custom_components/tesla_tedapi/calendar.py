"""Tesla tariff calendars (optional, cloud only): the buy and the feed-in
tariff as calendar events, one per stretch of time with the same period and
rate. Built from the tariff the Tesla tariff option reads."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import TedapiConfigEntry, TedapiCoordinator
from .entity import AddConfigEntryEntitiesCallback, TedapiEntity
from .tariff import normalize, schedule

# Coordinator-based: no limit on parallel updates.
PARALLEL_UPDATES = 0

# How far around now the current event is looked for (a period can run over
# midnight and through a weekend).
LOOK_BACK = timedelta(days=3)
LOOK_AHEAD = timedelta(days=3)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TedapiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator.tariff_enabled:
        async_add_entities([TariffCalendar(coordinator, "buy"), TariffCalendar(coordinator, "sell")])


class TariffCalendar(TedapiEntity, CalendarEntity):
    """The buy ("buy") or feed-in ("sell") side of the Tesla tariff."""

    def __init__(self, coordinator: TedapiCoordinator, side: str) -> None:
        super().__init__(coordinator, f"{side}_tariff")
        self._side = side
        self._attr_translation_key = f"{side}_tariff"
        # The events around today, kept until the tariff, the day or the
        # currency changes: the state is written on every poll.
        self._cache_key: tuple[Any, ...] | None = None
        self._cache: list[CalendarEvent] = []

    @property
    def available(self) -> bool:
        return self.coordinator.tariff is not None

    def _events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Events between start and end. Stretches without a rate on this
        side (no feed-in tariff, say) have no event."""
        currency = self.coordinator.hass.config.currency
        events: list[CalendarEvent] = []
        for item in schedule(normalize(self.coordinator.tariff), dt_util.as_local(start), dt_util.as_local(end)):
            rate = item[f"{self._side}_rate"]
            if rate is None:
                continue
            period = item["period"] or item["season"] or "-"
            summary = f"{period}: {rate:g} {currency}/kWh"
            previous = events[-1] if events else None
            if previous is not None and previous.end == item["start"] and previous.summary == summary:
                # Same period and rate on this side (only the other side's rate changed).
                events[-1] = CalendarEvent(start=previous.start, end=item["end"], summary=summary,
                                           description=previous.description)
                continue
            events.append(CalendarEvent(start=item["start"], end=item["end"], summary=summary,
                                        description=f"{item['season']} / {item['period']}"))
        return events

    @property
    def event(self) -> CalendarEvent | None:
        """The event now, else the next one."""
        now = dt_util.now()
        tariff = self.coordinator.tariff
        # The tariff itself in the key, not its id: a read gives a new dict,
        # and one with the same contents compares equal (no rebuild).
        key = (tariff, now.date(), self.coordinator.hass.config.currency)
        if key != self._cache_key:
            midnight = dt_util.start_of_local_day(now)
            self._cache = self._events(midnight - LOOK_BACK, midnight + timedelta(days=1) + LOOK_AHEAD)
            self._cache_key = key
        events = self._cache
        current = next((e for e in events if e.start <= now < e.end), None)
        return current or next((e for e in events if e.start > now), None)

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime,
                               end_date: datetime) -> list[CalendarEvent]:
        return self._events(start_date, end_date)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"backend": self.coordinator.tariff_backend}
