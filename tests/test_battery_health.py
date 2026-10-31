"""2.4.0: battery health per pack (against the rated 13.5 kWh), and the
Powerwall 3 wear counters (pypowerwall 0.23+)."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.tesla_tedapi.coordinator import _extract_packs
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
    assert _health(data) == 105.2  # 28.4 of 2 × 13.5 kWh
    attrs = _health_attrs(data)
    assert attrs["warranty_kwh"] == 18.9 and attrs["packs"] == 2
    assert attrs["lifetime_discharged_kwh"] == 100.0
    assert _health_attrs({})["lifetime_discharged_kwh"] is None
    assert _pct(None, 10) is None and _pct(5, 0) is None
    assert _cycles(None) is None
