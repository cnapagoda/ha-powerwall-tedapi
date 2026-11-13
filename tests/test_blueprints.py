"""2.4.0: the recommended blueprints load as automations and call the
integration's actions (mocked) as intended."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import shutil

from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint import models
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.tesla_tedapi.const import DOMAIN

SOURCE = Path(__file__).parent.parent / "blueprints" / "automation" / "tesla_tedapi"
BLUEPRINTS = sorted(p.name for p in SOURCE.glob("*.yaml"))
PRICE, FEED_IN = "sensor.import_price", "sensor.feed_in_price"
EXPORT, MODE, DISPATCH, LEVEL = ("select.powerwall_grid_export", "select.powerwall_operation_mode",
                                 "binary_sensor.powerwall_dispatch_active", "sensor.powerwall_battery_level")
GRID, GRID_POWER, PROBLEM, RESERVE = ("binary_sensor.powerwall_grid_connected", "sensor.powerwall_grid_power",
                                      "binary_sensor.powerwall_problem", "number.powerwall_backup_reserve")
PUMP, HEATER, WARNING = "input_boolean.pump", "input_boolean.heater", "binary_sensor.storm_warning"


@pytest.fixture
async def blueprint_dir(hass: HomeAssistant, tmp_path):
    hass.config.config_dir = str(tmp_path)
    shutil.copytree(SOURCE, tmp_path / "blueprints" / "automation" / "tesla_tedapi")
    hass.states.async_set(MODE, "self_consumption")
    hass.states.async_set(DISPATCH, "off")
    hass.states.async_set(LEVEL, "55")
    return tmp_path


async def _automation(hass: HomeAssistant, name: str, inputs: dict) -> None:
    assert await async_setup_component(hass, "automation", {"automation": {
        "alias": name, "use_blueprint": {"path": f"tesla_tedapi/{name}", "input": inputs}}})
    await hass.async_block_till_done()


def _calls(hass: HomeAssistant, domain: str, *services: str) -> dict:
    return {s: async_mock_service(hass, domain, s) for s in services}


@pytest.mark.parametrize("name", BLUEPRINTS)
def test_blueprint_is_valid(name: str) -> None:
    data = load_yaml(SOURCE / name)
    blueprint = models.Blueprint(data, expected_domain="automation", schema=AUTOMATION_BLUEPRINT_SCHEMA)
    assert blueprint.metadata["source_url"].endswith(f"blueprints/automation/tesla_tedapi/{name}")
    text = (SOURCE / name).read_text()
    if "charge_to" in text or "discharge_to" in text:
        assert "warranty" in blueprint.metadata["description"]


async def test_negative_feed_in(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, "select", "select_option")
    hass.states.async_set(EXPORT, "battery_ok")
    hass.states.async_set(FEED_IN, "0.05")
    await _automation(hass, "negative_feed_in.yaml", {
        "feed_in_price": FEED_IN, "export_select": EXPORT, "threshold": 0, "minutes": 5,
        "restore": "pv_only"})

    hass.states.async_set(FEED_IN, "-0.02")
    await hass.async_block_till_done()
    assert calls["select_option"] == []  # not before 5 minutes
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done()
    assert [c.data["option"] for c in calls["select_option"]] == ["never"]

    hass.states.async_set(EXPORT, "never")
    hass.states.async_set(FEED_IN, "0.03")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=12))
    await hass.async_block_till_done()
    assert [c.data["option"] for c in calls["select_option"]] == ["never", "pv_only"]


async def test_negative_feed_in_leaves_never_alone(hass: HomeAssistant, blueprint_dir) -> None:
    """Export already never: nothing to change when the price goes negative."""
    calls = _calls(hass, "select", "select_option")
    hass.states.async_set(EXPORT, "never")
    hass.states.async_set(FEED_IN, "0.05")
    await _automation(hass, "negative_feed_in.yaml", {"feed_in_price": FEED_IN, "export_select": EXPORT})
    hass.states.async_set(FEED_IN, "-0.02")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done()
    assert calls["select_option"] == []  # already never


async def test_cheap_import_charge(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, DOMAIN, "charge_to", "resume_battery_operation")
    hass.states.async_set(PRICE, "0.20")
    await _automation(hass, "cheap_import_charge.yaml", {
        "powerwall": "entry1", "import_price": PRICE, "threshold": 0.05, "target_soc": 80,
        "power": 4000, "max_minutes": 90, "dispatch_active": DISPATCH, "operation_mode": MODE})

    hass.states.async_set(PRICE, "-0.01")
    await hass.async_block_till_done()
    assert [c.data for c in calls["charge_to"]] == [
        {"config_entry_id": "entry1", "target_soc": 80, "power_watts": 4000, "max_minutes": 90}]

    hass.states.async_set(DISPATCH, "on", {"power_watts": -4000})
    hass.states.async_set(PRICE, "0.30")
    await hass.async_block_till_done()
    assert len(calls["resume_battery_operation"]) == 1

    # Autonomous (Tesla's time-based control): left alone.
    hass.states.async_set(MODE, "autonomous")
    hass.states.async_set(PRICE, "0.01")
    await hass.async_block_till_done()
    assert len(calls["charge_to"]) == 1


async def test_cheap_import_does_not_stop_a_discharge(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, DOMAIN, "charge_to", "resume_battery_operation")
    hass.states.async_set(PRICE, "0.01")
    await _automation(hass, "cheap_import_charge.yaml", {
        "powerwall": "entry1", "import_price": PRICE, "dispatch_active": DISPATCH, "operation_mode": MODE})
    hass.states.async_set(DISPATCH, "on", {"power_watts": 3000})
    hass.states.async_set(PRICE, "0.40")
    await hass.async_block_till_done()
    assert calls["resume_battery_operation"] == []


async def test_price_spike_discharge(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, DOMAIN, "discharge_to", "resume_battery_operation")
    hass.states.async_set(FEED_IN, "0.10")
    await _automation(hass, "price_spike_discharge.yaml", {
        "powerwall": "entry1", "feed_in_price": FEED_IN, "threshold": 2, "floor_soc": 35,
        "power": 6000, "max_minutes": 30, "dispatch_active": DISPATCH, "operation_mode": MODE,
        "skip_autonomous": False})

    hass.states.async_set(FEED_IN, "4.50")
    await hass.async_block_till_done()
    assert [c.data for c in calls["discharge_to"]] == [
        {"config_entry_id": "entry1", "target_soc": 35, "power_watts": 6000, "max_minutes": 30}]

    hass.states.async_set(DISPATCH, "on", {"power_watts": 6000})
    hass.states.async_set(FEED_IN, "0.20")
    await hass.async_block_till_done()
    assert len(calls["resume_battery_operation"]) == 1


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_time_of_use(hass: HomeAssistant, blueprint_dir, freezer) -> None:
    calls = _calls(hass, DOMAIN, "charge_to", "discharge_to", "resume_battery_operation")
    start = dt_util.start_of_local_day() + timedelta(hours=12)
    freezer.move_to(start)
    await _automation(hass, "time_of_use.yaml", {
        "powerwall": "entry1", "charge_start": "23:00:00", "charge_end": "05:00:00",
        "peak_start": "16:00:00", "peak_end": "21:00:00", "target_soc": 95, "floor_soc": 25})

    for hour in (16, 21, 23):
        freezer.move_to(dt_util.start_of_local_day(start) + timedelta(hours=hour))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert [c.data["max_minutes"] for c in calls["discharge_to"]] == [300]
    assert calls["discharge_to"][0].data["target_soc"] == 25
    assert [c.data["max_minutes"] for c in calls["charge_to"]] == [360]  # 23:00 -> 05:00
    assert len(calls["resume_battery_operation"]) == 1


async def test_grid_outage_alert(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, "notify", "my_phone")
    await _automation(hass, "grid_outage_alert.yaml", {
        "notify_action": "notify.my_phone", "battery_level": LEVEL})
    hass.bus.async_fire("tesla_tedapi_grid_outage", {"phase": "started"})
    await hass.async_block_till_done()
    hass.bus.async_fire("tesla_tedapi_grid_outage", {"phase": "ended", "duration_seconds": 1500})
    await hass.async_block_till_done()
    messages = [c.data["message"] for c in calls["my_phone"]]
    assert messages == ["Grid outage: the house is on the Powerwall (battery 55 %).",
                        "Grid back after 25 min (battery 55 %)."]


def _amber(start, count: int, minutes: int, price) -> list[dict]:
    """Amber-shaped forecast: intervals start one second past the boundary."""
    return [{"start_time": (start + timedelta(minutes=minutes * k, seconds=1)).isoformat(),
             "end_time": (start + timedelta(minutes=minutes * (k + 1))).isoformat(),
             "per_kwh": price(k)} for k in range(count)]


@pytest.mark.parametrize("expected_lingering_timers", [True])  # the 30-minute trigger
async def test_dynamic_price_tariff(hass: HomeAssistant, blueprint_dir, freezer) -> None:
    from custom_components.tesla_tedapi import tariff  # noqa: PLC0415

    await hass.config.async_set_time_zone("Australia/Adelaide")
    freezer.move_to("2026-11-01T05:59:40+00:00")  # 16:29:40 local
    now = dt_util.utcnow().replace(minute=55, second=0, microsecond=0)  # 16:25 local
    half = now + timedelta(minutes=35)  # 06:30 UTC = 17:00 local
    buy = _amber(now, 7, 5, lambda k: 0.23) + _amber(half, 46, 30, lambda k: 0.30 + k / 100)
    sell = _amber(now, 7, 5, lambda k: 0.07) + _amber(half, 46, 30, lambda k: -0.02 if k < 3 else 0.10)
    hass.states.async_set("sensor.amber_general_forecast", "0.23", {"forecasts": buy})
    hass.states.async_set("sensor.amber_feed_in_forecast", "0.07", {"forecasts": sell})
    from homeassistant.core import SupportsResponse  # noqa: PLC0415

    sent = []

    def set_tariff(call):
        sent.append(call)
        return {"warnings": tariff.check_write(call.data["tou_settings"])}

    hass.services.async_register(DOMAIN, "set_tariff", set_tariff, supports_response=SupportsResponse.OPTIONAL)
    await _automation(hass, "dynamic_price_tariff.yaml", {"powerwall": "entry1", "dry_run": False})

    freezer.tick(timedelta(seconds=40))  # 16:30:20
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(sent) == 1
    data = sent[0].data
    assert data["dry_run"] is False and data["config_entry_id"] == "entry1"
    assert data["read_back"] == "once" and data["only_if_changed"] is True
    tou = data["tou_settings"]
    assert tariff.validate_write(tou) == []
    content = tou["tariff_content_v2"]
    periods = content["seasons"]["Summer"]["tou_periods"]
    assert len(periods) == 48 and periods["PERIOD_23_30"]["periods"][0]["toHour"] == 0
    rates = content["energy_charges"]["Summer"]["rates"]
    feed = content["sell_tariff"]["energy_charges"]["Summer"]["rates"]
    assert rates["PERIOD_16_30"] == 0.23 and feed["PERIOD_16_30"] == 0.07  # the 5-minute prices
    assert rates["PERIOD_17_00"] == 0.30 and feed["PERIOD_17_00"] == -0.02
    assert rates["PERIOD_16_00"] == 0.75 and feed["PERIOD_16_00"] == 0.10  # past the forecast: the last known price
    # Every slot priced from the forecast: no gaps, no stale prices.
    assert not tariff.check_write(tou) or all("negative" in w for w in tariff.check_write(tou))


@pytest.mark.parametrize("expected_lingering_timers", [True])  # the 30-minute trigger
async def test_dynamic_price_tariff_refuses_a_thin_forecast(hass: HomeAssistant, blueprint_dir, freezer) -> None:
    await hass.config.async_set_time_zone("Australia/Adelaide")
    freezer.move_to("2026-11-01T05:59:40+00:00")
    start = dt_util.utcnow().replace(minute=30, second=0, microsecond=0)
    hass.states.async_set("sensor.amber_general_forecast", "0.2",
                          {"forecasts": _amber(start, 12, 30, lambda k: 0.2)})  # 6 of 24 hours
    hass.states.async_set("sensor.amber_feed_in_forecast", "0.1", {"forecasts": []})
    calls = _calls(hass, DOMAIN, "set_tariff")
    notes = _calls(hass, "persistent_notification", "create")
    await _automation(hass, "dynamic_price_tariff.yaml", {"powerwall": "entry1", "dry_run": False})
    freezer.tick(timedelta(seconds=40))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert calls["set_tariff"] == []
    assert "without a price" in notes["create"][0].data["message"]


async def _later(hass: HomeAssistant, minutes: int) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
    await hass.async_block_till_done()


async def _set(hass: HomeAssistant, entity_id: str, state: str, attributes: dict | None = None) -> None:
    hass.states.async_set(entity_id, state, attributes)
    await hass.async_block_till_done()


@pytest.fixture
async def loads(hass: HomeAssistant, blueprint_dir) -> str:
    """Two real loads (the pump on, the heater off) and the scene snapshot
    the blueprint restores them from."""
    for domain, config in (("homeassistant", {}), ("scene", {}), ("input_boolean", {"input_boolean": {
            "pump": {"initial": True}, "heater": {"initial": False}}})):
        assert await async_setup_component(hass, domain, config)
    hass.states.async_set(GRID, "on")
    await hass.async_block_till_done()
    return "scene.powerwall_shed_outage_load_shedding_yaml"


async def test_outage_load_shedding(hass: HomeAssistant, loads) -> None:
    await _automation(hass, "outage_load_shedding.yaml", {
        "grid_connected": GRID, "battery_level": LEVEL, "loads": [PUMP, HEATER], "minutes": 2})

    await _set(hass, GRID, "off")
    assert hass.states.get(PUMP).state == "on"  # not before 2 minutes
    await _later(hass, 3)
    assert (hass.states.get(PUMP).state, hass.states.get(HEATER).state) == ("off", "off")
    assert hass.states.get(loads) is not None

    # The battery level falling further doesn't take a second snapshot.
    await _set(hass, LEVEL, "40")
    await _set(hass, GRID, "on")
    assert (hass.states.get(PUMP).state, hass.states.get(HEATER).state) == ("on", "off")
    assert hass.states.get(loads) is None

    # An off-grid run started here isn't an outage.
    await _set(hass, GRID, "off", {"off_grid_run": True})
    await _later(hass, 6)
    assert hass.states.get(PUMP).state == "on"


async def test_outage_load_shedding_waits_for_the_battery_level(hass: HomeAssistant, loads) -> None:
    await _automation(hass, "outage_load_shedding.yaml", {
        "grid_connected": GRID, "battery_level": LEVEL, "loads": [PUMP, HEATER], "below": 50,
        "restore": False})

    await _set(hass, LEVEL, "45")  # low, but the grid is up
    await _set(hass, LEVEL, "55")
    await _set(hass, GRID, "off")
    await _later(hass, 2)
    assert hass.states.get(PUMP).state == "on"  # battery still at 55 %
    await _set(hass, LEVEL, "49")
    assert hass.states.get(PUMP).state == "off"

    await _set(hass, GRID, "on")
    assert hass.states.get(PUMP).state == "off"  # not put back
    assert hass.states.get(loads) is None  # ready for the next outage


async def test_storm_backup(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, DOMAIN, "start_max_backup", "cancel_max_backup")
    hass.states.async_set(WARNING, "off")
    await _automation(hass, "storm_backup.yaml", {"powerwall": "entry1", "warning": WARNING})

    await _set(hass, WARNING, "on")
    assert [c.data for c in calls["start_max_backup"]] == [{"config_entry_id": "entry1", "duration_minutes": 720}]
    # A warning sensor that drops out doesn't start it again or end it.
    await _set(hass, WARNING, "unavailable")
    await _set(hass, WARNING, "on")
    assert len(calls["start_max_backup"]) == 1
    assert calls["cancel_max_backup"] == []

    await _set(hass, WARNING, "off")
    assert [c.data for c in calls["cancel_max_backup"]] == [{"config_entry_id": "entry1"}]


async def test_storm_backup_can_run_its_time(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, DOMAIN, "start_max_backup", "cancel_max_backup")
    hass.states.async_set(WARNING, "off")
    await _automation(hass, "storm_backup.yaml", {
        "powerwall": "entry1", "warning": WARNING, "hours": 6, "cancel_when_clear": False})
    await _set(hass, WARNING, "on")
    await _set(hass, WARNING, "off")
    assert [c.data["duration_minutes"] for c in calls["start_max_backup"]] == [360]
    assert calls["cancel_max_backup"] == []


async def test_solar_surplus_load(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, "homeassistant", "turn_on", "turn_off")
    hass.states.async_set(LEVEL, "97")
    hass.states.async_set(GRID_POWER, "150")
    await _automation(hass, "solar_surplus_load.yaml", {
        "battery_level": LEVEL, "grid_power": GRID_POWER, "load": {"entity_id": "switch.pool_pump"},
        "start_soc": 95, "export_watts": 1000, "minutes": 5, "import_watts": 200, "stop_soc": 80})

    await _set(hass, GRID_POWER, "-1500")
    assert calls["turn_on"] == []  # not before 5 minutes
    await _later(hass, 6)
    assert [c.data["entity_id"] for c in calls["turn_on"]] == [["switch.pool_pump"]]

    await _set(hass, GRID_POWER, "-300")  # the load uses the surplus: stays on
    await _later(hass, 12)
    assert calls["turn_off"] == []
    await _set(hass, GRID_POWER, "600")
    await _later(hass, 18)
    assert [c.data["entity_id"] for c in calls["turn_off"]] == [["switch.pool_pump"]]


async def test_solar_surplus_load_follows_the_battery_level(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, "homeassistant", "turn_on", "turn_off")
    hass.states.async_set(LEVEL, "90")
    hass.states.async_set(GRID_POWER, "0")
    await _automation(hass, "solar_surplus_load.yaml", {
        "battery_level": LEVEL, "grid_power": GRID_POWER, "load": {"entity_id": "switch.pool_pump"}})

    await _set(hass, GRID_POWER, "-2000")
    await _later(hass, 6)
    assert calls["turn_on"] == []  # exporting, but the battery isn't full yet
    await _set(hass, LEVEL, "96")
    assert len(calls["turn_on"]) == 1

    await _set(hass, GRID_POWER, "-100")
    await _set(hass, LEVEL, "99")
    await _set(hass, LEVEL, "94")
    await _set(hass, LEVEL, "96")
    assert len(calls["turn_on"]) == 1  # battery full again, but nothing left to export
    await _set(hass, LEVEL, "79")
    assert len(calls["turn_off"]) == 1


async def test_problem_alert(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, "notify", "my_phone")
    hass.states.async_set(PROBLEM, "off", {"problems": []})
    await _automation(hass, "problem_alert.yaml", {"problem": PROBLEM, "notify_action": "notify.my_phone"})

    await _set(hass, PROBLEM, "on", {"problems": [
        {"alert": "BatteryComms", "description": "A battery isn't answering", "severity": "warning"},
        {"alert": "PVS_a018", "description": "Solar input fault", "severity": "fault"}]})
    await _set(hass, PROBLEM, "unavailable")  # a reload doesn't repeat it
    await _set(hass, PROBLEM, "on", {"problems": []})
    await _set(hass, PROBLEM, "off", {"problems": []})
    assert [(c.data["title"], c.data["message"]) for c in calls["my_phone"]] == [
        ("Powerwall", "A battery isn't answering; Solar input fault"), ("Powerwall", "The problem has cleared.")]


async def test_problem_alert_without_the_all_clear(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, "notify", "notify")
    hass.states.async_set(PROBLEM, "off")
    await _automation(hass, "problem_alert.yaml", {"problem": PROBLEM, "notify_cleared": False})
    await _set(hass, PROBLEM, "on")
    await _set(hass, PROBLEM, "off")
    assert [c.data["message"] for c in calls["notify"]] == ["The Powerwall reports a problem."]


async def test_low_battery_alert(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, "notify", "my_phone")
    hass.states.async_set(GRID, "on")
    await _automation(hass, "low_battery_alert.yaml", {
        "battery_level": LEVEL, "grid_connected": GRID, "threshold": 20, "notify_action": "notify.my_phone"})

    await _set(hass, LEVEL, "15")
    assert calls["my_phone"] == []  # the grid is up
    await _set(hass, GRID, "off")  # an outage with the battery already low
    await _set(hass, LEVEL, "30")
    await _set(hass, LEVEL, "18.5")
    await _set(hass, GRID, "on")
    await _set(hass, LEVEL, "60")
    await _set(hass, GRID, "off")  # an outage with enough in the battery
    assert [c.data["message"] for c in calls["my_phone"]] == [
        "Battery at 15 % and the grid is down.", "Battery at 18.5 % and the grid is down."]


async def test_low_battery_alert_with_the_grid_up(hass: HomeAssistant, blueprint_dir) -> None:
    calls = _calls(hass, "notify", "notify")
    hass.states.async_set(GRID, "on")
    await _automation(hass, "low_battery_alert.yaml", {
        "battery_level": LEVEL, "grid_connected": GRID, "only_outage": False})
    await _set(hass, LEVEL, "19")
    await _set(hass, LEVEL, "unavailable")
    await _set(hass, GRID, "off")
    assert [c.data["message"] for c in calls["notify"]] == ["Battery at 19 %."]


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_backup_reserve_schedule(hass: HomeAssistant, blueprint_dir, freezer) -> None:
    calls = _calls(hass, "number", "set_value")
    monday = dt_util.start_of_local_day(dt_util.parse_datetime("2026-11-16T12:00:00+00:00"))
    freezer.move_to(monday + timedelta(hours=12))
    await _automation(hass, "backup_reserve_schedule.yaml", {
        "backup_reserve": RESERVE, "start": "22:00:00", "end": "06:00:00", "reserve_inside": 70,
        "reserve_outside": 10, "weekdays": ["mon", "wed"]})

    # Monday 22:00 and Tuesday 06:00 (Monday's window), then Tuesday 22:00
    # and Wednesday 06:00 (Tuesday has no window), then Wednesday 22:00.
    for hours in (22, 30, 46, 54, 70):
        freezer.move_to(monday + timedelta(hours=hours))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert [(c.data["entity_id"], c.data["value"]) for c in calls["set_value"]] == [
        ([RESERVE], 70), ([RESERVE], 10), ([RESERVE], 70)]


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_backup_reserve_schedule_within_a_day(hass: HomeAssistant, blueprint_dir, freezer) -> None:
    calls = _calls(hass, "number", "set_value")
    saturday = dt_util.start_of_local_day(dt_util.parse_datetime("2026-11-14T12:00:00+00:00"))
    freezer.move_to(saturday + timedelta(hours=12))
    await _automation(hass, "backup_reserve_schedule.yaml", {
        "backup_reserve": RESERVE, "weekdays": ["mon", "tue", "wed", "thu", "fri"]})

    for hours in (14, 17, 24 + 14, 24 + 17, 48 + 14, 48 + 17):  # Saturday, Sunday, Monday
        freezer.move_to(saturday + timedelta(hours=hours))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert [c.data["value"] for c in calls["set_value"]] == [80, 20]
