"""2.9.0: MQTT publishing (optional, off by default)."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi.const import DOMAIN, EVENT_GRID_OUTAGE

from .conftest import DIN

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
ON = {"mqtt_publish": True, "mqtt_base_topic": "tesla_tedapi/powerwall"}
BASE = "tesla_tedapi/powerwall"
BRIDGE = "custom_components.tesla_tedapi.mqtt_bridge"
INTEGRATION = Path(__file__).parents[1] / "custom_components" / DOMAIN


async def _setup(hass: HomeAssistant, options: dict | None = None, data: dict | None = None,
                 unique_id: str = DIN) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=unique_id, data=data or V1R, options=options or {},
                            minor_version=2, title=f"Powerwall {unique_id}")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _messages(mqtt_mock) -> list[tuple[str, str, bool]]:
    """(topic, payload, retain) of every publish, in order."""
    return [(c.args[0], c.args[1], c.args[3]) for c in mqtt_mock.async_publish.call_args_list]


def _last(mqtt_mock, name: str) -> dict[str, Any] | str:
    payload = next(p for t, p, _ in reversed(_messages(mqtt_mock)) if t == f"{BASE}/{name}")
    try:
        return json.loads(payload)
    except ValueError:
        return payload


def _count(mqtt_mock, name: str) -> int:
    return sum(1 for t, _, _ in _messages(mqtt_mock) if t == f"{BASE}/{name}")


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    coordinator = entry.runtime_data.coordinator
    coordinator._full_at = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Optional by design
# ---------------------------------------------------------------------------


async def test_off_by_default_and_not_imported(hass: HomeAssistant, fake_powerwall) -> None:
    sys.modules.pop(BRIDGE, None)
    entry = await _setup(hass)
    assert entry.runtime_data.mqtt is None
    assert BRIDGE not in sys.modules


def test_only_the_bridge_imports_mqtt() -> None:
    """MQTT is never a requirement: only mqtt_bridge.py (imported while the
    option is on) imports Home Assistant's MQTT integration."""
    users = sorted(path.name for path in INTEGRATION.glob("*.py")
                   if "components import mqtt" in path.read_text(encoding="utf-8")
                   or "components.mqtt" in path.read_text(encoding="utf-8"))
    assert users == ["mqtt_bridge.py"]
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    assert "mqtt" in manifest["after_dependencies"] and "mqtt" not in manifest["dependencies"]
    assert not any("mqtt" in requirement for requirement in manifest["requirements"])


async def test_without_the_mqtt_integration_a_repair(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, ON)
    await hass.async_block_till_done()
    issue_id = f"mqtt_unavailable_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.translation_key == "mqtt_unavailable"
    assert DIN not in str(issue.translation_placeholders)
    # Everything else works.
    assert hass.states.get(er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DIN}_battery_level"))
    hass.config_entries.async_update_entry(entry, options={**entry.options, "mqtt_publish": False})
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


