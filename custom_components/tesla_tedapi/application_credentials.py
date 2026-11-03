"""Application credentials: the user's own Tesla developer app, for the
Tesla Fleet sign-in (cloud backup and key registration)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .fleet import AUTHORIZE_URL, SCOPES, TOKEN_URL


class TeslaFleetImplementation(AuthImplementation):
    """Tesla's OAuth server, asking only for the energy scopes."""

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        return {"scope": " ".join(SCOPES), "prompt": "login", "prompt_missing_scopes": "true"}


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    return TeslaFleetImplementation(hass, auth_domain, credential,
                                    AuthorizationServer(AUTHORIZE_URL, TOKEN_URL))


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    return {"developer_url": "https://developer.tesla.com/dashboard",
            "redirect_url": "https://my.home-assistant.io/redirect/oauth"}
