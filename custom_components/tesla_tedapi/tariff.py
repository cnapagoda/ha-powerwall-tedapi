"""Tesla tariff (cloud only): normalise Tesla's two tariff formats, find the
current period and rates, and validate time-of-use settings before a write.

Tesla returns a tariff in one shape (get_tariff, "tariff_content") and takes
writes in another (time_of_use_settings with "tariff_content_v2"):

- v1: energy_charges[season][period] = rate, and
  seasons[season]["tou_periods"][period] = [range, ...]
- v2: energy_charges[season]["rates"][period] = rate, and
  seasons[season]["tou_periods"][period]["periods"] = [range, ...]

Sell rates use the same layout under "sell_tariff".

Tesla leaves zero-valued fields out of what it returns (a range ending on
Monday has no toDayOfWeek, a free period no rate), so a missing field is 0.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

# Keys only the read format has at its top level.
READ_FORMAT_KEYS = ("energy_charges", "seasons", "code", "utility", "daily_charges")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MAX_WARNINGS = 5  # per kind of warning, then "... and N more"


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
            "from_month": season.get("fromMonth", 0), "to_month": season.get("toMonth", 0),
            "from_day": season.get("fromDay", 0), "to_day": season.get("toDay", 0),
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
    if not season["from_month"] or not season["to_month"]:
        return False  # an empty season (Tesla left its zero months out)
    start = (season["from_month"], season["from_day"])
    end = (season["to_month"], season["to_day"])
    today = (now.month, now.day)
    if start <= end:
        return start <= today <= end
    return today >= start or today <= end  # wraps over the new year


def _in_range(rng: dict[str, Any], now: datetime) -> bool:
    # Tesla numbers the days of the week from Monday = 0 (Python's weekday()).
    return now.weekday() in _days(rng) and _minutes(rng)[now.hour * 60 + now.minute]


def _days(rng: dict[str, Any]) -> set[int]:
    dow_from, dow_to = rng.get("fromDayOfWeek", 0), rng.get("toDayOfWeek", 0)
    if dow_from <= dow_to:
        return set(range(dow_from, dow_to + 1))
    return set(range(dow_from, 7)) | set(range(dow_to + 1))  # wraps over Sunday


def _minutes(rng: dict[str, Any]) -> list[bool]:
    """For each minute of the day, whether the range covers it."""
    start = rng.get("fromHour", 0) * 60 + rng.get("fromMinute", 0)
    end = rng.get("toHour", 0) * 60 + rng.get("toMinute", 0)
    if start == end:
        return [True] * 1440  # the whole day
    if start < end:
        return [start <= m < end for m in range(1440)]
    return [m >= start or m < end for m in range(1440)]  # over midnight


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
        # A known period without a rate is free (Tesla leaves zeros out).
        return 0.0 if period is not None and table else None

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
    if other := sorted(set(tou_settings) - {"tariff_content_v2"}):
        return [f"only tariff_content_v2 can be sent; remove {', '.join(other)}"]
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


def _resolve(table: dict[str, dict[str, float]], season: str, period: str) -> float | None:
    for s_key, p_key in ((season, period), (season, "ALL"), ("ALL", period), ("ALL", "ALL")):
        if p_key in table.get(s_key, {}):
            return table[s_key][p_key]
    return None


def as_tesla_stores(norm: dict[str, Any]) -> dict[tuple[str, str, str], float]:
    """{(buy|sell, season, period): rate} the way Tesla keeps them: negative
    rates raised to 0, a buy rate below the sell rate raised to match, and
    zero rates left out (Tesla drops them, "ALL": 0 placeholders included)."""
    buy = {(s, p): max(r, 0.0) for s, rates in norm["buy"].items() for p, r in rates.items()}
    sell = {(s, p): max(r, 0.0) for s, rates in norm["sell"].items() for p, r in rates.items()}
    for (s, p), r in sell.items():
        if (s, p) in buy and buy[(s, p)] < r:
            buy[(s, p)] = r
    out = {("buy", s, p): round(r, 4) for (s, p), r in buy.items()}
    out.update({("sell", s, p): round(r, 4) for (s, p), r in sell.items()})
    return {k: v for k, v in out.items() if v}


def same_rates(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Whether two normalised tariffs have the same buy and sell rates, once
    Tesla's own changes are allowed for (see as_tesla_stores)."""
    if not a or not b:
        return False
    return as_tesla_stores(a) == as_tesla_stores(b)


