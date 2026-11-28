"""2.9.0 (#24): one device per Powerwall, opt-in. Each Powerwall and
Expansion Pack gets a device of its own, linked to the gateway's, with its
pack, fan, solar input and inverter temperature sensors. Unique ids and
entity ids never change; switching the option off puts the entities back on
the gateway's device and removes the per-unit devices."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.tesla_tedapi.const import DOMAIN
from custom_components.tesla_tedapi.coordinator import unit_serials

from .conftest import DIN, FakePowerwall
from .test_storm import V1R, _refresh, _setup

LEADER = DIN.rsplit("--", 1)[-1]
FOLLOWER_DIN = "1707000-11-J--TG9876543210ZZ"
FOLLOWER = FOLLOWER_DIN.rsplit("--", 1)[-1]
EXPANSION_DIN = "1807000-20-B--TG4444444444XP"
EXPANSION = EXPANSION_DIN.rsplit("--", 1)[-1]
BLOCKS = [{"vin": DIN, "type": "Powerwall3", "battery_expansions": [{"din": EXPANSION_DIN}]},
          {"vin": FOLLOWER_DIN, "type": "Powerwall3"}]
FOLLOWER_VITALS = {
    f"TEPINV--{FOLLOWER_DIN}": {"PINV_Fout": 50.0, "PCH_AmbientTemp": 27.5, "PCH_heatsinkTemp": 33.0,
                                "PCH_FanSpeed_A": 1200, "PCH_FanSpeed_B": 1210},
    f"TEPOD--{FOLLOWER_DIN}": {"POD_nom_full_pack_energy": 13800, "POD_nom_energy_remaining": 6000,
                               "HVP_PackTempMax": 23.0, "HVP_PackTempMin": 21.0},
    f"PVAC--{FOLLOWER_DIN}": {"PVAC_PvState_A": "PV_Active", "PVAC_PVMeasuredVoltage_A": 370.0,
                              "PVAC_PVCurrent_A": 4.0, "PVAC_PVMeasuredPower_A": 1480.0},
    f"TEPOD--{EXPANSION_DIN}": {"POD_nom_full_pack_energy": 13600, "POD_nom_energy_remaining": 6500,
                                "HVP_PackTempMax": 22.0, "HVP_PackTempMin": 20.0},
}


def test_unit_serials_number_like_the_readings() -> None:
    serials = unit_serials({**FakePowerwall().vitals(), **FOLLOWER_VITALS}, DIN)
    assert serials["string_A"] == serials["fan_A"] == serials["fan_B"] == serials["inverter"] == LEADER
    assert serials["string_2A"] == serials["fan_2A"] == serials["fan_2B"] == serials["inverter_2"] == FOLLOWER
    assert unit_serials({}, DIN) == {}
    # A Powerwall 2 inverter's only fan is numbered by its PVAC block.
    assert unit_serials({"PVAC--3012170-05-B--PW2SERIAL": {}}, None)["fan_1"] == "PW2SERIAL"


def test_unit_names() -> None:
    """On a unit's device the name drops what the device name already says."""
    from custom_components.tesla_tedapi.sensor import (  # noqa: PLC0415
        INVERTER_TEMPERATURES,
        _fan_sensor,
        _inverter_temp_sensors,
        _pack_sensors,
        _string_sensors,
        _unit_named,
    )

    assert _unit_named(_fan_sensor("1")).translation_key == "unit_fan_speed"  # a Powerwall 2's only fan
    assert _unit_named(_fan_sensor("1")).translation_placeholders is None
    assert _unit_named(_fan_sensor("2B")).translation_placeholders == {"fan": "B"}
    assert _unit_named(_string_sensors("3C")[0]).translation_placeholders == {"string": "C"}
    assert _unit_named(_inverter_temp_sensors("2")[1]).translation_key == "inverter_heatsink_temperature"
    assert _unit_named(_pack_sensors("SN", "Expansion Pack")[0]).translation_key == "unit_pack_health"
    assert _unit_named(INVERTER_TEMPERATURES[0]) is INVERTER_TEMPERATURES[0]


