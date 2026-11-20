"""Gateway features read and set through aiopytedapi: the gateway's network
status, the site's import and export limits, the battery calibration delay and
the keys registered with the gateway.

pypowerwall still does the polling, the settings and the dispatch. These
features come from aiopytedapi, an async client on Home Assistant's own
aiohttp session, so nothing here blocks the event loop.

Reads run every EXTRAS_SECONDS, apart from the main poll, and a failed read
keeps the last values. Commands need the signed (v1r) connection.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from aiopytedapi import (
    WIFI_HOST,
    MessageFaultError,
    NotSupportedError,
    RsaKey,
    SiteLimits,
    TedapiClient,
    TedapiError,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import gateway
from .const import MODE_V1R
from .privacy import scrub

_LOGGER = logging.getLogger(__name__)

# How often the network status, the registered keys and the calibration
# delay status are read (seconds). They change rarely; each is one request.
EXTRAS_SECONDS = 300
# Fingerprint characters shown for a key: enough to tell keys apart and to
# remove one, without listing the whole fingerprint.
FINGERPRINT_CHARS = 16
# The interfaces as aiopytedapi names them, and as the entities name them.
INTERFACES = {"wifi": "wifi", "eth": "ethernet", "gsm": "cellular"}


def load_key(path: str) -> RsaKey:
    """The signing key from its PEM file (blocking: run in the executor)."""
    with open(path, "rb") as file:
        return RsaKey.from_pem(file.read())


def network_values(status: Any) -> dict[str, Any]:
    """The network status as the entities read it: the interface carrying
    the default route, and per interface its state and signal."""
    interfaces: dict[str, dict[str, Any]] = {}
    for name, label in INTERFACES.items():
        interface = getattr(status, name, None)
        if interface is None:
            continue
        interfaces[label] = {
            "enabled": interface.enabled,
            "connected": interface.connected_physical,
            "internet": interface.connected_internet,
            "tesla": interface.connected_tesla,
            "signal": interface.signal_strength_percent,
            "state": interface.device_state.removeprefix("NETWORK_DEVICE_STATE_").lower(),
        }
    active = status.active
    return {"active": INTERFACES.get(active) if active else "none", "interfaces": interfaces}


def keys_values(clients: Any, current_key: bytes | None) -> dict[str, Any]:
    """The registered keys: how many in each state, the state of the key this
    integration signs with, and each key's short fingerprint, type and state.
    Descriptions and public keys are left out."""
    keys = []
    states: dict[str, int] = {}
    this_key: str | None = None
    for client in clients.clients:
        state = client.state.removeprefix("AUTHORIZED_CLIENT_STATE_").lower()
        states[state] = states.get(state, 0) + 1
        mine = current_key is not None and client.public_key == current_key
        if mine:
            this_key = state
        keys.append({
            "fingerprint": client.fingerprint[:FINGERPRINT_CHARS],
            "type": client.type.removeprefix("AUTHORIZED_CLIENT_TYPE_").lower(),
            "state": state,
            "added": client.added_time.isoformat() if client.added_time else None,
            "this_key": mine,
        })
    return {"count": len(keys), "states": states, "this_key": this_key, "keys": keys}


class GatewayExtras:
    """aiopytedapi clients for one gateway, and the last values read."""

    def __init__(self, hass: HomeAssistant, *, mode: str, host: str, gw_pwd: str, rsa_key_path: str,
                 full_login: bool = False, wifi_host: str = "", din: str | None = None,
                 customer_pwd: str = "") -> None:
        self.hass = hass
        self.signed_supported = mode == MODE_V1R
        self._host = host
        self._gw_pwd = gw_pwd
        self._rsa_key_path = rsa_key_path
        self._full_login = full_login
        self._customer_pwd = customer_pwd
        self._din = din
        # The Gateway Wi-Fi (password) connection: the gateway itself in
        # Wi-Fi mode, else the Wi-Fi fallback or a gateway reached on its
        # own Wi-Fi address. A Powerwall 2 gives the network status only here.
        if mode != MODE_V1R:
            self._wifi_host = host
        elif wifi_host:
            self._wifi_host = wifi_host
        else:
            self._wifi_host = host if host == WIFI_HOST else ""
        self._signed: TedapiClient | None = None
        self._wifi: TedapiClient | None = None
        self._key_failed = False
        # The signed connection answered the network status with a message
        # fault (a Powerwall 2): use the Wi-Fi connection, or stop asking.
        self._network_signed = True
        self.network_supported = True
        # None until read; False once the firmware says it doesn't do it.
        self.calibration_supported: bool | None = None
        self.network: dict[str, Any] | None = None
        self.keys: dict[str, Any] | None = None
        self.calibration: dict[str, Any] | None = None
        self.site_limits: dict[str, Any] | None = None
        self._read_at: float | None = None

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    @property
    def has_wifi(self) -> bool:
        return bool(self._wifi_host and self._gw_pwd)

    async def async_signed(self) -> TedapiClient | None:
        """The signed client, built on first use; None in Wi-Fi mode or when
        the key file can't be read (logged once)."""
        if not self.signed_supported or self._key_failed:
            return None
        if self._signed is None:
            try:
                key = await self.hass.async_add_executor_job(load_key, self._rsa_key_path)
            except (OSError, ValueError) as err:
                self._key_failed = True
                _LOGGER.warning("The signing key can't be read (%s): network status, registered keys "
                                "and site limits can't use the signed connection", scrub(err))
                return None
            password = self._customer_pwd or (
                gateway.customer_password(self._gw_pwd, self._full_login) if self._gw_pwd else None)
            self._signed = TedapiClient.signed(async_get_clientsession(self.hass), self._host, key,
                                               password=password, din=self._din)
        return self._signed

    def wifi_client(self) -> TedapiClient | None:
        """The Gateway Wi-Fi (password) client, or None without one. Read-only."""
        if not self.has_wifi:
            return None
        if self._wifi is None:
            self._wifi = TedapiClient.wifi(async_get_clientsession(self.hass), self._gw_pwd,
                                           host=self._wifi_host, din=self._din)
        return self._wifi

    async def _reader(self) -> TedapiClient | None:
        return await self.async_signed() or self.wifi_client()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def due(self) -> bool:
        return self._read_at is None or time.monotonic() - self._read_at >= EXTRAS_SECONDS

    async def async_read(self, *, limits: bool) -> None:
        """Read what is due. limits=True also reads the site limits (when
        the main poll doesn't read the gateway's config). A failed read
        keeps the last values."""
        self._read_at = time.monotonic()
        await self._read_network()
        await self._read_keys()
        await self._read_calibration()
        if limits:
            await self._read_limits()

    async def _read_network(self) -> None:
        if not self.network_supported:
            return
        signed = await self.async_signed() if self._network_signed else None
        client = signed or self.wifi_client()
        if client is None:
            self.network_supported = False
            return
        try:
            status = await client.get_network_status()
        except MessageFaultError as err:
            if client is not signed:
                _LOGGER.debug("Network status read failed: %s", err)
                return
            # A Powerwall 2 answers it only over the Gateway Wi-Fi.
            self._network_signed = False
            self.network_supported = self.has_wifi
            _LOGGER.debug("Network status over the signed connection: %s; %s", err,
                          "using the Gateway Wi-Fi" if self.has_wifi else "not read again")
            if self.has_wifi:
                await self._read_network()
            return
        except TedapiError as err:
            _LOGGER.debug("Network status read failed: %s", err)
            return
        self.network = network_values(status)

    async def _read_keys(self) -> None:
        client = await self._reader()
        if client is None:
            return
        try:
            clients = await client.get_authorized_clients()
        except TedapiError as err:
            _LOGGER.debug("Registered keys read failed: %s", err)
            return
        self.keys = keys_values(clients, client.current_key if client.signed_connection else None)

    async def _read_calibration(self) -> None:
        if self.calibration_supported is False:
            return
        client = await self.async_signed()
        if client is None:
            return
        try:
            status = await client.get_battery_calibration_delay_status()
        except NotSupportedError:
            self.calibration_supported = False
            _LOGGER.debug("The gateway's firmware doesn't report the calibration delay status")
            return
        except TedapiError as err:
            _LOGGER.debug("Calibration delay status read failed: %s", err)
            return
        self.calibration_supported = True
        self.calibration = status.to_dict()

    async def _read_limits(self) -> None:
        client = await self._reader()
        if client is None:
            return
        try:
            self.site_limits = (await client.get_site_limits()).to_dict()
        except TedapiError as err:
            _LOGGER.debug("Site limits read failed: %s", err)

    # ------------------------------------------------------------------
    # Commands (signed connection; the caller checks signed_supported)
    # ------------------------------------------------------------------

    async def _command_client(self) -> TedapiClient:
        client = await self.async_signed()
        if client is None:
            raise TedapiError("the signing key can't be read")
        return client

    async def async_set_site_limit(self, direction: str, kilowatts: float | None) -> dict[str, Any]:
        client = await self._command_client()
        if direction == "import":
            limits: SiteLimits = await client.set_import_limit(kilowatts)
        else:
            limits = await client.set_export_limit(kilowatts)
        self.site_limits = limits.to_dict()
        return self.site_limits

    async def async_delay_calibration(self, hours: int) -> dict[str, Any] | None:
        client = await self._command_client()
        await client.delay_battery_calibration(hours)
        # The status after the delay, where the firmware reports it.
        self.calibration_supported = None
        await self._read_calibration()
        return self.calibration

    async def async_remove_key(self, fingerprint: str) -> dict[str, Any] | None:
        """Remove the one key whose fingerprint starts with fingerprint. The
        key this integration signs with is refused (ValueError)."""
        client = await self._command_client()
        await client.remove_authorized_client(fingerprint)
        await self._read_keys()
        return self.keys
