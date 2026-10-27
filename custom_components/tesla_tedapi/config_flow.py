"""Config flow for the Powerwall TEDAPI integration.

Paths:
  * Register a new key (recommended): Tesla login -> key registration ->
    physical confirmation at the Powerwall -> local connection.
  * Use a key that is already registered: local connection, optional Tesla login
    for the cloud backup.
  * Wi-Fi password only: monitoring over the Powerwall's own Wi-Fi network,
    optional Tesla login so settings can be changed through the cloud.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from . import tesla_cloud
from .const import (
    CONF_CONNECTION_MODE,
    CONF_GW_PWD,
    CONF_HOST,
    CONF_OWNERS_AUTHPATH,
    CONF_OWNERS_EMAIL,
    CONF_RSA_KEY_PATH,
    CONF_SCAN_INTERVAL,
    CONF_SITE_ID,
    CONF_WIFI_HOST,
    DEFAULT_HOST_WIFI,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    MODE_V1R,
    MODE_WIFI,
    RSA_KEY_FILE,
    STORAGE_FOLDER,
)

_LOGGER = logging.getLogger(__name__)

CONF_CALLBACK = "callback"
CONF_CLOUD = "cloud_backup"
CONF_SITE = "site"

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class CannotConnect(Exception):
    """The gateway did not answer."""


class KeyNotAccepted(Exception):
    """The gateway answered but not over signed v1r (key not verified)."""


class InvalidRsaKey(Exception):
    """The key file is missing or unreadable."""


def _probe(mode: str, host: str, gw_pwd: str, rsa_key_path: str, timezone: str) -> str:
    """Connect like the integration does and return the gateway DIN."""
    import pypowerwall  # noqa: PLC0415

    kwargs: dict[str, Any] = {
        "host": host, "password": "", "email": "", "timezone": timezone,
        "auto_select": True, "failover": False,
        # Keep pypowerwall from picking up a cloud token cache (see coordinator).
        "authpath": os.path.dirname(__file__),
    }
    if gw_pwd:
        kwargs["gw_pwd"] = gw_pwd
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


class TedapiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Powerwall."""

    VERSION = 1

    def __init__(self) -> None:
        self._mode = MODE_V1R
        self._register = False
        self._login: tesla_cloud.LoginStart | None = None
        self._token: dict[str, Any] | None = None
        self._email = ""
        self._sites: list[tesla_cloud.EnergySite] = []
        self._site: tesla_cloud.EnergySite | None = None
        self._public_der: bytes | None = None
        self._key_state: int | None = None
        self._data: dict[str, Any] = {}

    @property
    def _folder(self) -> str:
        return self.hass.config.path(STORAGE_FOLDER)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> TedapiOptionsFlow:
        return TedapiOptionsFlow()

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["register", "existing_key", "wifi"])

    async def async_step_register(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._mode, self._register = MODE_V1R, True
        return await self.async_step_login()

    async def async_step_existing_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._mode, self._register = MODE_V1R, False
        return await self.async_step_connect()

    async def async_step_wifi(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._mode, self._register = MODE_WIFI, False
        return await self.async_step_connect()

    # ------------------------------------------------------------------
    # Tesla account
    # ------------------------------------------------------------------

    async def async_step_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if self._login is None:
            self._login = await self.hass.async_add_executor_job(tesla_cloud.start_login)

        if user_input is not None:
            try:
                code = tesla_cloud.parse_callback(user_input[CONF_CALLBACK], self._login.state)
                self._token = await self.hass.async_add_executor_job(
                    tesla_cloud.exchange_code, code, self._login.code_verifier)
                self._email = tesla_cloud.token_email(self._token) or "tesla"
                self._sites = await self.hass.async_add_executor_job(
                    tesla_cloud.energy_sites, self._token["access_token"])
            except tesla_cloud.TeslaAuthError as err:
                _LOGGER.debug("Tesla login failed: %s", err)
                errors["base"] = "invalid_auth"
                self._login = None  # codes are single use: start over with a new URL
                self._login = await self.hass.async_add_executor_job(tesla_cloud.start_login)
            except tesla_cloud.TeslaCloudError as err:
                _LOGGER.warning("Tesla account error: %s", err)
                errors["base"] = "cloud_error"
            else:
                if not self._sites:
                    return self.async_abort(reason="no_sites")
                await self.hass.async_add_executor_job(
                    tesla_cloud.save_token, self._token, self._folder, self._email)
                if len(self._sites) == 1:
                    return await self._async_site_chosen(self._sites[0])
                return await self.async_step_site()

        return self.async_show_form(
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
        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema({vol.Required(CONF_SITE): SelectSelector(SelectSelectorConfig(options=options))}),
        )

    async def _async_site_chosen(self, site: tesla_cloud.EnergySite) -> ConfigFlowResult:
        self._site = site
        await self.hass.async_add_executor_job(tesla_cloud.save_site, self._folder, site.site_id)
        if self._register:
            return await self.async_step_register_key()
        return self._async_finish()

    # ------------------------------------------------------------------
    # Key registration
    # ------------------------------------------------------------------

    async def async_step_register_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._site and self._token
        try:
            key_path, self._public_der = await self.hass.async_add_executor_job(
                tesla_cloud.ensure_rsa_key, self._folder)
            self._key_state = await self.hass.async_add_executor_job(
                tesla_cloud.register_key, self._site.site_id, self._token["access_token"], self._public_der)
        except tesla_cloud.TeslaCloudError as err:
            _LOGGER.warning("Key registration failed: %s", err)
            return self.async_abort(reason="register_failed")
        self._data[CONF_RSA_KEY_PATH] = key_path
        if self._key_state == tesla_cloud.KEY_VERIFIED:
            return await self.async_step_connect()
        return await self.async_step_verify()

    async def async_step_verify(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._site and self._token and self._public_der
        errors: dict[str, str] = {}
        if user_input is not None:
            token = self._token["access_token"]
            try:
                if self._key_state == tesla_cloud.KEY_TIMEOUT:
                    # The window closed: registering the same key opens a new one.
                    self._key_state = await self.hass.async_add_executor_job(
                        tesla_cloud.register_key, self._site.site_id, token, self._public_der)
                else:
                    self._key_state = await self.hass.async_add_executor_job(
                        tesla_cloud.wait_for_key_state, self._site.site_id, token, self._public_der)
            except tesla_cloud.TeslaCloudError as err:
                _LOGGER.warning("Key state check failed: %s", err)
                errors["base"] = "cloud_error"
            else:
                if self._key_state == tesla_cloud.KEY_VERIFIED:
                    return await self.async_step_connect()
                errors["base"] = ("verify_timeout" if self._key_state == tesla_cloud.KEY_TIMEOUT
                                  else "not_verified")

        return self.async_show_form(
            step_id="verify",
            data_schema=vol.Schema({}),
            description_placeholders={"fingerprint": tesla_cloud.fingerprint(self._public_der)[:16]},
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Local connection
    # ------------------------------------------------------------------

    def _connect_schema(self, defaults: dict[str, Any]) -> vol.Schema:
        if self._mode == MODE_V1R:
            fields: dict[Any, Any] = {
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                vol.Optional(CONF_WIFI_HOST, default=defaults.get(CONF_WIFI_HOST, DEFAULT_HOST_WIFI)): str,
                vol.Optional(CONF_GW_PWD, default=defaults.get(CONF_GW_PWD, "")): PASSWORD,
            }
            if not self._register:
                fields[vol.Required(CONF_RSA_KEY_PATH, default=defaults.get(
                    CONF_RSA_KEY_PATH, self.hass.config.path(STORAGE_FOLDER, RSA_KEY_FILE)))] = str
        else:
            fields = {
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, DEFAULT_HOST_WIFI)): str,
                vol.Required(CONF_GW_PWD, default=defaults.get(CONF_GW_PWD, "")): PASSWORD,
            }
        if not self._register:
            fields[vol.Optional(CONF_CLOUD, default=defaults.get(CONF_CLOUD, False))] = bool
        return vol.Schema(fields)

    async def async_step_connect(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            gw_pwd = user_input.get(CONF_GW_PWD, "").strip()
            key_path = user_input.get(CONF_RSA_KEY_PATH, self._data.get(CONF_RSA_KEY_PATH, "")).strip()
            try:
                din = await self.hass.async_add_executor_job(
                    _probe, self._mode, host, gw_pwd, key_path, str(self.hass.config.time_zone))
            except InvalidRsaKey:
                errors[CONF_RSA_KEY_PATH] = "invalid_rsa_key"
            except KeyNotAccepted:
                errors["base"] = "key_not_accepted"
            except CannotConnect as err:
                _LOGGER.debug("Connection to %s failed: %s", host, err)
                errors["base"] = "cannot_connect"
            else:
                if self._site and self._site.din and self._site.din != din:
                    return self.async_abort(reason="wrong_powerwall")
                await self.async_set_unique_id(din)
                self._abort_if_unique_id_configured()
                self._data.update({
                    CONF_CONNECTION_MODE: self._mode,
                    CONF_HOST: host,
                    CONF_GW_PWD: gw_pwd,
                    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                })
                if self._mode == MODE_V1R:
                    wifi_host = user_input.get(CONF_WIFI_HOST, "").strip()
                    self._data[CONF_WIFI_HOST] = wifi_host if wifi_host != host else ""
                    self._data[CONF_RSA_KEY_PATH] = key_path
                if self._token is None and user_input.get(CONF_CLOUD):
                    return await self.async_step_login()
                return self._async_finish()

        return self.async_show_form(
            step_id=f"connect_{self._mode}",
            data_schema=self.add_suggested_values_to_schema(self._connect_schema({}), user_input),
            errors=errors,
        )

    # The form ids differ per mode (different help text); both submit here.
    async def async_step_connect_v1r(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_connect(user_input)

    async def async_step_connect_wifi(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_connect(user_input)

    @callback
    def _async_finish(self) -> ConfigFlowResult:
        if not self.unique_id:
            # Logged in before connecting (register path): connect first.
            return self.async_show_form(
                step_id=f"connect_{self._mode}",
                data_schema=self._connect_schema({}),
            )
        data = dict(self._data)
        if self._token is not None:
            data[CONF_OWNERS_EMAIL] = self._email
            data[CONF_OWNERS_AUTHPATH] = self._folder
            if self._site:
                data[CONF_SITE_ID] = self._site.site_id
        label = "v1r" if self._mode == MODE_V1R else "Wi-Fi"
        return self.async_create_entry(title=f"Powerwall {self.unique_id} ({label})", data=data)

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
            except InvalidRsaKey:
                errors[CONF_RSA_KEY_PATH] = "invalid_rsa_key"
            except KeyNotAccepted:
                errors["base"] = "key_not_accepted"
            except CannotConnect:
                errors["base"] = "cannot_connect"
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
            vol.Optional(CONF_RSA_KEY_PATH, default=current.get(CONF_RSA_KEY_PATH, "") or ""): str,
            vol.Optional(CONF_WIFI_HOST, default=current.get(CONF_WIFI_HOST, "") or ""): str,
            vol.Optional(CONF_GW_PWD, default=current.get(CONF_GW_PWD, "") or ""): PASSWORD,
        })
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)


class TedapiOptionsFlow(OptionsFlow):
    """Polling interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)),
            }),
        )
