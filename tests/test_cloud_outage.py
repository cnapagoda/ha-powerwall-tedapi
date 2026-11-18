"""An internet outage is not a sign-in problem: the cloud backup only counts a
failed login or read toward the "sign in again" repair while Tesla's server
answers."""
from __future__ import annotations

import logging
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
import requests

from custom_components.tesla_tedapi import cloud as cloud_mod, fleet as fleet_mod, tesla_cloud
from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import TESLEMETRY
from .test_features import ACCOUNT, V1R, _eid, _refresh, _setup
from .test_fleet import _setup as _setup_fleet

# The autouse cloud_net fixture replaces cloud.reachable; keep the real one.
_real_reachable = cloud_mod.reachable


def _issue(hass: HomeAssistant, prefix: str, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{prefix}_{entry.entry_id}")


async def _checks(hass: HomeAssistant, entry, name: str, times: int) -> None:
    """Make a backup's hourly check due and poll, times times."""
    backend = entry.runtime_data.coordinator.backend(name)
    for _ in range(times):
        backend.check_at -= 3601
        backend.cache = None
        backend.last_ok = None  # a recent good read also counts as a check
        if backend.client is not None and hasattr(backend.client, "_info"):
            backend.client._info = None  # the Fleet backup's own 30 s site_info cache
        await _refresh(hass, entry)


async def test_owner_api_outage_does_not_ask_to_sign_in(hass: HomeAssistant, fake_powerwall, cloud_net,
                                                        caplog: pytest.LogCaptureFixture) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    owner = entry.runtime_data.coordinator.backends[0]
    cloud_net.up = False
    fake_powerwall.cloud_reads_fail = True
    with caplog.at_level(logging.INFO, logger="custom_components.tesla_tedapi"):
        await _checks(hass, entry, "owner_api", 5)
    assert owner.failures == 0 and owner.offline
    assert tesla_cloud.OWNER_API in cloud_net.checked
    assert _issue(hass, "cloud_login", entry) is None
    # Not working, so setup health says so, without a repair.
    health = hass.states.get(_eid(hass, "sensor", "setup_health"))
    assert health.state == "degraded" and "cloud_backup" in health.attributes["problems"]
    assert caplog.text.count("can't be reached") == 1  # logged once, not every check

    # The internet is back: the next read works and all is well.
    cloud_net.up = True
    fake_powerwall.cloud_reads_fail = False
    with caplog.at_level(logging.INFO, logger="custom_components.tesla_tedapi"):
        await _checks(hass, entry, "owner_api", 1)
    assert owner.ok and not owner.offline
    assert "Owner API answers again" in caplog.text
    assert hass.states.get(_eid(hass, "sensor", "setup_health")).state == "ok"


async def test_login_failing_offline_is_not_counted(hass: HomeAssistant, fake_powerwall, cloud_net) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    owner = entry.runtime_data.coordinator.backends[0]
    cloud_net.up = False
    with patch.object(owner, "factory", side_effect=RuntimeError("Tesla cloud login failed")):
        for _ in range(4):
            owner.client = None
            await _checks(hass, entry, "owner_api", 1)
    assert owner.failures == 0
    assert _issue(hass, "cloud_login", entry) is None


async def test_rejected_sign_in_still_asks_to_sign_in(hass: HomeAssistant, fake_powerwall, cloud_net) -> None:
    """Tesla answers but every read fails: the sign-in is the problem."""
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    fake_powerwall.cloud_reads_fail = True
    await _checks(hass, entry, "owner_api", 3)
    assert _issue(hass, "cloud_login", entry) is not None
    assert not entry.runtime_data.coordinator.backends[0].offline


async def test_outage_after_the_repair_keeps_it_until_a_good_read(hass: HomeAssistant, fake_powerwall,
                                                                  cloud_net) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    owner = entry.runtime_data.coordinator.backends[0]
    fake_powerwall.cloud_reads_fail = True
    await _checks(hass, entry, "owner_api", 3)
    assert _issue(hass, "cloud_login", entry) is not None
    cloud_net.up = False
    await _checks(hass, entry, "owner_api", 2)
    assert owner.failures == 3 and _issue(hass, "cloud_login", entry) is not None
    cloud_net.up = True
    fake_powerwall.cloud_reads_fail = False
    await _checks(hass, entry, "owner_api", 1)
    assert _issue(hass, "cloud_login", entry) is None and owner.ok


async def test_fleet_api_outage_does_not_ask_to_sign_in(hass: HomeAssistant, fake_powerwall, fleet,
                                                        cloud_net) -> None:
    entry = await _setup_fleet(hass, {**V1R, **TESLEMETRY})
    backend = entry.runtime_data.coordinator.backend("tesla_fleet")
    cloud_net.up = False
    fleet.fail = fleet_mod.FleetError("Cannot connect to host")
    await _checks(hass, entry, "tesla_fleet", 4)
    assert backend.failures == 0 and backend.offline
    assert _issue(hass, "fleet_backup", entry) is None
    # The address checked is the Fleet API server the backup talks to.
    assert cloud_net.checked[-1].startswith("https://") and "teslemetry" in cloud_net.checked[-1]

    cloud_net.up = True
    await _checks(hass, entry, "tesla_fleet", 3)
    assert _issue(hass, "fleet_backup", entry) is not None  # Teslemetry answers and still fails


class _Answer:
    def __init__(self, status: int) -> None:
        self.status_code = status


@pytest.mark.parametrize(("answer", "expected"), [
    (_Answer(200), True),
    (_Answer(403), True),   # a refusal is still an answer
    (_Answer(404), True),
    (_Answer(503), False),  # Tesla's server is down
    (requests.ConnectionError("no route"), False),
    (requests.Timeout("slow"), False),
    (requests.exceptions.SSLError("captive portal"), False),
])
def test_reachable(answer, expected) -> None:
    fails = isinstance(answer, Exception)
    with patch("requests.head", side_effect=answer if fails else None,
               return_value=None if fails else answer) as head:
        assert _real_reachable("https://owner-api.teslamotors.com") is expected
    head.assert_called_once_with("https://owner-api.teslamotors.com", timeout=cloud_mod.PROBE_TIMEOUT,
                                 allow_redirects=False)


def test_backend_without_a_server_counts_every_failure() -> None:
    backend = cloud_mod.CloudBackend("owner_api", lambda: None)
    backend.note_failure()
    assert backend.failures == 1 and not backend.offline
