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


async def test_poll_failure_masks_the_address(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    """A failed poll's error, which Home Assistant logs at error level, has
    the gateway's address and serials masked."""
    from .conftest import local  # noqa: PLC0415
    from .test_storm import _refresh  # noqa: PLC0415

    entry = await _setup(hass, V1R)

    def _down(*args, **kwargs):
        raise ConnectionError("HTTPSConnectionPool(host='192.168.1.50', port=443): Read timed out")

    fake_powerwall.fail_local = True
    local(fake_powerwall).power = _down
    caplog.clear()
    await _refresh(hass, entry)
    coordinator = entry.runtime_data.coordinator
    assert not coordinator.last_update_success
    assert "host='<ip>'" in str(coordinator.last_exception)
    assert "192.168.1.50" not in str(coordinator.last_exception)
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("Powerwall update failed" in m for m in errors)
    assert not any("192.168.1.50" in m for m in errors)


def test_library_logs_masked(caplog) -> None:
    """pypowerwall logs its errors itself, with the gateway's address in them;
    a filter masks them, also for a module that logs through its own logger
    created later (the signed client is imported only when it connects)."""
    from custom_components.tesla_tedapi import privacy  # noqa: PLC0415

    privacy.mask_library_logs()
    privacy.mask_library_logs()  # again: one filter per logger
    for name in ("pypowerwall", "pypowerwall.tedapi.tedapi_v1r", "pypowerwall.tedapi", "aiopytedapi.transport"):
        assert logging.getLogger(name).filters.count(privacy._FILTER) == 1
    v1r = logging.getLogger("pypowerwall.tedapi.tedapi_v1r")
    with caplog.at_level(logging.ERROR, logger="pypowerwall"):
        v1r.error("v1r POST error: %s", "HTTPSConnectionPool(host='192.168.1.50', port=443): Read timed out")
        logging.getLogger("pypowerwall").error("Failed to get /api/system_status/grid_status")
        try:
            raise ConnectionError("gateway 192.168.1.50 for 1707000-11-J--TG0123456789AB")
        except ConnectionError:
            v1r.exception("v1r login error")
    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["v1r POST error: HTTPSConnectionPool(host='<ip>', port=443): Read timed out",
                        "Failed to get /api/system_status/grid_status", "v1r login error"]
    assert "192.168.1.50" not in caplog.text and "TG0123456789AB" not in caplog.text
    assert "gateway <ip> for <din>" in caplog.text  # the traceback, masked


async def test_setup_masks_library_logs(hass: HomeAssistant) -> None:
    with patch("custom_components.tesla_tedapi.mask_library_logs") as mask:
        await _setup(hass, V1R)
    mask.assert_called_once()


async def test_gateway_and_fallback_messages_name_the_entry(hass: HomeAssistant, fake_powerwall,
                                                            caplog) -> None:
    """The unreachable and answers-again messages name the entry (its default
    title holds the DIN, masked), never an address."""
    from .conftest import DIN, local  # noqa: PLC0415

    entry = await _setup(hass, {**V1R, "owners_email": "a@b.c", "owners_authpath": "/tmp",
                                "wifi_host": "192.168.91.1"})
    hass.config_entries.async_update_entry(entry, title=f"Powerwall {DIN} (v1r)")
    coordinator = entry.runtime_data.coordinator

    def down():
        raise ConnectionError("gateway down")

    fake_powerwall.fail_local = True
    local(fake_powerwall).power = down
    with caplog.at_level(logging.INFO, logger="custom_components.tesla_tedapi"):
        await coordinator.async_refresh()
        fake_powerwall.fail_local = False
        del local(fake_powerwall).power
        await coordinator.async_refresh()
        coordinator._fallback_answered = "192.168.91.1"
        coordinator._fallback_down_since = 0
        coordinator._sync_fallback_issue(False, 10**6)
        coordinator._sync_fallback_issue(True, 2 * 10**6)
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.INFO)
    assert "Powerwall <din> (v1r): the gateway can't be reached" in text
    assert "Powerwall <din> (v1r): the gateway answers again" in text
    assert "Powerwall <din> (v1r): the Wi-Fi fallback address hasn't answered for over an hour" in text
    assert "Powerwall <din> (v1r): the Wi-Fi fallback address answers again" in text
    assert "192.168.1.50" not in text and "192.168.91.1" not in text and DIN not in text
