"""Powerwall TEDAPI: local monitoring and control of a Tesla Powerwall."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CONFIRM,
    ATTR_DURATION_MINUTES,
    ATTR_DURATION_SECONDS,
    ATTR_MAX_MINUTES,
    ATTR_MAX_SOC,
    ATTR_MIN_SOC,
    ATTR_POWER_WATTS,
    ATTR_TARGET_SOC,
    DISPATCH_MAX_SECONDS,
    DISPATCH_MAX_WATTS,
    DOMAIN,
    MAX_BACKUP_MAX_MINUTES,
    OFF_GRID_MAX_MINUTES,
    SERVICE_CANCEL_MAX_BACKUP,
    SERVICE_CHARGE_TO,
    SERVICE_DISCHARGE_TO,
    SERVICE_DISPATCH,
    SERVICE_GO_OFF_GRID,
    SERVICE_RECONNECT_GRID,
    SERVICE_RESUME,
    SERVICE_START_MAX_BACKUP,
)
from .coordinator import TedapiConfigEntry, TedapiCoordinator, TedapiRuntimeData
from .dispatch import DispatchSession
from .off_grid import OffGridSession, off_grid_store

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_SOC = vol.All(vol.Coerce(float), vol.Range(min=0, max=100))
DISPATCH_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    # Positive discharges, negative charges. The gateway rejects > 10 kW.
    vol.Required(ATTR_POWER_WATTS): vol.All(
        vol.Coerce(float), vol.Range(min=-DISPATCH_MAX_WATTS, max=DISPATCH_MAX_WATTS)),
    vol.Required(ATTR_DURATION_SECONDS): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=DISPATCH_MAX_SECONDS)),
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
})


def _entry(hass: HomeAssistant, call: ServiceCall) -> TedapiConfigEntry:
    """The targeted (or only) loaded entry."""
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    entries: list[TedapiConfigEntry] = [
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED and (entry_id is None or e.entry_id == entry_id)
    ]
    if not entries:
        raise ServiceValidationError(
            "No loaded Powerwall TEDAPI entry found",
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

    async def dispatch(call: ServiceCall) -> ServiceResponse:
        return await _session(hass, call).async_start(
            call.data[ATTR_POWER_WATTS],
            call.data[ATTR_DURATION_SECONDS],
            call.data.get(ATTR_MIN_SOC),
            call.data.get(ATTR_MAX_SOC),
        )

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
            call.data.get(ATTR_DURATION_MINUTES), call.data.get(ATTR_MIN_SOC))

    async def reconnect_grid(call: ServiceCall) -> ServiceResponse:
        return await _off_grid(hass, call).async_reconnect()

    hass.services.async_register(DOMAIN, SERVICE_GO_OFF_GRID, go_off_grid, schema=GO_OFF_GRID_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_RECONNECT_GRID, reconnect_grid, schema=RESUME_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_START_MAX_BACKUP, start_max_backup,
                                 schema=START_MAX_BACKUP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CANCEL_MAX_BACKUP, cancel_max_backup, schema=RESUME_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DISPATCH, dispatch, schema=DISPATCH_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_CHARGE_TO, charge_to, schema=MOVE_TO_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_DISCHARGE_TO, discharge_to, schema=MOVE_TO_SCHEMA,
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

    entry.runtime_data = TedapiRuntimeData(
        coordinator=coordinator,
        dispatch=DispatchSession(hass, coordinator) if coordinator.dispatch_supported else None,
        off_grid=OffGridSession(hass, coordinator) if coordinator.dispatch_supported else None,
    )
    if entry.runtime_data.off_grid is not None:
        await entry.runtime_data.off_grid.async_load()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _LOGGER.debug("Powerwall %s loaded (firmware %s, %s, settings %s)",
                  coordinator.din, coordinator.firmware_version, coordinator.connection_mode,
                  "enabled" if coordinator.control_enabled else "read-only")
    return True


async def _async_options_updated(hass: HomeAssistant, entry: TedapiConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TedapiConfigEntry) -> bool:
    if entry.runtime_data.dispatch is not None:
        await entry.runtime_data.dispatch.async_shutdown()
    if entry.runtime_data.off_grid is not None:
        entry.runtime_data.off_grid.async_shutdown()
    entry.runtime_data.coordinator.async_clear_issues()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: TedapiConfigEntry) -> None:
    """Forget the saved alert times, export rule and off-grid run. The key and
    the Tesla sign-in stay in the tesla_tedapi folder (another entry may use them)."""
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
    await off_grid_store(hass, entry.entry_id).async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TedapiConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow removing devices other than the live gateway (stale leftovers)."""
    coordinator = entry.runtime_data.coordinator
    return (DOMAIN, coordinator.din) not in device_entry.identifiers
