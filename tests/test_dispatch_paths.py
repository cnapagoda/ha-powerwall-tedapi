"""Dispatch sessions when the gateway refuses or drops out, and the entry
lookup and setup paths shared by every action."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.tesla_tedapi import async_remove_config_entry_device
from custom_components.tesla_tedapi.const import DOMAIN, EVENT_DISPATCH_ENDED

from .conftest import DIN, FakePowerwall, local
from .test_storm import V1R, _refresh, _setup

COORDINATOR = "custom_components.tesla_tedapi.coordinator.TedapiCoordinator"


async def _tick(hass: HomeAssistant, seconds: int) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


async def _dispatch(hass: HomeAssistant, watts: int = 1000, seconds: int = 1800, **extra) -> None:
    await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                   {"power_watts": watts, "duration_seconds": seconds, **extra}, blocking=True)


async def test_refused_dispatch_raises(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    pw = local(fake_powerwall)
    with patch.object(pw, "dispatch_battery_power", return_value=None), pytest.raises(HomeAssistantError) as err:
        await _dispatch(hass)
    assert err.value.translation_key == "dispatch_refused"
    with patch.object(pw, "dispatch_battery_power", side_effect=ValueError("bad")), \
            pytest.raises(HomeAssistantError) as err:
        await _dispatch(hass)
    assert err.value.translation_key == "dispatch_invalid"


async def test_renewal_failures_end_the_session(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    await _dispatch(hass)
    pw = local(fake_powerwall)
    with patch.object(pw, "dispatch_battery_power", return_value=None):
        for n in (1, 2):
            await _tick(hass, 61 * n)
            assert not ended
        await _tick(hass, 61 * 3)
    assert ended[-1].data["reason"] == "connection_lost"


async def test_an_error_inside_the_connection_is_a_refusal(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    """pypowerwall raising (no answer, a dropped connection) is reported like
    a refusal: renewals count it, and the session ends with connection_lost
    (it used to show as running until its end time)."""
    entry = await _setup(hass, V1R)
    pw = local(fake_powerwall)
    with patch.object(pw, "dispatch_battery_power", side_effect=ConnectionError("no answer")), \
            pytest.raises(HomeAssistantError) as err:
        await _dispatch(hass)
    assert err.value.translation_key == "dispatch_refused_reason"
    assert err.value.translation_placeholders == {"reason": "ConnectionError: no answer"}
    assert "Battery dispatch: the command failed before the Powerwall answered" in caplog.text

    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    await _dispatch(hass)
    with patch.object(pw, "dispatch_battery_power", side_effect=ConnectionError("no answer")):
        for n in (1, 2, 3):
            await _tick(hass, 61 * n)
    assert ended[-1].data["reason"] == "connection_lost"

    # A resume that fails the same way: an error callers expect, and the
    # entry still unloads.
    await _dispatch(hass)
    with patch.object(pw, "resume_battery_operation", side_effect=OSError("gone")):
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(DOMAIN, "resume_battery_operation", {}, blocking=True)
        assert err.value.translation_key == "resume_refused"
        await _dispatch(hass)
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_one_failed_renewal_is_forgiven(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    await _dispatch(hass)
    pw = local(fake_powerwall)
    with patch.object(pw, "dispatch_battery_power", return_value=None):
        await _tick(hass, 61)
    await _tick(hass, 122)
    with patch.object(pw, "dispatch_battery_power", return_value=None):
        await _tick(hass, 183)
        await _tick(hass, 244)
    assert not ended  # the good renewal reset the count


async def test_failed_resume_at_the_end_still_ends(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    await _dispatch(hass, seconds=90)
    with patch.object(local(fake_powerwall), "resume_battery_operation", return_value=None):
        await _tick(hass, 91)
    assert ended[-1].data["reason"] == "completed"


async def test_failed_resume_while_unloading(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    await _dispatch(hass)
    with patch.object(local(fake_powerwall), "resume_battery_operation", return_value=None):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert ended[-1].data["reason"] == "unloaded"


async def test_home_assistant_stopping_hands_the_battery_back(hass: HomeAssistant, fake_powerwall) -> None:
    """Entries aren't unloaded when Home Assistant stops: the dispatch is
    ended there, with a resume and the ended event."""
    entry = await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    await _dispatch(hass)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert ended[-1].data["reason"] == "unloaded"
    assert local(fake_powerwall).resumes == 1
    assert entry.runtime_data.dispatch.state is None


async def test_home_assistant_stopping_without_a_dispatch(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert not ended
    assert local(fake_powerwall).resumes == 0


async def test_unload_after_home_assistant_stopped(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    """The stop listener is removed on unload even after it has run: no
    'Unable to remove unknown job listener' error."""
    entry = await _setup(hass, V1R)
    await _dispatch(hass)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "Unable to remove unknown job listener" not in caplog.text
    assert local(fake_powerwall).resumes == 1


async def test_resume_without_session(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    resp = await hass.services.async_call(DOMAIN, "resume_battery_operation", {},
                                          blocking=True, return_response=True)
    assert resp["response"] == {"accepted": True}
    assert local(fake_powerwall).resumes == 1


async def test_no_limit_check_without_readings(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    ended = async_capture_events(hass, EVENT_DISPATCH_ENDED)
    await _dispatch(hass, min_soc=49)
    entry.runtime_data.coordinator.data["local_ok"] = False
    await _tick(hass, 61)
    assert not ended  # level unknown: keep going, the chunk expires on its own


async def test_tick_just_before_the_end(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    session = entry.runtime_data.dispatch
    await _dispatch(hass, seconds=61)
    sent = len(local(fake_powerwall).dispatches)
    session.state.ends = dt_util.utcnow() + timedelta(seconds=0.5)
    await session._async_tick(dt_util.utcnow())  # under 1 s left: the end timer handles it
    assert len(local(fake_powerwall).dispatches) == sent


async def test_end_timer_after_stop(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    session = entry.runtime_data.dispatch
    await _dispatch(hass, seconds=90)
    await hass.services.async_call(DOMAIN, "resume_battery_operation", {}, blocking=True)
    await session._async_at_end(dt_util.utcnow())  # late timer: nothing to do
    await session._async_tick(dt_util.utcnow())
    assert local(fake_powerwall).resumes == 1


async def test_session_refuses_without_signed_connection(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    with patch.object(type(entry.runtime_data.coordinator), "dispatch_supported", False), \
            pytest.raises(ServiceValidationError):
        await _dispatch(hass)


async def test_no_entry_and_two_entries(hass: HomeAssistant, fake_powerwall) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError) as err:
        await _dispatch(hass)
    assert err.value.translation_key == "entry_not_found"

    await _setup(hass, V1R)
    other = MockConfigEntry(domain=DOMAIN, unique_id="other", data=V1R, minor_version=2)
    other.add_to_hass(hass)
    assert await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await _dispatch(hass)
    assert err.value.translation_key == "entry_ambiguous"
    # Naming one works.
    await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                   {"power_watts": 0, "duration_seconds": 60, "config_entry_id": other.entry_id},
                                   blocking=True)


async def test_newer_entry_version_is_refused(hass: HomeAssistant, fake_powerwall) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, version=2)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_setup_retries_on_errors(hass: HomeAssistant, fake_powerwall) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, minor_version=2)
    entry.add_to_hass(hass)
    with patch(f"{COORDINATOR}.async_config_entry_first_refresh", side_effect=RuntimeError("boom")):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_waits_for_the_din(hass: HomeAssistant, fake_powerwall) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=None, data=V1R, minor_version=2)
    entry.add_to_hass(hass)
    with patch.object(FakePowerwall, "din", lambda self: None):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY  # no DIN counts as unreachable


async def test_remove_stale_device_only(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    reg = dr.async_get(hass)
    live = reg.async_get_device(identifiers={(DOMAIN, DIN)})
    stale = reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "old-gateway")})
    assert await async_remove_config_entry_device(hass, entry, live) is False
    assert await async_remove_config_entry_device(hass, entry, stale) is True


async def test_refresh_keeps_a_session(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    await _dispatch(hass)
    await _refresh(hass, entry)
    assert entry.runtime_data.dispatch.active


async def test_one_warning_per_session(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    """The session start carries the warranty warning; pypowerwall's warning
    for each accepted command (every renewal) is dropped."""
    import logging  # noqa: PLC0415

    from custom_components.tesla_tedapi.dispatch import (  # noqa: PLC0415
        PYPOWERWALL_DISPATCH_LOGGER,
        RepeatDispatchFilter,
        quiet_dispatch_repeats,
    )

    await _setup(hass, V1R)
    quiet_dispatch_repeats()  # twice: still one filter
    pp_logger = logging.getLogger(PYPOWERWALL_DISPATCH_LOGGER)
    assert sum(isinstance(f, RepeatDispatchFilter) for f in pp_logger.filters) == 1

    with caplog.at_level(logging.WARNING):
        await _dispatch(hass)
        pp_logger.warning("Battery dispatch accepted: %s %g W for %d s. use at your own risk.", "discharge", 1000, 300)
        pp_logger.warning("Something else from pypowerwall")
    texts = [r.getMessage() for r in caplog.records]
    assert sum("may affect the Powerwall warranty" in t for t in texts) == 1
    assert not any(t.startswith("Battery dispatch accepted") for t in texts)
    assert "Something else from pypowerwall" in texts


async def test_charge_with_grid_charging_off_warns(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    """A charge draws from the grid only while Grid charging is on: starting
    one with it off logs a warning; with it on, or for a discharge, it doesn't."""
    import logging  # noqa: PLC0415

    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    warning = "charging with Grid charging off"
    assert coordinator.data["grid_charging"] is False
    with caplog.at_level(logging.WARNING):
        await _dispatch(hass, watts=-1000)
    assert sum(warning in r.getMessage() for r in caplog.records) == 1

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await _dispatch(hass, watts=1000)
        FakePowerwall.grid_charging_disallow = False
        await _refresh(hass, entry)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.data["grid_charging"] is True
        await _dispatch(hass, watts=-1000)
    assert not any(warning in r.getMessage() for r in caplog.records)
