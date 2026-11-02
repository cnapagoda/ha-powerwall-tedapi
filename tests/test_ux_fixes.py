"""2.5.1: device page kept current, texts in Home Assistant's language, Grid
status states, options applied without a reload, counter resets."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed

from custom_components.tesla_tedapi import logbook
from custom_components.tesla_tedapi.alerts import CATALOGUE, SHORT, describe, summary
from custom_components.tesla_tedapi.const import (
    DOMAIN,
    ENERGY_RESET_POLLS,
    EVENT_DISPATCH_ENDED,
    EVENT_GRID_OUTAGE,
)
from custom_components.tesla_tedapi.coordinator import detect_model, model_id
from custom_components.tesla_tedapi.texts import DATA_TEXTS, ENGLISH, Texts, texts

from .conftest import DIN, local
from .test_storm import ACCOUNT, V1R, _eid, _refresh, _setup


def _device(hass: HomeAssistant):
    return dr.async_get(hass).async_get_device(identifiers={(DOMAIN, DIN)})


# ---- #1 device page: firmware and model ----

async def test_device_shows_model_and_firmware(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    device = _device(hass)
    assert device.model == "Powerwall 3"
    assert device.model_id == "1707000-11-J"
    assert device.sw_version == "26.34.1"  # without the build hash


async def test_firmware_update_updates_the_device(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    local(fake_powerwall).version = lambda: "26.38.0 0a1b2c3d"
    await _refresh(hass, entry)
    assert _device(hass).sw_version == "26.38.0"
    # Nothing to change: no registry write.
    with patch.object(dr.DeviceRegistry, "async_update_device") as update:
        await _refresh(hass, entry)
    update.assert_not_called()


async def test_device_name_set_by_user_is_kept(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    registry = dr.async_get(hass)
    registry.async_update_device(_device(hass).id, name_by_user="Garage battery")
    local(fake_powerwall).version = lambda: "26.38.0 0a1b2c3d"
    await _refresh(hass, entry)
    assert _device(hass).name_by_user == "Garage battery"
    assert _device(hass).sw_version == "26.38.0"


def test_detect_model() -> None:
    assert detect_model(DIN, None) == "Powerwall 3"
    # A gateway that isn't a Powerwall 3, with a Powerwall 3 pack behind it.
    pack = {"x": {"part_number": "1707000-21-K"}}
    assert detect_model("1232100-00-E--TG1", pack) == "Powerwall 3"
    assert detect_model("1232100-00-E--TG1", {"x": {"part_number": None}}) == "Powerwall"
    assert detect_model(None, None) == "Powerwall"
    assert model_id(DIN) == "1707000-11-J"
    assert model_id("TG0123") is None
    assert model_id(None) is None


async def test_model_follows_the_packs(hass: HomeAssistant, fake_powerwall) -> None:
    """The model comes from the gateway's readings, so it's right also when
    the DIN alone doesn't tell."""
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    coordinator.model = "Powerwall"
    registry = dr.async_get(hass)
    registry.async_update_device(_device(hass).id, model="Powerwall")
    await _refresh(hass, entry)
    assert _device(hass).model == "Powerwall 3"


