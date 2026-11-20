"""2.8.2: identifiers stay out of the log (logs end up in public issues)."""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.tesla_tedapi import tesla_cloud
from custom_components.tesla_tedapi.privacy import scrub

from .test_followers import BLOCKS, FOLLOWER, _poll
from .test_storm import V1R, _setup


@pytest.mark.parametrize(("text", "expected"), [
    ("POST https://192.168.1.50/tedapi/v1r failed", "POST https://<ip>/tedapi/v1r failed"),
    ("No payload for 1707000-11-J--TG0123456789AB", "No payload for <din>"),
    ("pack TG9876543210ZZ offline", "pack <serial> offline"),
    ("mac a1:b2:c3:d4:e5:f6 and A1-B2-C3-D4-E5-F6", "mac <mac> and <mac>"),
    ("https://owner-api.teslamotors.com/api/1/energy_sites/1234567890123/live_status",
     "https://owner-api.teslamotors.com/api/1/energy_sites/<id>/live_status"),
    ("from someone@example.com", "from <email>"),
    ("url?access_token=abc.def&x=1 and refresh_token=xyz",
     "url?access_token=<redacted>&x=1 and refresh_token=<redacted>"),
    ("Authorization: Bearer eyJhbGciOi.payload.sig", "Authorization: Bearer <redacted>"),
    ("HTTP 401, the full password", "HTTP 401, the full password"),  # nothing to mask
    ("v1r response fault: MESSAGEFAULT_ERROR_TIMEOUT", "v1r response fault: MESSAGEFAULT_ERROR_TIMEOUT"),
])
def test_scrub(text, expected) -> None:
    assert scrub(text) == expected
    assert scrub(OSError(text)) == expected


def test_owner_api_error_keeps_only_status_and_tesla_error() -> None:
    body = {"response": None, "error": "device offline", "site_name": "Home",
            "energy_site_id": 1234567890123, "error_description": ""}
    with patch("pypowerwall.v1r_register.api_call", return_value=(408, body)), \
            pytest.raises(tesla_cloud.TeslaCloudError) as err:
        tesla_cloud.energy_sites("token")
    assert str(err.value) == "Owner API HTTP 408: device offline"
    with patch("pypowerwall.v1r_register.api_call", return_value=(500, "<html>oops</html>")), \
            pytest.raises(tesla_cloud.TeslaCloudError) as err:
        tesla_cloud.energy_sites("token")
    assert str(err.value) == "Owner API HTTP 500"


async def test_missing_follower_warning_has_no_serial(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    from custom_components.tesla_tedapi.coordinator import FOLLOWER_MISSING_POLLS  # noqa: PLC0415

    from .conftest import FakePowerwall  # noqa: PLC0415

    FakePowerwall.battery_blocks = BLOCKS
    entry = await _setup(hass, V1R)
    caplog.set_level(logging.WARNING)
    await _poll(hass, entry, FOLLOWER_MISSING_POLLS)
    assert "1 Powerwall 3(s) missing from the readings" in caplog.text
    assert FOLLOWER.split("--")[-1] not in caplog.text


def test_debug_toggle_leaves_pypowerwall_payload_logging_off() -> None:
    """The integration's Enable debug logging switches on the loggers in the
    manifest: not pypowerwall's, which logs the gateway's data unredacted."""
    with open("custom_components/tesla_tedapi/manifest.json", encoding="utf-8") as file:
        manifest = json.load(file)
    assert manifest["loggers"] == ["aiopytedapi"]
