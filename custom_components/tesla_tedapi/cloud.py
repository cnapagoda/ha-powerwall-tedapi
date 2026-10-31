"""Cloud backups for settings: one per Tesla API, tried in the chosen order.

Each backup keeps its own client, cache, failure count and hourly check, so one
that stops working is reported (and skipped for reads) while the other keeps
the settings working."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
import time
from typing import Any

from .const import (
    BACKEND_FLEET,
    BACKEND_OWNER,
    CLOUD_AUTH_FAILURES,
    OWNER_RENEW_RETRY_SECONDS,
    OWNER_RENEW_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

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
    # Storm Watch and other cloud-only values: (monotonic time, values), read
    # at most every extras_seconds (Tesla Fleet's stored data: every poll).
    extras: tuple[float, dict[str, Any]] | None = None
    extras_seconds: float = 0

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
        renew = getattr(self.client, "renew_token", None)
        if renew is not None:
            renew()
        return self.client

    def drop(self) -> None:
        """Make a new client next time (e.g. log in again)."""
        self.client = None


def site_extras(info: dict[str, Any], user_settings: dict[str, Any], components: dict[str, Any],
                live: dict[str, Any]) -> dict[str, Any]:
    """Cloud-only site values: Storm Watch, grid services and the off-grid
    vehicle charging reserve (None when Tesla doesn't report them)."""
    return {
        "storm_watch_capable": components.get("storm_mode_capable"),
        "storm_watch": user_settings.get("storm_mode_enabled"),
        "storm_active": live.get("storm_mode_active"),
        "grid_services_enabled": components.get("grid_services_enabled"),
        "grid_services_active": live.get("grid_services_active"),
        "off_grid_reserve_supported": components.get("off_grid_vehicle_charging_reserve_supported"),
        "off_grid_reserve": info.get("off_grid_vehicle_charging_reserve_percent"),
    }


class OwnerSettings:
    """The pypowerwall Owner API client, with the export rule read from the
    site's own setting. pypowerwall reports "never" whenever the site has
    non_export_configured, which stays true on sites that do export with
    "battery_ok"; the gateway and Tesla Fleet report the rule itself."""

    def __init__(self, pw: Any) -> None:
        self._pw = pw
        self._renew_after = 0.0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pw, name)

    def renew_token(self) -> bool:
        """Refresh the access token when it expires within OWNER_RENEW_SECONDS,
        so no call gets HTTP 401 for it first. True if it was refreshed; a
        failure is only logged, as pypowerwall still refreshes on 401."""
        tesla = getattr(self._pw.client, "tesla", None)
        token = getattr(tesla, "token", None) or {}
        expires = token.get("expires_at")
        if (not token.get("refresh_token") or not isinstance(expires, (int, float))
                or expires - time.time() > OWNER_RENEW_SECONDS or time.monotonic() < self._renew_after):
            return False
        try:
            tesla.refresh_token(tesla.auto_refresh_url, refresh_token=token["refresh_token"],
                                **tesla.auto_refresh_kwargs)
        except Exception as err:  # noqa: BLE001
            self._renew_after = time.monotonic() + OWNER_RENEW_RETRY_SECONDS
            _LOGGER.debug("Owner API token renewal failed: %s", err)
            return False
        _LOGGER.debug("Renewed the Owner API token (%d min left)", (expires - time.time()) / 60)
        return True

    def get_grid_export(self) -> str | None:
        config = self._pw.client.get_site_config() or {}
        components = (config.get("response") or {}).get("components") or {}
        if not components:
            return None
        return components.get("customer_preferred_export_rule") or "battery_ok"

    def get_extras(self) -> dict[str, Any]:
        client = self._pw.client
        info = (client.get_site_config() or {}).get("response") or {}
        if not info:
            return {}
        live = (client.get_site_power() or {}).get("response") or {}
        return site_extras(info, info.get("user_settings") or {}, info.get("components") or {}, live)

    def _site_command(self, name: str, **kwargs: Any) -> Any:
        client = self._pw.client
        result = client.site.api(name, **kwargs)
        try:
            client.pwcachetime["SITE_CONFIG"] = 0  # read the change next time
        except (AttributeError, TypeError):
            pass
        return result

    def set_storm_watch(self, enabled: bool) -> Any:
        return self._site_command("STORM_MODE_SETTINGS", enabled=bool(enabled))

    def set_off_grid_reserve(self, percent: int) -> Any:
        return self._site_command("OFF_GRID_VEHICLE_CHARGING_RESERVE",
                                  off_grid_vehicle_charging_reserve_percent=int(percent))

    def get_tariff(self) -> dict[str, Any] | None:
        """The site's tariff from the Owner API (Tesla's SITE_TARIFF)."""
        tariff = self._pw.get_tariff(force=True)
        return tariff if isinstance(tariff, dict) and tariff else None

    def set_tariff(self, tou_settings: dict[str, Any]) -> Any:
        """Write time-of-use settings ({"tariff_content_v2": ...}); None on failure."""
        return self._pw.set_tariff(tou_settings)