async def test_no_device_yet_is_fine(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    registry = dr.async_get(hass)
    registry.async_remove_device(_device(hass).id)
    coordinator._sync_device()  # nothing to update, no error
    coordinator.din = None
    coordinator._sync_device()
    assert _device(hass) is None


# ---- #3 texts in Home Assistant's language ----

def test_every_alert_code_has_texts_in_every_language() -> None:
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    folder = Path(__file__).parents[1] / "custom_components" / DOMAIN
    files = [folder / "strings.json", *sorted((folder / "translations").glob("*.json"))]
    assert len(files) == 9
    for path in files:
        selector = json.loads(path.read_text(encoding="utf-8"))["selector"]
        descriptions = selector["alert_description"]["options"]
        shorts = selector["alert_short"]["options"]
        assert set(descriptions) == {code.lower() for code in CATALOGUE}, path.name
        assert set(shorts) == {code.lower() for code in SHORT} | {"no_alerts"}, path.name
        assert set(selector["logbook"]["options"]) == {
            "firmware_updated", "outage_started", "outage_ended", "outage_ended_unknown",
            "dispatch_ended", "off_grid_ended"}, path.name
        assert set(selector["dispatch_end_reason"]["options"]) == set(logbook.DISPATCH_REASONS), path.name
        assert set(selector["off_grid_end_reason"]["options"]) == set(logbook.OFF_GRID_REASONS), path.name


def test_english_texts_match_the_built_in_ones() -> None:
    """strings.json and the English fallbacks in the code say the same."""
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    strings = json.loads((Path(__file__).parents[1] / "custom_components" / DOMAIN / "strings.json")
                         .read_text(encoding="utf-8"))["selector"]
    for code, (_, description) in CATALOGUE.items():
        assert strings["alert_description"]["options"][code.lower()] == description
    for code, label in SHORT.items():
        assert strings["alert_short"]["options"][code.lower()] == label
    assert strings["alert_short"]["options"]["no_alerts"] == "None"
    for reason, text in logbook.DISPATCH_REASONS.items():
        assert strings["dispatch_end_reason"]["options"][reason] == text
    for reason, text in logbook.OFF_GRID_REASONS.items():
        assert strings["off_grid_end_reason"]["options"][reason] == text
    messages = strings["logbook"]["options"]
    assert messages["firmware_updated"] == logbook.FIRMWARE_UPDATED
    assert messages["outage_ended"] == logbook.OUTAGE_ENDED
    assert messages["dispatch_ended"] == logbook.DISPATCH_ENDED
    assert messages["off_grid_ended"] == logbook.OFF_GRID_ENDED


def test_texts_fall_back_to_english() -> None:
    german = Texts({f"component.{DOMAIN}.selector.alert_short.options.backfeedlimited": "Einspeisung begrenzt",
                    f"component.{DOMAIN}.selector.logbook.options.dispatch_ended": "kaputt ({nope})"}, "de")
    assert summary(["BackfeedLimited", "SystemConnectedToGrid"], german) == \
        "Connected to the grid · Einspeisung begrenzt"
    # A translation with a wrong placeholder uses the English text.
    assert german.get("logbook", "dispatch_ended", "ended ({reason})", reason="x") == "ended (x)"
    assert describe("SomethingNew", german) == ("notice", "SomethingNew")
    assert ENGLISH.get("logbook", "anything", "fallback") == "fallback"


async def _german(hass: HomeAssistant) -> None:
    await hass.config.async_update(language="de")
    await hass.async_block_till_done()


async def test_alerts_in_german(hass: HomeAssistant, fake_powerwall) -> None:
    await _german(hass)
    fake_powerwall.alert_list = ["SystemConnectedToGrid", "BatteryFault"]
    entry = await _setup(hass, V1R)
    assert hass.states.get(_eid(hass, "sensor", "active_alerts")).state == \
        "Batteriestörung · Mit dem Netz verbunden"
    details = hass.states.get(_eid(hass, "sensor", "alerts")).attributes["details"]
    assert details[0]["description"] == "Batteriestörung"
    # Lasting fault: Problem sensor and repair in German.
    coordinator = entry.runtime_data.coordinator
    coordinator.alerts.since["BatteryFault"] -= timedelta(minutes=30)
    await _refresh(hass, entry)
    problems = hass.states.get(_eid(hass, "binary_sensor", "alert_problem")).attributes["problems"]
    assert problems[0]["description"] == "Batteriestörung"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"alert_{DIN}_BatteryFault")
    assert issue.translation_placeholders["description"] == "Batteriestörung"


async def test_alert_event_in_german(hass: HomeAssistant, fake_powerwall) -> None:
    await _german(hass)
    entry = await _setup(hass, V1R)
    fake_powerwall.alert_list = ["SystemConnectedToGrid", "PVInverterComms"]
    await _refresh(hass, entry)
    event = hass.states.get(_eid(hass, "event", "alert_event"))
    assert event.attributes["description"] == "Verbindung zum Solar-Wechselrichter verloren"


async def test_no_alerts_in_german(hass: HomeAssistant, fake_powerwall) -> None:
    await _german(hass)
    fake_powerwall.alert_list = []
    await _setup(hass, V1R)
    assert hass.states.get(_eid(hass, "sensor", "active_alerts")).state == "Keine"


