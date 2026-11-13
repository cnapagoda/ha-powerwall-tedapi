"""2.4.0: battery health per pack (against the rated 13.5 kWh), and the
Powerwall 3 wear counters (pypowerwall 0.23+)."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.tesla_tedapi.coordinator import _extract_packs, name_packs
from custom_components.tesla_tedapi.sensor import _cycles, _health, _health_attrs, _pct

from .conftest import local
from .test_storm import V1R, _eid, _refresh, _setup

SERIAL = "TG0123456789AB"
WEAR = {"BMS_kwhDischargeTotal": 2969.767, "BMS_LOG_numFullBattCycles": 0,
        "BMS_LOG_overDischargeCounter": 2, "BMS_LOG_cacAvg": 171.2}


def _with_wear(fake, **fields) -> None:
    """The gateway's TEPOD block for the main pack, plus the wear fields."""
    pw = local(fake)
    original = pw.vitals

    def vitals():
        v = original()
        key = next(k for k in v if k.startswith("TEPOD--") and k.endswith(SERIAL))
        v[key] = {**v[key], **WEAR, **fields}
        return v

    pw.vitals = vitals


def _state(hass: HomeAssistant, key: str):
    return hass.states.get(_eid(hass, "sensor", key))


async def test_health_against_rated_capacity(hass: HomeAssistant, fake_powerwall) -> None:
    """A new pack holds more than the rated 13.5 kWh, so it reads above 100 %."""
    entry = await _setup(hass, V1R)
    pack = _state(hass, f"pack_{SERIAL}_health")
    assert pack.state == "103.0"  # 13.9 kWh
    assert pack.attributes["nominal_kwh"] == 13.5
    assert pack.attributes["warranty_kwh"] == 9.45

    pw = local(fake_powerwall)
    original = pw.vitals

    def worn():
        v = original()
        key = next(k for k in v if k.startswith("TEPOD--"))
        v[key] = {**v[key], "POD_nom_full_pack_energy": 12150}
        return v

    pw.vitals = worn
    await _refresh(hass, entry)
    assert _state(hass, f"pack_{SERIAL}_health").state == "90.0"


