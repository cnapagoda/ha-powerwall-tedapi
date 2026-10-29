"""Cloud backups for settings: one per Tesla API, tried in the chosen order.

Each backup keeps its own client, cache, failure count and hourly check, so one
that stops working is reported (and skipped for reads) while the other keeps
the settings working."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .const import BACKEND_FLEET, BACKEND_OWNER, CLOUD_AUTH_FAILURES

LABELS = {BACKEND_OWNER: "Owner API", BACKEND_FLEET: "Tesla Fleet"}


@dataclass(eq=False)
class CloudBackend:
    """One cloud backup. factory builds a pypowerwall-like client (executor);
    starting() is true while it can't be used yet but is no failure either;
    writable() is false when it can read but not change settings."""

    name: str
    factory: Callable[[], Any]
    starting: Callable[[], bool] = lambda: False
    writable: Callable[[], bool] = lambda: True
    client: Any = None
    failures: int = 0
    last_ok: datetime | None = None
    # (monotonic time, settings) of the last read, good or failed.
    cache: tuple[float, dict[str, Any]] | None = None
    check_at: float | None = None
    # The repair currently shown for it (its translation key), if any.
    issue: str | None = None

    @property
    def label(self) -> str:
        return LABELS.get(self.name, self.name)

    @property
    def failing(self) -> bool:
        """Failed often enough that its repair is up."""
        return self.failures >= CLOUD_AUTH_FAILURES

    @property
    def ok(self) -> bool:
        """Proven by a real read (not just "no failures yet")."""
        return self.failures == 0 and self.last_ok is not None

    def get_client(self) -> Any:
        if self.client is None:
            try:
                self.client = self.factory()
            except Exception:
                self.failures += 1
                raise
            # A login alone doesn't reset failures: only a read that returns
            # data does (the Owner API can log in, then reject reads).
        return self.client

    def drop(self) -> None:
        """Make a new client next time (e.g. log in again)."""
        self.client = None


class OwnerSettings:
    """The pypowerwall Owner API client, with the export rule read from the
    site's own setting. pypowerwall reports "never" whenever the site has
    non_export_configured, which stays true on sites that do export with
    "battery_ok"; the gateway and Tesla Fleet report the rule itself."""

    def __init__(self, pw: Any) -> None:
        self._pw = pw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pw, name)

    def get_grid_export(self) -> str | None:
        config = self._pw.client.get_site_config() or {}
        components = (config.get("response") or {}).get("components") or {}
        if not components:
            return None
        return components.get("customer_preferred_export_rule") or "battery_ok"
