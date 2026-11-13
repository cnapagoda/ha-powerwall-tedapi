"""Fast polling for power, and the cloud backup's live status: Wall
Connectors, and readings while the gateway can't be reached."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import homeassistant.helpers.config_validation as cv
import voluptuous_serialize

from custom_components.tesla_tedapi.cloud import live_readings, live_time, wall_connectors
from custom_components.tesla_tedapi.const import DOMAIN
from custom_components.tesla_tedapi.coordinator import FOLLOWER_MISSING_POLLS
from custom_components.tesla_tedapi.diagnostics import async_get_config_entry_diagnostics

from .conftest import DIN, TESLEMETRY, FakePowerwall, cloud, local
from .test_fleet import _setup as _setup_fleet
from .test_storm import ACCOUNT, V1R, _eid, _refresh, _setup

# -- fast polling ----------------------------------------------------------


def _down():
    raise ConnectionError("gateway down")


async def _poll(hass: HomeAssistant, entry) -> None:
    """A scheduled poll (a refresh asked for, as after a write, is a full read)."""
    await entry.runtime_data.coordinator._async_refresh(log_failures=True, scheduled=True)  # noqa: SLF001
    await hass.async_block_till_done()


async def test_fast_polling_reads_only_the_live_values(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R, {"power_interval": 5})
    coordinator = entry.runtime_data.coordinator
    pw = local(fake_powerwall)
    assert coordinator.update_interval == timedelta(seconds=5)
    assert pw.kwargs["pwcacheexpire"] == 4  # shorter than a poll
    assert coordinator.data["full_read"] is True
    vitals_reads = []
    original = pw.vitals
    pw.vitals = lambda: vitals_reads.append(1) or original()

    FakePowerwall.soe = 62.0  # 60.0 on the app's scale
    await _poll(hass, entry)
    assert coordinator.data["full_read"] is False and not vitals_reads
    assert hass.states.get(_eid(hass, "sensor", "battery_level")).state == "60.0"
    # Everything else is the last full read's.
    assert coordinator.data["packs"] and coordinator.data["firmware_version"]
    assert hass.states.get(_eid(hass, "sensor", "grid_energy_imported")).state == "1000.0"

    coordinator._full_at -= 60  # the polling interval has passed
    await _poll(hass, entry)
    assert coordinator.data["full_read"] is True and vitals_reads

    # A failed read is followed by a full one.
    pw.power = _down
    await _poll(hass, entry)
    assert coordinator.last_update_success is False and coordinator._full_at is None


async def test_fast_polls_do_not_count_as_polls_for_the_follower_repair(hass: HomeAssistant, fake_powerwall) -> None:
    FakePowerwall.battery_blocks = [{"vin": DIN, "type": "Powerwall3"},
                                    {"vin": "1707000-11-J--TG9876543210ZZ", "type": "Powerwall3"}]
    entry = await _setup(hass, V1R, {"power_interval": 5})
    for _ in range(FOLLOWER_MISSING_POLLS + 2):
        await _poll(hass, entry)
    assert entry.runtime_data.coordinator.data["full_read"] is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"powerwall_follower_hidden_{entry.entry_id}") is None


async def test_power_interval_is_off_unless_it_is_shorter(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.update_interval == timedelta(seconds=30)
    assert local(fake_powerwall).kwargs["pwcacheexpire"] == 5
    await _refresh(hass, entry)
    assert coordinator.data["full_read"] is True  # every read is a full one
    for options, seconds in (({"power_interval": 0}, 30), ({"power_interval": 45}, 30), ({"power_interval": 2}, 5),
                             ({"power_interval": 10, "scan_interval": 10}, 10), ({"power_interval": None}, 30)):
        stand_in = SimpleNamespace(options=options, data=V1R)
        assert coordinator._scan_interval(stand_in) == timedelta(seconds=seconds), options


async def test_power_interval_option(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    assert "power_interval" in result["data_schema"].schema["general"].schema.schema
    assert "cloud_readings" in result["data_schema"].schema["advanced"].schema.schema
    # The frontend must be able to show the form: no plain functions in it.
    voluptuous_serialize.convert(result["data_schema"], custom_serializer=cv.custom_serializer)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "general": {"scan_interval": 30, "power_interval": 2, "restore_export": True},
        "alerts": {"status_alert_events": False, "problem_minutes": 10},
        "dispatch": {"dispatch_floor_reserve": True, "dispatch_max_watts": 5000},
        "advanced": {"tesla_tariff": False, "cloud_readings": True, "diagnostics_gateway_config": False}})
    await hass.async_block_till_done()
    assert entry.options["power_interval"] == 5  # shorter than the minimum: the minimum
    assert entry.options["cloud_readings"] is True
    assert entry.runtime_data.coordinator.update_interval == timedelta(seconds=5)  # reloaded with it


async def test_cloud_readings_not_offered_without_a_cloud_backup(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    assert "cloud_readings" not in result["data_schema"].schema["advanced"].schema.schema


# -- the cloud backup's live status ------------------------------------------

WALL_CONNECTOR = "1457768-02-G--WC0123456789"
LIVE = {"response": {
    "solar_power": 1234.44, "battery_power": -500, "grid_power": 20.0, "load_power": 754.4,
    "percentage_charged": 61.23, "storm_mode_active": False, "grid_services_active": False,
    "wall_connectors": [
        {"din": WALL_CONNECTOR, "vin": "5YJ3E1EA0KF000000", "wall_connector_state": 2,
         "wall_connector_fault_state": 2, "wall_connector_power": 7200.44},
        "junk", {"din": ""}]}}


def _live(self, force=False):
    return LIVE


def test_live_time() -> None:
    read = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)
    assert live_time({"timestamp": "2026-11-11T11:25:00+10:30"}, read) == datetime(2026, 11, 11, 0, 55, tzinfo=UTC)
    assert live_time({"timestamp": "2026-11-11T00:58:00Z"}, read) == datetime(2026, 11, 11, 0, 58, tzinfo=UTC)
    # Never later than the read; no time zone, junk or none: the read.
    assert live_time({"timestamp": "2026-11-11T02:00:00Z"}, read) == read
    for live in ({"timestamp": "2026-11-11T00:58:00"}, {"timestamp": "soon"}, {"timestamp": 5}, {}, None):
        assert live_time(live, read) == read


def test_live_status_helpers() -> None:
    live = LIVE["response"]
    assert wall_connectors(live) == {WALL_CONNECTOR: {"power": 7200.4, "state": 2, "fault_state": 2}}
    assert wall_connectors({}) == {} and wall_connectors({"wall_connectors": "x"}) == {}
    assert wall_connectors({"wall_connectors": [{"din": "a", "wall_connector_power": None}]}) == {
        "a": {"power": None, "state": None, "fault_state": None}}
    assert live_readings(live) == {"solar_power": 1234.4, "battery_power": -500.0, "grid_power": 20.0,
                                   "home_power": 754.4, "battery_level": 61.2}
    assert live_readings({"solar_power": None, "grid_power": True}) == {}


async def test_wall_connector_sensors(hass: HomeAssistant, fake_powerwall) -> None:
    with patch.object(FakePowerwall, "_site_power", _live):
        entry = await _setup(hass, {**V1R, **ACCOUNT})
        power = hass.states.get(_eid(hass, "sensor", f"wall_connector_{WALL_CONNECTOR}_power"))
        assert power.state == "7200.4" and power.attributes["unit_of_measurement"] == "W"
        assert "6789" in power.attributes["friendly_name"]
        assert hass.states.get(_eid(hass, "sensor", f"wall_connector_{WALL_CONNECTOR}_state")).state == "2"
        fault = er.async_get(hass).async_get(_eid(hass, "sensor", f"wall_connector_{WALL_CONNECTOR}_fault_state"))
        assert fault.disabled_by is not None
        # The live status is read at most once a few minutes.
        client = cloud(fake_powerwall).client
        reads = []
        client.get_site_power = lambda force=False: reads.append(1) or LIVE
        await _refresh(hass, entry)
        assert not reads
        # The vehicle's VIN never leaves the cloud client.
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert "5YJ3E1EA0KF000000" not in str(diagnostics) and WALL_CONNECTOR not in str(diagnostics)
        assert diagnostics["data"]["wall_connectors"] == [{"power": 7200.4, "state": 2, "fault_state": 2}]
        # Gone from the live status: unavailable, not a stale reading.
        entry.runtime_data.coordinator.backends[0].live = None
        client.get_site_power = lambda force=False: {"response": {"wall_connectors": []}}
        await _refresh(hass, entry)
        assert hass.states.get(_eid(hass, "sensor", f"wall_connector_{WALL_CONNECTOR}_power")).state == "unavailable"


async def test_wall_connector_through_the_fleet_api(hass: HomeAssistant, fake_powerwall, fleet) -> None:
    """The Owner API comes first and reports no Wall Connector; once it
    fails, the Fleet API sign-in is asked, for the extras and the live status."""
    fleet.live = dict(LIVE["response"])
    entry = await _setup_fleet(hass, {**V1R, **TESLEMETRY, **ACCOUNT}, "owner_then_fleet")
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["wall_connectors"] == {}
    owner = coordinator.backend("owner_api")
    owner.failures = 3
    for backend in coordinator.backends:
        backend.extras = None
    await _refresh(hass, entry)
    assert coordinator.data["cloud_extras"]["backend"] == "tesla_fleet"
    assert coordinator.backend("tesla_fleet").live is not None and owner.live is None
    assert hass.states.get(_eid(hass, "sensor", f"wall_connector_{WALL_CONNECTOR}_power")).state == "7200.4"


async def test_no_live_status_read_without_a_wall_connector(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["wall_connectors"] == {}
    assert coordinator.backends[0].live is None  # nothing needed it


async def test_no_wall_connectors_without_a_cloud_backup(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    assert entry.runtime_data.coordinator.data["wall_connectors"] == {}


async def test_cloud_readings_while_the_gateway_is_down(hass: HomeAssistant, fake_powerwall) -> None:
    with patch.object(FakePowerwall, "_site_power", _live):
        entry = await _setup(hass, {**V1R, **ACCOUNT}, {"cloud_readings": True})
        solar = hass.states.get(_eid(hass, "sensor", "solar_power"))
        assert solar.state == "3000.0" and "source" not in solar.attributes  # the gateway's own

        fake_powerwall.fail_local = True
        local(fake_powerwall).power = _down
        await _refresh(hass, entry)
        coordinator = entry.runtime_data.coordinator
        assert coordinator.data["readings_source"] == "cloud"
        solar = hass.states.get(_eid(hass, "sensor", "solar_power"))
        assert solar.state == "1234.4" and solar.attributes["source"] == "cloud"
        # How old the cloud's reading is: when it was read (no timestamp in it).
        assert solar.attributes["last_updated"] == coordinator.backends[0].live_at.isoformat()
        assert hass.states.get(_eid(hass, "sensor", "battery_level")).state == "61.2"
        assert hass.states.get(_eid(hass, "sensor", "battery_charge_power")).state == "500.0"
        assert hass.states.get(_eid(hass, "sensor", "home_power")).state == "754.4"
        # Only what the live status has: no energy counters, no raw state of energy.
        assert hass.states.get(_eid(hass, "sensor", "grid_energy_imported")).state == "unavailable"
        assert hass.states.get(_eid(hass, "sensor", "battery_soe")).state == "unavailable"
        # The flows are the gateway's readings only.
        assert coordinator.flows.last is None


async def test_no_cloud_readings_unless_the_option_is_on(hass: HomeAssistant, fake_powerwall) -> None:
    with patch.object(FakePowerwall, "_site_power", _live):
        entry = await _setup(hass, {**V1R, **ACCOUNT})
        fake_powerwall.fail_local = True
        local(fake_powerwall).power = _down
        await _refresh(hass, entry)
        assert "readings_source" not in entry.runtime_data.coordinator.data
        assert hass.states.get(_eid(hass, "sensor", "solar_power")).state == "unavailable"


async def test_cloud_live_read_failing(hass: HomeAssistant, fake_powerwall) -> None:
    """The live status can't be read: no readings, no error."""
    entry = await _setup(hass, {**V1R, **ACCOUNT}, {"cloud_readings": True})
    fake_powerwall.fail_local = True
    local(fake_powerwall).power = _down
    cloud(fake_powerwall).client.get_site_power = lambda force=False: _down()
    await _refresh(hass, entry)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.last_update_success and "readings_source" not in coordinator.data
    assert hass.states.get(_eid(hass, "sensor", "solar_power")).state == "unavailable"


async def test_setting_change_shows_at_once_with_fast_polling(hass: HomeAssistant, fake_powerwall) -> None:
    """The refresh after a write is a full read, not a fast one: the new
    setting shows straight away."""
    entry = await _setup(hass, V1R, {"power_interval": 5})
    eid = _eid(hass, "select", "operation_mode")
    await hass.services.async_call("select", "select_option", {"entity_id": eid, "option": "backup"}, blocking=True)
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator.data["full_read"] is True
    assert hass.states.get(eid).state == "backup"
