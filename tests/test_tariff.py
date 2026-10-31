"""Tesla tariff (optional, cloud only): formats, current period, validation,
the option, the sensor and the get/set actions."""
from __future__ import annotations

import copy
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import tariff
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN, TARIFF, cloud

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
ACCOUNT = {"owners_email": "a@b.c", "owners_authpath": "/tmp", "site_id": 42}

# The same tariff in Tesla's write format (tariff_content_v2), peak raised to 60c.
V2 = {
    "name": "ZeroHero", "utility": "GloBird",
    "seasons": {"Year": {"fromMonth": 1, "toMonth": 12, "fromDay": 1, "toDay": 31, "tou_periods": {
        name: {"periods": ranges} for name, ranges in TARIFF["seasons"]["Year"]["tou_periods"].items()}}},
    "energy_charges": {"ALL": {"rates": {"ALL": 0}},
                       "Year": {"rates": {"OFF_PEAK": 0.0, "ON_PEAK": 0.60, "PARTIAL_PEAK": 0.44}}},
    "sell_tariff": {"energy_charges": {"ALL": {"rates": {"ALL": 0}},
                                       "Year": {"rates": {"OFF_PEAK": 0.0, "ON_PEAK": 0.02, "PARTIAL_PEAK": 0.0}}}},
}


# -- pure functions -----------------------------------------------------------

@pytest.mark.parametrize(("hour", "period", "buy", "sell"), [
    (12, "OFF_PEAK", 0.0, 0.0),
    (17, "ON_PEAK", 0.583, 0.02),
    (1, "PARTIAL_PEAK", 0.44, 0.0),   # over midnight
    (15, "PARTIAL_PEAK", 0.44, 0.0),
])
def test_current_period_and_rates(hour, period, buy, sell) -> None:
    now = datetime(2026, 11, 2, hour, 30)
    got = tariff.current(tariff.normalize(TARIFF), now)
    assert got == {"season": "Year", "period": period, "buy_rate": buy, "sell_rate": sell}


def test_v2_normalises_like_v1() -> None:
    v1, v2 = tariff.normalize(TARIFF), tariff.normalize(V2)
    assert v2["seasons"] == v1["seasons"]
    assert v2["buy"]["Year"]["ON_PEAK"] == 0.60 and v1["buy"]["Year"]["ON_PEAK"] == 0.583
    assert not tariff.same_rates(v1, v2)
    assert tariff.same_rates(v2, tariff.normalize(copy.deepcopy(V2)))
    assert not tariff.same_rates(v1, None)


def test_seasons_weekdays_and_edge_cases() -> None:
    summer = {"fromMonth": 12, "toMonth": 2, "fromDay": 1, "toDay": 28, "tou_periods": {
        "ON_PEAK": [{"fromDayOfWeek": 0, "toDayOfWeek": 4, "fromHour": 16, "toHour": 21}]}}
    winter = {"fromMonth": 3, "toMonth": 11, "fromDay": 1, "toDay": 30, "tou_periods": {
        "ALL_DAY": [{"fromDayOfWeek": 5, "toDayOfWeek": 1, "fromHour": 0, "toHour": 0}]}}
    norm = tariff.normalize({"code": "X", "seasons": {"Summer": summer, "Winter": winter},
                             "energy_charges": {"Summer": {"ON_PEAK": 0.5}, "Winter": {"ALL": 0.3}}})
    assert norm["name"] == "X"
    # 7 Jan 2026 is a Wednesday: summer (wraps the new year), weekday peak.
    assert tariff.current(norm, datetime(2026, 1, 7, 17))["buy_rate"] == 0.5
    # 10 Jan 2026 is a Saturday: no weekend peak in summer -> no period, no rate.
    assert tariff.current(norm, datetime(2026, 1, 10, 17))["period"] is None
    # Winter Sunday: weekend range wraps Sat..Mon, the whole day.
    got = tariff.current(norm, datetime(2026, 6, 7, 3))
    assert got["season"] == "Winter" and got["period"] == "ALL_DAY" and got["buy_rate"] == 0.3
    # Winter Wednesday: outside the Sat..Mon range.
    assert tariff.current(norm, datetime(2026, 6, 10, 3))["period"] is None
    # One season without dates: used for any day.
    flat = tariff.normalize({"energy_charges": {"Only": {"ALL": 0.25}}})
    assert tariff.current(flat, datetime(2026, 6, 7, 3))["buy_rate"] == 0.25
    assert tariff.current(None, datetime(2026, 6, 7, 3))["buy_rate"] is None
    assert tariff.normalize({}) is None and tariff.normalize("x") is None
    odd = tariff.normalize({"seasons": {"S": "bad"}, "energy_charges": {"S": "bad", "T": {"P": True}}})
    assert odd["seasons"] == {} and odd["buy"] == {"T": {}}