async def test_logbook_in_german(hass: HomeAssistant, fake_powerwall) -> None:
    await _german(hass)
    entry = await _setup(hass, V1R)
    described = {}
    logbook.async_describe_events(hass, lambda domain, kind, fn: described.__setitem__(kind, fn))

    def message(kind: str, **data) -> str:
        event = SimpleNamespace(data={"config_entry_id": entry.entry_id, **data})
        return described[kind](event)[LOGBOOK_ENTRY_MESSAGE]

    assert message(EVENT_GRID_OUTAGE, phase="ended", duration_seconds=600) == "Netz nach 10 Min. Ausfall zurück"
    assert message(EVENT_DISPATCH_ENDED, reason="target_reached") == "Batteriesteuerung beendet (Ziel erreicht)"


async def test_language_change_reloads_texts(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    assert texts(hass).language == "en"
    await _german(hass)
    await hass.async_block_till_done()
    assert texts(hass).language == "de"
    # The coordinator uses the same, updated texts on its next reading.
    fake_powerwall.alert_list = []
    await _refresh(hass, entry)
    assert hass.states.get(_eid(hass, "sensor", "active_alerts")).state == "Keine"
    # Another core config change (not the language) doesn't reload them.
    with patch("custom_components.tesla_tedapi.texts.async_get_translations") as load:
        hass.bus.async_fire(EVENT_CORE_CONFIG_UPDATE, {})
        await hass.async_block_till_done()
    load.assert_not_called()


async def test_texts_that_cannot_load_are_english(hass: HomeAssistant, fake_powerwall) -> None:
    with patch("custom_components.tesla_tedapi.texts.async_get_translations", side_effect=OSError("disk")):
        await _german(hass)
        fake_powerwall.alert_list = []
        await _setup(hass, V1R)
    assert hass.states.get(_eid(hass, "sensor", "active_alerts")).state == "None"
    assert hass.data[DATA_TEXTS].language == "de"


@pytest.mark.parametrize(("status", "state"), [("UP", "up"), ("DOWN", "down"), ("SYNCING", "syncing"),
                                               ("SOMETHING", "unknown"), (None, "unknown")])
async def test_grid_status_states(hass: HomeAssistant, fake_powerwall, status, state) -> None:
    fake_powerwall.grid = status
    await _setup(hass, V1R)
    sensor = hass.states.get(_eid(hass, "sensor", "grid_status"))
    assert sensor.state == state
    assert sensor.attributes["options"] == ["up", "down", "syncing"]


# ---- #4 options applied without a reload ----

async def _set_options(hass: HomeAssistant, entry, **options) -> None:
    hass.config_entries.async_update_entry(entry, options={**entry.options, **options})
    await hass.async_block_till_done()


async def test_live_options_keep_a_dispatch_running(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    ended = async_capture_events(hass, f"{DOMAIN}_dispatch_ended")
    await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                   {"power_watts": 1000, "duration_seconds": 1800}, blocking=True)
    coordinator = entry.runtime_data.coordinator
    session = entry.runtime_data.dispatch
    with patch.object(hass.config_entries, "async_reload") as reload:
        await _set_options(hass, entry, problem_minutes=25, scan_interval=45, restore_export=False,
                           status_alert_events=True, dispatch_max_watts=4000, dispatch_floor_reserve=False,
                           diagnostics_gateway_config=True)
    reload.assert_not_called()
    assert session.active and not ended
    assert entry.runtime_data.coordinator is coordinator
    assert coordinator.problem_after == timedelta(minutes=25)
    assert coordinator.update_interval == timedelta(seconds=45)
    assert coordinator.restore_settings is False
    problem = hass.states.get(_eid(hass, "binary_sensor", "alert_problem"))
    assert problem.attributes["minutes_before_problem"] == 25
    # The new dispatch limit applies straight away.
    from homeassistant.exceptions import ServiceValidationError  # noqa: PLC0415
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                       {"power_watts": 5000, "duration_seconds": 60}, blocking=True)