def _entity(hass: HomeAssistant, key: str) -> er.RegistryEntry:
    registry = er.async_get(hass)
    return registry.async_get(registry.async_get_entity_id("sensor", DOMAIN, f"{DIN}_{key}"))


def _device(hass: HomeAssistant, identifier: str) -> dr.DeviceEntry | None:
    return dr.async_get(hass).async_get_device(identifiers={(DOMAIN, identifier)})


# The sensors of each unit, by unique id key.
LEADER_KEYS = [f"pack_{LEADER}_health", f"pack_{LEADER}_capacity", f"pack_{LEADER}_temperature",
               "fan_A_speed", "fan_B_speed", "string_A_power", "inverter_ambient_temperature"]
FOLLOWER_KEYS = [f"pack_{FOLLOWER}_health", "fan_2A_speed", "string_2A_power",
                 "inverter_2_ambient_temperature", "inverter_2_heatsink_temperature"]
EXPANSION_KEYS = [f"pack_{EXPANSION}_health", f"pack_{EXPANSION}_capacity"]


async def _set_option(hass: HomeAssistant, entry, on: bool) -> None:
    hass.config_entries.async_update_entry(entry, options={**entry.options, "unit_devices": on})
    await hass.async_block_till_done()


async def test_off_by_default_everything_on_the_gateway(hass: HomeAssistant, fake_powerwall) -> None:
    FakePowerwall.battery_blocks = BLOCKS
    FakePowerwall.extra_packs = FOLLOWER_VITALS
    await _setup(hass, V1R)
    gateway = _device(hass, DIN)
    for key in LEADER_KEYS + FOLLOWER_KEYS + EXPANSION_KEYS:
        assert _entity(hass, key).device_id == gateway.id, key
    assert _device(hass, f"{DIN}_{LEADER}") is None
    assert len(dr.async_entries_for_config_entry(dr.async_get(hass), _entity(hass, "fan_A_speed").config_entry_id)) == 1


async def test_on_off_on(hass: HomeAssistant, fake_powerwall) -> None:
    FakePowerwall.battery_blocks = BLOCKS
    FakePowerwall.extra_packs = FOLLOWER_VITALS
    entry = await _setup(hass, V1R)
    gateway = _device(hass, DIN)
    entity_ids = {key: _entity(hass, key).entity_id for key in LEADER_KEYS + FOLLOWER_KEYS + EXPANSION_KEYS}

    # On: a device per Powerwall and Expansion Pack, linked to the gateway.
    await _set_option(hass, entry, True)
    leader, follower, expansion = (_device(hass, f"{DIN}_{sn}") for sn in (LEADER, FOLLOWER, EXPANSION))
    assert leader.name == "Powerwall 3" and follower.name == "Powerwall 3 (2)"
    assert expansion.name == "Expansion Pack" and expansion.model == "Expansion Pack"
    assert leader.model == "Powerwall 3" and leader.model_id == "1707000-11-J" and leader.serial_number == LEADER
    assert {leader.via_device_id, follower.via_device_id, expansion.via_device_id} == {gateway.id}
    for keys, device in ((LEADER_KEYS, leader), (FOLLOWER_KEYS, follower), (EXPANSION_KEYS, expansion)):
        for key in keys:
            assert _entity(hass, key).device_id == device.id, key
    # Entity ids stay; the names say less, the device says which unit.
    assert {key: _entity(hass, key).entity_id for key in entity_ids} == entity_ids
    state = hass.states.get(entity_ids[f"pack_{FOLLOWER}_health"])
    assert state.attributes["friendly_name"] == "Powerwall 3 (2) Battery health"
    assert hass.states.get(entity_ids["fan_2A_speed"]).attributes["friendly_name"] == "Powerwall 3 (2) Fan A speed"
    assert (hass.states.get(entity_ids["string_2A_power"]).attributes["friendly_name"]
            == "Powerwall 3 (2) Solar input A power")
    assert (hass.states.get(entity_ids["inverter_2_ambient_temperature"]).attributes["friendly_name"]
            == "Powerwall 3 (2) Inverter ambient temperature")
    assert hass.states.get(entity_ids["fan_2A_speed"]).state == "1200.0"
    # System-wide sensors stay on the gateway.
    assert _entity(hass, "battery_level").device_id == gateway.id
    dr.async_get(hass).async_update_device(follower.id, area_id="garage", name_by_user="Shed Powerwall")

    # Off: back on the gateway, the per-unit devices removed, entity ids kept.
    await _set_option(hass, entry, False)
    for key, entity_id in entity_ids.items():
        assert _entity(hass, key).device_id == gateway.id, key
        assert _entity(hass, key).entity_id == entity_id
    assert _device(hass, f"{DIN}_{FOLLOWER}") is None
    assert (hass.states.get(entity_ids["fan_2A_speed"]).attributes["friendly_name"]
            == "Powerwall TEDAPI Fan 2A speed")

    # On again: the same devices come back.
    await _set_option(hass, entry, True)
    again = _device(hass, f"{DIN}_{FOLLOWER}")
    assert again.id == follower.id
    # Home Assistant keeps what the user set on a removed device.
    assert again.area_id == "garage" and again.name_by_user == "Shed Powerwall"
    assert _entity(hass, "fan_2A_speed").device_id == follower.id


