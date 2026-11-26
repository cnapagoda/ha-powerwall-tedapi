"""MQTT publishing (optional, off by default; 2.9.0).

The Powerwall's readings, settings, status and events go to Home Assistant's
MQTT broker, through Home Assistant's own MQTT integration (no broker settings
here), for tools outside Home Assistant. Imported only while the option is on;
the integration loads and works without MQTT.

Topics under the base topic (default tesla_tedapi/powerwall): availability,
power, battery, energy, settings and status (retained), and event/<name>
(not retained). With commands on, <base>/cmd/<action> runs the same code as
the entities and actions (every limit applies) and answers on
<base>/cmd/result. JSON payloads carry "schema", "ts" (UTC) and "source" (the
connection path). Units and signs are the entities' own: W, kWh, % on the
Tesla app's scale; grid + is importing, battery + is discharging. Nothing
identifies the Powerwall: no DIN, serials, pack names or addresses.
"""
from __future__ import annotations

from collections.abc import Callable
import hmac
import json
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import mqtt
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    CONF_MQTT_BASE_TOPIC,
    CONF_MQTT_BATTERY_COMMANDS,
    CONF_MQTT_COMMANDS,
    CONF_MQTT_SECRET,
    DEFAULT_MQTT_BASE_TOPIC,
    DOMAIN,
    EVENT_DISPATCH_ENDED,
    EVENT_FIRMWARE_UPDATED,
    EVENT_GRID_OUTAGE,
    EVENT_OFF_GRID_ENDED,
    GRID_EXPORT_MODES,
    OPERATION_MODES,
    SERVICE_CANCEL_MAX_BACKUP,
    SERVICE_CHARGE_TO,
    SERVICE_DISCHARGE_TO,
    SERVICE_DISPATCH,
    SERVICE_RESUME,
    SERVICE_START_MAX_BACKUP,
)
from .firmware import split_version
from .privacy import scrub

if TYPE_CHECKING:
    from .coordinator import TedapiConfigEntry, TedapiCoordinator

_LOGGER = logging.getLogger(__name__)

SCHEMA = 1
ONLINE = "online"
OFFLINE = "offline"
# The retained topics: cleared (an empty retained message) when publishing
# is turned off or the entry is removed.
RETAINED = ("availability", "power", "battery", "energy", "settings", "status")
# Our events, published as event/<name> without "config_entry_id".
EVENTS = {
    EVENT_DISPATCH_ENDED: "dispatch_ended",
    EVENT_OFF_GRID_ENDED: "off_grid_ended",
    EVENT_GRID_OUTAGE: "grid_outage",
    EVENT_FIRMWARE_UPDATED: "firmware_updated",
}

# Commands: settings (the first switch), and battery control (the second,
# on top of the first), run as the integration's own actions. Going off-grid
# and site limits aren't offered over MQTT.
SETTINGS_COMMANDS: dict[str, vol.Schema] = {
    "set_mode": vol.Schema({vol.Required("mode"): vol.In(OPERATION_MODES)}),
    "set_reserve": vol.Schema({vol.Required("percent"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100))}),
    "set_grid_charging": vol.Schema({vol.Required("enabled"): bool}),
    "set_grid_export": vol.Schema({vol.Required("rule"): vol.In(GRID_EXPORT_MODES)}),
}
BATTERY_COMMANDS = {
    "dispatch": SERVICE_DISPATCH,
    "charge_to": SERVICE_CHARGE_TO,
    "discharge_to": SERVICE_DISCHARGE_TO,
    "resume": SERVICE_RESUME,
    "start_max_backup": SERVICE_START_MAX_BACKUP,
    "cancel_max_backup": SERVICE_CANCEL_MAX_BACKUP,
}
RESULT = "result"
MAX_COMMAND_BYTES = 2048
# Without Home Assistant's MQTT client, start is tried again this often (s):
# the MQTT integration may be set up later.
RETRY_SECONDS = 300


