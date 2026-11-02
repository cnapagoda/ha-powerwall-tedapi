"""2.5.1: attributes that change on every reading are kept out of the recorder
(they're still on the live state)."""
from __future__ import annotations

from homeassistant.components.recorder import get_instance, history
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
PACK = "TG0123456789AB"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """A real (in-memory) recorder, set up before hass (overrides conftest's)."""
    yield


def _eid(hass: HomeAssistant, key: str) -> str:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DIN}_{key}")


async def _recorded(hass: HomeAssistant, entity_id: str, start) -> dict:
    await async_wait_recording_done(hass)
    states = await get_instance(hass).async_add_executor_job(
        history.state_changes_during_period, hass, start, None, entity_id)
    return dict(states[entity_id][-1].attributes)


async def test_churning_attributes_not_recorded(hass: HomeAssistant, fake_powerwall) -> None:
    start = dt_util.utcnow()
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, title="Powerwall")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Pack capacity: remaining energy is live, not recorded.
    capacity = _eid(hass, f"pack_{PACK}_capacity")
    assert hass.states.get(capacity).attributes["remaining_kwh"] == 7.0
    assert "remaining_kwh" not in await _recorded(hass, capacity, start)

    # Grid counter check: today's running totals are live, not recorded.
    check = _eid(hass, "grid_counter_check")
    assert "covered_minutes" in hass.states.get(check).attributes
    assert "covered_minutes" not in await _recorded(hass, check, start)

    # Setup health: the per-check detail is live; the problems stay recorded.
    health = _eid(hass, "setup_health")
    live = hass.states.get(health).attributes
    assert "checks" in live and "problems" in live
    recorded = await _recorded(hass, health, start)
    assert "checks" not in recorded and "problems" in recorded

    # The alert codes stay recorded; the details don't.
    alerts = _eid(hass, "alerts")
    recorded = await _recorded(hass, alerts, start)
    assert recorded["alerts"] == ["SystemConnectedToGrid"] and "details" not in recorded


async def test_full_capacity_attributes_do_not_churn(hass: HomeAssistant, fake_powerwall) -> None:
    """Battery full capacity lists each pack's capacity, not its remaining
    energy, so its attributes don't change on every reading."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, title="Powerwall")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    full = _eid(hass, "battery_full_capacity")
    assert hass.states.get(full).attributes["Powerwall 3"] == {"serial": PACK, "full_kwh": 13.9}