async def test_publishes_the_readings(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    entry = await _setup(hass, ON)
    assert _last(mqtt_mock, "availability") == "online"
    retained = {t for t, _, retain in _messages(mqtt_mock) if retain}
    assert {f"{BASE}/{name}" for name in ("availability", "power", "battery", "energy", "settings",
                                           "status")} <= retained
    power = _last(mqtt_mock, "power")
    assert power["schema"] == 1 and power["source"] == "signed" and power["ts"].endswith("+00:00")
    registry = er.async_get(hass)

    def state(key: str) -> float:
        return float(hass.states.get(registry.async_get_entity_id("sensor", DOMAIN, f"{DIN}_{key}")).state)

    assert power["battery_level"] == state("battery_level")
    assert power["solar_w"] == state("solar_power") and power["grid_w"] == state("grid_power")
    battery = _last(mqtt_mock, "battery")
    assert battery["packs"] and battery["packs"][0]["index"] == 1 and "name" not in battery["packs"][0]
    settings = _last(mqtt_mock, "settings")
    assert settings["operation_mode"] == "self_consumption" and settings["settings_source"] == "local"
    assert _last(mqtt_mock, "status")["health"] in ("ok", "degraded")
    assert entry.runtime_data.mqtt.started


async def test_unchanged_messages_arent_sent_again(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    entry = await _setup(hass, ON)
    before = {name: _count(mqtt_mock, name) for name in ("power", "battery", "settings", "status")}
    await _refresh(hass, entry)
    assert {name: _count(mqtt_mock, name) for name in before} == before
    fake_powerwall.soe = 80.0
    await _refresh(hass, entry)
    assert _count(mqtt_mock, "power") == before["power"] + 1
    assert _count(mqtt_mock, "battery") == before["battery"] + 1


async def test_offline_while_the_gateway_is_down_and_on_unload(hass: HomeAssistant, fake_powerwall,
                                                               mqtt_mock) -> None:
    entry = await _setup(hass, ON)
    fake_powerwall.fail_local = True
    entry.runtime_data.coordinator._pw = None
    await _refresh(hass, entry)
    assert _last(mqtt_mock, "availability") == "offline"
    fake_powerwall.fail_local = False
    await _refresh(hass, entry)
    assert _last(mqtt_mock, "availability") == "online"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _messages(mqtt_mock)[-1] == (f"{BASE}/availability", "offline", True)


async def test_events_and_alerts(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    entry = await _setup(hass, ON)
    hass.bus.async_fire(EVENT_GRID_OUTAGE, {"config_entry_id": entry.entry_id, "phase": "started",
                                            "start": "2026-11-26T09:00:00+00:00", "duration_seconds": None})
    hass.bus.async_fire(EVENT_GRID_OUTAGE, {"config_entry_id": "another", "phase": "started"})
    await hass.async_block_till_done()
    events = [(p, retain) for t, p, retain in _messages(mqtt_mock) if t == f"{BASE}/event/grid_outage"]
    assert len(events) == 1 and events[0][1] is False
    payload = json.loads(events[0][0])
    assert payload["phase"] == "started" and "config_entry_id" not in payload and payload["schema"] == 1

    fake_powerwall.alert_list = ["SystemConnectedToGrid", "PodCommissioningTime"]
    await _refresh(hass, entry)
    alerts = [json.loads(p) for t, p, _ in _messages(mqtt_mock) if t == f"{BASE}/event/alert"]
    assert alerts and alerts[-1]["kind"] == "raised" and alerts[-1]["alert"] == "PodCommissioningTime"
    assert "sources" not in alerts[-1]


async def test_nothing_identifies_the_powerwall(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    entry = await _setup(hass, ON)
    hass.bus.async_fire(EVENT_GRID_OUTAGE, {"config_entry_id": entry.entry_id, "phase": "started"})
    fake_powerwall.alert_list = ["SystemConnectedToGrid", "PodCommissioningTime"]
    await _refresh(hass, entry)
    secrets = {DIN, DIN.split("--")[-1], "192.168.1.50", entry.entry_id, entry.title}
    secrets |= set((entry.runtime_data.coordinator.data or {}).get("packs") or {})  # pack serials
    for topic, payload, _ in _messages(mqtt_mock):
        for secret in secrets:
            assert secret not in topic and secret not in payload, (topic, secret)


# ---------------------------------------------------------------------------
# Options -> MQTT
# ---------------------------------------------------------------------------


async def _mqtt_step(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "mqtt" in result["menu_options"]
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "mqtt"})


def _default(result: dict, key: str) -> Any:
    return next(k for k in result["data_schema"].schema if k == key).default()


async def test_options_turn_it_on(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    entry = await _setup(hass)
    result = await _mqtt_step(hass, entry)
    assert _default(result, "mqtt_publish") is False and _default(result, "mqtt_base_topic") == BASE
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"mqtt_publish": True, "mqtt_base_topic": "/bad/+"})
    assert result["errors"] == {"mqtt_base_topic": "invalid_base_topic"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"mqtt_publish": True, "mqtt_base_topic": " home/pw "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["mqtt_base_topic"] == "home/pw"
    assert ("home/pw/availability", "online", True) in _messages(mqtt_mock)


async def test_options_turn_it_off_clears_the_retained_messages(hass: HomeAssistant, fake_powerwall,
                                                                mqtt_mock) -> None:
    entry = await _setup(hass, ON)
    result = await _mqtt_step(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"mqtt_publish": False, "mqtt_base_topic": BASE})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    cleared = {t for t, p, retain in _messages(mqtt_mock) if p == "" and retain}
    assert cleared == {f"{BASE}/{name}" for name in ("availability", "power", "battery", "energy", "settings",
                                                      "status")}
    assert entry.runtime_data.mqtt is None
    # Nothing retained is left: the bridge stopping after the clear doesn't
    # leave "offline" behind (found in the live test).
    last = {}
    for topic, payload, retain in _messages(mqtt_mock):
        if retain:
            last[topic] = payload
    assert all(payload == "" for payload in last.values()), last


async def test_moving_the_base_topic_leaves_nothing_under_the_old_one(hass: HomeAssistant, fake_powerwall,
                                                                       mqtt_mock) -> None:
    entry = await _setup(hass, ON)
    result = await _mqtt_step(hass, entry)
    await hass.config_entries.options.async_configure(result["flow_id"], {"mqtt_publish": True,
                                                                          "mqtt_base_topic": "home/pw"})
    await hass.async_block_till_done()
    last = {}
    for topic, payload, retain in _messages(mqtt_mock):
        if retain:
            last[topic] = payload
    assert all(payload == "" for topic, payload in last.items() if topic.startswith(f"{BASE}/")), last
    assert last["home/pw/availability"] == "online"


async def test_options_a_second_powerwall_gets_its_own_topic(hass: HomeAssistant, fake_powerwall,
                                                             mqtt_mock) -> None:
    await _setup(hass, ON)
    # Not loaded (it's another gateway): its options still open.
    other = MockConfigEntry(domain=DOMAIN, unique_id="1707000-11-J--TG0000000000OTHER", data=V1R, minor_version=2)
    other.add_to_hass(hass)
    result = await _mqtt_step(hass, other)
    assert _default(result, "mqtt_base_topic") == f"{BASE}_2"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"mqtt_publish": True, "mqtt_base_topic": BASE})
    assert result["errors"] == {"mqtt_base_topic": "base_topic_in_use"}


