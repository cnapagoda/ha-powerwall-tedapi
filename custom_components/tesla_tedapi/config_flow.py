"""Config flow for the Powerwall TEDAPI integration.

Order: find the gateway (typed, network scan or DHCP discovery) -> gateway
password, checked at once -> how to connect:
  * Register a new key (recommended): Tesla sign-in -> key registration ->
    physical confirmation, detected automatically -> signed (v1r) connection.
  * Upload a key that is already registered: signed connection, optional Tesla
    sign-in for the cloud backup.
  * Wi-Fi password only: monitoring over the Powerwall's own Wi-Fi network,
    optional Tesla sign-in so settings can be changed through the cloud.
Reauth offers a new password, a new registration or a key upload."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import os
import shutil
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

if TYPE_CHECKING:
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from . import fleet, gateway, tesla_cloud
from .const import (
    BACKEND_FLEET,
    BACKEND_OWNER,
    CLOUD_CHOICES,
    CLOUD_WITH_OWNER,
    CLOUD_WITHOUT_FLEET,
    CLOUD_WITHOUT_OWNER,
    CONF_CLOUD_BACKENDS,
    CONF_CONNECTION_MODE,
    CONF_DISPATCH_FLOOR_RESERVE,
    CONF_DISPATCH_MAX_WATTS,
    CONF_GW_PWD,
    CONF_HOST,
    CONF_OWNERS_AUTHPATH,
    CONF_OWNERS_EMAIL,
    CONF_PROBLEM_MINUTES,
    CONF_RESTORE_EXPORT,
    CONF_RSA_KEY_PATH,
    CONF_SCAN_INTERVAL,
    CONF_SITE_ID,
    CONF_STATUS_EVENTS,
    CONF_WIFI_HOST,
    DEFAULT_HOST_WIFI,
    DEFAULT_PROBLEM_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DISPATCH_MAX_WATTS,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    MODE_V1R,
    MODE_WIFI,
    RSA_KEY_FILE,
    STORAGE_FOLDER,
    cloud_choice,
)
from .fleet import fleet_available, fleet_can_register

_LOGGER = logging.getLogger(__name__)

CONF_CALLBACK = "callback"
CONF_CLOUD = "cloud_backup"
CONF_SITE = "site"
CONF_KEY_FILE = "key_file"
CONF_FOUND = "found"
CONF_BACKUP = "backup"
MANUAL = "manual"

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
KEY_FILE = FileSelector(FileSelectorConfig(accept=".pem,.key,.txt"))

# Automatic key confirmation: check this often, for this long.
VERIFY_POLL_SECONDS = 10
# Reauth data flag: a new key was asked for in the options, nothing failed.
NEW_KEY = "new_key"
VERIFY_WINDOW_SECONDS = 600


def _register_failure(err: Exception) -> str:
    """Abort reason for a failed key registration, by its likely cause."""
    status = getattr(err, "status", None)
    if isinstance(err, fleet.FleetUnavailable):
        return "register_fleet_unavailable"
    if isinstance(err, tesla_cloud.TeslaAuthError):
        return "register_token_refused"
    if status == 404:
        return "register_wrong_site"
    if status in (408, 502, 503, 504) or "offline" in str(err).lower() or "timeout" in str(err).lower():
        return "register_unreachable"
    return "register_failed"


class CannotConnect(Exception):
    """The gateway did not answer."""


class KeyNotAccepted(Exception):
    """The gateway answered but not over signed v1r (key not verified)."""


class InvalidRsaKey(Exception):
    """The key file is missing or unreadable."""


class PasswordRequired(Exception):
    """Signed (v1r) access needs the gateway password."""


def _probe(mode: str, host: str, gw_pwd: str, rsa_key_path: str, timezone: str) -> str:
    """Connect like the integration does and return the gateway DIN."""
    import pypowerwall  # noqa: PLC0415

    if not gw_pwd:
        # pypowerwall logs in to the gateway with the password's last 5
        # characters before using the key; without it v1r cannot start.
        raise PasswordRequired
    kwargs: dict[str, Any] = {
        "host": host, "password": "", "email": "", "timezone": timezone,
        "auto_select": True, "failover": False, "gw_pwd": gw_pwd,
        # Keep pypowerwall from picking up a cloud token cache (see coordinator).
        "authpath": os.path.dirname(__file__),
    }
    if mode == MODE_V1R:
        if not os.path.isfile(rsa_key_path):
            raise InvalidRsaKey(rsa_key_path)
        kwargs["rsa_key_path"] = rsa_key_path
    try:
        pw = pypowerwall.Powerwall(**kwargs)
        if mode == MODE_V1R and getattr(pw, "tedapi_mode", None) != "v1r":
            raise KeyNotAccepted
        din = pw.din()
    except KeyNotAccepted:
        raise
    except Exception as err:  # noqa: BLE001
        raise CannotConnect(str(err)) from err
    if not din:
        if mode == MODE_V1R:
            raise KeyNotAccepted
        raise CannotConnect("no DIN")
    return din


def _check_gateway(host: str, gw_pwd: str) -> str | None:
    """Password check (customer login) and the DIN, if the firmware gives it."""
    return gateway.din(host, gw_pwd)


def _scan() -> list[dict[str, str]]:
    from pypowerwall.scan import scan  # noqa: PLC0415

    return scan(json_output=True, timeout=1.0)


def _store_key(hass: HomeAssistant, file_id: str, folder: str) -> str:
    """Check the uploaded key and copy it into the integration folder (0600)."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415
    from homeassistant.components.file_upload import process_uploaded_file  # noqa: PLC0415

    with process_uploaded_file(hass, file_id) as path:
        pem = path.read_bytes()
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as err:
        raise InvalidRsaKey("not a PEM private key") from err
    if not isinstance(key, rsa.RSAPrivateKey):
        raise InvalidRsaKey("not an RSA key")
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, RSA_KEY_FILE)
    if os.path.isfile(target):
        shutil.copy2(target, target + ".bak")
    with open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        f.write(pem)
    return target