@pytest.mark.parametrize(("payload", "message"), [
    ("nope", "must be an object"),
    (TARIFF, "format get_tariff returns"),
    ({"optimization_strategy": "economics"}, "tariff_content_v2 is missing"),
    ({"tariff_content_v2": []}, "must be an object"),
    ({"tariff_content_v2": {"seasons": {}}}, "seasons must be a non-empty object"),
    ({"tariff_content_v2": {"seasons": {"Y": {}}, "energy_charges": {"Y": {"ON_PEAK": 1}}}},
     "needs a \"rates\" object"),
])
def test_validate_write_rejects(payload, message) -> None:
    assert any(message in e for e in tariff.validate_write(payload))


def test_validate_write_accepts_v2() -> None:
    assert tariff.validate_write({"tariff_content_v2": V2}) == []


# -- option, sensor and actions ----------------------------------------------

async def _setup(hass: HomeAssistant, data: dict, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data, options=options or {}, title="Powerwall")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _eid(hass: HomeAssistant) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DIN}_tesla_tariff")


async def _call(hass: HomeAssistant, service: str, **data):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=True)


async def test_option_off_no_sensor_and_actions_refuse(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, {**V1R, **ACCOUNT})
    assert _eid(hass) is None
    with pytest.raises(ServiceValidationError):
        await _call(hass, "get_tariff")


async def test_option_needs_a_cloud_backup(hass: HomeAssistant, fake_powerwall) -> None:
    """Without a cloud backup the option has no effect."""
    entry = await _setup(hass, V1R, {"tesla_tariff": True})
    assert entry.runtime_data.coordinator.tariff_enabled is False
    assert _eid(hass) is None


async def test_sensor_and_get_tariff(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, {**V1R, **ACCOUNT}, {"tesla_tariff": True})
    state = hass.states.get(_eid(hass))
    assert state.state == "ZeroHero"
    assert state.attributes["utility"] == "GloBird"
    assert state.attributes["season"] == "Year"
    assert state.attributes["backend"] == "Owner API"
    assert state.attributes["tariff"]["code"] == "GB-ZH"
    from custom_components.tesla_tedapi.sensor import TeslaTariffSensor  # noqa: PLC0415
    assert "tariff" in TeslaTariffSensor._unrecorded_attributes

    resp = await _call(hass, "get_tariff")
    assert resp["backend"] == "Owner API" and resp["tariff"]["name"] == "ZeroHero"


async def test_set_tariff_dry_run_and_validation(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, {**V1R, **ACCOUNT}, {"tesla_tariff": True})
    resp = await _call(hass, "set_tariff", tou_settings={"tariff_content_v2": V2})
    assert resp == {"dry_run": True, "would_send": {"tariff_content_v2": V2}, "backend": "Owner API"}
    assert not [w for w in cloud(fake_powerwall).writes if w[0] == "tariff"]
    with pytest.raises(ServiceValidationError):
        await _call(hass, "set_tariff", tou_settings=TARIFF, dry_run=False)


async def test_set_tariff_writes_and_confirms(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT}, {"tesla_tariff": True})
    resp = await _call(hass, "set_tariff", tou_settings={"tariff_content_v2": V2}, dry_run=False)
    assert resp == {"dry_run": False, "backend": "Owner API", "confirmed": True}
    assert ("tariff", {"tariff_content_v2": V2}) in cloud(fake_powerwall).writes
    await hass.async_block_till_done()
    assert hass.states.get(_eid(hass)).attributes["tariff"]["energy_charges"]["Year"]["rates"]["ON_PEAK"] == 0.60
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"tariff_write_{entry.entry_id}") is None


