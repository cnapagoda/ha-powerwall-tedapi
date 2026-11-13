"""The local pypowerwall client: one place for its arguments, and a check
that the pypowerwall loaded in this Home Assistant can take them.

Home Assistant runs one copy of a Python package for all integrations. If
another custom integration pins an older pypowerwall and loaded it first,
that copy stays in use until a restart (and may be installed again at the
next one): setup can then pass while every poll fails.
"""
from __future__ import annotations

import inspect
import os
import re
from typing import Any

from .const import DEFAULT_HOST_WIFI, MIN_PYPOWERWALL, MODE_V1R


class ClientTooOld(Exception):
    """The loaded pypowerwall is older than this integration needs, or its
    client doesn't take an argument the integration passes."""

    def __init__(self, found: str, missing: tuple[str, ...] = ()) -> None:
        super().__init__(
            f"The loaded pypowerwall ({found}) doesn't take {', '.join(missing)}; {required()} or newer is needed"
            if missing else f"pypowerwall {found} is loaded, {required()} or newer is needed")
        self.found = found
        self.missing = missing


def required() -> str:
    return ".".join(str(n) for n in MIN_PYPOWERWALL)


def release(version: Any) -> tuple[int, ...]:
    """(0, 25, 0) from "0.25.0", "0.25.0rc1" or "0.25.0+local"."""
    return tuple(int(n) for n in re.findall(r"\d+", str(version or "").split("+")[0])[:3])


def local_kwargs(mode: str, host: str, gw_pwd: str, rsa_key_path: str, timezone: str, *,
                 full_login: bool = False, wifi_host: str = "", cache_seconds: int | None = None,
                 fallback: bool = True) -> dict[str, Any]:
    """Arguments for the local pypowerwall client, for setup's connection
    check and for polling alike.

    fallback=False leaves out the Wi-Fi fallback: the connection check must
    answer for the gateway address itself, not for the address pypowerwall
    would fail over to."""
    kwargs: dict[str, Any] = {
        "host": host,
        # A Backup Gateway 1 logs in with the full password; otherwise
        # pypowerwall uses the last 5 characters of gw_pwd.
        "password": gw_pwd if full_login and mode == MODE_V1R else "",
        "email": "",
        "timezone": timezone,
        "auto_select": True,
        "failover": False,
        # pypowerwall's failover switches to cloud mode when it finds a token
        # cache in authpath (default: the working directory, which is the HA
        # config folder). Point it at a folder without one: this client is
        # local only, and the cloud backup is a separate client.
        "authpath": os.path.dirname(__file__),
    }
    if cache_seconds is not None:
        kwargs["pwcacheexpire"] = cache_seconds
    if gw_pwd or mode != MODE_V1R:
        kwargs["gw_pwd"] = gw_pwd
    if mode != MODE_V1R:
        return kwargs
    kwargs["rsa_key_path"] = rsa_key_path
    if not fallback:
        return kwargs
    if wifi_host and wifi_host != host:
        # After 3 failures on the home-network address, use signed v1r
        # over the Powerwall's Wi-Fi hotspot; the main host is retried later.
        kwargs["wifi_host"] = wifi_host
        kwargs["failover"] = True
        kwargs["wifi_v1r_fallback"] = True
    elif host == DEFAULT_HOST_WIFI and gw_pwd:
        # The signed connection already uses the Powerwall's own Wi-Fi.
        # pypowerwall reads follower Powerwall 3s through wifi_host
        # only, and that may be the same address: there is nothing to
        # fail over to (its failover needs a different address).
        kwargs["wifi_host"] = host
    return kwargs


# Every argument local_kwargs can pass.
ARGUMENTS = frozenset(local_kwargs(MODE_V1R, "host", "password", "key", "UTC", full_login=True,
                                   wifi_host="wifi", cache_seconds=1))


def ensure_supported() -> None:
    """Raise ClientTooOld unless the loaded pypowerwall is new enough and its
    client takes every argument local_kwargs can pass. Imports pypowerwall:
    call it in the executor."""
    import pypowerwall  # noqa: PLC0415

    found = str(getattr(pypowerwall, "version", None) or getattr(pypowerwall, "__version__", "") or "unknown")
    if release(found) < MIN_PYPOWERWALL:
        raise ClientTooOld(found)
    try:
        parameters = inspect.signature(pypowerwall.Powerwall.__init__).parameters
    except (TypeError, ValueError):
        return
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return
    missing = tuple(sorted(ARGUMENTS - set(parameters)))
    if missing:
        raise ClientTooOld(found, missing)
