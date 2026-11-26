"""2.9.0: before a battery dispatch stops renewing (or refuses to start) on
a battery level that's too old, one extra read of the level (or the poll
already running, waited for)."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import local
from .test_storm import V1R, _setup


async def _dispatch(hass: HomeAssistant, watts: int = -2000, seconds: int = 3600, **extra) -> None:
    await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                   {"power_watts": watts, "duration_seconds": seconds, **extra}, blocking=True)
    await hass.async_block_till_done()  # the refresh it asks for


class Level:
    """The gateway's battery level read; fails while `fail` is set."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.soe = 52.5

    def __call__(self) -> float:
        self.calls += 1
        if self.fail:
            raise ConnectionError("read timed out")
        return self.soe


def _spy(coordinator):
    """Counts the dispatch's extra reads (not the polls')."""
    calls: list[float] = []
    original = coordinator.async_read_battery_level

    async def read(timeout: float) -> bool:
        calls.append(timeout)
        return await original(timeout)

    return calls, patch.object(coordinator, "async_read_battery_level", read)


async def _tick(hass: HomeAssistant, session) -> None:
    await session._async_tick(dt_util.utcnow())
    await hass.async_block_till_done()


async def test_a_stale_start_reads_the_level_once(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    calls, spy = _spy(coordinator)
    freezer.tick(timedelta(seconds=300))
    with spy:
        await _dispatch(hass)
    assert len(calls) == 1 and calls[0] == 20
    assert entry.runtime_data.dispatch.active


async def test_a_late_poll_doesnt_pause_renewals(hass: HomeAssistant, fake_powerwall, freezer, caplog) -> None:
    entry = await _setup(hass, V1R)
    session = entry.runtime_data.dispatch
    await _dispatch(hass)
    pw = local(fake_powerwall)
    sent = len(pw.dispatches)
    level = Level()
    freezer.tick(timedelta(seconds=150))  # no poll meanwhile
    with patch.object(pw, "level", level), caplog.at_level(logging.WARNING):
        await _tick(hass, session)
    assert level.calls >= 1
    # Renewed (its own renewal timer may have fired too as the clock moved).
    assert len(pw.dispatches) > sent
    assert "not renewed" not in caplog.text
    age = (dt_util.utcnow() - entry.runtime_data.coordinator.battery_level_at).total_seconds()
    assert age < 1


async def test_a_failed_extra_read_changes_nothing(hass: HomeAssistant, fake_powerwall, freezer, caplog) -> None:
    entry = await _setup(hass, V1R)
    session = entry.runtime_data.dispatch
    await _dispatch(hass)
    pw = local(fake_powerwall)
    sent = len(pw.dispatches)
    level = Level()
    level.fail = True
    freezer.tick(timedelta(seconds=150))
    with patch.object(pw, "level", level), caplog.at_level(logging.WARNING):
        await _tick(hass, session)
    assert level.calls >= 1
    assert len(pw.dispatches) == sent
    assert "not renewed" in caplog.text


async def test_a_running_poll_is_waited_for(hass: HomeAssistant, fake_powerwall) -> None:
    """No second read beside a poll: its reading is used when it ends."""
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    coordinator._unschedule_refresh()  # no real poll in between
    coordinator.battery_level_at = dt_util.utcnow() - timedelta(minutes=5)
    coordinator._polling = True
    coordinator._poll_done.clear()

    async def poll_ends() -> None:
        await asyncio.sleep(0)
        coordinator.battery_level_at = dt_util.utcnow()
        coordinator._polling = False
        coordinator._poll_done.set()

    task = hass.async_create_task(poll_ends())
    assert await coordinator.async_read_battery_level(5)
    await task
    # A poll that doesn't end in time.
    coordinator._polling = True
    coordinator._poll_done.clear()
    assert not await coordinator.async_read_battery_level(0.01)
    # A poll that ends without a battery level.
    hass.loop.call_soon(coordinator._poll_done.set)
    assert not await coordinator.async_read_battery_level(5)
    coordinator._polling = False


async def test_no_extra_read_while_the_gateway_is_down(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    pw = local(fake_powerwall)
    level = Level()
    freezer.tick(timedelta(seconds=300))
    with patch.object(pw, "level", level):
        coordinator.last_update_success = False
        assert not await coordinator.async_read_battery_level(5)
        coordinator.last_update_success = True
        coordinator._pw = None  # no connection: never a new login for it
        assert not await coordinator.async_read_battery_level(5)
    assert level.calls == 0


async def test_no_extra_read_for_a_hold(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    """A 0 W hold moves no energy: it needs no fresh level."""
    entry = await _setup(hass, V1R)
    calls, spy = _spy(entry.runtime_data.coordinator)
    freezer.tick(timedelta(seconds=300))
    with spy:
        await _dispatch(hass, 0)
        freezer.tick(timedelta(seconds=300))
        await _tick(hass, entry.runtime_data.dispatch)
    assert calls == []
    assert entry.runtime_data.dispatch.active


async def test_the_extra_read_can_end_it_at_the_limit(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    entry = await _setup(hass, V1R)
    session = entry.runtime_data.dispatch
    await _dispatch(hass, -2000, 3600, max_soc=60)
    pw = local(fake_powerwall)
    sent = len(pw.dispatches)
    level = Level()
    level.soe = 80.0  # well past 60 % on the app's scale
    freezer.tick(timedelta(seconds=150))
    with patch.object(pw, "level", level):
        await _tick(hass, session)
    assert not session.active
    assert len(pw.dispatches) == sent  # stopped, not renewed


async def test_an_extra_read_without_a_level(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    entry = await _setup(hass, V1R)
    freezer.tick(timedelta(seconds=300))
    with patch.object(local(fake_powerwall), "level", return_value=None):
        assert not await entry.runtime_data.coordinator.async_read_battery_level(5)