def _more(items: list[str], what: str) -> list[str]:
    if len(items) <= MAX_WARNINGS:
        return items
    return [*items[:MAX_WARNINGS], f"... and {len(items) - MAX_WARNINGS} more {what}"]


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _runs(values: list[Any], bad: Callable[[Any], bool]) -> list[tuple[int, int, Any]]:
    """(start, end, value) for each run of equal consecutive values where bad(value)."""
    runs, i = [], 0
    while i < len(values):
        j = i
        while j < len(values) and values[j] == values[i]:
            j += 1
        if bad(values[i]):
            runs.append((i, j, values[i]))
        i = j
    return runs


def _day_warnings(name: str, season: dict[str, Any]) -> list[str]:
    """Hours of a day in no period, or in more than one."""
    periods = {p: ranges for p, ranges in season["periods"].items() if ranges}
    if not periods:
        return []
    gaps, overlaps = [], []
    for day in range(7):
        owners: list[tuple[str, ...]] = [()] * 1440
        for p, ranges in periods.items():
            for rng in ranges:
                if day in _days(rng):
                    covered = _minutes(rng)
                    owners = [o + (p,) if covered[m] and p not in o else o for m, o in enumerate(owners)]
        for start, end, who in _runs(owners, lambda o: len(o) != 1):
            when = f"{name}: {DAYS[day]} {_hhmm(start)}-{_hhmm(end)}"
            if who:
                overlaps.append(f"{when} is in more than one period ({', '.join(who)})")
            else:
                gaps.append(f"{when} is in no period")
    return _more(gaps, "gaps") + _more(overlaps, "overlaps")


def _year_warnings(seasons: dict[str, dict[str, Any]]) -> list[str]:
    """Days of the year in no season, or in more than one."""
    out = [f"season {n} has no months, so Tesla treats it as inactive"
           for n, s in seasons.items() if not s["from_month"] or not s["to_month"]]
    active = {n: s for n, s in seasons.items() if s["from_month"] and s["to_month"]}
    if not active:
        return out
    days = [datetime(2024, 1, 1) + timedelta(days=i) for i in range(366)]  # a leap year
    owners = [tuple(n for n, s in active.items() if _in_season(s, d)) for d in days]
    for start, end, who in _runs(owners, lambda o: len(o) != 1):
        first, last = days[start], days[end - 1]
        when = f"{first.day} {MONTHS[first.month - 1]}-{last.day} {MONTHS[last.month - 1]}"
        out.append(f"{when} is in more than one season ({', '.join(who)})" if who else f"{when} is in no season")
    return out


def _price_warnings(norm: dict[str, Any]) -> list[str]:
    """Prices without periods and periods without a buy price."""
    out = []
    for side in ("buy", "sell"):
        for s, rates in norm[side].items():
            if s == "ALL":
                continue
            if s not in norm["seasons"]:
                out.append(f"{side} prices for season {s}, which isn't in seasons")
                continue
            periods = norm["seasons"][s]["periods"]
            out += [f"{s}.{p} has a {side} price but no time periods"
                    for p in rates if p != "ALL" and not periods.get(p)]
    for s, season in norm["seasons"].items():
        rates = norm["buy"].get(s, {})
        out += [f"{s}.{p} has time periods but no buy price (Tesla uses 0)"
                for p, ranges in season["periods"].items() if ranges and p not in rates and "ALL" not in rates]
    return out


def _rate_warnings(norm: dict[str, Any]) -> list[str]:
    """What Tesla changes on its own: negative rates, sell above buy."""
    out = [f"{side} rate {s}.{p} is negative ({r}); Tesla stores 0"
           for side in ("buy", "sell") for s, rates in norm[side].items() for p, r in rates.items() if r < 0]
    for s, rates in norm["sell"].items():
        for p, r in rates.items():
            buy = _resolve(norm["buy"], s, p)
            if buy is not None and max(r, 0.0) > max(buy, 0.0):
                out.append(f"sell rate {s}.{p} ({r}) is above the buy rate ({buy}); "
                           "Tesla raises the buy rate to match")
    return out


def check_write(tou_settings: dict[str, Any]) -> list[str]:
    """Warnings for a payload validate_write accepted: what Tesla would change
    or leave unpriced. They don't stop a write; a dry run lists them."""
    content = tou_settings["tariff_content_v2"]
    norm = normalize(content)
    if norm is None:  # pragma: no cover - validate_write requires content
        return []
    out = _rate_warnings(norm) + _price_warnings(norm) + _year_warnings(norm["seasons"])
    for name, season in norm["seasons"].items():
        out += _day_warnings(name, season)
    if not isinstance(content.get("sell_tariff"), dict):
        out.append("no sell_tariff, so no feed-in rates are sent")
    return out
