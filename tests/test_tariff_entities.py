"""Tesla tariff as entities: the schedule the calendars are built from, the
buy and sell price sensors and the two calendars."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
import pytest

from custom_components.tesla_tedapi import tariff

from .conftest import TARIFF
from .test_storm import ACCOUNT, V1R, _eid, _setup

ADELAIDE = ZoneInfo("Australia/Adelaide")

# Weekday-only peak, and two seasons, one over the new year.
SEASONAL = {"name": "Seasonal", "seasons": {
    "Summer": {"fromMonth": 11, "toMonth": 3, "fromDay": 1, "toDay": 31, "tou_periods": {
        "ON_PEAK": [{"fromDayOfWeek": 0, "toDayOfWeek": 4, "fromHour": 15, "toHour": 21}],
        "OFF_PEAK": [{"fromDayOfWeek": 0, "toDayOfWeek": 6}]}},
    "Winter": {"fromMonth": 4, "toMonth": 10, "fromDay": 1, "toDay": 31, "tou_periods": {
        "OFF_PEAK": [{"fromDayOfWeek": 0, "toDayOfWeek": 6}]}}},
    "energy_charges": {"Summer": {"ON_PEAK": 0.5, "OFF_PEAK": 0.2}, "Winter": {"OFF_PEAK": 0.25}}}


@pytest.mark.parametrize(("raw", "start"), [
    (TARIFF, datetime(2026, 11, 1, 7, 13, tzinfo=ADELAIDE)),
    (SEASONAL, datetime(2026, 3, 28, 0, 0, tzinfo=ADELAIDE)),   # into winter
    (SEASONAL, datetime(2026, 10, 30, 9, 0, tzinfo=ADELAIDE)),  # into summer, over a weekend
])
def test_schedule_agrees_with_current(raw, start) -> None:
    """Every stretch of the schedule has the period and rates current() gives
    for the times in it, the stretches join up, and neighbours differ."""
    norm = tariff.normalize(raw)
    end = start + timedelta(days=5, hours=3)
    schedule = tariff.schedule(norm, start, end)
    assert schedule[0]["start"] == start and schedule[-1]["end"] == end
    keys = ("season", "period", "buy_rate", "sell_rate")
    for first, second in zip(schedule, schedule[1:], strict=False):
        assert first["end"] == second["start"]
        assert [first[key] for key in keys] != [second[key] for key in keys]
    for item in schedule:
        when, stop = item["start"].replace(tzinfo=None), item["end"].replace(tzinfo=None)
        while when < stop:
            now = tariff.current(norm, when)
            assert [now[key] for key in keys] == [item[key] for key in keys], when
            when += timedelta(minutes=7)


def test_schedule_of_a_day() -> None:
    day = datetime(2026, 11, 2, tzinfo=ADELAIDE)
    schedule = tariff.schedule(tariff.normalize(TARIFF), day, day + timedelta(days=1))
    assert [(item["start"].hour, item["period"], item["buy_rate"]) for item in schedule] == [
        (0, "PARTIAL_PEAK", 0.44), (11, "OFF_PEAK", 0.0), (14, "PARTIAL_PEAK", 0.44), (16, "ON_PEAK", 0.583),
        (23, "PARTIAL_PEAK", 0.44)]
    # The stretch over midnight is one entry.
    two_days = tariff.schedule(tariff.normalize(TARIFF), day, day + timedelta(days=2))
    over_midnight = next(item for item in two_days if item["start"].hour == 23)
    assert over_midnight["end"] == day + timedelta(days=1, hours=11)


def test_schedule_edges() -> None:
    norm = tariff.normalize(TARIFF)
    start = datetime(2026, 11, 2, tzinfo=ADELAIDE)
    assert tariff.schedule(None, start, start + timedelta(days=1)) == []
    assert tariff.schedule(norm, start, start) == []
    capped = tariff.schedule(norm, start, start + timedelta(days=400))
    assert capped[-1]["end"] <= start + timedelta(days=tariff.MAX_SCHEDULE_DAYS + 1)
    # A tariff with rates but no seasons: one stretch, the flat rate.
    flat = tariff.normalize({"name": "Flat", "energy_charges": {"ALL": {"ALL": 0.3}}})
    assert [(item["period"], item["buy_rate"], item["sell_rate"]) for item in
            tariff.schedule(flat, start, start + timedelta(days=2))] == [(None, 0.3, None)]


async def test_price_sensors_and_calendars(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    await hass.config.async_set_time_zone("Australia/Adelaide")
    freezer.move_to("2026-11-02T06:30:00+00:00")  # 17:00 local: the peak
    fake_powerwall.tariff = copy.deepcopy(TARIFF)
    await _setup(hass, {**V1R, **ACCOUNT}, {"tesla_tariff": True})

    buy = hass.states.get(_eid(hass, "sensor", "tariff_buy_price"))
    assert buy.state == "0.583" and buy.attributes["period"] == "ON_PEAK"
    assert buy.attributes["unit_of_measurement"] == f"{hass.config.currency}/kWh"
    assert hass.states.get(_eid(hass, "sensor", "tariff_sell_price")).state == "0.02"

    buy_calendar = _eid(hass, "calendar", "buy_tariff")
    state = hass.states.get(buy_calendar)
    assert state.state == "on" and state.attributes["message"].startswith("ON_PEAK: 0.583 ")
    assert state.attributes["backend"] == "Owner API"
    response = await hass.services.async_call("calendar", "get_events", {
        "entity_id": buy_calendar, "start_date_time": "2026-11-02T00:00:00+10:30",
        "end_date_time": "2026-11-03T00:00:00+10:30"}, blocking=True, return_response=True)
    events = response[buy_calendar]["events"]
    assert [event["summary"].split(":")[0] for event in events] == [
        "PARTIAL_PEAK", "OFF_PEAK", "PARTIAL_PEAK", "ON_PEAK", "PARTIAL_PEAK"]
    assert events[3]["start"].startswith("2026-11-02T16:00:00") and events[3]["end"].startswith("2026-11-02T23:00:00")

    sell_calendar = _eid(hass, "calendar", "sell_tariff")
    assert hass.states.get(sell_calendar).attributes["message"].startswith("ON_PEAK: 0.02 ")


async def test_calendar_without_a_feed_in_tariff(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    """No sell rates: the buy side works, the sell calendar has no events
    and the sell price is unavailable."""
    await hass.config.async_set_time_zone("Australia/Adelaide")
    freezer.move_to("2026-11-02T06:30:00+00:00")
    fake_powerwall.tariff = {key: value for key, value in copy.deepcopy(TARIFF).items() if key != "sell_tariff"}
    await _setup(hass, {**V1R, **ACCOUNT}, {"tesla_tariff": True})
    assert hass.states.get(_eid(hass, "sensor", "tariff_buy_price")).state == "0.583"
    assert hass.states.get(_eid(hass, "sensor", "tariff_sell_price")).state == "unavailable"
    assert hass.states.get(_eid(hass, "calendar", "sell_tariff")).state == "off"


async def test_no_tariff_entities_without_the_option(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, {**V1R, **ACCOUNT})
    assert _eid(hass, "sensor", "tariff_buy_price") is None
    assert _eid(hass, "calendar", "buy_tariff") is None


async def test_calendar_event_runs_on_over_a_change_of_season(hass: HomeAssistant, fake_powerwall, freezer) -> None:
    """Off-peak costs the same in both seasons: the night into the new
    season is one event, not two."""
    await hass.config.async_set_time_zone("Australia/Adelaide")
    freezer.move_to("2026-03-31T01:30:00+00:00")  # Tuesday noon, the last day of summer
    same = copy.deepcopy(SEASONAL)
    same["energy_charges"]["Winter"]["OFF_PEAK"] = 0.2
    fake_powerwall.tariff = same
    await _setup(hass, {**V1R, **ACCOUNT}, {"tesla_tariff": True})
    calendar = _eid(hass, "calendar", "buy_tariff")
    response = await hass.services.async_call("calendar", "get_events", {
        "entity_id": calendar, "start_date_time": "2026-03-31T00:00:00+10:30",
        "end_date_time": "2026-04-02T00:00:00+10:30"}, blocking=True, return_response=True)
    events = response[calendar]["events"]
    assert [event["summary"].split(":")[0] for event in events] == ["OFF_PEAK", "ON_PEAK", "OFF_PEAK"]
    assert events[2]["start"].startswith("2026-03-31T21:00:00") and events[2]["end"].startswith("2026-04-02T00:00:00")
