"""Tesla Powerwall TEDAPI: local monitoring and control of a Tesla Powerwall."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from . import client
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CONFIRM,
    ATTR_CURTAILMENT,
    ATTR_CURTAILMENT_ONLY,
    ATTR_DAILY_LIMIT_MINUTES,
    ATTR_DELAY_HOURS,
    ATTR_DIRECTION,
    ATTR_DRY_RUN,
    ATTR_DURATION_MINUTES,
    ATTR_DURATION_SECONDS,
    ATTR_FINGERPRINT,
    ATTR_LIMIT_KW,
    ATTR_MAX_MINUTES,
    ATTR_MAX_POWER_WATTS,
    ATTR_MAX_SOC,
    ATTR_MIN_SOC,
    ATTR_NET,
    ATTR_ONLY_IF_CHANGED,
    ATTR_POWER_WATTS,
    ATTR_READ_BACK,
    ATTR_SOURCE_CHARGED,
    ATTR_SOURCE_DISCHARGED,
    ATTR_TARGET_SOC,
    ATTR_TOU_SETTINGS,
    CALIBRATION_DELAY_MAX_HOURS,
    CONF_FLEET_SERVER,
    CONF_MACS,
    CONF_MQTT_PUBLISH,
    CONF_OAUTH_TOKEN,
    DISPATCH_MAX_SECONDS,
    DISPATCH_MAX_WATTS,
    DOMAIN,
    FREEZE_DIRECTIONS,
    LIVE_OPTIONS,
    MAX_BACKUP_MAX_MINUTES,
    OFF_GRID_MAX_MINUTES,
    SERVICE_CANCEL_MAX_BACKUP,
    SERVICE_CHARGE_TO,
    SERVICE_DELAY_CALIBRATION,
    SERVICE_DISCHARGE_TO,
    SERVICE_DISPATCH,
    SERVICE_FREEZE,
    SERVICE_GET_TARIFF,
    SERVICE_GO_OFF_GRID,
    SERVICE_IMPORT_BATTERY_HISTORY,
    SERVICE_RECONNECT_GRID,
    SERVICE_REMOVE_KEY,
    SERVICE_RESUME,
    SERVICE_SET_SITE_LIMIT,
    SERVICE_SET_TARIFF,
    SERVICE_START_MAX_BACKUP,
    TARIFF_READ_BACK_MODES,
)
from .coordinator import TedapiConfigEntry, TedapiCoordinator, TedapiRuntimeData
from .dispatch import DispatchSession, quiet_dispatch_repeats
from .off_grid import OffGridSession, off_grid_store
from .privacy import mask_library_logs, scrub
from .texts import async_setup_texts

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.CALENDAR, Platform.EVENT, Platform.NUMBER,
             Platform.SELECT, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Entities made only with a cloud backup, only with the Tesla tariff option,
# or only while settings can be changed (a signed connection or a cloud
# backup), by the key in their unique id ("<DIN>_<key>"). Once the entry no
# longer makes them, they're removed from the entity registry rather than
# left unavailable (2.9.0); their history stays in the recorder.
CLOUD_ONLY_KEYS = frozenset({"storm_watch", "storm_watch_active", "grid_services_active", "off_grid_reserve"})
CLOUD_ONLY_PREFIXES = ("wall_connector_",)
TARIFF_KEYS = frozenset({"tesla_tariff", "tariff_buy_price", "tariff_sell_price", "buy_tariff", "sell_tariff"})
CONTROL_KEYS = frozenset({"backup_reserve", "grid_charging", "operation_mode", "grid_export"})

_SOC = vol.All(vol.Coerce(float), vol.Range(min=0, max=100))
DISPATCH_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    # Positive discharges, negative charges. Within the ceiling the gateway
    # decides (one Powerwall 3 refuses more than 10 kW).
    vol.Required(ATTR_POWER_WATTS): vol.All(
        vol.Coerce(float), vol.Range(min=-DISPATCH_MAX_WATTS, max=DISPATCH_MAX_WATTS)),
    vol.Required(ATTR_DURATION_SECONDS): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=DISPATCH_MAX_SECONDS)),
    vol.Optional(ATTR_MIN_SOC): _SOC,
    vol.Optional(ATTR_MAX_SOC): _SOC,
})
# Battery one way only, at the power the readings call for (Predbat's freezes).
FREEZE_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_DIRECTION): vol.In(FREEZE_DIRECTIONS),
    vol.Required(ATTR_DURATION_SECONDS): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=DISPATCH_MAX_SECONDS)),
    vol.Optional(ATTR_MAX_POWER_WATTS): vol.All(vol.Coerce(float), vol.Range(min=100, max=DISPATCH_MAX_WATTS)),
    vol.Optional(ATTR_MIN_SOC): _SOC,
    vol.Optional(ATTR_MAX_SOC): _SOC,
})
MOVE_TO_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_TARGET_SOC): vol.All(vol.Coerce(float), vol.Range(min=1, max=100)),
    # A size, not a direction: the action gives the sign.
    vol.Required(ATTR_POWER_WATTS): vol.All(vol.Coerce(float), vol.Range(min=100, max=DISPATCH_MAX_WATTS)),
    vol.Required(ATTR_MAX_MINUTES): vol.All(vol.Coerce(int), vol.Range(min=1, max=DISPATCH_MAX_SECONDS // 60)),
})
RESUME_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})
IMPORT_HISTORY_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Optional(ATTR_DRY_RUN, default=True): cv.boolean,
    vol.Required(ATTR_SOURCE_CHARGED): cv.entity_id,
    vol.Required(ATTR_SOURCE_DISCHARGED): cv.entity_id,
    vol.Optional(ATTR_NET, default=False): cv.boolean,
})
SET_TARIFF_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_TOU_SETTINGS): dict,
    vol.Optional(ATTR_DRY_RUN, default=True): cv.boolean,
    vol.Optional(ATTR_READ_BACK, default="full"): vol.In(TARIFF_READ_BACK_MODES),
    vol.Optional(ATTR_ONLY_IF_CHANGED, default=False): cv.boolean,
})
START_MAX_BACKUP_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_DURATION_MINUTES): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=MAX_BACKUP_MAX_MINUTES)),
})
GO_OFF_GRID_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_CONFIRM): cv.boolean,
    vol.Optional(ATTR_DURATION_MINUTES): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=OFF_GRID_MAX_MINUTES)),
    vol.Optional(ATTR_MIN_SOC): _SOC,
    vol.Optional(ATTR_CURTAILMENT, default=False): cv.boolean,
    vol.Optional(ATTR_DAILY_LIMIT_MINUTES): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=OFF_GRID_MAX_MINUTES)),
})
RECONNECT_GRID_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Optional(ATTR_CURTAILMENT_ONLY, default=False): cv.boolean,
})
SET_SITE_LIMIT_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_DIRECTION): vol.In(("import", "export")),
    # Left out: no limit.
    vol.Optional(ATTR_LIMIT_KW): vol.All(vol.Coerce(float), vol.Range(min=0, max=10000)),
    vol.Required(ATTR_CONFIRM): cv.boolean,
})
DELAY_CALIBRATION_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_DELAY_HOURS): vol.All(vol.Coerce(int), vol.Range(min=1, max=CALIBRATION_DELAY_MAX_HOURS)),
})
REMOVE_KEY_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    # The start of the key's fingerprint, as the Registered keys sensor lists it.
    vol.Required(ATTR_FINGERPRINT): vol.All(cv.string, vol.Strip, vol.Lower, vol.Match(r"^[0-9a-f]{8,64}$")),
    vol.Required(ATTR_CONFIRM): cv.boolean,
})


def _confirmed(call: ServiceCall) -> None:
    if not call.data[ATTR_CONFIRM]:
        raise ServiceValidationError(
            "Set confirm to true to send this command",
            translation_domain=DOMAIN, translation_key="command_not_confirmed",
        )


def _entry(hass: HomeAssistant, call: ServiceCall) -> TedapiConfigEntry:
    """The targeted (or only) loaded entry."""
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    entries: list[TedapiConfigEntry] = [
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED and (entry_id is None or e.entry_id == entry_id)
    ]
    if not entries:
        raise ServiceValidationError(
            "No loaded Tesla Powerwall TEDAPI entry found",
            translation_domain=DOMAIN, translation_key="entry_not_found",
        )
    if len(entries) > 1:
        raise ServiceValidationError(
            "Several Powerwalls are set up: choose one with config_entry_id",
            translation_domain=DOMAIN, translation_key="entry_ambiguous",
        )
    return entries[0]


def _session(hass: HomeAssistant, call: ServiceCall) -> DispatchSession:
    """The dispatch session of the targeted (or only) loaded entry."""
    session = _entry(hass, call).runtime_data.dispatch
    if session is None:
        raise ServiceValidationError(
            "Battery dispatch needs the signed (v1r) connection",
            translation_domain=DOMAIN, translation_key="dispatch_needs_v1r",
        )
    return session


def _max_backup_coordinator(hass: HomeAssistant, call: ServiceCall) -> TedapiCoordinator:
    coordinator = _entry(hass, call).runtime_data.coordinator
    if not coordinator.dispatch_supported:
        raise ServiceValidationError(
            "A max backup needs the signed (v1r) connection",
            translation_domain=DOMAIN, translation_key="max_backup_needs_v1r",
        )
    return coordinator


def _tariff_coordinator(hass: HomeAssistant, call: ServiceCall) -> TedapiCoordinator:
    coordinator = _entry(hass, call).runtime_data.coordinator
    if not coordinator.tariff_enabled:
        raise ServiceValidationError(
            "Turn on the Tesla tariff option (it needs a cloud backup)",
            translation_domain=DOMAIN, translation_key="tariff_disabled",
        )
    return coordinator


def _off_grid(hass: HomeAssistant, call: ServiceCall) -> OffGridSession:
    session = _entry(hass, call).runtime_data.off_grid
    if session is None:
        raise ServiceValidationError(
            "Going off-grid needs the signed (v1r) connection",
            translation_domain=DOMAIN, translation_key="off_grid_needs_v1r",
        )
    return session


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions once; they look up the entry per call."""
    quiet_dispatch_repeats()
    await async_setup_texts(hass)

    async def dispatch(call: ServiceCall) -> ServiceResponse:
        return await _session(hass, call).async_start(
            call.data[ATTR_POWER_WATTS],
            call.data[ATTR_DURATION_SECONDS],
            call.data.get(ATTR_MIN_SOC),
            call.data.get(ATTR_MAX_SOC),
        )

    async def freeze_battery(call: ServiceCall) -> ServiceResponse:
        return await _session(hass, call).async_start(
            0, call.data[ATTR_DURATION_SECONDS], call.data.get(ATTR_MIN_SOC), call.data.get(ATTR_MAX_SOC),
            freeze=call.data[ATTR_DIRECTION], max_power=call.data.get(ATTR_MAX_POWER_WATTS))

    async def charge_to(call: ServiceCall) -> ServiceResponse:
        return await _session(hass, call).async_move_to(
            call.data[ATTR_TARGET_SOC], call.data[ATTR_POWER_WATTS], call.data[ATTR_MAX_MINUTES], charge=True)

    async def discharge_to(call: ServiceCall) -> ServiceResponse:
        return await _session(hass, call).async_move_to(
            call.data[ATTR_TARGET_SOC], call.data[ATTR_POWER_WATTS], call.data[ATTR_MAX_MINUTES], charge=False)

    async def resume(call: ServiceCall) -> ServiceResponse:
        result = await _session(hass, call).async_stop()
        return {"response": result}

    async def start_max_backup(call: ServiceCall) -> None:
        await _max_backup_coordinator(hass, call).async_max_backup(call.data[ATTR_DURATION_MINUTES])

    async def cancel_max_backup(call: ServiceCall) -> None:
        await _max_backup_coordinator(hass, call).async_max_backup(None)

    async def go_off_grid(call: ServiceCall) -> ServiceResponse:
        if not call.data[ATTR_CONFIRM]:
            raise ServiceValidationError(
                "Set confirm to true to take the house off the grid",
                translation_domain=DOMAIN, translation_key="off_grid_confirm",
            )
        return await _off_grid(hass, call).async_go_off_grid(
            call.data.get(ATTR_DURATION_MINUTES), call.data.get(ATTR_MIN_SOC),
            curtailment=call.data[ATTR_CURTAILMENT], daily_limit=call.data.get(ATTR_DAILY_LIMIT_MINUTES))

    async def reconnect_grid(call: ServiceCall) -> ServiceResponse:
        return await _off_grid(hass, call).async_reconnect(curtailment_only=call.data[ATTR_CURTAILMENT_ONLY])

    async def get_tariff(call: ServiceCall) -> ServiceResponse:
        coordinator = _tariff_coordinator(hass, call)
        tariff = await coordinator.async_read_tariff()
        coordinator.async_update_listeners()
        return {"backend": coordinator.tariff_backend, "tariff": tariff}

    async def import_battery_history(call: ServiceCall) -> ServiceResponse:
        from .history_import import async_import  # noqa: PLC0415

        coordinator = _entry(hass, call).runtime_data.coordinator
        return await async_import(hass, coordinator.din, {
            "battery_energy_charged": call.data[ATTR_SOURCE_CHARGED],
            "battery_energy_discharged": call.data[ATTR_SOURCE_DISCHARGED],
        }, call.data[ATTR_DRY_RUN], net=call.data[ATTR_NET])

    async def set_tariff(call: ServiceCall) -> ServiceResponse:
        return await _tariff_coordinator(hass, call).async_set_tariff(
            call.data[ATTR_TOU_SETTINGS], call.data[ATTR_DRY_RUN], call.data[ATTR_READ_BACK],
            call.data[ATTR_ONLY_IF_CHANGED])

    async def set_site_limit(call: ServiceCall) -> ServiceResponse:
        _confirmed(call)
        coordinator = _entry(hass, call).runtime_data.coordinator
        return await coordinator.async_set_site_limit(call.data[ATTR_DIRECTION], call.data.get(ATTR_LIMIT_KW))

    async def delay_battery_calibration(call: ServiceCall) -> ServiceResponse:
        coordinator = _entry(hass, call).runtime_data.coordinator
        return {"status": await coordinator.async_delay_calibration(call.data[ATTR_DELAY_HOURS])}

    async def remove_registered_key(call: ServiceCall) -> ServiceResponse:
        _confirmed(call)
        coordinator = _entry(hass, call).runtime_data.coordinator
        return {"registered_keys": await coordinator.async_remove_key(call.data[ATTR_FINGERPRINT])}

    hass.services.async_register(DOMAIN, SERVICE_SET_SITE_LIMIT, set_site_limit, schema=SET_SITE_LIMIT_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_DELAY_CALIBRATION, delay_battery_calibration,
                                 schema=DELAY_CALIBRATION_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_REMOVE_KEY, remove_registered_key, schema=REMOVE_KEY_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_GO_OFF_GRID, go_off_grid, schema=GO_OFF_GRID_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_RECONNECT_GRID, reconnect_grid, schema=RECONNECT_GRID_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_START_MAX_BACKUP, start_max_backup,
                                 schema=START_MAX_BACKUP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CANCEL_MAX_BACKUP, cancel_max_backup, schema=RESUME_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DISPATCH, dispatch, schema=DISPATCH_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_FREEZE, freeze_battery, schema=FREEZE_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_CHARGE_TO, charge_to, schema=MOVE_TO_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_DISCHARGE_TO, discharge_to, schema=MOVE_TO_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_IMPORT_BATTERY_HISTORY, import_battery_history,
                                 schema=IMPORT_HISTORY_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_GET_TARIFF, get_tariff, schema=RESUME_SCHEMA,
                                 supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SERVICE_SET_TARIFF, set_tariff, schema=SET_TARIFF_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_RESUME, resume, schema=RESUME_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: TedapiConfigEntry) -> bool:
    """1.1 -> 1.2 (2.3.0): drop the "fleet_backup" option left by a test build
    of 2.2.1. The Tesla Fleet backup is now chosen under Cloud backup."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        options = {k: v for k, v in entry.options.items() if k != "fleet_backup"}
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TedapiConfigEntry) -> bool:
    try:
        await hass.async_add_executor_job(client.ensure_supported)
    except client.ClientTooOld as err:
        # Not ConfigEntryNotReady: retrying can't help, only a restart
        # without the integration that loaded the older pypowerwall.
        _LOGGER.error("%s. Another integration loaded it; remove that one and restart Home Assistant", scrub(err))
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="pypowerwall_too_old",
            translation_placeholders={"found": err.found, "required": client.required()}) from err
    await hass.async_add_executor_job(mask_library_logs)
    coordinator = TedapiCoordinator(hass, entry)
    await coordinator.async_load_state()
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        raise
    except Exception as err:
        raise ConfigEntryNotReady(f"Unable to reach the Powerwall: {err}") from err

    if not coordinator.din:
        # Entities are keyed by the DIN; without it they would be created under
        # a placeholder identity and duplicated later. Retry instead.
        raise ConfigEntryNotReady("Powerwall DIN not available yet")

    coordinator.async_sync_signin_issue()
    _async_remove_unmade_entities(hass, entry, coordinator)
    _async_remove_unit_devices(hass, entry, coordinator)
    entry.runtime_data = TedapiRuntimeData(
        coordinator=coordinator,
        dispatch=DispatchSession(hass, coordinator) if coordinator.dispatch_supported else None,
        off_grid=OffGridSession(hass, coordinator) if coordinator.dispatch_supported else None,
        applied_data=dict(entry.data),
        applied_options=dict(entry.options),
    )
    if (dispatch := entry.runtime_data.dispatch) is not None:
        entry.async_on_unload(dispatch.async_setup())
    if (off_grid := entry.runtime_data.off_grid) is not None:
        await off_grid.async_load()
        coordinator.intentional_off_grid = lambda: off_grid.intentional
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    if entry.options.get(CONF_MQTT_PUBLISH):
        # Imported only with the option on: no MQTT needed otherwise.
        from .mqtt_bridge import MqttBridge  # noqa: PLC0415

        entry.runtime_data.mqtt = bridge = MqttBridge(hass, entry, coordinator)
        # In the background: waiting for the MQTT client mustn't hold up setup.
        entry.async_create_background_task(hass, bridge.async_start(), "tesla_tedapi MQTT publishing")
    else:
        ir.async_delete_issue(hass, DOMAIN, f"mqtt_unavailable_{entry.entry_id}")

    async def _async_stop(_event: Event) -> None:
        # Home Assistant doesn't unload entries when it stops or restarts:
        # end a running dispatch here, so the battery is handed back now (not
        # when the last command expires) and the ended event still fires.
        # Save the state too: the flows counted on fast polls since the last
        # full read aren't saved yet.
        if (dispatch := entry.runtime_data.dispatch) is not None:
            await dispatch.async_shutdown()
        await coordinator.async_save_state()

    # async_listen, not async_listen_once: removing a once-listener that has
    # already fired (unload after the stop) logs an error.
    entry.async_on_unload(hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _async_stop))
    _LOGGER.debug("Powerwall %s loaded (firmware %s, %s, settings %s)",
                  scrub(entry.title), coordinator.firmware_version, coordinator.connection_mode,
                  "enabled" if coordinator.control_enabled else "read-only")
    return True


@callback
def _async_remove_unmade_entities(hass: HomeAssistant, entry: TedapiConfigEntry,
                                  coordinator: TedapiCoordinator) -> None:
    """Remove the entities this entry no longer makes: the cloud backup's
    without one, the Tesla tariff's with the option off, and the settings
    when they can't be changed. They come back when that changes."""
    unmade: set[str] = set()
    prefixes: tuple[str, ...] = ()
    if not coordinator.cloud_enabled:
        unmade |= CLOUD_ONLY_KEYS
        prefixes = CLOUD_ONLY_PREFIXES
    if not coordinator.tariff_enabled:
        unmade |= TARIFF_KEYS
    if not coordinator.control_enabled:
        unmade |= CONTROL_KEYS
    registry = er.async_get(hass)
    owners = tuple(f"{owner}_" for owner in (coordinator.din, entry.entry_id) if owner)
    removed = 0
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        key = next((entity.unique_id[len(owner):] for owner in owners if entity.unique_id.startswith(owner)), None)
        if key is not None and (key in unmade or key.startswith(prefixes)):
            registry.async_remove(entity.entity_id)
            removed += 1
    if removed:
        _LOGGER.info("Removed %d entities that need a cloud backup, the Tesla tariff or settings control, "
                     "which this Powerwall's setup no longer has", removed)


