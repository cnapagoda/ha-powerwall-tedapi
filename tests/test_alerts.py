"""Alert tracking, problem sensor, events and repairs."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.tesla_tedapi.alerts import (
    SEVERITY_FAULT,
    SEVERITY_NOTICE,
    SEVERITY_STATUS,
    AlertTracker,
    describe,
    level,
    summary,
)
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345",
       "scan_interval": 30}


def test_describe() -> None:
    assert describe("SystemConnectedToGrid")[0] == SEVERITY_STATUS
    assert describe("BatteryFault")[0] == SEVERITY_FAULT
    assert describe("SomethingNew") == (SEVERITY_NOTICE, "SomethingNew")


def test_level_and_summary() -> None:
    assert level(["SystemConnectedToGrid", "BackfeedLimited"]) == "ok"
    assert level(["SystemConnectedToGrid", "EnergyAccumulatorIrrational"]) == "notice"
    assert level(["IslandChecksFailed", "BatteryFault"]) == "fault"
    assert summary([]) == "None"
    assert summary(["SystemConnectedToGrid", "PWS_a009_Rsd"]) == "Rapid shutdown active · Connected to the grid"
    long_text = summary([f"Unknown{i:03d}LongAlertName" for i in range(20)])
    assert len(long_text) <= 255 and long_text.endswith("…")


def test_tracker() -> None:
    t = AlertTracker()
    now = dt_util.utcnow()
    assert t.update(["SystemConnectedToGrid"], {}, now) == []  # baseline only
    changes = t.update(["SystemConnectedToGrid", "BatteryFault"], {"Powerwall 3": ["BatteryFault"]}, now)
    assert [(c.kind, c.name, c.sources) for c in changes] == [("raised", "BatteryFault", ["gateway", "Powerwall 3"])]
    assert t.details()[0]["alert"] == "BatteryFault"  # most severe first
    assert t.problems(now + timedelta(minutes=5)) == []
    assert t.problems(now + timedelta(minutes=10)) == ["BatteryFault"]
    changes = t.update(["SystemConnectedToGrid"], {}, now + timedelta(minutes=11))
    assert [(c.kind, c.name) for c in changes] == [("cleared", "BatteryFault")]
    assert t.problems(now + timedelta(minutes=11)) == []


def _eid(hass: HomeAssistant, platform: str, key: str) -> str:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DIN}_{key}")


async def test_alert_entities_and_repairs(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    problem = _eid(hass, "binary_sensor", "alert_problem")
    event = _eid(hass, "event", "alert_event")
    assert hass.states.get(problem).state == "off"
    assert hass.states.get(_eid(hass, "sensor", "alert_level")).state == "ok"
    assert hass.states.get(_eid(hass, "sensor", "active_alerts")).state == "Connected to the grid"

    # A fault appears: an event, but not yet a problem
    fake_powerwall.alert_list = ["SystemConnectedToGrid", "BatteryFault"]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    ev = hass.states.get(event)
    assert ev.attributes["event_type"] == "raised"
    assert ev.attributes["alert"] == "BatteryFault"
    assert ev.attributes["severity"] == SEVERITY_FAULT
    assert hass.states.get(problem).state == "off"
    assert hass.states.get(_eid(hass, "sensor", "alert_level")).state == "fault"
    assert hass.states.get(_eid(hass, "sensor", "active_alerts")).state == "Battery fault · Connected to the grid"
    details = hass.states.get(_eid(hass, "sensor", "alerts")).attributes["details"]
    assert details[0]["description"] == "Battery fault"

    # Still there after 10 minutes: problem + repair issue
    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(problem).state == "on"
    issue_id = f"alert_{DIN}_BatteryFault"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.severity == ir.IssueSeverity.ERROR

    # Cleared: problem off, repair gone, "cleared" event
    fake_powerwall.alert_list = ["SystemConnectedToGrid"]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(problem).state == "off"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert hass.states.get(event).attributes["event_type"] == "cleared"