async def test_status_event_option_applies_live(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    event = _eid(hass, "event", "alert_event")
    fake_powerwall.alert_list = ["SystemConnectedToGrid", "BackfeedLimited"]
    await _refresh(hass, entry)
    assert hass.states.get(event).attributes.get("alert") is None  # status alert: no event
    await _set_options(hass, entry, status_alert_events=True)
    fake_powerwall.alert_list = ["SystemConnectedToGrid"]
    await _refresh(hass, entry)
    assert hass.states.get(event).attributes["alert"] == "BackfeedLimited"
    assert hass.states.get(event).attributes["event_type"] == "cleared"


async def test_saving_the_same_options_does_nothing(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, {"problem_minutes": 10})
    coordinator = entry.runtime_data.coordinator
    with patch.object(coordinator, "async_apply_options") as apply, \
            patch.object(hass.config_entries, "async_reload") as reload:
        # A data-only no-op update triggers the listener with nothing changed.
        hass.config_entries.async_update_entry(entry, title="Renamed")
        await hass.async_block_till_done()
    apply.assert_not_called()
    reload.assert_not_called()


@pytest.mark.parametrize("change", [{"options": {"tesla_tariff": True}},
                                    {"options": {"cloud_backends": "none"}},
                                    {"data": {"host": "192.168.1.51"}}])
async def test_other_changes_reload(hass: HomeAssistant, fake_powerwall, change) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    ended = async_capture_events(hass, f"{DOMAIN}_dispatch_ended")
    await hass.services.async_call(DOMAIN, "dispatch_battery_power",
                                   {"power_watts": 1000, "duration_seconds": 1800}, blocking=True)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, **change.get("options", {})}, data={**entry.data, **change.get("data", {})})
    await hass.async_block_till_done()
    # Reloaded: the dispatch ended, as documented.
    assert ended[-1].data["reason"] == "unloaded"
    assert entry.runtime_data.dispatch.active is False


# ---- #7 lifetime counters that really reset ----

async def test_counter_glitch_is_ignored(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    sensor = _eid(hass, "sensor", "grid_energy_imported")
    assert hass.states.get(sensor).state == "1000.0"
    coordinator._take_energy("grid_energy_imported", 900_000)  # one low reading
    coordinator._take_energy("grid_energy_imported", 1_001_000)  # back to normal
    assert coordinator._energy_last["grid_energy_imported"] == 1001.0
    # A few low readings, then normal again: still a glitch.
    for _ in range(ENERGY_RESET_POLLS - 1):
        coordinator._take_energy("grid_energy_imported", 5_000)
    coordinator._take_energy("grid_energy_imported", 1_002_000)
    assert coordinator._energy_last["grid_energy_imported"] == 1002.0
    for value in (0, -5, None, True, "x"):
        coordinator._take_energy("grid_energy_imported", value)
    assert coordinator._energy_last["grid_energy_imported"] == 1002.0


async def test_counter_reset_is_taken(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    entry = await _setup(hass, V1R)
    sensor = _eid(hass, "sensor", "grid_energy_imported")
    reset = {"site": {"energy_imported": 2_000, "energy_exported": 2_000_000},
             "battery": {"energy_imported": 500_000, "energy_exported": 450_000},
             "solar": {"energy_exported": 3_000_000}, "load": {"energy_imported": 1_500_000}}
    pw = local(fake_powerwall)
    pw.poll = lambda path: reset
    for _ in range(ENERGY_RESET_POLLS - 1):
        await _refresh(hass, entry)
    assert hass.states.get(sensor).state == "1000.0"
    await _refresh(hass, entry)
    assert hass.states.get(sensor).state == "2.0"
    assert "taking it as a counter reset" in caplog.text
    # The other counters didn't move.
    assert hass.states.get(_eid(hass, "sensor", "grid_energy_exported")).state == "2000.0"


async def test_scan_interval_change_reschedules(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    pw = local(fake_powerwall)
    calls = []
    original = pw.power
    pw.power = lambda: calls.append(1) or original()
    await _set_options(hass, entry, scan_interval=120)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    first = len(calls)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=62))
    await hass.async_block_till_done()
    assert len(calls) == first  # not every 30 s any more
