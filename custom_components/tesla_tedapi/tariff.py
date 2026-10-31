"""Tesla tariff (cloud only): normalise Tesla's two tariff formats, find the
current period and rates, and validate time-of-use settings before a write.

Tesla returns a tariff in one shape (get_tariff, "tariff_content") and takes
writes in another (time_of_use_settings with "tariff_content_v2"):

- v1: energy_charges[season][period] = rate, and
  seasons[season]["tou_periods"][period] = [range, ...]
- v2: energy_charges[season]["rates"][period] = rate, and
  seasons[season]["tou_periods"][period]["periods"] = [range, ...]

Sell rates use the same layout under "sell_tariff".
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

# Keys only the read format has at its top level.
READ_FORMAT_KEYS = ("energy_charges", "seasons", "code", "utility", "daily_charges")


def _rates(charges: Any) -> dict[str, dict[str, float]]:
    """{season: {period: rate}} from v1 or v2 energy_charges."""
    out: dict[str, dict[str, float]] = {}
    if not isinstance(charges, dict):
        return out
    for season, value in charges.items():
        if not isinstance(value, dict):
            continue
        periods = value.get("rates") if isinstance(value.get("rates"), dict) else value
        out[season] = {p: float(r) for p, r in periods.items()
                       if isinstance(r, (int, float)) and not isinstance(r, bool)}
    return out


def _ranges(tou_period: Any) -> list[dict[str, Any]]:
    if isinstance(tou_period, dict):
        tou_period = tou_period.get("periods")
    return [r for r in tou_period or [] if isinstance(r, dict)]


def normalize(tariff: Any) -> dict[str, Any] | None:
    """One shape for either format, or None when it isn't a tariff."""
    if not isinstance(tariff, dict) or not tariff:
        return None
    seasons: dict[str, dict[str, Any]] = {}
    for name, season in (tariff.get("seasons") or {}).items():
        if not isinstance(season, dict):
            continue
        seasons[name] = {
            "from_month": season.get("fromMonth", 1), "to_month": season.get("toMonth", 12),
            "from_day": season.get("fromDay", 1), "to_day": season.get("toDay", 31),
            "periods": {p: _ranges(v) for p, v in (season.get("tou_periods") or {}).items()},
        }
    sell = tariff.get("sell_tariff") if isinstance(tariff.get("sell_tariff"), dict) else {}
    return {
        "name": tariff.get("name") or tariff.get("code"),
        "utility": tariff.get("utility"),
        "code": tariff.get("code"),
        "seasons": seasons,
        "buy": _rates(tariff.get("energy_charges")),
        "sell": _rates(sell.get("energy_charges")),
    }


def _in_season(season: dict[str, Any], now: datetime) -> bool:
    start = (season["from_month"], season["from_day"])
    end = (season["to_month"], season["to_day"])
    today = (now.month, now.day)
    if start <= end:
        return start <= today <= end
    return today >= start or today <= end  # wraps over the new year


def _in_range(rng: dict[str, Any], now: datetime) -> bool:
    # Tesla numbers the days of the week from Monday = 0 (Python's weekday()).
    dow_from, dow_to = rng.get("fromDayOfWeek", 0), rng.get("toDayOfWeek", 6)
    weekday = now.weekday()
    if not (dow_from <= weekday <= dow_to if dow_from <= dow_to else weekday >= dow_from or weekday <= dow_to):
        return False
    start = rng.get("fromHour", 0) * 60 + rng.get("fromMinute", 0)
    end = rng.get("toHour", 0) * 60 + rng.get("toMinute", 0)
    minute = now.hour * 60 + now.minute
    if start == end:
        return True  # the whole day
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # over midnight


def current(norm: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Season, period and buy/sell rates for `now` (local time)."""
    result: dict[str, Any] = {"season": None, "period": None, "buy_rate": None, "sell_rate": None}
    if not norm:
        return result
    season_name = next((n for n, s in norm["seasons"].items() if _in_season(s, now)), None)
    if season_name is None and len(norm["buy"]) == 1:
        season_name = next(iter(norm["buy"]))
    period = None
    if season_name in norm["seasons"]:
        period = next((p for p, ranges in norm["seasons"][season_name]["periods"].items()
                       if any(_in_range(r, now) for r in ranges)), None)
    result["season"], result["period"] = season_name, period

    def rate(table: dict[str, dict[str, float]]) -> float | None:
        for season in (season_name, "ALL"):
            rates = table.get(season) or {}
            for key in (period, "ALL"):
                if key in rates:
                    return rates[key]
        return None

    result["buy_rate"], result["sell_rate"] = rate(norm["buy"]), rate(norm["sell"])
    return result


def validate_write(tou_settings: Any) -> list[str]:
    """Problems with a time_of_use_settings payload (empty when it looks right)."""
    if not isinstance(tou_settings, dict):
        return ["tou_settings must be an object"]
    content = tou_settings.get("tariff_content_v2")
    if content is None:
        if any(key in tou_settings for key in READ_FORMAT_KEYS):
            return ["this looks like the format get_tariff returns; writes need it wrapped and converted "
                    "as {\"tariff_content_v2\": {...}} (see the wiki)"]
        return ["tariff_content_v2 is missing"]
    if not isinstance(content, dict):
        return ["tariff_content_v2 must be an object"]
    errors = []
    for key in ("seasons", "energy_charges"):
        if not isinstance(content.get(key), dict) or not content.get(key):
            errors.append(f"tariff_content_v2.{key} must be a non-empty object")
    if not errors:
        for season, value in content["energy_charges"].items():
            if not isinstance(value, dict) or not isinstance(value.get("rates"), dict):
                errors.append(f"tariff_content_v2.energy_charges.{season} needs a \"rates\" object")
        for season, value in content["seasons"].items():
            if not isinstance(value, dict) or not isinstance(value.get("tou_periods"), dict):
                errors.append(f"tariff_content_v2.seasons.{season} needs a \"tou_periods\" object")
    return errors


def same_rates(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Whether two normalised tariffs have the same buy and sell rates."""
    if not a or not b:
        return False

    def flat(table: dict[str, dict[str, float]]) -> set[tuple[str, str, float]]:
        return {(s, p, round(r, 4)) for s, rates in table.items() for p, r in rates.items()}

    return flat(a["buy"]) == flat(b["buy"]) and flat(a["sell"]) == flat(b["sell"])