def issue_id(entry_id: str) -> str:
    return f"mqtt_unavailable_{entry_id}"


def base_topic(entry: TedapiConfigEntry) -> str:
    return entry.options.get(CONF_MQTT_BASE_TOPIC) or DEFAULT_MQTT_BASE_TOPIC


# ---------------------------------------------------------------------------
# Payloads (pure functions of the coordinator's data)
# ---------------------------------------------------------------------------


def power_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "solar_w": data.get("solar_power"),
        "battery_w": data.get("battery_power"),
        "grid_w": data.get("grid_power"),
        "home_w": data.get("home_power"),
        "battery_level": data.get("battery_level"),
        "grid_status": str(data["grid_status"]).lower() if data.get("grid_status") is not None else None,
    }


def battery_payload(data: dict[str, Any]) -> dict[str, Any]:
    # Packs by position only: a pack's name can end in its serial.
    packs = sorted((data.get("packs") or {}).values(), key=lambda pack: str(pack.get("name")))
    return {
        "level": data.get("battery_level"),
        "soe": data.get("battery_soe"),
        "full_kwh": data.get("battery_full_kwh"),
        "remaining_kwh": data.get("battery_remaining_kwh"),
        "usable_kwh": data.get("battery_usable_kwh"),
        "backup_hours": data.get("backup_hours"),
        "packs": [{"index": index, "full_kwh": pack.get("full_kwh"), "remaining_kwh": pack.get("remaining_kwh"),
                   "temp_max": pack.get("temp_max"), "temp_min": pack.get("temp_min")}
                  for index, pack in enumerate(packs, start=1)],
    }


def energy_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "lifetime": dict(data.get("energy") or {}),
        "today": dict(data.get("energy_today") or {}),
        "flows": dict(data.get("flows") or {}),
        "battery_net": dict(data.get("battery_net") or {}),
    }


def settings_payload(data: dict[str, Any]) -> dict[str, Any]:
    extras = data.get("cloud_extras") or {}
    return {
        "operation_mode": data.get("operation_mode"),
        "backup_reserve": data.get("reserve"),
        "grid_charging": data.get("grid_charging"),
        "grid_export": data.get("grid_export"),
        "site_limits": data.get("site_limits"),
        "storm_watch": extras.get("storm_watch"),
        # Where the settings came from: local, cloud or mixed.
        "settings_source": data.get("control_source"),
    }


def status_payload(data: dict[str, Any], dispatch: dict[str, Any] | None) -> dict[str, Any]:
    outages = data.get("outages") or {}
    return {
        "alert_level": data.get("alert_level"),
        "alerts": list(data.get("alerts") or []),
        "network": (data.get("network") or {}).get("active"),
        "firmware_version": split_version(data.get("firmware_version"))[0],
        "health": (data.get("health") or {}).get("state"),
        "dispatch": {key: (dispatch or {}).get(key) for key in ("active", "power_watts", "ends")}
        if dispatch is not None else None,
        "outage": bool(outages.get("ongoing")),
    }


def event_payload(event_data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event_data.items() if key != "config_entry_id"}


def alert_payload(change: Any) -> dict[str, Any]:
    # Not "sources": they're pack names, and a name can end in a serial.
    return {"kind": change.kind, "alert": change.name, "severity": change.severity,
            "description": change.description}


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True)


async def async_clear(hass: HomeAssistant, base: str) -> None:
    """Remove our retained messages under a base topic (empty retained
    messages), so nothing is left on the broker once publishing stops."""
    if not await mqtt.async_wait_for_mqtt_client(hass):
        return
    for name in RETAINED:
        try:
            await mqtt.async_publish(hass, f"{base}/{name}", "", qos=0, retain=True)
        except HomeAssistantError as err:
            _LOGGER.debug("Clearing %s/%s failed: %s", base, name, err)
            return


