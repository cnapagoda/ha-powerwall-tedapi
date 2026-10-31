"""Managed battery dispatch sessions.

A dispatch tells the Powerwall to run the battery at a fixed power, overriding
its backup reserve and export setting. Instead of sending the full duration at
once, a session sends short commands (DISPATCH_CHUNK_SECONDS) and renews them,
so the Powerwall goes back to normal within minutes if Home Assistant stops. It
also stops on state-of-charge limits, which the Powerwall itself ignores."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DISPATCH_FLOOR_RESERVE,
    CONF_DISPATCH_MAX_WATTS,
    DISPATCH_CHUNK_SECONDS,
    DISPATCH_MAX_FAILURES,
    DISPATCH_MAX_WATTS,
    DISPATCH_RENEW_SECONDS,
    DOMAIN,
    EVENT_DISPATCH_ENDED,
)
from .coordinator import TedapiCoordinator

_LOGGER = logging.getLogger(__name__)
# pypowerwall (0.22.2+) logs a warning for every accepted dispatch command.
PYPOWERWALL_DISPATCH_LOGGER = "pypowerwall.tedapi.tedapi_v1r"


class RepeatDispatchFilter(logging.Filter):
    """Drops pypowerwall's per-command warning. A session renews its command
    every minute, and its start is logged here with the same warning."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith("Battery dispatch accepted")


def quiet_dispatch_repeats() -> None:
    logger = logging.getLogger(PYPOWERWALL_DISPATCH_LOGGER)
    if not any(isinstance(f, RepeatDispatchFilter) for f in logger.filters):
        logger.addFilter(RepeatDispatchFilter())


@dataclass
class DispatchState:
    power_watts: float
    started: datetime
    ends: datetime
    min_soc: float | None
    max_soc: float | None
    # Started by charge_to / discharge_to: reaching the limit is the goal.
    target: bool = False


