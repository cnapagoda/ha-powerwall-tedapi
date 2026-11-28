"""How long the gateway takes to answer (2.9.0): the last hour of local reads
(full and power-only polls, by connection path), the ones that failed or
timed out, how late Home Assistant's event loop ran (a busy Home Assistant
looks like a slow gateway otherwise), and how old the battery level was at
each battery dispatch renewal. Shown on Setup health and in diagnostics, to
tell a slow link from a refused key or password without debug logs. Nothing
identifying is kept: times, counts and the connection path only."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import median
from typing import Any

WINDOW_SECONDS = 3600
# Enough for fast polling every 5 s for an hour, with room to spare.
MAX_SAMPLES = 1000
KINDS = ("full", "fast")


@dataclass(frozen=True)
class Read:
    at: float  # monotonic
    kind: str  # "full" or "fast"
    seconds: float
    ok: bool
    timeout: bool
    path: str | None


def is_timeout(err: BaseException) -> bool:
    """A read that got no answer in time (requests, urllib3 and asyncio all
    say "timed out")."""
    return isinstance(err, TimeoutError) or "timed out" in str(err).lower() or "timeout" in type(err).__name__.lower()


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


class ReadTimes:
    def __init__(self) -> None:
        self.reads: deque[Read] = deque(maxlen=MAX_SAMPLES)
        self.loop_lags: deque[tuple[float, float]] = deque(maxlen=MAX_SAMPLES)
        self.level_ages: deque[tuple[float, float]] = deque(maxlen=MAX_SAMPLES)

    def read(self, now: float, kind: str, seconds: float, *, ok: bool, timeout: bool = False,
             path: str | None = None) -> None:
        self.reads.append(Read(now, kind, max(0.0, seconds), ok, timeout, path))

    def loop_lag(self, now: float, seconds: float) -> None:
        self.loop_lags.append((now, max(0.0, seconds)))

    def level_age(self, now: float, seconds: float) -> None:
        self.level_ages.append((now, max(0.0, seconds)))

    def summary(self, now: float) -> dict[str, Any]:
        since = now - WINDOW_SECONDS
        reads = [r for r in self.reads if r.at >= since]
        out: dict[str, Any] = {"window_minutes": WINDOW_SECONDS // 60}
        for kind in KINDS:
            mine = [r for r in reads if r.kind == kind]
            good = [r.seconds for r in mine if r.ok]
            paths: dict[str, int] = {}
            for r in mine:
                if r.ok and r.path:
                    paths[r.path] = paths.get(r.path, 0) + 1
            out[kind] = {
                "count": len(mine),
                "median_s": _round(median(good)) if good else None,
                "slowest_s": _round(max(good)) if good else None,
                "failed": sum(1 for r in mine if not r.ok),
                "timeouts": sum(1 for r in mine if r.timeout),
                "paths": paths,
            }
        lags = [lag for at, lag in self.loop_lags if at >= since]
        out["loop_delay_ms"] = {"median": _round(median(lags) * 1000, 1) if lags else None,
                                "max": _round(max(lags) * 1000, 1) if lags else None}
        ages = [age for at, age in self.level_ages if at >= since]
        out["dispatch_level_age_s"] = {"last": _round(ages[-1], 1) if ages else None,
                                       "max": _round(max(ages), 1) if ages else None,
                                       "renewals": len(ages)}
        return out