def _gateway_errors(err: Exception) -> dict[str, str]:
    if isinstance(err, gateway.GatewayPasswordRejected):
        return {CONF_GW_PWD: "invalid_gw_pwd"}
    if isinstance(err, PasswordRequired):
        return {CONF_GW_PWD: "password_required"}
    if isinstance(err, InvalidRsaKey):
        return {"base": "invalid_rsa_key"}
    if isinstance(err, KeyNotAccepted):
        return {"base": "key_not_accepted"}
    return {"base": "cannot_connect"}


class TeslaLoginMixin:
    """Tesla sign-in (pasted callback URL) and site choice, shared by the
    config flow, the options flow and the cloud sign-in repair."""

    hass: HomeAssistant
    _login: tesla_cloud.LoginStart | None
    _token: dict[str, Any] | None
    _email: str
    _sites: list[tesla_cloud.EnergySite]
    _din: str | None

    def _init_login(self) -> None:
        self._login = None
        self._token = None
        self._email = ""
        self._sites = []

    @property
    def _folder(self) -> str:
        return self.hass.config.path(STORAGE_FOLDER)

    async def _async_logged_in(self, site: tesla_cloud.EnergySite) -> ConfigFlowResult:
        raise NotImplementedError

    async def async_step_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if self._login is None:
            self._login = await self.hass.async_add_executor_job(tesla_cloud.start_login)

        if user_input is not None:
            pasted = user_input[CONF_CALLBACK]
            # A new link is only needed once a code from this one reached
            # Tesla (codes work once). A paste that fails before that, or a
            # refresh token, leaves the link as it is, so a tab still open
            # on it keeps working.
            code_sent = False
            try:
                if tesla_cloud.is_refresh_token(pasted):
                    self._token = await self.hass.async_add_executor_job(tesla_cloud.refresh_login, pasted)
                else:
                    try:
                        code = tesla_cloud.parse_callback(pasted, self._login.state)
                    except tesla_cloud.TeslaAuthError as err:
                        _LOGGER.debug("Not a callback from the current sign-in link: %s", err)
                        errors["base"] = "invalid_callback"
                        return self._login_form(errors)
                    code_sent = True
                    self._token = await self.hass.async_add_executor_job(
                        tesla_cloud.exchange_code, code, self._login.code_verifier)
                self._email = tesla_cloud.token_email(self._token) or "tesla"
                self._sites = await self.hass.async_add_executor_job(
                    tesla_cloud.energy_sites, self._token["access_token"])
            except tesla_cloud.TeslaRefused as err:
                _LOGGER.warning("Tesla sign-in worked but the Owner API refused the token: %s", err)
                errors["base"] = "owner_api_refused"
                if code_sent:
                    self._login = await self.hass.async_add_executor_job(tesla_cloud.start_login)
            except tesla_cloud.TeslaAuthError as err:
                _LOGGER.debug("Tesla login failed: %s", err)
                if code_sent:
                    errors["base"] = "invalid_auth"
                    self._login = await self.hass.async_add_executor_job(tesla_cloud.start_login)
                else:
                    errors["base"] = "invalid_token"
            except tesla_cloud.TeslaCloudError as err:
                _LOGGER.warning("Tesla account error: %s", err)
                errors["base"] = "cloud_error"
                if code_sent:
                    self._login = await self.hass.async_add_executor_job(tesla_cloud.start_login)
            else:
                if not self._sites:
                    return self.async_abort(reason="no_sites")  # type: ignore[attr-defined]
                await self.hass.async_add_executor_job(
                    tesla_cloud.save_token, self._token, self._folder, self._email)
                matching = [s for s in self._sites if self._din and s.din == self._din]
                if matching:
                    return await self._async_site_chosen(matching[0])
                if len(self._sites) == 1:
                    return await self._async_site_chosen(self._sites[0])
                return await self.async_step_site()

        return self._login_form(errors)

    def _login_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        assert self._login
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="login",
            data_schema=vol.Schema({vol.Required(CONF_CALLBACK): str}),
            description_placeholders={"url": self._login.url},
            errors=errors,
        )

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            site = next(s for s in self._sites if str(s.site_id) == user_input[CONF_SITE])
            return await self._async_site_chosen(site)
        options = [SelectOptionDict(value=str(s.site_id), label=f"{s.name} ({s.din})") for s in self._sites]
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="site",
            data_schema=vol.Schema({vol.Required(CONF_SITE): SelectSelector(SelectSelectorConfig(options=options))}),
        )

    async def _async_site_chosen(self, site: tesla_cloud.EnergySite) -> ConfigFlowResult:
        if self._din and site.din and site.din != self._din:
            return self.async_abort(reason="wrong_powerwall")  # type: ignore[attr-defined]
        # Kept for entries made before 2.2; new entries pass the site id directly.
        await self.hass.async_add_executor_job(tesla_cloud.save_site, self._folder, site.site_id)
        return await self._async_logged_in(site)

    def _account_data(self, site: tesla_cloud.EnergySite) -> dict[str, Any]:
        return {CONF_OWNERS_EMAIL: self._email, CONF_OWNERS_AUTHPATH: self._folder, CONF_SITE_ID: site.site_id}