class DispatchSession:
    """At most one active dispatch per Powerwall."""

    def __init__(self, hass: HomeAssistant, coordinator: TedapiCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.state: DispatchState | None = None
        self.last_end_reason: str | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._failures = 0
        self._listeners: list[Callable[[], None]] = []

    # ---- listeners (entities) ----

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(update)

        @callback
        def remove() -> None:
            self._listeners.remove(update)

        return remove

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    # ---- helpers ----

    @property
    def active(self) -> bool:
        return self.state is not None

    def _soc(self) -> float | None:
        data = self.coordinator.data or {}
        if not data.get("local_ok", True):
            return None
        return data.get("battery_level")

    def _limit_hit(self, state: DispatchState) -> str | None:
        soc = self._soc()
        if soc is None:
            return None
        if state.power_watts > 0 and state.min_soc is not None and soc <= state.min_soc:
            return "min_soc"
        if state.power_watts < 0 and state.max_soc is not None and soc >= state.max_soc:
            return "max_soc"
        return None

    def _chunk(self, state: DispatchState) -> int:
        left = (state.ends - dt_util.utcnow()).total_seconds()
        return max(1, min(DISPATCH_CHUNK_SECONDS, math.ceil(left)))

    def as_dict(self) -> dict[str, Any]:
        s = self.state
        if s is None:
            return {"active": False, "last_end_reason": self.last_end_reason}
        return {
            "active": True,
            "power_watts": s.power_watts,
            "started": s.started.isoformat(),
            "ends": s.ends.isoformat(),
            "min_soc": s.min_soc,
            "max_soc": s.max_soc,
            "target_soc": (s.max_soc if s.power_watts < 0 else s.min_soc) if s.target else None,
        }

    # ---- control ----

    async def async_start(self, power_watts: float, duration_seconds: int,
                          min_soc: float | None, max_soc: float | None,
                          target: bool = False) -> dict[str, Any]:
        if not self.coordinator.dispatch_supported:
            raise ServiceValidationError(
                "Battery dispatch needs the signed (v1r) connection",
                translation_domain=DOMAIN, translation_key="dispatch_needs_v1r",
            )
        options = self.coordinator.config_entry.options
        cap = options.get(CONF_DISPATCH_MAX_WATTS, DISPATCH_MAX_WATTS)
        if abs(power_watts) > cap:
            raise ServiceValidationError(
                f"{power_watts} W is above the {cap} W limit set in the integration options",
                translation_domain=DOMAIN, translation_key="dispatch_power_cap",
                translation_placeholders={"power": str(power_watts), "cap": str(cap)},
            )
        if power_watts > 0 and min_soc is None and options.get(CONF_DISPATCH_FLOOR_RESERVE, True):
            # Without a floor a discharge would run the battery below the backup
            # reserve; stop there unless the caller gives min_soc.
            reserve = (self.coordinator.data or {}).get("reserve")
            if isinstance(reserve, (int, float)):
                min_soc = float(reserve)
        now = dt_util.utcnow()
        state = DispatchState(power_watts, now, now + timedelta(seconds=duration_seconds),
                              min_soc, max_soc, target)
        reason = self._limit_hit(state)
        if reason:
            raise ServiceValidationError(
                f"Battery level {self._soc()}% is already past the {reason} limit",
                translation_domain=DOMAIN, translation_key="dispatch_soc_limit",
                translation_placeholders={"soc": str(self._soc()), "limit": reason},
            )

        result = await self.coordinator.async_send_dispatch(power_watts, self._chunk(state))
        self._cancel_timers()
        self.state = state
        self._failures = 0
        self._unsubs = [
            async_track_time_interval(self.hass, self._async_tick,
                                      timedelta(seconds=DISPATCH_RENEW_SECONDS)),
            async_track_point_in_utc_time(self.hass, self._async_at_end, state.ends),
        ]
        _LOGGER.warning("Battery dispatch started: %s W until %s. A dispatch overrides the backup "
                        "reserve and export setting and may affect the Powerwall warranty; use at "
                        "your own risk", power_watts, state.ends)
        self._notify()
        self.hass.async_create_task(self.coordinator.async_request_refresh())
        return {**self.as_dict(), "response": result}

    async def async_stop(self, reason: str = "stopped") -> dict[str, Any] | None:
        """Hand the battery back to the Powerwall (also when no session is known,
        e.g. one started before a restart)."""
        had_session = self.state is not None
        self._end(reason)
        result = await self.coordinator.async_send_resume()
        if had_session:
            _LOGGER.info("Battery dispatch ended (%s)", reason)
        self.hass.async_create_task(self.coordinator.async_request_refresh())
        return result

    async def async_shutdown(self) -> None:
        """Entry unloading: end the session and try to hand the battery back."""
        if self.state is None:
            self._cancel_timers()
            return
        self._end("unloaded")
        try:
            await self.coordinator.async_send_resume()
        except HomeAssistantError as err:
            _LOGGER.warning("Could not resume the battery while unloading: %s", err)

    async def async_move_to(self, target_soc: float, power_watts: float, minutes: int,
                            charge: bool) -> dict[str, Any]:
        """Charge (or discharge) at power_watts until the battery reaches
        target_soc, or for at most `minutes`."""
        soc = self._soc()
        if soc is not None and (soc >= target_soc if charge else soc <= target_soc):
            raise ServiceValidationError(
                f"The battery is already at {soc}%",
                translation_domain=DOMAIN, translation_key="target_reached",
                translation_placeholders={"soc": str(soc), "target": str(target_soc)},
            )
        power = -abs(power_watts) if charge else abs(power_watts)
        return await self.async_start(power, minutes * 60,
                                      None if charge else target_soc,
                                      target_soc if charge else None, target=True)

    # ---- internals ----

    def _cancel_timers(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    @callback
    def _end(self, reason: str) -> None:
        self._cancel_timers()
        state, self.state = self.state, None
        if state is None:
            return
        self.last_end_reason = reason
        self.hass.bus.async_fire(EVENT_DISPATCH_ENDED, {
            "config_entry_id": self.coordinator.config_entry.entry_id,
            "reason": reason,
            "power_watts": state.power_watts,
            "battery_level": self._soc(),
        })
        self._notify()

    async def _async_resume_quietly(self, reason: str) -> None:
        self._end(reason)
        try:
            await self.coordinator.async_send_resume()
        except HomeAssistantError as err:
            # The last command expires on its own within DISPATCH_CHUNK_SECONDS.
            _LOGGER.warning("Resume after %s failed: %s", reason, err)
        await self.coordinator.async_request_refresh()

    async def _async_at_end(self, _now: datetime) -> None:
        if self.state is not None:
            await self._async_resume_quietly("completed")

    async def _async_tick(self, _now: datetime) -> None:
        state = self.state
        if state is None:
            return
        reason = self._limit_hit(state)
        if reason:
            _LOGGER.info("Battery dispatch stopped: %s limit reached", reason)
            if state.target:
                reason = "target_reached"
            await self._async_resume_quietly(reason)
            return
        if (state.ends - dt_util.utcnow()).total_seconds() < 1:
            return  # the end timer handles it
        try:
            await self.coordinator.async_send_dispatch(state.power_watts, self._chunk(state))
            self._failures = 0
        except HomeAssistantError as err:
            self._failures += 1
            _LOGGER.warning("Battery dispatch renewal failed (%s/%s): %s",
                            self._failures, DISPATCH_MAX_FAILURES, err)
            if self._failures >= DISPATCH_MAX_FAILURES and self.state is state:
                self._end("connection_lost")
