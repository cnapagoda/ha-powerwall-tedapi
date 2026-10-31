"""Dispatch commands reach the gateway in order: a resume can't overtake a
renewal (or start) still on its way and then be undone by it."""
from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .test_storm import V1R, _setup


class Gateway:
    """Stands in for the coordinator's sends. A send can be held "on its way"
    until released; `got` is the order the gateway received them in."""

    def __init__(self) -> None:
        self.got: list[str] = []
        self.hold: dict[str, asyncio.Event] = {}

    def held(self, kind: str) -> asyncio.Event:
        self.hold[kind] = asyncio.Event()
        return self.hold[kind]

    async def _send(self, kind: str) -> dict:
        event = self.hold.pop(kind, None)
        if event is not None:
            await event.wait()
        self.got.append(kind)
        return {"ok": True}

    async def dispatch(self, power_watts: float, duration_seconds: int) -> dict:
        return await self._send("dispatch")

    async def resume(self) -> dict:
        return await self._send("resume")


async def _session(hass: HomeAssistant, gateway: Gateway):
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    coordinator.async_send_dispatch = gateway.dispatch
    coordinator.async_send_resume = gateway.resume
    return entry.runtime_data.dispatch


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_stop_waits_for_a_renewal_on_its_way(hass: HomeAssistant, fake_powerwall) -> None:
    gateway = Gateway()
    session = await _session(hass, gateway)
    await session.async_start(-3000, 1800, None, None)
    release = gateway.held("dispatch")
    renewal = hass.async_create_task(session._async_tick(dt_util.utcnow()))
    await _settle()                          # the renewal is on its way
    stop = hass.async_create_task(session.async_stop("target_reached"))
    await _settle()
    assert session.state is None and gateway.got == ["dispatch"]  # resume waits
    release.set()
    await asyncio.gather(renewal, stop)
    assert gateway.got == ["dispatch", "dispatch", "resume"]  # resume last: battery handed back


async def test_renewal_queued_behind_a_stop_is_dropped(hass: HomeAssistant, fake_powerwall) -> None:
    """A renewal waiting for the lock when the session ends doesn't send."""
    gateway = Gateway()
    session = await _session(hass, gateway)
    await session.async_start(1000, 1800, None, None)
    release = gateway.held("dispatch")
    first = hass.async_create_task(session._async_tick(dt_util.utcnow()))
    await _settle()
    second = hass.async_create_task(session._async_tick(dt_util.utcnow()))  # waits for the lock
    await _settle()
    stop = hass.async_create_task(session._async_resume_quietly("stopped"))
    await _settle()
    release.set()
    await asyncio.gather(first, second, stop)
    assert gateway.got == ["dispatch", "dispatch", "resume"]  # the second renewal never went out


async def test_unload_waits_for_a_renewal_on_its_way(hass: HomeAssistant, fake_powerwall) -> None:
    gateway = Gateway()
    session = await _session(hass, gateway)
    await session.async_start(1000, 1800, None, None)
    release = gateway.held("dispatch")
    renewal = hass.async_create_task(session._async_tick(dt_util.utcnow()))
    await _settle()
    unload = hass.async_create_task(session.async_shutdown())
    await _settle()
    release.set()
    await asyncio.gather(renewal, unload)
    assert gateway.got[-1] == "resume"


async def test_stop_during_start_wins(hass: HomeAssistant, fake_powerwall) -> None:
    """A stop while the start command is on its way: no session is left
    running and the resume goes out after the start."""
    gateway = Gateway()
    session = await _session(hass, gateway)
    release = gateway.held("dispatch")
    start = hass.async_create_task(session.async_start(1000, 1800, None, None))
    await _settle()
    stop = hass.async_create_task(session.async_stop())
    await _settle()
    release.set()
    result, _ = await asyncio.gather(start, stop)
    assert gateway.got == ["dispatch", "resume"]
    assert session.state is None and result["active"] is False
