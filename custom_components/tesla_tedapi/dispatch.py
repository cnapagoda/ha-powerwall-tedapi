"""Managed battery dispatch sessions.

A dispatch tells the Powerwall to run the battery at a fixed power, overriding
its backup reserve and export setting. Instead of sending the full duration at
once, a session sends short commands (DISPATCH_CHUNK_SECONDS) and renews them,
so the Powerwall goes back to normal within minutes if Home Assistant stops. It
also stops on state-of-charge limits, which the Powerwall itself ignores, and
is only renewed while the battery level it checks them against is fresh.
With the option on, a charge turns Grid charging on for its duration."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
import time
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DISPATCH_FLOOR_RESERVE,
    CONF_DISPATCH_GRID_CHARGING,
    CONF_DISPATCH_MAX_WATTS,
    CONF_DISPATCH_STALE_MINUTES,
    DISPATCH_CHUNK_SECONDS,
    DISPATCH_DEFAULT_WATTS,
    DISPATCH_FRESH_READ_SECONDS,
    DISPATCH_GRID_RETRY_SECONDS,
    DISPATCH_MAX_FAILURES,
    DISPATCH_RENEW_SECONDS,
    DISPATCH_STALE_MINUTES_MAX,
    DISPATCH_STALE_MINUTES_MIN,
    DISPATCH_STALE_POLLS,
    DISPATCH_STALE_SECONDS,
    DOMAIN,
    EVENT_DISPATCH_ENDED,
)
from .coordinator import TedapiCoordinator
from .privacy import scrub

_LOGGER = logging.getLogger(__name__)
# pypowerwall (0.22.2+) logs a warning for every accepted dispatch command.
PYPOWERWALL_DISPATCH_LOGGER = "pypowerwall.tedapi.tedapi_v1r"


class RepeatDispatchFilter(logging.Filter):
    """Drops pypowerwall's per-command warning. A session renews its command
    every 30 seconds, and its start is logged here with the same warning."""

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
        # Whether a missing battery level has been logged for this stretch.
        self._stale_logged = False
        self._listeners: list[Callable[[], None]] = []
        # Every command to the gateway (start, renewal, resume) is sent under
        # this lock, so they reach it in order: a resume can't overtake a
        # renewal still on its way and be undone by it. _ends counts ends, so
        # a start overtaken by a stop doesn't install its session.
        self._send_lock = asyncio.Lock()
        self._ends = 0
        # Turning Grid charging back off after a charge (option): one at a
        # time, retried at most every DISPATCH_GRID_RETRY_SECONDS.
        self._grid_lock = asyncio.Lock()
        self._grid_retry_at = 0.0

    @callback
    def async_setup(self) -> CALLBACK_TYPE:
        """Listen to every reading: Grid charging a charge turned on is turned
        off again also when that failed at the end, or the session ended with
        a restart."""
        return self.coordinator.async_add_listener(self._on_any_reading)

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
        # The user's own limit (Largest dispatch power); within it the gateway decides.
        cap = options.get(CONF_DISPATCH_MAX_WATTS, DISPATCH_DEFAULT_WATTS)
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
        # A charge or discharge needs a fresh battery level to check its
        # limits against. Refusing here also covers a planner re-sending its
        # dispatch every few minutes: a re-send can't keep a blind session
        # going (a 0 W hold moves no energy, so it may).
        age = self._level_age()
        if (power_watts != 0 and (age is None or age > self._stale_limit())
                and not await self._async_fresh_level(DISPATCH_FRESH_READ_SECONDS)):
            raise ServiceValidationError(
                "The battery level hasn't been read recently, so the dispatch limits can't be checked",
                translation_domain=DOMAIN, translation_key="dispatch_battery_level_unknown",
            )
        reason = self._limit_hit(state)
        if reason:
            raise ServiceValidationError(
                f"Battery level {self._soc()}% is already past the {reason} limit",
                translation_domain=DOMAIN, translation_key="dispatch_soc_limit",
                translation_placeholders={"soc": str(self._soc()), "limit": reason},
            )

        async with self._send_lock:
            ends = self._ends
            result = await self.coordinator.async_send_dispatch(power_watts, self._chunk(state))
            if self._ends != ends:
                # Stopped while the command was on its way; the stop's resume
                # is queued behind this lock and hands the battery back.
                _LOGGER.info("Battery dispatch stopped before it started")
                return {**self.as_dict(), "response": result}
        # The same power again while running (a planner such as Predbat
        # re-sends its dispatch every cycle): already warned at the start.
        repeat = self.state is not None and self.state.power_watts == power_watts
        self._cancel_timers()
        self.state = state
        self._failures = 0
        self._stale_logged = False
        self._unsubs = [
            async_track_time_interval(self.hass, self._async_tick,
                                      timedelta(seconds=DISPATCH_RENEW_SECONDS)),
            async_track_point_in_utc_time(self.hass, self._async_at_end, state.ends),
            # Check the SOC limit on every reading too, not just at renewals,
            # so a target stops on the reading that reaches it.
            self.coordinator.async_add_listener(self._on_reading),
        ]
        if repeat:
            _LOGGER.debug("Battery dispatch extended: %s W until %s", power_watts, state.ends)
        else:
            _LOGGER.warning("Battery dispatch started: %s W until %s. A dispatch overrides the backup "
                            "reserve and export setting and may affect the Powerwall warranty; use at "
                            "your own risk", power_watts, state.ends)
        if not repeat:
            await self._async_grid_charging_for(power_watts)
        self._notify()
        self.hass.async_create_task(self.coordinator.async_request_refresh())
        return {**self.as_dict(), "response": result}

    async def async_stop(self, reason: str = "stopped") -> dict[str, Any] | None:
        """Hand the battery back to the Powerwall (also when no session is known,
        e.g. one started before a restart)."""
        had_session = self.state is not None
        self._end(reason)
        async with self._send_lock:
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
        self._end("unloaded", release=False)
        try:
            async with self._send_lock:
                await self.coordinator.async_send_resume()
        except HomeAssistantError as err:
            _LOGGER.warning("Could not resume the battery while unloading: %s", scrub(err))
        # If this fails, it's saved and done after the next start.
        await self._async_release_grid_charging()

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
    def _end(self, reason: str, release: bool = True) -> None:
        self._cancel_timers()
        self._ends += 1
        state, self.state = self.state, None
        if state is None:
            return
        if release and self.coordinator.grid_charging_by_dispatch:
            self.hass.async_create_task(self._async_release_grid_charging())
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
            async with self._send_lock:
                await self.coordinator.async_send_resume()
        except HomeAssistantError as err:
            # The last command expires on its own within DISPATCH_CHUNK_SECONDS.
            _LOGGER.warning("Resume after %s failed: %s", reason, scrub(err))
        await self.coordinator.async_request_refresh()

    async def _async_at_end(self, _now: datetime) -> None:
        if self.state is not None:
            await self._async_resume_quietly("completed")

    # ---- grid charging for a charge (option) ----

    async def _async_grid_charging_for(self, power_watts: float) -> None:
        """A new charge turns Grid charging on (option on) or warns that it
        charges from solar only; a discharge or hold after a charge turns it
        back off."""
        coordinator = self.coordinator
        if power_watts >= 0:
            if coordinator.grid_charging_by_dispatch:
                await self._async_release_grid_charging()
            return
        # After a turn-off still under way (the end of an earlier charge).
        async with self._grid_lock:
            if coordinator.grid_charging_now() is not False:
                return  # on (perhaps by an earlier charge), or not known
            if not coordinator.config_entry.options.get(CONF_DISPATCH_GRID_CHARGING, False):
                _LOGGER.warning("Battery dispatch is charging with Grid charging off: the Powerwall "
                                "charges from solar only, not from the grid. Turn on Grid charging "
                                "to charge from the grid")
                return
            try:
                await coordinator.async_set_grid_charging(True, by_dispatch=True)
            except HomeAssistantError as err:
                _LOGGER.warning("Battery dispatch: could not turn on Grid charging (%s), so it charges "
                                "from solar only", scrub(err))
                return
        _LOGGER.info("Battery dispatch turned on Grid charging; it's turned off again when the dispatch ends")

    async def _async_release_grid_charging(self) -> None:
        """Turn off Grid charging that a charge turned on, unless a charge is
        running again or someone turned it off already. Changing the setting
        any other way clears the coordinator's flag, so it's left alone then."""
        coordinator = self.coordinator
        async with self._grid_lock:
            if not coordinator.grid_charging_by_dispatch:
                return
            if self.state is not None and self.state.power_watts < 0:
                return  # a new charge needs it
            if coordinator.grid_charging_now() is False:
                coordinator.set_grid_charging_by_dispatch(False)  # already off
                return
            self._grid_retry_at = time.monotonic() + DISPATCH_GRID_RETRY_SECONDS
            try:
                await coordinator.async_set_grid_charging(False, by_dispatch=True)
            except HomeAssistantError as err:
                _LOGGER.warning("Could not turn Grid charging off again after the battery dispatch "
                                "(%s); trying again in %d minutes", scrub(err), DISPATCH_GRID_RETRY_SECONDS // 60)
                return
            _LOGGER.info("Grid charging turned off again after the battery dispatch")

    @callback
    def _on_any_reading(self) -> None:
        if (self.state is None and self.coordinator.grid_charging_by_dispatch
                and not self._grid_lock.locked() and time.monotonic() >= self._grid_retry_at):
            self.hass.async_create_task(self._async_release_grid_charging())

    def _level_age(self) -> float | None:
        """Seconds since the gateway last gave a battery level, or None if it
        hasn't (the coordinator records it only from a real local reading)."""
        read_at = self.coordinator.battery_level_at
        return None if read_at is None else (dt_util.utcnow() - read_at).total_seconds()

    def _stale_limit(self) -> float:
        """How old the battery level may be to start or renew on: the
        option (1-5 minutes, default 2), or 2.5 polling intervals on a slower
        poll. Read each time: the option changes without a reload."""
        minutes = self.coordinator.config_entry.options.get(CONF_DISPATCH_STALE_MINUTES)
        if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
            limit = min(max(float(minutes), DISPATCH_STALE_MINUTES_MIN), DISPATCH_STALE_MINUTES_MAX) * 60
        else:
            limit = float(DISPATCH_STALE_SECONDS)
        interval = self.coordinator.update_interval
        polls = interval.total_seconds() * DISPATCH_STALE_POLLS if interval else 0
        return max(limit, polls)

    async def _async_fresh_level(self, window: float) -> bool:
        """One extra read when the battery level is too old to go on: True if
        it's fresh now. At most DISPATCH_FRESH_READ_SECONDS, and never past
        the window left before the session would end."""
        timeout = max(1.0, min(float(DISPATCH_FRESH_READ_SECONDS), window))
        if not await self.coordinator.async_read_battery_level(timeout):
            return False
        age = self._level_age()
        fresh = age is not None and age <= self._stale_limit()
        if fresh:
            _LOGGER.debug("Battery dispatch: an extra reading gave a fresh battery level")
        return fresh

    def _stale_for(self, state: DispatchState) -> float | None:
        """Seconds since the last battery level, if it's too old to renew on;
        None while it's fresh, and always for a 0 W hold."""
        if state.power_watts == 0:
            return None
        age = self._level_age()
        if age is None:  # no reading at all: count from the session's start
            age = (dt_util.utcnow() - state.started).total_seconds()
        if age > self._stale_limit():
            return age
        self._stale_logged = False  # fresh again: a later gap is logged again
        return None

    @callback
    def _on_reading(self) -> None:
        state = self.state
        if state is not None and self._limit_hit(state):
            self.hass.async_create_task(self._async_stop_at_limit(state))

    async def _async_stop_at_limit(self, state: DispatchState) -> bool:
        """End the session if the SOC limit is reached; True if it ended."""
        if self.state is not state:
            return False  # already ended (tick and reading can both see it)
        reason = self._limit_hit(state)
        if not reason:
            return False
        _LOGGER.info("Battery dispatch stopped: %s limit reached", reason)
        await self._async_resume_quietly("target_reached" if state.target else reason)
        return True

    async def _async_tick(self, _now: datetime) -> None:
        state = self.state
        if state is None:
            return
        if await self._async_stop_at_limit(state):
            return
        if (state.ends - dt_util.utcnow()).total_seconds() < 1:
            return  # the end timer handles it
        stale = self._stale_for(state)
        if stale is not None:
            # One extra read first: a late poll shouldn't end the session.
            window = self._stale_limit() + DISPATCH_CHUNK_SECONDS - stale
            if window > 0 and await self._async_fresh_level(window):
                if self.state is not state or await self._async_stop_at_limit(state):
                    return
                stale = self._stale_for(state)
        if stale is not None:
            # Without a fresh battery level the limits can't be checked, and
            # the Powerwall ignores its backup reserve during a dispatch: don't
            # renew. The last command runs out on its own; end the session
            # once it has.
            if stale >= self._stale_limit() + DISPATCH_CHUNK_SECONDS:
                _LOGGER.warning("Battery dispatch ended: no battery level from the gateway for %d s",
                                stale)
                await self._async_resume_quietly("battery_level_unknown")
                return
            if not self._stale_logged:
                self._stale_logged = True
                _LOGGER.warning("Battery dispatch not renewed: no fresh battery level from the gateway "
                                "(%d s); it ends if none arrives within %d s", stale,
                                self._stale_limit() + DISPATCH_CHUNK_SECONDS - stale)
            return
        age = self._level_age()
        if age is not None:
            self.coordinator.read_times.level_age(time.monotonic(), age)
        self.coordinator.sample_loop_lag()
        try:
            async with self._send_lock:
                if self.state is not state:
                    return  # ended while waiting for the lock: don't restart it
                await self.coordinator.async_send_dispatch(state.power_watts, self._chunk(state))
            self._failures = 0
        except HomeAssistantError as err:
            self._failures += 1
            _LOGGER.warning("Battery dispatch renewal failed (%s/%s): %s",
                            self._failures, DISPATCH_MAX_FAILURES, scrub(err))
            if self._failures >= DISPATCH_MAX_FAILURES and self.state is state:
                self._end("connection_lost")