async def test_wear_sensors_when_the_gateway_reports_them(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    assert _eid(hass, "sensor", f"pack_{SERIAL}_discharged") is None  # not reported: no sensor

    _with_wear(fake_powerwall)
    await _refresh(hass, entry)
    discharged = _state(hass, f"pack_{SERIAL}_discharged")
    assert discharged.state == "2969.8"
    assert discharged.attributes["state_class"] == "total_increasing"
    cycles = _state(hass, f"pack_{SERIAL}_cycles")
    assert cycles.state == "220.0"  # 2969.8 kWh / 13.5 kWh
    assert cycles.attributes["bms_full_cycles"] == 0
    assert cycles.attributes["over_discharges"] == 2
    health = _state(hass, "battery_health")
    assert health.attributes["lifetime_discharged_kwh"] == 2969.8
    assert health.attributes["warranty_kwh"] == 9.45


def test_extract_wear_fields() -> None:
    packs = _extract_packs({
        "TEPOD--1707000-11-J--A1": {"POD_nom_full_pack_energy": 14000, **WEAR},
        "TEPOD--1807000-20-B--B2": {"POD_nom_full_pack_energy": 14300, "BMS_LOG_numFullBattCycles": True},
    })
    assert packs["A1"]["discharged_kwh"] == 2969.8
    assert packs["A1"]["bms_cycles"] == 0 and packs["A1"]["over_discharges"] == 2
    assert packs["B2"]["discharged_kwh"] is None and packs["B2"]["bms_cycles"] is None


def test_health_helpers() -> None:
    assert _health({}) is None
    data = {"battery_full_kwh": 28.4, "packs": {
        "a": {"full_kwh": 14.0, "discharged_kwh": 100.0},
        "b": {"full_kwh": 14.4}}}
    assert _health(data) == 105.2  # 28.4 of 2 × 13.5 kWh, from the packs
    # The system total also counts a follower Powerwall 3 the integration
    # can't see: health still comes from the visible packs (was ~160 %).
    assert _health({**data, "battery_full_kwh": 43.2}) == 105.2
    # Packs without their own capacity: the system total, as before.
    assert _health({"battery_full_kwh": 27.0, "packs": {"a": {}, "b": {"full_kwh": None}}}) == 100.0
    attrs = _health_attrs(data)
    assert attrs["warranty_kwh"] == 18.9 and attrs["packs"] == 2 and attrs["packs_read"] == 2
    # The gateway's config lists a third pack (a follower Powerwall 3) that
    # isn't in the readings: the system total over all three packs.
    hidden = {**data, "battery_full_kwh": 43.2, "pack_count": 3}
    assert _health(hidden) == 106.7  # 43.2 of 3 × 13.5 kWh
    attrs = _health_attrs(hidden)
    assert attrs["packs"] == 3 and attrs["packs_read"] == 2 and attrs["warranty_kwh"] == 28.35
    assert _health({**hidden, "battery_full_kwh": None}) == 105.2  # no total: the packs read
    # The config lists no more packs than are read (or can't be read): as before.
    for listed in (2, 1, None, True):
        assert _health({**data, "battery_full_kwh": 43.2, "pack_count": listed}) == 105.2
    attrs = _health_attrs(data)
    assert attrs["lifetime_discharged_kwh"] == 100.0
    assert _health_attrs({})["lifetime_discharged_kwh"] is None
    assert _pct(None, 10) is None and _pct(5, 0) is None
    assert _cycles(None) is None


FOLLOWER = "TG0FOLLOWER001"


def test_name_packs() -> None:
    """Packs of one model get names of their own, the leader's first, and
    keep them whatever else is in the readings."""
    def packs(*serials: str, name: str = "Powerwall 3") -> dict:
        return {serial: {"name": name} for serial in serials}

    din = "1707000-11-J--TG2"
    names: dict[str, str] = {}
    named = name_packs(packs("TG1", "TG2", "TG3"), names, din)
    assert {sn: p["name"] for sn, p in named.items()} == {
        "TG2": "Powerwall 3", "TG1": "Powerwall 3 (2)", "TG3": "Powerwall 3 (3)"}
    # A follower alone (the leader's pack missing from this reading), another
    # model, and a pack that is new: nobody's name changes.
    assert name_packs(packs("TG3"), names, din)["TG3"]["name"] == "Powerwall 3 (3)"
    more = name_packs({**packs("TG1", "TG4"), **packs("EX2", "EX1", name="Expansion Pack")}, names, din)
    assert {sn: p["name"] for sn, p in more.items()} == {
        "TG1": "Powerwall 3 (2)", "TG4": "Powerwall 3 (4)", "EX1": "Expansion Pack", "EX2": "Expansion Pack (2)"}
    assert names == {"TG2": "Powerwall 3", "TG1": "Powerwall 3 (2)", "TG3": "Powerwall 3 (3)",
                     "TG4": "Powerwall 3 (4)", "EX1": "Expansion Pack", "EX2": "Expansion Pack (2)"}
    # One of each: as before. No DIN: by serial.
    single = name_packs({"A": {"name": "Powerwall 3"}, "B": {"name": "Expansion Pack"}}, {}, None)
    assert [p["name"] for p in single.values()] == ["Powerwall 3", "Expansion Pack"]
    assert [p["name"] for p in name_packs(packs("B", "A"), {}, None).values()] == ["Powerwall 3 (2)", "Powerwall 3"]
    # A follower seen before the leader keeps the name it was given.
    late: dict[str, str] = {}
    name_packs(packs("TG1"), late, din)
    assert name_packs(packs("TG1", "TG2"), late, din)["TG2"]["name"] == "Powerwall 3 (2)"


def test_a_replaced_pack_takes_the_name_back() -> None:
    """A replaced pack (more packs known than the config lists) gives up its
    name; a pack only missing from the readings keeps it."""
    din = "1707000-11-J--PW3"
    names: dict[str, str] = {}
    name_packs({"PW3": {"name": "Powerwall 3"}, "EXOLD": {"name": "Expansion Pack"}}, names, din, 2)
    # Missing from one reading, still in the config: kept.
    name_packs({"PW3": {"name": "Powerwall 3"}}, names, din, 2)
    assert names == {"PW3": "Powerwall 3", "EXOLD": "Expansion Pack"}
    # Replaced: the new pack is "Expansion Pack", not "(2)".
    named = name_packs({"PW3": {"name": "Powerwall 3"}, "EXNEW": {"name": "Expansion Pack"}}, names, din, 2)
    assert named["EXNEW"]["name"] == "Expansion Pack"
    assert names == {"PW3": "Powerwall 3", "EXNEW": "Expansion Pack"}
    # Config unreadable (no count): nothing is given up.
    name_packs({"PW3": {"name": "Powerwall 3"}, "EX3": {"name": "Expansion Pack"}}, names, din, None)
    assert names["EXNEW"] == "Expansion Pack" and names["EX3"] == "Expansion Pack (2)"


async def test_two_powerwall_3s_are_told_apart(hass: HomeAssistant, fake_powerwall) -> None:
    """Both were "Powerwall 3": same sensor names, one of them missing from
    the attributes, and one pack's alerts dropped when both had one."""
    fake_powerwall.extra_packs = {f"TEPOD--1707000-11-J--{FOLLOWER}": {
        "POD_nom_full_pack_energy": 13700, "POD_nom_energy_remaining": 6000, "alerts": ["FollowerPackAlert"]}}
    entry = await _setup(hass, V1R)
    _with_wear(fake_powerwall, alerts=["LeaderPackAlert"])
    await _refresh(hass, entry)
    leader, follower = _state(hass, f"pack_{SERIAL}_capacity"), _state(hass, f"pack_{FOLLOWER}_capacity")
    assert "Powerwall 3 capacity" in leader.name and "(2)" not in leader.name
    assert "Powerwall 3 (2) capacity" in follower.name
    assert _state(hass, "alerts").attributes["pack_alerts"] == {
        "Powerwall 3": ["LeaderPackAlert"], "Powerwall 3 (2)": ["FollowerPackAlert"]}
    sources = {d["alert"]: d["sources"] for d in entry.runtime_data.coordinator.data["alert_details"]}
    assert sources["LeaderPackAlert"] == ["Powerwall 3"]
    assert sources["FollowerPackAlert"] == ["Powerwall 3 (2)"]
    capacity = _state(hass, "battery_full_capacity").attributes
    assert capacity["Powerwall 3"]["serial"] == SERIAL and capacity["Powerwall 3 (2)"]["serial"] == FOLLOWER
    # Saved, so the names are the same after a restart; the follower keeps
    # its name while the leader's pack is missing from a reading.
    coordinator = entry.runtime_data.coordinator
    assert coordinator._state_to_save()["pack_names"] == {SERIAL: "Powerwall 3", FOLLOWER: "Powerwall 3 (2)"}
    pw = local(fake_powerwall)
    full = pw.vitals
    pw.vitals = lambda: {k: v for k, v in full().items() if not k.endswith(SERIAL) or not k.startswith("TEPOD--")}
    await _refresh(hass, entry)
    assert [p["name"] for p in coordinator.data["packs"].values()] == ["Powerwall 3 (2)"]
