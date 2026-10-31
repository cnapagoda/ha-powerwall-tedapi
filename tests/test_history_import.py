"""Import battery history from Tesla Fleet / Teslemetry (real test recorder)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import DIN

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
T0 = datetime(2026, 9, 1, tzinfo=UTC)          # source history starts
JOIN = T0 + timedelta(hours=48)                # our own history starts
SITE = "268716912127"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """A real (in-memory) recorder, set up before hass (overrides conftest's)."""
    yield


def _meta(statistic_id: str) -> dict:
    meta = {"has_mean": False, "has_sum": True, "name": None, "source": "recorder",
            "statistic_id": statistic_id, "unit_of_measurement": "kWh"}
    # Newer recorders (HA 2025.4+, 2025.11+) want these spelt out.
    try:
        from homeassistant.components.recorder.models import StatisticMeanType  # noqa: PLC0415
    except ImportError:  # pragma: no cover - older HA
        return meta
    return {**meta, "mean_type": StatisticMeanType.NONE, "unit_class": "energy"}


def _counter(statistic_id: str, start: datetime, hours: int, per_hour: float, *, first_sum: float = 0.0,
             first_state: float = 100.0, negative_at: int | None = None) -> None:
    """Hourly statistics for a counter rising per_hour each hour."""
    from homeassistant.components.recorder.statistics import async_import_statistics  # noqa: PLC0415

    rows, total, state = [], first_sum, first_state
    for h in range(hours):
        step = -1.0 if h == negative_at else per_hour
        total += step
        state += step
        rows.append({"start": start + timedelta(hours=h), "sum": total, "state": state})
    async_import_statistics(_HASS[0], _meta(statistic_id), rows)


_HASS: list[HomeAssistant] = []


async def _setup(hass: HomeAssistant) -> dict[str, str]:
    """Our entry, two cloud source sensors and hourly statistics for both.
    Returns entity ids."""
    _HASS[:] = [hass]
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=V1R, title="Powerwall")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    ids = {
        "charged": registry.async_get_entity_id("sensor", DOMAIN, f"{DIN}_battery_energy_charged"),
        "discharged": registry.async_get_entity_id("sensor", DOMAIN, f"{DIN}_battery_energy_discharged"),
    }
    for key, theirs in (("src_charged", "total_battery_charge"), ("src_discharged", "total_battery_discharge")):
        ids[key] = registry.async_get_or_create(
            "sensor", "tesla_fleet", f"{SITE}-{theirs}", translation_key=theirs,
            suggested_object_id=f"fleet_{theirs}").entity_id
    return ids


async def _stats(hass: HomeAssistant, statistic_id: str, period: str = "hour",
                 types: frozenset = frozenset({"sum", "state", "change"})) -> list[dict]:
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

    await async_wait_recording_done(hass)
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2000, 1, 1, tzinfo=UTC), None, {statistic_id}, period,
        None, set(types))
    return result.get(statistic_id, [])


async def _call(hass: HomeAssistant, **data):
    return await hass.services.async_call(DOMAIN, "import_battery_history", data, blocking=True,
                                          return_response=True)


async def _history(hass: HomeAssistant, ids: dict[str, str], negative_at: int | None = None) -> None:
    # Source: 72 h from T0, 1 kWh/h charged and 0.5 kWh/h discharged.
    _counter(ids["src_charged"], T0, 72, 1.0, negative_at=negative_at)
    _counter(ids["src_discharged"], T0, 72, 0.5)
    # Ours: 24 h from JOIN, starting from 0 with a lifetime counter of 6539.
    _counter(ids["charged"], JOIN, 24, 1.0, first_state=6539.0)
    _counter(ids["discharged"], JOIN, 24, 0.5, first_state=6354.0)
    await async_wait_recording_done(hass)


