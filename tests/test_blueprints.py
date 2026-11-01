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
