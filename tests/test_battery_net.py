"""2.8.2 (#35): battery energy netted in 5-minute windows, as Tesla's app
counts it; leftover alert repairs named after the DIN; the issue forms."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
import yaml

from custom_components.tesla_tedapi.battery_net import NetBatteryEnergy
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, FakePowerwall
from .test_storm import V1R, _eid, _refresh, _setup

T0 = datetime(2026, 11, 23, 0, 0, tzinfo=UTC)


def _minutes(m: float) -> datetime:
    return T0 + timedelta(minutes=m)


def test_both_ways_in_one_window_counts_only_the_difference() -> None:
    net = NetBatteryEnergy()
    net.update(_minutes(0), 100.0, 200.0)
    # It starts from the gateway's counters.
    assert net.summary() == {"battery_energy_charged": 100.0, "battery_energy_discharged": 200.0}
    # Discharging 0.4 kWh in the window while the charged counter creeps up 0.025.
    net.update(_minutes(2), 100.01, 200.2)
    net.update(_minutes(4), 100.025, 200.4)
    assert net.summary() == {"battery_energy_charged": 100.0, "battery_energy_discharged": 200.0}  # still open
    net.update(_minutes(5.5), 100.03, 200.45)  # first reading of the next window closes it
    assert net.summary() == {"battery_energy_charged": 100.0, "battery_energy_discharged": 200.42}
    # Charging in the next window: only the charged side grows.
    net.update(_minutes(10.2), 101.03, 200.46)
    assert net.summary() == {"battery_energy_charged": 100.99, "battery_energy_discharged": 200.42}
    # The gateway counters gained 1.03 and 0.46; the net totals 0.99 and 0.42.


def test_a_gap_is_one_window_and_a_reset_starts_again() -> None:
    net = NetBatteryEnergy()
    net.update(_minutes(0), 10.0, 20.0)
    net.update(_minutes(1), 10.1, 20.0)
    # Three hours without readings: netted as one window.
    net.update(_minutes(181), 13.0, 22.5)
    assert net.summary() == {"battery_energy_charged": 10.5, "battery_energy_discharged": 20.0}
    # The gateway's counters went back (a reset): the open window is counted
    # up to the last reading, and the totals carry on from there.
    net.update(_minutes(183), 13.4, 22.5)
    net.update(_minutes(184), 0.2, 0.1)
    assert net.summary() == {"battery_energy_charged": 10.9, "battery_energy_discharged": 20.0}
    net.update(_minutes(191), 0.2, 0.6)
    assert net.summary() == {"battery_energy_charged": 10.9, "battery_energy_discharged": 20.5}
    # A missing reading changes nothing.
    net.update(_minutes(200), None, 1.0)
    net.update(_minutes(200), True, 1.0)
    assert net.summary() == {"battery_energy_charged": 10.9, "battery_energy_discharged": 20.5}


def test_save_and_restore() -> None:
    net = NetBatteryEnergy()
    assert net.summary() == {"battery_energy_charged": None, "battery_energy_discharged": None}
    net.update(_minutes(0), 10.0, 20.0)
    net.update(_minutes(1), 10.0, 20.3)
    copy = NetBatteryEnergy()
    copy.restore(net.as_dict())
    assert copy.as_dict() == net.as_dict()
    # Restarted mid-window: the window carries on across the restart.
    copy.update(_minutes(6), 10.05, 20.4)
    assert copy.summary() == {"battery_energy_charged": 10.0, "battery_energy_discharged": 20.35}
    # Damaged or old stored data: start again, keeping valid totals.
    copy.restore({"window": "x", "start": [1, 2], "last": [1], "charged": 5.0, "discharged": 6.0})
    assert copy.window is None and copy.start is None and copy.last is None
    assert copy.summary() == {"battery_energy_charged": 5.0, "battery_energy_discharged": 6.0}
    copy.restore({})
    assert copy.summary() == {"battery_energy_charged": 5.0, "battery_energy_discharged": 6.0}


async def test_net_sensors(hass: HomeAssistant, fake_powerwall, hass_storage, freezer) -> None:
    freezer.move_to("2026-11-23T00:00:30+00:00")
    meters = {"battery": {"energy_imported": 500_000, "energy_exported": 450_000}}
    original = FakePowerwall.poll
    FakePowerwall.poll = lambda self, path: meters
    try:
        entry = await _setup(hass, V1R)
        charged = _eid(hass, "sensor", "battery_energy_charged_net")
        discharged = _eid(hass, "sensor", "battery_energy_discharged_net")
        state = hass.states.get(charged)
        assert float(state.state) == 500.0
        assert state.attributes["state_class"] == "total_increasing"
        assert state.attributes["device_class"] == "energy" and state.attributes["unit_of_measurement"] == "kWh"
        assert float(hass.states.get(discharged).state) == 450.0

        # 0.5 kWh out and 0.03 kWh "in" in the same window: 0.47 out.
        meters["battery"] = {"energy_imported": 500_030, "energy_exported": 450_500}
        freezer.move_to("2026-11-23T00:02:00+00:00")
        await _refresh(hass, entry)
        freezer.move_to("2026-11-23T00:05:10+00:00")
        await _refresh(hass, entry)
        assert float(hass.states.get(charged).state) == 500.0
        assert float(hass.states.get(discharged).state) == pytest.approx(450.47)
        # The gateway's own counters are unchanged.
        assert float(hass.states.get(_eid(hass, "sensor", "battery_energy_charged")).state) == 500.03

        # A restart keeps the totals and the open window.
        meters["battery"] = {"energy_imported": 500_040, "energy_exported": 450_600}
        await _refresh(hass, entry)
        saved = entry.runtime_data.coordinator._state_to_save()
        assert saved["battery_net"]["charged"] == 500.0
        assert await hass.config_entries.async_unload(entry.entry_id)
        hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 1, "key": f"{DOMAIN}.{entry.entry_id}",
                                                       "data": saved}
        freezer.move_to("2026-11-23T00:10:20+00:00")
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert float(hass.states.get(discharged).state) == pytest.approx(450.56)
        assert float(hass.states.get(charged).state) == 500.0
    finally:
        FakePowerwall.poll = original


async def test_alert_repairs_named_after_the_din_are_removed(hass: HomeAssistant, fake_powerwall) -> None:
    """Up to 2.8.0 alert repairs were named after the DIN (it holds the
    serial). Home Assistant keeps them, inactive, after a restart, and lists
    them in diagnostics: they're removed, others are left alone."""
    for issue_id in (f"alert_{DIN}_BMS_a023_SW_Assertion", "alert_1234567-00-A--TG000000000000_BatteryFault",
                     "tariff_write_other"):
        ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=False,
                              severity=ir.IssueSeverity.WARNING, translation_key="powerwall_alert")
    ir.async_create_issue(hass, "other", f"alert_{DIN}_x", is_fixable=False,
                          severity=ir.IssueSeverity.WARNING, translation_key="x")
    await _setup(hass, V1R)
    issues = set(ir.async_get(hass).issues)
    assert (DOMAIN, f"alert_{DIN}_BMS_a023_SW_Assertion") not in issues
    assert (DOMAIN, "alert_1234567-00-A--TG000000000000_BatteryFault") in issues  # another gateway's
    assert (DOMAIN, "tariff_write_other") in issues
    assert ("other", f"alert_{DIN}_x") in issues


def test_issue_forms_have_no_reserved_dropdown_options() -> None:
    """GitHub hides a form whose dropdown offers "None" (#35: the Bug report
    form was missing from the new-issue page)."""
    forms = Path(__file__).parents[1] / ".github" / "ISSUE_TEMPLATE"
    checked = 0
    for path in forms.glob("*.yml"):
        if path.name == "config.yml":
            continue
        for element in yaml.safe_load(path.read_text())["body"]:
            if element["type"] == "dropdown":
                options = [str(o).strip().lower() for o in element["attributes"]["options"]]
                assert "none" not in options, (path.name, element.get("id"))
                assert len(options) == len(set(options)), (path.name, element.get("id"))
                checked += 1
    assert checked
