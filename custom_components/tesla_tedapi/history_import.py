"""One-off import of battery history from Tesla Fleet / Teslemetry.

Copies the cloud integration's hourly battery charged/discharged statistics
into this Powerwall's battery energy statistics for the hours before their
own history starts. The imported hours are anchored to the first recorded
hour so the series joins without a jump, then everything is shifted so the
oldest imported hour starts at zero (the recorder measures the first period
against 0).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .fleet import find_site

SOURCE_PLATFORMS = ("tesla_fleet", "teslemetry")
# Our key -> the cloud integrations' translation key.
PAIRS = {
    "battery_energy_charged": "total_battery_charge",
    "battery_energy_discharged": "total_battery_discharge",
}
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
UNITS = {"energy": "kWh"}


def _refuse(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key,
                                  translation_placeholders=placeholders or None)


def find_sources(hass: HomeAssistant, din: str | None,
                 overrides: dict[str, str | None]) -> dict[str, str]:
    """Our key -> source entity id. Overrides win; otherwise the sensors of
    the Tesla Fleet / Teslemetry site with this Powerwall, or the only ones."""
    registry = er.async_get(hass)
    found = find_site(hass, din)
    site_id = getattr(found[1], "id", None) if found else None
    sources: dict[str, str] = {}
    for ours, theirs in PAIRS.items():
        if overrides.get(ours):
            sources[ours] = overrides[ours]
            continue
        candidates = [e for e in registry.entities.values()
                      if e.platform in SOURCE_PLATFORMS and e.domain == "sensor"
                      and (e.translation_key == theirs or str(e.unique_id).endswith(f"-{theirs}"))]
        if site_id is not None:
            candidates = [e for e in candidates if str(e.unique_id).startswith(f"{site_id}-")]
        if len(candidates) != 1:
            raise _refuse("history_no_source", sensor=theirs)
        sources[ours] = candidates[0].entity_id
    return sources


def _ts(value: Any) -> float:
    return value.timestamp() if isinstance(value, datetime) else float(value)


def plan(hass: HomeAssistant, target: str, source: str) -> dict[str, Any]:
    """Work out the rows to import (blocking: run in the recorder executor)."""
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        get_metadata,
        statistics_during_period,
    )

    meta = get_metadata(hass, statistic_ids={target}).get(target)
    own = statistics_during_period(hass, EPOCH, None, {target}, "hour", UNITS, {"sum", "state"}).get(target)
    if meta is None or not own:
        raise _refuse("history_no_target", sensor=target)
    first = own[0]
    first_start = _ts(first["start"])
    src = statistics_during_period(hass, EPOCH, None, {source}, "hour", UNITS, {"change"}).get(source) or []
    if not src:
        raise _refuse("history_nothing_to_import", sensor=source)
    # Imported before (or the source is newer): our history already reaches
    # back to the source's first hour.
    if first_start <= _ts(src[0]["start"]):
        raise _refuse("history_already_imported", sensor=target)
    src = [r for r in src if _ts(r["start"]) < first_start]

    changes = [max(r.get("change") or 0.0, 0.0) for r in src]
    # Each row holds the totals at the END of its hour. Our first hour was
    # recorded against 0, so the last source hour ends at 0 (and the lifetime
    # counter at its value before our first hour); each earlier hour is the
    # next minus the energy of the next (sum(h) = sum(h+1) - change(h+1)).
    first_sum = float(first["sum"] or 0.0)
    total, state = 0.0, float(first["state"] or 0.0) - first_sum
    rows: list[dict[str, Any]] = []
    for r, c in zip(reversed(src), reversed(changes), strict=True):
        rows.append({"start": _ts(r["start"]), "sum": total, "state": state})
        total -= c
        state -= c
    rows.reverse()
    shift = round(changes[0] - rows[0]["sum"], 3)
    for row in rows:
        row["sum"] = round(row["sum"] + shift, 3)
        row["state"] = round(row["state"], 3)
    iso = lambda ts: datetime.fromtimestamp(ts, UTC).isoformat()  # noqa: E731
    return {
        "metadata": meta[1],
        "rows": rows,
        "first_start": first_start,
        "summary": {
            "source": source,
            "hours": len(rows),
            "kwh": round(sum(changes), 3),
            "from": iso(rows[0]["start"]),
            "to": iso(rows[-1]["start"]),
            "join": iso(first_start),
            "shift_kwh": shift,
            "lifetime_counter_at_start": rows[0]["state"],
            "negative_hours_skipped": sum(1 for r in src if (r.get("change") or 0) < 0),
        },
    }


async def async_import(hass: HomeAssistant, din: str | None, overrides: dict[str, str | None],
                       dry_run: bool) -> dict[str, Any]:
    """Plan (and unless dry_run, run) the import for both battery sensors."""
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.statistics import async_import_statistics  # noqa: PLC0415

    registry = er.async_get(hass)
    targets = {key: registry.async_get_entity_id("sensor", DOMAIN, f"{din}_{key}") for key in PAIRS}
    if not all(targets.values()):
        raise _refuse("history_no_target", sensor=", ".join(k for k, v in targets.items() if not v))
    sources = find_sources(hass, din, overrides)
    instance = get_instance(hass)
    # Plan both first, so nothing is written unless both can be imported.
    plans = {key: await instance.async_add_executor_job(plan, hass, targets[key], sources[key])
             for key in PAIRS}
    result: dict[str, Any] = {"dry_run": dry_run}
    for key, p in plans.items():
        result[key] = {"target": targets[key], **p["summary"]}
        if dry_run:
            continue
        unit = p["metadata"].get("unit_of_measurement") or "kWh"
        async_import_statistics(hass, {**p["metadata"], "source": "recorder"}, [
            {"start": datetime.fromtimestamp(r["start"], UTC), "sum": r["sum"], "state": r["state"]}
            for r in p["rows"]
        ])
        instance.async_adjust_statistics(targets[key], datetime.fromtimestamp(p["first_start"], UTC),
                                         p["summary"]["shift_kwh"], unit)
        result[key]["imported"] = True
    return result