class TedapiConfigFlow(TeslaLoginMixin, ConfigFlow, domain=DOMAIN):
    """Set up a Powerwall."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._init_login()
        self._mode = MODE_V1R
        self._register = False
        # Register the key through the Tesla Fleet (or Teslemetry) integration
        # instead of a Tesla sign-in here.
        self._via_fleet = False
        self._cloud_wanted = False
        self._site: tesla_cloud.EnergySite | None = None
        self._public_der: bytes | None = None
        self._key_state: int | None = None
        self._verify_task: asyncio.Task | None = None
        self._din: str | None = None
        self._found: list[dict[str, str]] = []
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TedapiOptionsFlow:
        return TedapiOptionsFlow()

    # ------------------------------------------------------------------
    # Find the gateway
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["gateway", "scan"])

    async def async_step_scan(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            if user_input[CONF_FOUND] != MANUAL:
                self._data[CONF_HOST] = user_input[CONF_FOUND]
            return await self.async_step_gateway()
        try:
            self._found = await self.hass.async_add_executor_job(_scan)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Network scan failed: %s", err)
            self._found = []
        configured = {e.data.get(CONF_HOST) for e in self._async_current_entries()}
        found = [f for f in self._found if f.get("ip") and f["ip"] not in configured]
        if not found:
            return await self.async_step_gateway(errors={"base": "none_found"})
        options = [SelectOptionDict(value=f["ip"], label=f"{f['ip']} ({f.get('din') or 'Powerwall'})")
                   for f in found]
        options.append(SelectOptionDict(value=MANUAL, label=MANUAL))
        return self.async_show_form(
            step_id="scan",
            data_schema=vol.Schema({vol.Required(CONF_FOUND, default=found[0]["ip"]): SelectSelector(
                SelectSelectorConfig(options=options, translation_key="found"))}),
        )

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """A gateway joined the network: follow a known one to its new address,
        or offer to set up a new one."""
        ip = discovery_info.ip
        await self.async_set_unique_id(discovery_info.macaddress.replace(":", "").lower())
        self._abort_if_unique_id_configured()
        for entry in self._async_current_entries(include_ignore=False):
            host = entry.data.get(CONF_HOST)
            if host == ip:
                return self.async_abort(reason="already_configured")
            if entry.data.get(CONF_CONNECTION_MODE) != MODE_V1R or host == DEFAULT_HOST_WIFI \
                    or not entry.data.get(CONF_GW_PWD):
                continue
            try:
                din = await self.hass.async_add_executor_job(_check_gateway, ip, entry.data[CONF_GW_PWD])
            except (gateway.GatewayUnreachable, gateway.GatewayPasswordRejected):
                continue
            if din and din == entry.unique_id:
                _LOGGER.info("Powerwall %s moved from %s to %s", din, host, ip)
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: ip})
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="already_configured")
        self._data[CONF_HOST] = ip
        self.context["title_placeholders"] = {"host": ip}
        return await self.async_step_gateway()

    async def async_step_gateway(self, user_input: dict[str, Any] | None = None,
                                 errors: dict[str, str] | None = None) -> ConfigFlowResult:
        errors = dict(errors or {})
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            gw_pwd = user_input[CONF_GW_PWD].strip()
            try:
                self._din = await self.hass.async_add_executor_job(_check_gateway, host, gw_pwd)
            except (gateway.GatewayUnreachable, gateway.GatewayPasswordRejected) as err:
                _LOGGER.debug("Gateway check at %s failed: %s", host, err)
                errors = _gateway_errors(err)
            else:
                if self._din:
                    await self.async_set_unique_id(self._din, raise_on_progress=False)
                    self._abort_if_unique_id_configured()
                wifi_host = (user_input.get(CONF_WIFI_HOST) or "").strip()
                self._data.update({
                    CONF_HOST: host,
                    CONF_GW_PWD: gw_pwd,
                    CONF_WIFI_HOST: wifi_host if wifi_host != host else "",
                })
                return await self.async_step_access()
            self._data.update(user_input)

        host = self._data.get(CONF_HOST, "")
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=host): str,
            vol.Required(CONF_GW_PWD, default=self._data.get(CONF_GW_PWD, "")): PASSWORD,
            vol.Optional(CONF_WIFI_HOST, default=self._data.get(
                CONF_WIFI_HOST, "" if host == DEFAULT_HOST_WIFI else DEFAULT_HOST_WIFI)): str,
        })
        return self.async_show_form(step_id="gateway", data_schema=schema, errors=errors)

    def _register_options(self) -> list[str]:
        """Register through Tesla Fleet is offered when it has this Powerwall."""
        if fleet_can_register(self.hass, self._din):
            return ["register_fleet", "register"]
        return ["register"]

    async def async_step_access(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="access",
                                    menu_options=[*self._register_options(), "key_upload", "wifi"])

    # ------------------------------------------------------------------
    # Access methods
    # ------------------------------------------------------------------

    async def async_step_register(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._mode, self._register, self._via_fleet = MODE_V1R, True, False
        return await self.async_step_login()

    async def async_step_register_fleet(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._mode, self._register, self._via_fleet = MODE_V1R, True, True
        return await self.async_step_register_key()

    async def async_step_key_upload(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._mode, self._register = MODE_V1R, False
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._data[CONF_RSA_KEY_PATH] = await self.hass.async_add_executor_job(
                    _store_key, self.hass, user_input[CONF_KEY_FILE], self._folder)
            except InvalidRsaKey:
                errors[CONF_KEY_FILE] = "invalid_rsa_key"
            else:
                self._cloud_wanted = bool(user_input.get(CONF_CLOUD))
                return await self._async_connect()
        fields: dict[Any, Any] = {vol.Required(CONF_KEY_FILE): KEY_FILE}
        if self.source != SOURCE_REAUTH:
            fields[vol.Optional(CONF_CLOUD, default=False)] = bool
        return self.async_show_form(step_id="key_upload", data_schema=vol.Schema(fields), errors=errors)

    async def async_step_wifi(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._mode, self._register = MODE_WIFI, False
        if user_input is not None:
            self._data[CONF_HOST] = user_input[CONF_HOST].strip()
            self._cloud_wanted = bool(user_input.get(CONF_CLOUD))
            return await self._async_connect()
        host = self._data.get(CONF_HOST, DEFAULT_HOST_WIFI)
        return self.async_show_form(
            step_id="wifi",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=host if host == DEFAULT_HOST_WIFI else DEFAULT_HOST_WIFI): str,
                vol.Optional(CONF_CLOUD, default=False): bool,
            }),
        )

    # ------------------------------------------------------------------
    # Tesla account -> key registration
    # ------------------------------------------------------------------

    async def _async_logged_in(self, site: tesla_cloud.EnergySite) -> ConfigFlowResult:
        self._site = site
        if self._register:
            return await self.async_step_register_key()
        return await self._async_finish()

    async def _async_register(self) -> int | None:
        """Register the key (again) through Tesla Fleet or the Owner API."""
        assert self._public_der
        if self._via_fleet:
            return await fleet.async_register_key(self.hass, self._din, self._public_der)
        assert self._site and self._token
        return await self.hass.async_add_executor_job(
            tesla_cloud.register_key, self._site.site_id, self._token["access_token"], self._public_der)

    async def _async_key_state(self) -> int | None:
        assert self._public_der
        if self._via_fleet:
            return await fleet.async_key_state(self.hass, self._din, self._public_der)
        assert self._site and self._token
        return await self.hass.async_add_executor_job(
            tesla_cloud.key_state, self._site.site_id, self._token["access_token"], self._public_der)

    async def async_step_register_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        try:
            key_path, self._public_der = await self.hass.async_add_executor_job(
                tesla_cloud.ensure_rsa_key, self._folder)
            self._key_state = await self._async_register()
        except (tesla_cloud.TeslaCloudError, fleet.FleetUnavailable, fleet.FleetRegisterError) as err:
            _LOGGER.warning("Key registration failed: %s", err)
            return self.async_abort(reason=_register_failure(err))
        self._data[CONF_RSA_KEY_PATH] = key_path
        if self._key_state == tesla_cloud.KEY_VERIFIED:
            return await self._async_connect()
        return await self.async_step_verify()

    async def _async_wait_verified(self) -> int | None:
        """Check the key every VERIFY_POLL_SECONDS until the Powerwall confirms
        it; re-register once if the 10-minute window closes."""
        reregistered = False
        state = self._key_state
        deadline = self.hass.loop.time() + 2 * VERIFY_WINDOW_SECONDS
        while self.hass.loop.time() < deadline:
            await asyncio.sleep(VERIFY_POLL_SECONDS)
            try:
                state = await self._async_key_state()
                if state == tesla_cloud.KEY_TIMEOUT and not reregistered:
                    reregistered = True
                    state = await self._async_register()
            except (tesla_cloud.TeslaCloudError, fleet.FleetUnavailable, fleet.FleetRegisterError) as err:
                _LOGGER.debug("Key state check failed: %s", err)
                continue
            if state in (tesla_cloud.KEY_VERIFIED, tesla_cloud.KEY_REMOVED) \
                    or (state == tesla_cloud.KEY_TIMEOUT and reregistered):
                return state
        return state

    async def async_step_verify(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._public_der
        if self._verify_task is None:
            self._verify_task = self.hass.async_create_task(self._async_wait_verified())
        if not self._verify_task.done():
            return self.async_show_progress(
                step_id="verify",
                progress_action="confirm_key",
                progress_task=self._verify_task,
                description_placeholders={"fingerprint": tesla_cloud.fingerprint(self._public_der)[:16]},
            )
        try:
            self._key_state = self._verify_task.result()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Key confirmation failed: %s", err)
            self._key_state = None
        self._verify_task = None
        if self._key_state == tesla_cloud.KEY_VERIFIED:
            return self.async_show_progress_done(next_step_id="verified")
        return self.async_show_progress_done(next_step_id="verify_failed")

    async def async_step_verified(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_connect()

    async def async_step_verify_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            # Registering the same key again opens a new confirmation window.
            return await self.async_step_register_key()
        return self.async_show_form(step_id="verify_failed", data_schema=vol.Schema({}))

    # ------------------------------------------------------------------
    # Connect and finish
    # ------------------------------------------------------------------

    async def _async_connect(self) -> ConfigFlowResult:
        data = self._data
        try:
            din = await self.hass.async_add_executor_job(
                _probe, self._mode, data[CONF_HOST], data.get(CONF_GW_PWD, ""),
                data.get(CONF_RSA_KEY_PATH, ""), str(self.hass.config.time_zone))
        except (CannotConnect, KeyNotAccepted, InvalidRsaKey, PasswordRequired) as err:
            _LOGGER.debug("Connection check failed: %r", err)
            return self.async_show_form(
                step_id="connect_failed", data_schema=vol.Schema({}),
                errors=_gateway_errors(err))
        if self._site and self._site.din and self._site.din != din:
            return self.async_abort(reason="wrong_powerwall")
        if self.source == SOURCE_REAUTH:
            await self.async_set_unique_id(din)
            self._abort_if_unique_id_mismatch(reason="wrong_powerwall")
        else:
            await self.async_set_unique_id(din, raise_on_progress=False)
            self._abort_if_unique_id_configured()
        self._din = din
        if self._token is None and self._cloud_wanted:
            return await self.async_step_login()
        return await self._async_finish()

    async def async_step_connect_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Shown when the final connection check fails; submitting goes back to
        the gateway step (address, password) to correct it."""
        return await self.async_step_gateway()

    async def _async_finish(self) -> ConfigFlowResult:
        if not self.unique_id or not self._din:
            return await self._async_connect()
        data = dict(self._data)
        data[CONF_CONNECTION_MODE] = self._mode
        if self._mode != MODE_V1R:
            data.pop(CONF_WIFI_HOST, None)
            data.pop(CONF_RSA_KEY_PATH, None)
        if self._token is not None and self._site:
            data.update(self._account_data(self._site))
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            return self.async_update_reload_and_abort(entry, data_updates=data)
        data.setdefault(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        label = "v1r" if self._mode == MODE_V1R else "Wi-Fi"
        # A key registered through Tesla Fleet: it is also the cloud backup.
        options = {CONF_CLOUD_BACKENDS: BACKEND_FLEET} if self._via_fleet and self._token is None else {}
        return self.async_create_entry(title=f"Powerwall {self.unique_id} ({label})", data=data,
                                       options=options)

    # ------------------------------------------------------------------
    # Reauth: the gateway refuses the password or the key
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        self._data = {k: entry.data[k] for k in (CONF_HOST, CONF_GW_PWD, CONF_WIFI_HOST, CONF_RSA_KEY_PATH)
                      if k in entry.data}
        self._din = entry.unique_id
        self._mode = entry.data.get(CONF_CONNECTION_MODE, MODE_V1R)
        if entry_data.get(NEW_KEY):
            return await self.async_step_new_key()
        return await self.async_step_reauth_confirm()

    async def async_step_new_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Started from the options: register another key while the current
        one still works. The entry only changes once the new key is confirmed."""
        return self.async_show_menu(step_id="new_key", menu_options=[*self._register_options(), "key_upload"])

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="reauth_confirm",
                                    menu_options=["reauth_password", *self._register_options(), "key_upload"])

    async def async_step_reauth_password(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            gw_pwd = user_input[CONF_GW_PWD].strip()
            try:
                await self.hass.async_add_executor_job(_check_gateway, self._data[CONF_HOST], gw_pwd)
            except (gateway.GatewayUnreachable, gateway.GatewayPasswordRejected) as err:
                errors = _gateway_errors(err)
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates={CONF_GW_PWD: gw_pwd})
        return self.async_show_form(
            step_id="reauth_password",
            data_schema=vol.Schema({vol.Required(CONF_GW_PWD): PASSWORD}),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Reconfigure: change addresses, password or key; Wi-Fi entries can
    # switch to v1r by giving a key path.
    # ------------------------------------------------------------------

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        current = dict(entry.data)

        if user_input is not None:
            key_path = user_input.get(CONF_RSA_KEY_PATH, "").strip()
            mode = MODE_V1R if key_path else MODE_WIFI
            host = user_input[CONF_HOST].strip()
            gw_pwd = user_input.get(CONF_GW_PWD, "").strip()
            try:
                din = await self.hass.async_add_executor_job(
                    _probe, mode, host, gw_pwd, key_path, str(self.hass.config.time_zone))
            except (CannotConnect, KeyNotAccepted, InvalidRsaKey, PasswordRequired) as err:
                errors = _gateway_errors(err)
                if isinstance(err, InvalidRsaKey):
                    errors = {CONF_RSA_KEY_PATH: "invalid_rsa_key"}
            else:
                await self.async_set_unique_id(din)
                self._abort_if_unique_id_mismatch()
                wifi_host = user_input.get(CONF_WIFI_HOST, "").strip()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_CONNECTION_MODE: mode,
                        CONF_HOST: host,
                        CONF_GW_PWD: gw_pwd,
                        CONF_RSA_KEY_PATH: key_path,
                        CONF_WIFI_HOST: wifi_host if mode == MODE_V1R and wifi_host != host else "",
                    },
                )
            current.update(user_input)

        schema = vol.Schema({
            vol.Required(CONF_HOST, default=current.get(CONF_HOST, DEFAULT_HOST_WIFI)): str,
            vol.Required(CONF_GW_PWD, default=current.get(CONF_GW_PWD, "") or ""): PASSWORD,
            vol.Optional(CONF_RSA_KEY_PATH, default=current.get(CONF_RSA_KEY_PATH, "") or ""): str,
            vol.Optional(CONF_WIFI_HOST, default=current.get(CONF_WIFI_HOST, "") or ""): str,
        })
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)


class TedapiOptionsFlow(TeslaLoginMixin, OptionsFlow):
    """Settings, and linking or unlinking a Tesla account after setup."""

    def __init__(self) -> None:
        self._init_login()
        self._din: str | None = None

    def _backups_in_use(self) -> list[str]:
        """Cloud backups this entry uses, in order: what Unlink offers."""
        names = CLOUD_CHOICES.get(cloud_choice(self.config_entry.data, self.config_entry.options), ())
        return [name for name in names
                if name != BACKEND_OWNER or self.config_entry.data.get(CONF_OWNERS_EMAIL)]

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        menu = ["settings", "cloud_backup"]
        if not self.config_entry.data.get(CONF_OWNERS_EMAIL):
            menu.append("link_account")
        if self._backups_in_use():
            menu.append("unlink_account")
        if self.config_entry.data.get(CONF_CONNECTION_MODE, MODE_V1R) == MODE_V1R:
            menu.append("new_key")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_new_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Register another signed key (e.g. through Tesla Fleet). It goes on as
        a reauthentication, which Home Assistant shows on the integration."""
        self.config_entry.async_start_reauth(self.hass, data={**self.config_entry.data, NEW_KEY: True})
        return self.async_abort(reason="new_key_started")

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        opts = self.config_entry.options
        scan = opts.get(CONF_SCAN_INTERVAL, self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))

        def box(lo: int, hi: int, step: int, unit: str) -> NumberSelector:
            return NumberSelector(NumberSelectorConfig(
                min=lo, max=hi, step=step, unit_of_measurement=unit, mode=NumberSelectorMode.BOX))

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema({
                vol.Required(CONF_SCAN_INTERVAL, default=scan): vol.All(
                    box(MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL, 1, "s"), vol.Coerce(int)),
                vol.Required(CONF_RESTORE_EXPORT, default=opts.get(CONF_RESTORE_EXPORT, True)): bool,
                vol.Required(CONF_STATUS_EVENTS, default=opts.get(CONF_STATUS_EVENTS, False)): bool,
                vol.Required(CONF_PROBLEM_MINUTES, default=opts.get(
                    CONF_PROBLEM_MINUTES, DEFAULT_PROBLEM_MINUTES)): vol.All(box(1, 240, 1, "min"), vol.Coerce(int)),
                vol.Required(CONF_DISPATCH_FLOOR_RESERVE, default=opts.get(
                    CONF_DISPATCH_FLOOR_RESERVE, True)): bool,
                vol.Required(CONF_DISPATCH_MAX_WATTS, default=opts.get(
                    CONF_DISPATCH_MAX_WATTS, DISPATCH_MAX_WATTS)): vol.All(
                        box(100, DISPATCH_MAX_WATTS, 100, "W"), vol.Coerce(int)),
            }),
        )

    async def async_step_cloud_backup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Which cloud backup to use, and in what order when there are two:
        the Tesla account linked here (Owner API) and the Tesla Fleet
        integration's sign-in (Fleet API)."""
        if user_input is not None:
            return self.async_create_entry(
                data={**self.config_entry.options, CONF_CLOUD_BACKENDS: user_input[CONF_CLOUD_BACKENDS]})
        owner = bool(self.config_entry.data.get(CONF_OWNERS_EMAIL))
        fleet = fleet_available(self.hass, self.config_entry.unique_id)
        current = cloud_choice(self.config_entry.data, self.config_entry.options)
        choices = [choice for choice, names in CLOUD_CHOICES.items()
                   if (owner or BACKEND_OWNER not in names) and (fleet or BACKEND_FLEET not in names)]
        if current not in choices:
            # E.g. Tesla Fleet was removed: keep showing what is set.
            choices.append(current)
        return self.async_show_form(
            step_id="cloud_backup",
            data_schema=vol.Schema({vol.Required(CONF_CLOUD_BACKENDS, default=current): SelectSelector(
                SelectSelectorConfig(options=choices, translation_key="cloud_backends",
                                     mode=SelectSelectorMode.LIST))}),
            description_placeholders={
                "owner": "linked" if owner else "not linked",
                "fleet": "set up" if fleet else "not set up",
            },
        )

    def _options_with(self, table: dict[str, str]) -> dict[str, Any]:
        """The options after the account changed: a cloud backup choice that
        was set is adjusted to add or drop the Owner API."""
        options = dict(self.config_entry.options)
        choice = options.get(CONF_CLOUD_BACKENDS)
        if choice in table:
            options[CONF_CLOUD_BACKENDS] = table[choice]
        return options

    async def async_step_link_account(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._din = self.config_entry.unique_id
        return await self.async_step_login()

    async def _async_logged_in(self, site: tesla_cloud.EnergySite) -> ConfigFlowResult:
        return self._save(data={**self.config_entry.data, **self._account_data(site)},
                          options=self._options_with(CLOUD_WITH_OWNER))

    def _save(self, data: dict[str, Any], options: dict[str, Any]) -> ConfigFlowResult:
        """Data and options in one update, so the single reload it starts sees
        both (two updates reloaded with the account but the old cloud choice)."""
        self.hass.config_entries.async_update_entry(self.config_entry, data=data, options=options)
        return self.async_create_entry(data=options)

    async def async_step_unlink_account(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Stop using one cloud backup: the Owner API (the Tesla account linked
        here is removed) or Tesla Fleet (its integration is left as it is)."""
        in_use = self._backups_in_use()
        if user_input is not None:
            if user_input[CONF_BACKUP] == BACKEND_FLEET:
                return self._save(data=dict(self.config_entry.data),
                                  options=self._options_with(CLOUD_WITHOUT_FLEET))
            data = {k: v for k, v in self.config_entry.data.items()
                    if k not in (CONF_OWNERS_EMAIL, CONF_OWNERS_AUTHPATH, CONF_SITE_ID)}
            return self._save(data=data, options=self._options_with(CLOUD_WITHOUT_OWNER))
        return self.async_show_form(
            step_id="unlink_account",
            data_schema=vol.Schema({vol.Required(CONF_BACKUP, default=in_use[0]): SelectSelector(
                SelectSelectorConfig(options=in_use, translation_key="cloud_backends",
                                     mode=SelectSelectorMode.LIST))}),
        )