async def test_dry_run_summary(hass: HomeAssistant, fake_powerwall) -> None:
    ids = await _setup(hass)
    await _history(hass, ids)
    resp = await _call(hass)
    assert resp["dry_run"] is True
    charged = resp["battery_energy_charged"]
    assert charged["source"] == ids["src_charged"] and charged["target"] == ids["charged"]
    assert charged["hours"] == 48 and charged["kwh"] == pytest.approx(48.0)
    assert charged["join"] == JOIN.isoformat()
    assert "imported" not in charged
    # Nothing written.
    assert (await _stats(hass, ids["charged"]))[0]["start"] == JOIN.timestamp()


async def test_import_joins_and_rebases(hass: HomeAssistant, fake_powerwall) -> None:
    ids = await _setup(hass)
    await _history(hass, ids)
    resp = await _call(hass, dry_run=False)
    assert resp["battery_energy_charged"]["imported"] is True
    rows = await _stats(hass, ids["charged"])
    assert rows[0]["start"] == T0.timestamp()
    assert rows[0]["sum"] == pytest.approx(1.0)                      # starts at the first hour's energy
    sums = [r["sum"] for r in rows]
    steps = [b - a for a, b in zip(sums, sums[1:], strict=False)]
    assert all(s == pytest.approx(1.0) for s in steps)               # continuous, also at the join
    join = next(r for r in rows if r["start"] == JOIN.timestamp())
    assert join["state"] == pytest.approx(6540.0)                    # our lifetime counter untouched
    # The second run is refused: already imported.
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, dry_run=False)
    assert err.value.translation_key == "history_already_imported"


async def test_negative_hours_are_skipped(hass: HomeAssistant, fake_powerwall) -> None:
    ids = await _setup(hass)
    await _history(hass, ids, negative_at=10)
    resp = await _call(hass)
    assert resp["battery_energy_charged"]["negative_hours_skipped"] == 1


async def test_refusals(hass: HomeAssistant, fake_powerwall) -> None:
    ids = await _setup(hass)
    # No statistics of our own yet.
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass)
    assert err.value.translation_key == "history_no_target"
    # Ours, but the source has no statistics at all.
    _counter(ids["charged"], T0 + timedelta(hours=5), 24, 1.0)
    _counter(ids["discharged"], T0 + timedelta(hours=5), 24, 1.0)
    await async_wait_recording_done(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass)
    assert err.value.translation_key == "history_nothing_to_import"
    # The source starts after ours: nothing older to bring in.
    _counter(ids["src_charged"], T0 + timedelta(hours=6), 24, 1.0)
    _counter(ids["src_discharged"], T0 + timedelta(hours=6), 24, 1.0)
    await async_wait_recording_done(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass)
    assert err.value.translation_key == "history_already_imported"


async def test_sources_found_by_site_or_override(hass: HomeAssistant, fake_powerwall) -> None:
    from custom_components.tesla_tedapi.history_import import find_sources  # noqa: PLC0415

    ids = await _setup(hass)
    registry = er.async_get(hass)
    # A second site makes the lookup ambiguous...
    other = registry.async_get_or_create("sensor", "teslemetry", "999-total_battery_charge",
                                         translation_key="total_battery_charge").entity_id
    with pytest.raises(ServiceValidationError):
        find_sources(hass, DIN, {})
    # ...unless the Fleet site with this Powerwall is known...
    with patch("custom_components.tesla_tedapi.history_import.find_site",
               return_value=(None, SimpleNamespace(id=int(SITE)))):
        found = find_sources(hass, DIN, {})
    assert found == {"battery_energy_charged": ids["src_charged"],
                     "battery_energy_discharged": ids["src_discharged"]}
    # ...or the source is chosen.
    found = find_sources(hass, DIN, {"battery_energy_charged": other,
                                     "battery_energy_discharged": ids["src_discharged"]})
    assert found["battery_energy_charged"] == other


async def test_missing_target_entities(hass: HomeAssistant, fake_powerwall) -> None:
    from custom_components.tesla_tedapi.history_import import async_import  # noqa: PLC0415

    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await async_import(hass, "OTHER-DIN", {}, True)