async def test_removing_the_entry_clears_the_retained_messages(hass: HomeAssistant, fake_powerwall,
                                                               mqtt_mock) -> None:
    entry = await _setup(hass, ON)
    mqtt_mock.async_publish.reset_mock()
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    cleared = {t for t, p, retain in _messages(mqtt_mock) if p == "" and retain}
    assert f"{BASE}/power" in cleared and f"{BASE}/availability" in cleared


async def test_a_failed_publish_is_sent_again_next_time(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    from custom_components.tesla_tedapi import mqtt_bridge  # noqa: PLC0415

    entry = await _setup(hass, ON)
    fake_powerwall.soe = 80.0
    with patch.object(mqtt_bridge.mqtt, "async_publish", side_effect=HomeAssistantError("broker away")):
        await _refresh(hass, entry)
        # Clearing gives up quietly too.
        await mqtt_bridge.async_clear(hass, BASE)
    before = _count(mqtt_mock, "power")
    await _refresh(hass, entry)
    assert _count(mqtt_mock, "power") == before + 1
    assert _last(mqtt_mock, "battery")["soe"] == 80.0


async def test_clearing_without_mqtt_does_nothing(hass: HomeAssistant) -> None:
    from custom_components.tesla_tedapi import mqtt_bridge  # noqa: PLC0415

    await mqtt_bridge.async_clear(hass, BASE)  # no MQTT integration: returns at once


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

COMMANDS = {**ON, "mqtt_commands": True}
BATTERY = {**COMMANDS, "mqtt_battery_commands": True}


async def _command(hass: HomeAssistant, mqtt_mock, action: str, payload: Any, retain: bool = False
                   ) -> dict[str, Any] | None:
    """Send a command; its answer on cmd/result (None without one)."""
    from pytest_homeassistant_custom_component.common import async_fire_mqtt_message  # noqa: PLC0415

    before = _count(mqtt_mock, "cmd/result")
    body = payload if isinstance(payload, str) else json.dumps(payload)
    async_fire_mqtt_message(hass, f"{BASE}/cmd/{action}", body, retain=retain)
    await hass.async_block_till_done()
    if _count(mqtt_mock, "cmd/result") == before:
        return None
    return _last(mqtt_mock, "cmd/result")


def _local(fake_powerwall):
    return [p for p in fake_powerwall.instances if p.mode == "local"][-1]


async def test_commands_are_off_unless_turned_on(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    await _setup(hass, ON)
    assert await _command(hass, mqtt_mock, "set_mode", {"mode": "backup"}) is None
    assert ("mode", "backup") not in _local(fake_powerwall).writes


async def test_setting_commands(hass: HomeAssistant, fake_powerwall, mqtt_mock, caplog) -> None:
    await _setup(hass, COMMANDS)
    result = await _command(hass, mqtt_mock, "set_mode", {"id": "a1", "mode": "backup"})
    assert result["ok"] is True and result["id"] == "a1" and result["action"] == "set_mode"
    assert ("mode", "backup") in _local(fake_powerwall).writes
    assert (await _command(hass, mqtt_mock, "set_reserve", {"percent": 30}))["ok"] is True
    assert (await _command(hass, mqtt_mock, "set_grid_charging", {"enabled": True}))["ok"] is True
    assert (await _command(hass, mqtt_mock, "set_grid_export", {"rule": "battery_ok"}))["ok"] is True
    result = await _command(hass, mqtt_mock, "set_reserve", {"percent": 150})
    assert result["ok"] is False and result["error"] == "invalid_arguments"
    result = await _command(hass, mqtt_mock, "set_mode", {"mode": "party"})
    assert result["error"] == "invalid_arguments"
    assert "MQTT command set_mode: done" in caplog.text


async def test_battery_commands_need_their_own_switch(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    await _setup(hass, COMMANDS)
    result = await _command(hass, mqtt_mock, "dispatch", {"power_watts": 1000, "duration_seconds": 60})
    assert result["error"] == "not_enabled"
    assert _local(fake_powerwall).dispatches == []


async def test_battery_commands(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    await _setup(hass, BATTERY)
    result = await _command(hass, mqtt_mock, "dispatch", {"power_watts": 1000, "duration_seconds": 60})
    assert result["ok"] is True
    assert _local(fake_powerwall).dispatches
    assert (await _command(hass, mqtt_mock, "resume", {}))["ok"] is True
    result = await _command(hass, mqtt_mock, "freeze", {"direction": "charge_only", "duration_seconds": 60})
    assert result["ok"] is True
    assert (await _command(hass, mqtt_mock, "resume", {}))["ok"] is True
    # The action's own limits apply.
    result = await _command(hass, mqtt_mock, "dispatch", {"power_watts": 99999, "duration_seconds": 60})
    assert result["error"] == "invalid_arguments"
    result = await _command(hass, mqtt_mock, "start_max_backup", {"duration_minutes": 60})
    assert result["ok"] is True
    assert (await _command(hass, mqtt_mock, "cancel_max_backup", ""))["ok"] is True
    # Going off-grid isn't offered over MQTT.
    assert (await _command(hass, mqtt_mock, "go_off_grid", {"confirm": True}))["error"] == "unknown_action"


async def test_a_refused_command_says_why(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    """Wi-Fi password connection without a cloud backup: settings can't be
    changed, and the answer says so."""
    await _setup(hass, COMMANDS, data={"connection_mode": "wifi", "host": "192.168.91.1",
                                       "gw_pwd": "ABCDE12345", "scan_interval": 30})
    result = await _command(hass, mqtt_mock, "set_mode", {"mode": "backup"})
    assert result["ok"] is False and result["error"] == "settings_need_control"


async def test_command_secret(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    await _setup(hass, {**COMMANDS, "mqtt_secret": "s3cret"})
    result = await _command(hass, mqtt_mock, "set_mode", {"mode": "backup", "token": "wrong"})
    assert result["error"] == "unauthorized" and result["message"] is None
    assert ("mode", "backup") not in _local(fake_powerwall).writes
    assert (await _command(hass, mqtt_mock, "set_mode", {"mode": "backup"}))["error"] == "unauthorized"
    result = await _command(hass, mqtt_mock, "set_mode", {"mode": "backup", "token": "s3cret"})
    assert result["ok"] is True
    assert all("s3cret" not in payload for _, payload, _ in _messages(mqtt_mock))


async def test_bad_commands(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    await _setup(hass, BATTERY)
    # Retained: never run (it would run again at every restart).
    result = await _command(hass, mqtt_mock, "set_mode", {"mode": "backup"}, retain=True)
    assert result["error"] == "retained"
    assert ("mode", "backup") not in _local(fake_powerwall).writes
    assert (await _command(hass, mqtt_mock, "set_mode", "{not json"))["error"] == "invalid_json"
    assert (await _command(hass, mqtt_mock, "set_mode", "[1, 2]"))["error"] == "invalid_json"
    assert (await _command(hass, mqtt_mock, "set_mode", json.dumps({"mode": "x" * 3000})))["error"] == "invalid_json"
    result = await _command(hass, mqtt_mock, "reboot", {"id": True})
    assert result["error"] == "unknown_action" and result["id"] is None
    # Our own answers aren't commands.
    assert await _command(hass, mqtt_mock, "result", {"ok": True}) is None


async def test_unexpected_failure_is_answered(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    entry = await _setup(hass, COMMANDS)
    with patch.object(entry.runtime_data.coordinator, "async_set_mode", side_effect=RuntimeError("boom")):
        result = await _command(hass, mqtt_mock, "set_mode", {"mode": "backup"})
    assert result["ok"] is False and result["error"] == "failed"


async def test_options_commands_and_secret(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    from custom_components.tesla_tedapi.diagnostics import async_get_config_entry_diagnostics  # noqa: PLC0415

    entry = await _setup(hass)
    result = await _mqtt_step(hass, entry)
    assert _default(result, "mqtt_commands") is False and _default(result, "mqtt_battery_commands") is False
    # Battery control needs setting commands on.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "mqtt_publish": True, "mqtt_base_topic": BASE, "mqtt_commands": False, "mqtt_battery_commands": True,
        "mqtt_secret": " s3cret "})
    await hass.async_block_till_done()
    assert entry.options["mqtt_battery_commands"] is False and entry.options["mqtt_secret"] == "s3cret"
    assert "s3cret" not in json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)
    result = await _mqtt_step(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "mqtt_publish": True, "mqtt_base_topic": BASE, "mqtt_commands": True, "mqtt_battery_commands": True,
        "mqtt_secret": ""})
    await hass.async_block_till_done()
    assert entry.options["mqtt_commands"] and entry.options["mqtt_battery_commands"]
    assert "mqtt_secret" not in entry.options


async def test_a_command_as_bytes(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    entry = await _setup(hass, COMMANDS)
    await entry.runtime_data.mqtt._async_command("set_mode", b'{"mode": "backup"}', False)
    await hass.async_block_till_done()
    assert _last(mqtt_mock, "cmd/result")["ok"] is True


async def test_nothing_after_offline(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    """A reading still being published when the entry unloads sends nothing
    after "offline"."""
    entry = await _setup(hass, BATTERY)
    bridge = entry.runtime_data.mqtt
    fake_powerwall.soe = 90.0
    fake_powerwall.alert_list = ["SystemConnectedToGrid", "PodCommissioningTime"]
    await bridge.async_stop()
    before = len(_messages(mqtt_mock))
    await _refresh(hass, entry)
    await bridge._async_publish_all()
    assert len(_messages(mqtt_mock)) == before
    assert _messages(mqtt_mock)[-1] == (f"{BASE}/availability", "offline", True)


async def test_starts_once_mqtt_is_set_up_later(hass: HomeAssistant, fake_powerwall, mqtt_mock) -> None:
    """MQTT wasn't there at first: tried again every 5 minutes, and the repair
    goes once publishing starts."""
    from datetime import timedelta  # noqa: PLC0415

    from homeassistant.util import dt as dt_util  # noqa: PLC0415
    from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: PLC0415

    from custom_components.tesla_tedapi import mqtt_bridge  # noqa: PLC0415

    with patch.object(mqtt_bridge.mqtt, "async_wait_for_mqtt_client", return_value=False):
        entry = await _setup(hass, ON)
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=mqtt_bridge.RETRY_SECONDS + 1))
        await hass.async_block_till_done()
    issue_id = f"mqtt_unavailable_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert not entry.runtime_data.mqtt.started
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2 * mqtt_bridge.RETRY_SECONDS + 2))
    await hass.async_block_till_done()
    assert entry.runtime_data.mqtt.started
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert _last(mqtt_mock, "availability") == "online"


async def test_unload_while_waiting_for_mqtt(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, ON)
    bridge = entry.runtime_data.mqtt
    assert bridge._retry is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert bridge._retry is None