@callback
def _async_remove_unit_devices(hass: HomeAssistant, entry: TedapiConfigEntry,
                               coordinator: TedapiCoordinator) -> None:
    """With one device per Powerwall off (#24): put the entities of each
    Powerwall's or Expansion Pack's device back on the gateway's device, then
    remove those devices. In that order: removing a device removes the
    entities still on it. Unique ids and entity ids don't change; Home
    Assistant keeps the removed devices' ids, so switching the option on
    again brings back the same devices."""
    din = coordinator.din
    if coordinator.unit_devices or not din:
        return
    devices = dr.async_get(hass)
    prefix = f"{din}_"
    units = [device for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
             if any(domain == DOMAIN and str(value).startswith(prefix) for domain, value in device.identifiers)]
    if not units:
        return
    # The gateway's device (it can't be removed while the entry is loaded;
    # its entities fill in the rest when they're added).
    gateway = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, din)})
    entities = er.async_get(hass)
    for unit in units:
        for entity in er.async_entries_for_device(entities, unit.id, include_disabled_entities=True):
            entities.async_update_entity(entity.entity_id, device_id=gateway.id)
        devices.async_remove_device(unit.id)
    _LOGGER.info("One device per Powerwall is off: %d Powerwall and Expansion Pack devices removed, "
                 "their entities are on the gateway's device again", len(units))


