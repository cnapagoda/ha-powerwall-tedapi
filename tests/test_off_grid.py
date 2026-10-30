"""Go off-grid and reconnect (local, signed v1r): checks before and after each
command, automatic reconnect, and a run that survives a reload."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.tesla_tedapi.const import DOMAIN, EVENT_OFF_GRID_ENDED

from .conftest import local
from .test_storm import V1R, WIFI, _eid, _refresh, _setup


async def _go(hass: HomeAssistant, **data) -> dict:
    return await hass.services.async_call(DOMAIN, "go_off_grid", {"confirm": True, **data},
                                          blocking=True, return_response=True)


def _grid_attrs(hass: HomeAssistant) -> dict:
    return dict(hass.states.get(_eid(hass, "binary_sensor", "grid_connected")).attributes)


def _islands(fake) -> list:
    return [w for w in local(fake).writes if w[0] == "island"]


async def test_go_off_grid_and_reconnect(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    events = async_capture_events(hass, EVENT_OFF_GRID_ENDED)

    response = await _go(hass, duration_minutes=60)
    await hass.async_block_till_done()
    assert _islands(fake_powerwall) == [("island", 6)]
    assert response["grid_status"] == "DOWN"
    attrs = _grid_attrs(hass)
    assert attrs["off_grid_run"] is True
    assert attrs["reconnect_at"] is not None
    assert attrs["reconnect_below"] == 20.0  # the backup reserve
    assert hass.states.get(_eid(hass, "binary_sensor", "grid_connected")).state == "off"

    response = await hass.services.async_call(DOMAIN, "reconnect_grid", {}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    assert response["grid_status"] == "SYNCING"
    assert _islands(fake_powerwall)[-1] == ("island", 1)
    assert _grid_attrs(hass)["off_grid_run"] is False
    assert _grid_attrs(hass)["last_end_reason"] == "requested"
    assert [e.data["reason"] for e in events] == ["requested"]


async def test_go_off_grid_needs_confirm(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "go_off_grid", {"confirm": False}, blocking=True)
    assert _islands(fake_powerwall) == []


async def test_go_off_grid_needs_v1r(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, WIFI)
    with pytest.raises(ServiceValidationError):
        await _go(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "reconnect_grid", {}, blocking=True)


async def test_go_off_grid_only_from_the_grid(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    fake_powerwall.grid = "DOWN"
    await _refresh(hass, entry)
    with pytest.raises(ServiceValidationError):
        await _go(hass)
    assert _islands(fake_powerwall) == []


async def test_go_off_grid_refused_on_low_battery(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)  # battery 50 %
    with pytest.raises(ServiceValidationError):
        await _go(hass, min_soc=60)
    assert _islands(fake_powerwall) == []


async def test_go_off_grid_refused_by_gateway(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    fake_powerwall.contactor_answers = False
    with pytest.raises(HomeAssistantError):
        await _go(hass)
    assert _grid_attrs(hass)["off_grid_run"] is False


async def test_go_off_grid_not_confirmed_is_taken_back(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    fake_powerwall.contactor_moves = False
    with patch("custom_components.tesla_tedapi.off_grid.CONTACTOR_WAIT_SECONDS", 0), \
            pytest.raises(HomeAssistantError):
        await _go(hass)
    assert _islands(fake_powerwall) == [("island", 6), ("island", 1)]
    assert _grid_attrs(hass)["off_grid_run"] is False


async def test_reconnect_not_confirmed(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    await _go(hass)
    fake_powerwall.contactor_moves = False
    with patch("custom_components.tesla_tedapi.off_grid.CONTACTOR_WAIT_SECONDS", 0), \
            pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "reconnect_grid", {}, blocking=True)
    assert _grid_attrs(hass)["off_grid_run"] is True


async def test_reconnects_at_the_end(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    events = async_capture_events(hass, EVENT_OFF_GRID_ENDED)
    await _go(hass, duration_minutes=30)
    entry.runtime_data.off_grid.state.ends = dt_util.utcnow() - timedelta(seconds=1)
    await _refresh(hass, entry)
    assert _islands(fake_powerwall)[-1] == ("island", 1)
    assert [e.data["reason"] for e in events] == ["completed"]


async def test_reconnects_on_low_battery(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    events = async_capture_events(hass, EVENT_OFF_GRID_ENDED)
    await _go(hass, min_soc=40)
    fake_powerwall.soe = 42.0  # 39 % on the app scale
    await _refresh(hass, entry)
    assert _islands(fake_powerwall)[-1] == ("island", 1)
    assert [e.data["reason"] for e in events] == ["min_soc"]


async def test_reconnected_elsewhere(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    events = async_capture_events(hass, EVENT_OFF_GRID_ENDED)
    await _go(hass)
    session = entry.runtime_data.off_grid
    session.state.started -= timedelta(minutes=5)
    fake_powerwall.grid = "UP"  # e.g. reconnected in the Tesla app
    await _refresh(hass, entry)
    assert session.state is None
    assert _islands(fake_powerwall) == [("island", 6)]
    assert [e.data["reason"] for e in events] == ["reconnected_elsewhere"]


async def test_failed_reconnect_raises_repair_and_retries(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    await _go(hass, duration_minutes=30)
    session = entry.runtime_data.off_grid
    session.state.ends = dt_util.utcnow() - timedelta(seconds=1)
    fake_powerwall.contactor_answers = False
    await _refresh(hass, entry)
    issue_id = f"off_grid_reconnect_failed_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert session.state is not None

    # Not again before the retry time.
    tries = len(_islands(fake_powerwall))
    await _refresh(hass, entry)
    assert len(_islands(fake_powerwall)) == tries

    fake_powerwall.contactor_answers = True
    session._retry_at = 0
    await _refresh(hass, entry)
    assert session.state is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_run_survives_a_reload(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    await _go(hass, duration_minutes=30, min_soc=10)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = entry.runtime_data.off_grid.state
    assert state is not None and state.min_soc == 10 and state.ends is not None
    assert _grid_attrs(hass)["off_grid_run"] is True

    # Back on the grid by the next load: the saved run is dropped.
    fake_powerwall.grid = "UP"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.off_grid.state is None
    assert _grid_attrs(hass)["off_grid_run"] is False


async def test_removing_the_entry_forgets_the_run(hass: HomeAssistant, fake_powerwall, hass_storage) -> None:
    entry: MockConfigEntry = await _setup(hass, V1R)
    await _go(hass)
    await hass.async_block_till_done()
    key = f"{DOMAIN}.{entry.entry_id}.off_grid"
    assert key in hass_storage
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