async def test_set_tariff_mismatch_raises_repair(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT}, {"tesla_tariff": True})
    fake_powerwall.tariff_applied = TARIFF  # Tesla keeps the old rates
    resp = await _call(hass, "set_tariff", tou_settings={"tariff_content_v2": V2}, dry_run=False)
    assert resp["confirmed"] is False
    issue_id = f"tariff_write_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    # A later write that matches clears it.
    fake_powerwall.tariff_applied = None
    resp = await _call(hass, "set_tariff", tou_settings={"tariff_content_v2": V2}, dry_run=False)
    assert resp["confirmed"] is True
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_set_tariff_refused_and_read_failures(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, {**V1R, **ACCOUNT}, {"tesla_tariff": True})
    fake_powerwall.tariff_writes_ok = False
    with pytest.raises(HomeAssistantError):
        await _call(hass, "set_tariff", tou_settings={"tariff_content_v2": V2}, dry_run=False)
    fake_powerwall.cloud_reads_fail = True
    with pytest.raises(HomeAssistantError):
        await _call(hass, "get_tariff")


async def test_write_ok_but_read_back_fails(hass: HomeAssistant, fake_powerwall) -> None:
    """Accepted but unreadable afterwards: not confirmed, repair raised."""
    entry = await _setup(hass, {**V1R, **ACCOUNT}, {"tesla_tariff": True})
    pw = cloud(fake_powerwall)
    original = pw.set_tariff

    def write_then_break(tou, jsonformat=False):
        result = original(tou)
        fake_powerwall.cloud_reads_fail = True
        return result

    pw.set_tariff = write_then_break
    resp = await _call(hass, "set_tariff", tou_settings={"tariff_content_v2": V2}, dry_run=False)
    assert resp["confirmed"] is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"tariff_write_{entry.entry_id}") is not None


async def test_periodic_read_failure_keeps_last_value(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT}, {"tesla_tariff": True})
    coordinator = entry.runtime_data.coordinator
    fake_powerwall.cloud_reads_fail = True
    coordinator._tariff_at = None  # due now
    await coordinator.async_refresh()
    assert hass.states.get(_eid(hass)).state == "ZeroHero"


async def test_sensor_without_name_and_unreadable_tariff(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT}, {"tesla_tariff": True})
    coordinator = entry.runtime_data.coordinator
    coordinator.tariff = {"seasons": {}}  # a tariff with no name
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(_eid(hass)).state == "unknown"
    coordinator.tariff = "garbage"
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert "utility" not in hass.states.get(_eid(hass)).attributes


async def test_cloud_errors_on_read_and_write(hass: HomeAssistant, fake_powerwall) -> None:
    """A client error on read or write is collected and reported; a cloud
    timeout on write stops at once (it may still have been applied)."""
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.tesla_tedapi.fleet import FleetTimeout  # noqa: PLC0415

    entry = await _setup(hass, {**V1R, **ACCOUNT}, {"tesla_tariff": True})
    coordinator = entry.runtime_data.coordinator
    client = coordinator.backends[0].get_client()
    with patch.object(type(client), "get_tariff", side_effect=RuntimeError("boom")), \
            pytest.raises(HomeAssistantError, match="boom"):
        await _call(hass, "get_tariff")
    with patch.object(type(client), "set_tariff", side_effect=RuntimeError("bad")), \
            pytest.raises(HomeAssistantError, match="bad"):
        await _call(hass, "set_tariff", tou_settings={"tariff_content_v2": V2}, dry_run=False)
    with patch.object(type(client), "set_tariff", side_effect=FleetTimeout("slow")), \
            pytest.raises(HomeAssistantError):
        await _call(hass, "set_tariff", tou_settings={"tariff_content_v2": V2}, dry_run=False)


async def test_no_cloud_backup_refuses(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    with pytest.raises(HomeAssistantError):
        await entry.runtime_data.coordinator.async_read_tariff()


@pytest.mark.parametrize(("data", "offered"), [({**V1R, **ACCOUNT}, True), (V1R, False)])
async def test_option_offered_only_with_cloud(hass: HomeAssistant, fake_powerwall, data, offered) -> None:
    entry = await _setup(hass, data)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    assert ("tesla_tariff" in result["data_schema"].schema) is offered