# Entry data updated by the integration itself (a refreshed token, the
# account's region server, the gateway's MACs): saved without a reload.
SELF_UPDATED = (CONF_OAUTH_TOKEN, CONF_FLEET_SERVER, CONF_MACS)


def _without_token(data: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key not in SELF_UPDATED}


def reloads_on_update(entry: TedapiConfigEntry, data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    """Whether the update listener reloads the entry for this data and these
    options: anything but a refreshed token or region, or options outside
    LIVE_OPTIONS. False when the entry isn't loaded (no listener runs)."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None or not entry.update_listeners:
        return False
    changed = {key for key in runtime.applied_options.keys() | options.keys()
               if runtime.applied_options.get(key) != options.get(key)}
    return _without_token(data) != _without_token(runtime.applied_data) or not changed <= LIVE_OPTIONS


async def _async_options_updated(hass: HomeAssistant, entry: TedapiConfigEntry) -> None:
    """Apply a change of options in place when only LIVE_OPTIONS changed, so a
    running battery dispatch carries on; reload for anything else (connection,
    account, cloud backup, Tesla tariff)."""
    runtime = entry.runtime_data
    changed = {key for key in runtime.applied_options.keys() | entry.options.keys()
               if runtime.applied_options.get(key) != entry.options.get(key)}
    if not reloads_on_update(entry, entry.data, entry.options):
        # A refreshed Tesla Fleet token or region is saved in the data: no reload.
        runtime.applied_data = dict(entry.data)
        runtime.applied_options = dict(entry.options)
        if changed:
            _LOGGER.debug("Options %s applied without a reload", ", ".join(sorted(changed)))
            runtime.coordinator.async_apply_options()
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TedapiConfigEntry) -> bool:
    if entry.runtime_data.mqtt is not None:
        await entry.runtime_data.mqtt.async_stop()
    if entry.runtime_data.dispatch is not None:
        await entry.runtime_data.dispatch.async_shutdown()
    if entry.runtime_data.off_grid is not None:
        entry.runtime_data.off_grid.async_shutdown()
    entry.runtime_data.coordinator.async_clear_issues()
    await entry.runtime_data.coordinator.async_save_state()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: TedapiConfigEntry) -> None:
    """Forget the saved alert times, export rule and off-grid run, and clear
    the retained MQTT messages. The key and the Tesla sign-in stay in the
    tesla_tedapi folder (another entry may use them)."""
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
    await off_grid_store(hass, entry.entry_id).async_remove()
    ir.async_delete_issue(hass, DOMAIN, f"mqtt_unavailable_{entry.entry_id}")
    if entry.options.get(CONF_MQTT_PUBLISH):
        from .mqtt_bridge import async_clear, base_topic  # noqa: PLC0415

        await async_clear(hass, base_topic(entry))


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TedapiConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow removing devices other than the live gateway (stale leftovers)."""
    coordinator = entry.runtime_data.coordinator
    return (DOMAIN, coordinator.din) not in device_entry.identifiers
