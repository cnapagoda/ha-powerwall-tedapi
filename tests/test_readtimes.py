"""2.9.0: how long the gateway takes to answer, on Setup health and in
diagnostics."""
from __future__ import annotations

from datetime import timedelta
import json
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import requests

from custom_components.tesla_tedapi.const import DOMAIN
from custom_components.tesla_tedapi.diagnostics import async_get_config_entry_diagnostics
from custom_components.tesla_tedapi.readtimes import ReadTimes, is_timeout

from .conftest import DIN, FakePowerwall, local
from .test_storm import V1R, _setup


def test_summary() -> None:
    times = ReadTimes()
    times.read(0, "full", 9.0, ok=True, path="signed")  # over an hour before "now"
    times.read(4000, "full", 2.0, ok=True, path="signed")
    times.read(4010, "full", 4.0, ok=True, path="wifi_fallback")
    times.read(4020, "full", 10.0, ok=False, timeout=True)
    times.read(4030, "fast", 0.5, ok=True, path="signed")
    times.read(4040, "fast", 3.0, ok=False)
    times.loop_lag(4000, 0.002)
    times.loop_lag(4010, 0.250)
    times.level_age(4050, 35.04)
    times.level_age(4060, 95.0)
    summary = times.summary(4100)
    assert summary["window_minutes"] == 60
    assert summary["full"] == {"count": 3, "median_s": 3.0, "slowest_s": 4.0, "failed": 1, "timeouts": 1,
                               "paths": {"signed": 1, "wifi_fallback": 1}}
    assert summary["fast"] == {"count": 2, "median_s": 0.5, "slowest_s": 0.5, "failed": 1, "timeouts": 0,
                               "paths": {"signed": 1}}
    assert summary["loop_delay_ms"] == {"median": 126.0, "max": 250.0}
    assert summary["dispatch_level_age_s"] == {"last": 95.0, "max": 95.0, "renewals": 2}
    empty = ReadTimes().summary(0)
    assert empty["full"]["median_s"] is None and empty["loop_delay_ms"]["max"] is None
    assert empty["dispatch_level_age_s"] == {"last": None, "max": None, "renewals": 0}


def test_is_timeout() -> None:
    assert is_timeout(TimeoutError())
    assert is_timeout(requests.exceptions.ReadTimeout("boom"))
    assert is_timeout(RuntimeError("HTTPSConnectionPool: Read timed out. (read timeout=10)"))
    assert not is_timeout(ConnectionError("refused"))


def _health(hass: HomeAssistant):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DIN}_setup_health")
    return hass.states.get(entity_id).attributes["read_times"]


async def test_polls_are_timed(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    times = _health(hass)
    assert times["full"]["count"] >= 2 and times["full"]["failed"] == 0
    assert times["full"]["paths"] == {"signed": times["full"]["count"]}
    assert times["full"]["median_s"] is not None
    assert times["loop_delay_ms"]["median"] is not None

    # A poll that times out.
    def timed_out(self):
        raise requests.exceptions.ReadTimeout("Read timed out.")

    local(fake_powerwall).power = timed_out.__get__(local(fake_powerwall), FakePowerwall)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    summary = coordinator.read_times.summary(time.monotonic())
    assert summary["full"]["failed"] == 1 and summary["full"]["timeouts"] == 1

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["read_times"]["full"]["timeouts"] == 1
    assert DIN not in json.dumps(diagnostics["read_times"])


async def test_fast_polls_are_timed_apart(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, {"power_interval": 5})
    coordinator = entry.runtime_data.coordinator
    # A scheduled poll right after the full read: power only (async_refresh
    # would force a full read).
    await coordinator._async_refresh(scheduled=True)
    times = coordinator.read_times.summary(time.monotonic())
    assert times["fast"]["count"] == 1 and times["full"]["count"] == 1


async def test_renewals_record_the_level_age(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    entry = await _setup(hass, V1R)
    await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                   {"power_watts": -2000, "duration_seconds": 3600}, blocking=True)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=40))
    await entry.runtime_data.dispatch._async_tick(dt_util.utcnow())
    ages = entry.runtime_data.coordinator.read_times.summary(time.monotonic())
    assert ages["dispatch_level_age_s"]["renewals"] >= 1
    assert ages["dispatch_level_age_s"]["max"] is not None