async def test_a_pack_read_later_moves_its_sensors(hass: HomeAssistant, fake_powerwall) -> None:
    """A fan or solar input whose Powerwall has no battery pack in the
    readings yet (a follower read after the leader) has no unit device to go
    to: it starts on the gateway's, and moves to its Powerwall's device when
    the pack is read, without a reload. A disabled sensor moves too."""
    FakePowerwall.battery_blocks = BLOCKS
    FakePowerwall.extra_packs = {k: v for k, v in FOLLOWER_VITALS.items() if not k.startswith("TEPOD--1707")}
    entry = await _setup(hass, V1R, {"unit_devices": True})
    gateway = _device(hass, DIN)
    entity_ids = {key: _entity(hass, key).entity_id for key in FOLLOWER_KEYS[1:] + ["fan_2B_speed"]}
    for key in entity_ids:
        assert _entity(hass, key).device_id == gateway.id, key
    assert hass.states.get(entity_ids["fan_2A_speed"]).attributes["friendly_name"] == "Powerwall TEDAPI Fan 2A speed"
    assert _entity(hass, "fan_A_speed").device_id == _device(hass, f"{DIN}_{LEADER}").id
    er.async_get(hass).async_update_entity(entity_ids["fan_2B_speed"], disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()

    FakePowerwall.extra_packs = FOLLOWER_VITALS
    await _refresh(hass, entry)
    follower = _device(hass, f"{DIN}_{FOLLOWER}")
    for key, entity_id in entity_ids.items():
        assert _entity(hass, key).device_id == follower.id, key
        assert _entity(hass, key).entity_id == entity_id
    assert _entity(hass, "fan_2B_speed").disabled_by is er.RegistryEntryDisabler.USER
    state = hass.states.get(entity_ids["fan_2A_speed"])
    assert state.attributes["friendly_name"] == "Powerwall 3 (2) Fan A speed"
    assert state.state == "1200.0"
    assert (hass.states.get(entity_ids["string_2A_power"]).attributes["friendly_name"]
            == "Powerwall 3 (2) Solar input A power")
    # Nothing waits any more: the next reading changes nothing.
    await _refresh(hass, entry)
    assert hass.states.get(entity_ids["fan_2A_speed"]).state == "1200.0"
    assert _entity(hass, "fan_2A_speed").device_id == follower.id


async def test_new_install_with_the_option_on(hass: HomeAssistant, fake_powerwall) -> None:
    """Set up with the option on: entity ids come from the unit's device name."""
    await _setup(hass, V1R, {"unit_devices": True})
    leader = _device(hass, f"{DIN}_{LEADER}")
    assert _entity(hass, f"pack_{LEADER}_health").entity_id == "sensor.powerwall_3_battery_health"
    assert _entity(hass, "fan_A_speed").entity_id == "sensor.powerwall_3_fan_a_speed"
    assert _entity(hass, "fan_A_speed").device_id == leader.id
    assert _entity(hass, "battery_level").entity_id == "sensor.powerwall_tedapi_battery_level"