class MqttBridge:
    """Publishes one entry's readings on every coordinator update."""

    def __init__(self, hass: HomeAssistant, entry: TedapiConfigEntry, coordinator: TedapiCoordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.base = base_topic(entry)
        self.started = False
        self._unsubs: list[Callable[[], None]] = []
        # The last payload sent on each retained topic, without "ts": an
        # unchanged one isn't sent again.
        self._last: dict[str, str] = {}
        self._fired_alerts: Any = None
        options = entry.options
        self.commands = bool(options.get(CONF_MQTT_COMMANDS))
        self.battery_commands = self.commands and bool(options.get(CONF_MQTT_BATTERY_COMMANDS))
        self._secret = str(options.get(CONF_MQTT_SECRET) or "")
        self._retry: Callable[[], None] | None = None

    async def async_start(self) -> bool:
        """Start publishing once Home Assistant's MQTT client is there. False,
        with a repair, when the MQTT integration isn't set up."""
        self._retry = None
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            registry = ir.async_get(self.hass)
            if registry.async_get_issue(DOMAIN, issue_id(self.entry.entry_id)) is None:
                _LOGGER.warning("%s: MQTT publishing is on, but the MQTT integration isn't set up",
                                scrub(self.entry.title))
                ir.async_create_issue(
                    self.hass, DOMAIN, issue_id(self.entry.entry_id), is_fixable=False, is_persistent=False,
                    severity=ir.IssueSeverity.WARNING, translation_key="mqtt_unavailable",
                    translation_placeholders={"name": scrub(self.entry.title)})
            self._retry = async_call_later(self.hass, RETRY_SECONDS, self._retry_start)
            return False
        ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.entry.entry_id))
        self.started = True
        self._fired_alerts = self.coordinator.alert_changes
        self._unsubs.append(self.coordinator.async_add_listener(self._on_update))
        for event_type in EVENTS:
            self._unsubs.append(self.hass.bus.async_listen(event_type, self._on_event))
        if self.commands:
            self._unsubs.append(await mqtt.async_subscribe(self.hass, f"{self.base}/cmd/+", self._on_command))
        _LOGGER.debug("Publishing to MQTT under %s%s", self.base, " (with commands)" if self.commands else "")
        self._on_update()
        return True

    @callback
    def _retry_start(self, _now: Any) -> None:
        self._retry = None
        self.entry.async_create_background_task(self.hass, self.async_start(), "tesla_tedapi MQTT publishing")

    async def async_stop(self) -> None:
        """Stop, and say offline."""
        if self._retry is not None:
            self._retry()
            self._retry = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self.started:
            self.started = False
            await self._async_send("availability", OFFLINE, retain=True)

    @callback
    def _on_update(self) -> None:
        self.hass.async_create_task(self._async_publish_all(), eager_start=True)

    @callback
    def _on_event(self, event: Event) -> None:
        if event.data.get("config_entry_id") != self.entry.entry_id:
            return
        payload = {**event_payload(dict(event.data)), "schema": SCHEMA, "ts": dt_util.utcnow().isoformat()}
        self.hass.async_create_task(
            self._async_send(f"event/{EVENTS[event.event_type]}", _json(payload), retain=False), eager_start=True)

    # ---- commands ----

    @callback
    def _on_command(self, msg: Any) -> None:
        action = str(msg.topic).rsplit("/", 1)[-1]
        if action == RESULT:
            return  # our own answers
        self.hass.async_create_task(self._async_command(action, msg.payload, bool(msg.retain)), eager_start=True)

    async def _async_command(self, action: str, raw: Any, retained: bool) -> None:
        """Run one command and answer on cmd/result. A retained command is
        never run: it would run again at every restart."""
        body: Any = None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if isinstance(raw, str) and len(raw) <= MAX_COMMAND_BYTES:
            try:
                body = json.loads(raw) if raw.strip() else {}
            except ValueError:
                body = None
        request_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            request_id = None
        error, message = None, None
        if retained:
            error = "retained"
        elif not isinstance(body, dict):
            error = "invalid_json"
        elif self._secret and not hmac.compare_digest(str(body.get("token") or "").encode(), self._secret.encode()):
            error = "unauthorized"
        elif action not in SETTINGS_COMMANDS and action not in BATTERY_COMMANDS:
            error = "unknown_action"
        elif action in BATTERY_COMMANDS and not self.battery_commands:
            error = "not_enabled"
        else:
            params = {key: value for key, value in body.items() if key not in ("id", "token")}
            try:
                await self._async_run(action, params)
            except vol.Invalid as err:
                error, message = "invalid_arguments", scrub(err)
            except HomeAssistantError as err:
                error, message = err.translation_key or "failed", scrub(err)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("MQTT command %s failed: %s", action, scrub(err))
                error, message = "failed", scrub(err)
        _LOGGER.info("MQTT command %s: %s", action, error or "done")
        result = {"id": str(request_id)[:64] if request_id is not None else None, "action": action[:64],
                  "ok": error is None, "error": error, "message": message,
                  "schema": SCHEMA, "ts": dt_util.utcnow().isoformat()}
        await self._async_send(f"cmd/{RESULT}", _json(result), retain=False)

    async def _async_run(self, action: str, params: dict[str, Any]) -> None:
        coordinator = self.coordinator
        if action in SETTINGS_COMMANDS:
            args = SETTINGS_COMMANDS[action](params)
            if action == "set_mode":
                await coordinator.async_set_mode(args["mode"])
            elif action == "set_reserve":
                await coordinator.async_set_reserve(args["percent"])
            elif action == "set_grid_charging":
                await coordinator.async_set_grid_charging(args["enabled"])
            else:
                await coordinator.async_set_grid_export(args["rule"])
            return
        await self.hass.services.async_call(
            DOMAIN, BATTERY_COMMANDS[action], {**params, ATTR_CONFIG_ENTRY_ID: self.entry.entry_id}, blocking=True)

    async def _async_publish_all(self) -> None:
        coordinator = self.coordinator
        data = coordinator.data or {}
        local = coordinator.last_update_success and bool(data.get("local_ok"))
        await self._async_retained("availability", ONLINE if local else OFFLINE, raw=True)
        if not coordinator.last_update_success:
            return
        source = coordinator.connection_path
        if local or data.get("readings_source") == "cloud":
            await self._async_retained("power", power_payload(data), source)
        if data.get("control_source") is not None:
            await self._async_retained("settings", settings_payload(data), source)
        if local and data.get("full_read", True):
            await self._async_retained("battery", battery_payload(data), source)
            await self._async_retained("energy", energy_payload(data), source)
        runtime = getattr(self.entry, "runtime_data", None)
        dispatch = runtime.dispatch.as_dict() if runtime is not None and runtime.dispatch is not None else None
        await self._async_retained("status", status_payload(data, dispatch), source)
        changes = coordinator.alert_changes
        if changes is not self._fired_alerts:
            self._fired_alerts = changes
            for change in changes:
                if not self.started:
                    return
                payload = {**alert_payload(change), "schema": SCHEMA, "ts": dt_util.utcnow().isoformat()}
                await self._async_send("event/alert", _json(payload), retain=False)

    async def _async_retained(self, name: str, payload: Any, source: str | None = None, *,
                              raw: bool = False) -> None:
        if not self.started:
            return  # stopped meanwhile: nothing after "offline"
        if raw:
            body = key = str(payload)
        else:
            key = _json({**payload, "source": source, "schema": SCHEMA})
            body = _json({**payload, "source": source, "schema": SCHEMA, "ts": dt_util.utcnow().isoformat()})
        if self._last.get(name) == key:
            return
        if await self._async_send(name, body, retain=True):
            self._last[name] = key

    async def _async_send(self, name: str, body: str, *, retain: bool) -> bool:
        try:
            await mqtt.async_publish(self.hass, f"{self.base}/{name}", body, qos=0, retain=retain)
        except HomeAssistantError as err:
            _LOGGER.debug("MQTT publish to %s/%s failed: %s", self.base, name, err)
            return False
        return True
