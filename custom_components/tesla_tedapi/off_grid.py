"""Go off-grid and reconnect.

Tesla's setIslandMode command (signed v1r) opens or closes the grid contactor,
like "Go off-grid" in the Tesla app. Each command is checked against the
gateway's grid status. An off-grid run started here reconnects at its end time,
or when the battery falls to its floor, which the Powerwall itself doesn't do.
The run is saved, so these checks go on after a restart."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONTACTOR_POLL_SECONDS,
    CONTACTOR_WAIT_SECONDS,
    DOMAIN,
    EVENT_OFF_GRID_ENDED,
    RECONNECT_RETRY_SECONDS,
)
from .coordinator import TedapiCoordinator

_LOGGER = logging.getLogger(__name__)


def off_grid_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, 1, f"{DOMAIN}.{entry_id}.off_grid")


@dataclass
class OffGridState:
    started: datetime
    ends: datetime | None
    min_soc: float | None


class OffGridSession:
    """At most one off-grid run per Powerwall."""

    def __init__(self, hass: HomeAssistant, coordinator: TedapiCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.state: OffGridState | None = None
        self.last_end_reason: str | None = None
        self._store = off_grid_store(hass, coordinator.config_entry.entry_id)
        self._unsubs: list[CALLBACK_TYPE] = []
        self._busy = False
        self._retry_at: float | None = None
        self._issue = False

    @property
    def _issue_id(self) -> str:
        return f"off_grid_reconnect_failed_{self.coordinator.config_entry.entry_id}"

    def as_dict(self) -> dict[str, Any]:
        s = self.state
        if s is None:
            return {"off_grid_run": False, "last_end_reason": self.last_end_reason}
        return {
            "off_grid_run": True,
            "off_grid_since": s.started.isoformat(),
            "reconnect_at": s.ends.isoformat() if s.ends else None,
            "reconnect_below": s.min_soc,
        }

    def _soc(self) -> float | None:
        data = self.coordinator.data or {}
        if not data.get("local_ok", True):
            return None
        return data.get("battery_level")

    # ---- control ----

    async def async_go_off_grid(self, minutes: int | None, min_soc: float | None) -> dict[str, Any]:
        data = self.coordinator.data or {}
        status = data.get("grid_status") if data.get("local_ok") else None
        if status != "UP":
            raise ServiceValidationError(
                "The Powerwall is not connected to the grid",
                translation_domain=DOMAIN, translation_key="off_grid_not_on_grid",
                translation_placeholders={"status": str(status)},
            )
        if min_soc is None:
            # Never run the house below the backup reserve unless asked to.
            reserve = data.get("reserve")
            if isinstance(reserve, (int, float)):
                min_soc = float(reserve)
        soc = self._soc()
        if min_soc is not None and isinstance(soc, (int, float)) and soc <= min_soc:
            raise ServiceValidationError(
                f"Battery level {soc}% is at or below {min_soc}%",
                translation_domain=DOMAIN, translation_key="off_grid_battery_low",
                translation_placeholders={"soc": str(soc), "limit": str(min_soc)},
            )

        result = await self.coordinator.async_send_island(True)
        status = await self._async_wait(lambda s: s != "UP")
        if status is None:
            # Take the request back, so the contactor doesn't open later on.
            try:
                await self.coordinator.async_send_island(False)
            except HomeAssistantError as err:
                _LOGGER.warning("Could not take back the off-grid request: %s", err)
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="off_grid_not_confirmed")

        now = dt_util.utcnow()
        self._cancel_timers()
        self.state = OffGridState(now, now + timedelta(minutes=minutes) if minutes else None, min_soc)
        self._retry_at = None
        await self._store.async_save(self._state_to_save())
        self._arm()
        _LOGGER.warning("The Powerwall is off-grid (%s); it reconnects %s", status, self._plan())
        self.coordinator.async_update_listeners()
        self.hass.async_create_task(self.coordinator.async_request_refresh())
        return {**self.as_dict(), "grid_status": status, "response": result}

    async def async_reconnect(self, reason: str = "requested") -> dict[str, Any]:
        """Close the contactor (also when no run is known, e.g. one started in
        the Tesla app) and wait until the Powerwall syncs back to the grid."""
        result = await self.coordinator.async_send_island(False)
        status = await self._async_wait(lambda s: s in ("UP", "SYNCING"))
        if status is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="reconnect_not_confirmed")
        await self._async_end(reason)
        self.hass.async_create_task(self.coordinator.async_request_refresh())
        return {"grid_status": status, "response": result}

    async def async_load(self) -> None:
        """Pick up a run saved before a restart or reload."""
        stored = await self._store.async_load()
        if not stored:
            return
        started = dt_util.parse_datetime(stored.get("started") or "")
        if started is None:
            await self._store.async_remove()
            return
        ends = dt_util.parse_datetime(stored["ends"]) if stored.get("ends") else None
        self.state = OffGridState(started, ends, stored.get("min_soc"))
        data = self.coordinator.data or {}
        if data.get("local_ok") and data.get("grid_status") == "UP":
            await self._async_end("reconnected_elsewhere")
            return
        _LOGGER.warning("An off-grid run started %s is still on; it reconnects %s", started, self._plan())
        self._arm()

    def async_shutdown(self) -> None:
        """Entry unloading: stop watching. A run stays saved and is watched again
        when the entry loads; the Powerwall stays off-grid meanwhile."""
        self._cancel_timers()
        if self.state is not None:
            _LOGGER.warning("Unloading while the Powerwall is off-grid: it stays off-grid until "
                            "the integration loads again or it is reconnected")

    # ---- internals ----

    def _plan(self) -> str:
        s = self.state
        parts = []
        if s and s.ends:
            parts.append(f"at {dt_util.as_local(s.ends).isoformat(timespec='minutes')}")
        if s and s.min_soc is not None:
            parts.append(f"when the battery falls to {s.min_soc}%")
        return " or ".join(parts) or "only when asked to"

    def _state_to_save(self) -> dict[str, Any]:
        s = self.state
        assert s is not None
        return {"started": s.started.isoformat(), "ends": s.ends.isoformat() if s.ends else None,
                "min_soc": s.min_soc}

    async def _async_wait(self, done: Callable[[str], bool]) -> str | None:
        """The gateway's grid status once done() accepts it, or None after
        CONTACTOR_WAIT_SECONDS."""
        deadline = time.monotonic() + CONTACTOR_WAIT_SECONDS
        while True:
            status = await self.coordinator.async_read_grid_status()
            if status is not None and done(status):
                return status
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(CONTACTOR_POLL_SECONDS)

    def _arm(self) -> None:
        self._unsubs = [self.coordinator.async_add_listener(self._check)]
        if self.state and self.state.ends:
            self._unsubs.append(async_track_point_in_utc_time(self.hass, self._at_end, self.state.ends))

    def _cancel_timers(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    @callback
    def _at_end(self, _now: datetime) -> None:
        self._check()

    @callback
    def _check(self) -> None:
        """After each poll and at the end time: reconnect when the run is over or
        the battery is low; forget the run if the grid came back elsewhere."""
        state = self.state
        if state is None or self._busy:
            return
        now = dt_util.utcnow()
        data = self.coordinator.data or {}
        if (data.get("local_ok") and data.get("grid_status") == "UP"
                and (now - state.started).total_seconds() > CONTACTOR_WAIT_SECONDS):
            self.hass.async_create_task(self._async_end("reconnected_elsewhere"))
            return
        soc = self._soc()
        reason = None
        if state.ends and now >= state.ends:
            reason = "completed"
        elif state.min_soc is not None and isinstance(soc, (int, float)) and soc <= state.min_soc:
            reason = "min_soc"
        if reason and (self._retry_at is None or time.monotonic() >= self._retry_at):
            self._busy = True
            self.hass.async_create_task(self._async_auto_reconnect(reason))

    async def _async_auto_reconnect(self, reason: str) -> None:
        try:
            _LOGGER.warning("Reconnecting the Powerwall to the grid (%s)", reason)
            await self.async_reconnect(reason)
        except HomeAssistantError as err:
            self._retry_at = time.monotonic() + RECONNECT_RETRY_SECONDS
            _LOGGER.error("Automatic grid reconnect (%s) failed, trying again in %s s: %s",
                          reason, RECONNECT_RETRY_SECONDS, err)
            if not self._issue:
                self._issue = True
                ir.async_create_issue(
                    self.hass, DOMAIN, self._issue_id,
                    is_fixable=False,
                    is_persistent=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="off_grid_reconnect_failed",
                    translation_placeholders={"soc": str(self._soc())},
                )
        finally:
            self._busy = False

    async def _async_end(self, reason: str) -> None:
        self._cancel_timers()
        self._retry_at = None
        if self._issue:
            self._issue = False
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        state, self.state = self.state, None
        if state is None:
            return
        await self._store.async_remove()
        self.last_end_reason = reason
        _LOGGER.warning("Off-grid run ended (%s)", reason)
        self.hass.bus.async_fire(EVENT_OFF_GRID_ENDED, {
            "config_entry_id": self.coordinator.config_entry.entry_id,
            "reason": reason,
            "battery_level": self._soc(),
        })
        self.coordinator.async_update_listeners()
