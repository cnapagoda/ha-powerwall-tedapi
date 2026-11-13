"""The local pypowerwall client: its arguments, and the check that the
pypowerwall loaded in Home Assistant can take them (another custom
integration's older pypowerwall let setup pass and every poll fail)."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import client, config_flow
from custom_components.tesla_tedapi.const import DOMAIN, MIN_PYPOWERWALL

from .conftest import DIN, FakePowerwall, local

PWD = "ABCDE12345"
V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "192.168.91.1",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": PWD, "scan_interval": 30}
UNEXPECTED = "Powerwall.__init__() got an unexpected keyword argument 'wifi_v1r_fallback'"


class Powerwall021:
    """pypowerwall 0.21's client arguments: no wifi_v1r_fallback."""

    def __init__(self, host="", password="", email="nobody@nowhere.com",  # noqa: PLR0913, PLR0917
                 timezone="America/Los_Angeles",
                 pwcacheexpire=5, timeout=5, poolmaxsize=10, cloudmode=False, siteid=None, authpath="",
                 authmode="cookie", cachefile=".powerwall", fleetapi=False, auto_select=False,
                 retry_modes=False, gw_pwd=None, rsa_key_path=None, wifi_host=None, failover=True) -> None:
        raise AssertionError("not connected in these tests")


async def _setup(hass: HomeAssistant, data: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data, title="Powerwall")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def test_minimum_matches_the_manifest() -> None:
    manifest = json.loads((Path(__file__).parents[1] / "custom_components" / DOMAIN / "manifest.json")
                          .read_text(encoding="utf-8"))
    requirement = next(r for r in manifest["requirements"] if r.startswith("pypowerwall"))
    assert client.release(re.search(r">=\s*([\d.]+)", requirement).group(1)) == MIN_PYPOWERWALL
    assert client.required() == ".".join(str(n) for n in MIN_PYPOWERWALL)


def test_release() -> None:
    assert client.release("0.25.0") == (0, 25, 0)
    assert client.release("0.25.1rc2+local.7") == (0, 25, 1)
    assert client.release("0.21.0") < MIN_PYPOWERWALL
    assert client.release(None) == () < MIN_PYPOWERWALL


def test_local_kwargs() -> None:
    folder = os.path.dirname(client.__file__)
    # Signed, with a Wi-Fi fallback at another address: fail over to it.
    kwargs = client.local_kwargs("v1r", "192.168.1.50", PWD, "/k.pem", "UTC", wifi_host="192.168.91.1",
                                 cache_seconds=5)
    assert kwargs == {
        "host": "192.168.1.50", "password": "", "email": "", "timezone": "UTC", "auto_select": True,
        "failover": True, "authpath": folder, "pwcacheexpire": 5, "gw_pwd": PWD, "rsa_key_path": "/k.pem",
        "wifi_host": "192.168.91.1", "wifi_v1r_fallback": True}
    # The connection check answers for the gateway address alone.
    check = client.local_kwargs("v1r", "192.168.1.50", PWD, "/k.pem", "UTC", wifi_host="192.168.91.1",
                                fallback=False)
    assert check["failover"] is False and "wifi_host" not in check and "wifi_v1r_fallback" not in check
    assert "pwcacheexpire" not in check
    # A Backup Gateway 1 logs in with the full password (signed only).
    assert client.local_kwargs("v1r", "h", PWD, "/k.pem", "UTC", full_login=True)["password"] == PWD
    assert client.local_kwargs("wifi", "h", PWD, "", "UTC", full_login=True)["password"] == ""
    # Signed over the Powerwall's own Wi-Fi: followers are read there, no failover.
    own = client.local_kwargs("v1r", "192.168.91.1", PWD, "/k.pem", "UTC")
    assert own["wifi_host"] == "192.168.91.1" and own["failover"] is False and "wifi_v1r_fallback" not in own
    assert "wifi_host" not in client.local_kwargs("v1r", "192.168.91.1", "", "/k.pem", "UTC")
    assert "gw_pwd" not in client.local_kwargs("v1r", "h", "", "/k.pem", "UTC")
    # Password only: no key, no fallback.
    wifi = client.local_kwargs("wifi", "192.168.91.1", PWD, "/k.pem", "UTC", wifi_host="10.0.0.2")
    assert wifi["gw_pwd"] == PWD and not {"rsa_key_path", "wifi_host", "wifi_v1r_fallback"} & set(wifi)
    assert client.local_kwargs("wifi", "h", "", "", "UTC")["gw_pwd"] == ""
    assert {"wifi_v1r_fallback", "wifi_host", "rsa_key_path", "pwcacheexpire"} <= client.ARGUMENTS


def test_ensure_supported(fake_powerwall) -> None:
    client.ensure_supported()  # the pypowerwall the tests run with
    with patch("pypowerwall.version", "0.21.0"), pytest.raises(client.ClientTooOld) as info:
        client.ensure_supported()
    assert info.value.found == "0.21.0" and info.value.missing == ()
    assert "0.21.0" in str(info.value) and client.required() in str(info.value)
    # The version says enough, but the client doesn't take an argument.
    with patch("pypowerwall.Powerwall", Powerwall021), pytest.raises(client.ClientTooOld) as info:
        client.ensure_supported()
    assert info.value.missing == ("wifi_v1r_fallback",) and "wifi_v1r_fallback" in str(info.value)


def test_probe_checks_the_client_first(tmp_path, fake_powerwall) -> None:
    with patch("pypowerwall.version", "0.21.0"), pytest.raises(client.ClientTooOld):
        config_flow._probe("v1r", "h", "", str(tmp_path / "missing.pem"), "UTC")


async def test_the_connection_check_and_polling_use_the_same_arguments(
        hass: HomeAssistant, fake_powerwall, tmp_path) -> None:
    key = tmp_path / "k.pem"
    key.write_text("x")
    await hass.async_add_executor_job(config_flow._probe, "v1r", "192.168.1.50", PWD, str(key), "UTC")
    checked = dict(local(fake_powerwall).kwargs)
    entry = await _setup(hass, {**V1R, "rsa_key_path": str(key)})
    assert entry.state is ConfigEntryState.LOADED
    polled = dict(local(fake_powerwall).kwargs)
    # Polling adds its cache time and the Wi-Fi fallback; nothing else differs.
    extra = {"pwcacheexpire", "wifi_host", "wifi_v1r_fallback"}
    assert set(polled) - set(checked) == extra and set(checked) <= set(polled)
    different = {k for k in checked if checked[k] != polled[k]}
    assert "failover" in different and different <= {"failover", "timezone"}
    assert set(polled) <= client.ARGUMENTS | {"cloudmode"}


async def test_setup_stops_on_an_older_pypowerwall(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    with patch("pypowerwall.version", "0.21.0"), caplog.at_level(logging.ERROR):
        entry = await _setup(hass, V1R)
    # An error, not a retry: only a restart without the other integration helps.
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "pypowerwall 0.21.0 is loaded" in caplog.text
    assert [p for p in fake_powerwall.instances if p.mode == "local"] == []


async def test_the_flow_stops_on_an_older_pypowerwall(hass: HomeAssistant, fake_powerwall) -> None:
    with patch("pypowerwall.version", "0.21.0"):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "pypowerwall_too_old"
    assert result["description_placeholders"] == {"found": "0.21.0", "required": client.required()}
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU


async def test_reconfigure_stops_on_an_older_pypowerwall(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    with patch("pypowerwall.version", "0.21.0"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "host": "192.168.1.50", "gw_pwd": PWD, "rsa_key_path": "/tmp/key.pem"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "pypowerwall_too_old"
    assert result["description_placeholders"] == {"found": "0.21.0", "required": client.required()}


async def test_a_client_mismatch_is_not_a_refused_key(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    """The client refusing its own arguments says nothing about the key:
    logged once, and no request to set up access again."""
    entry = await _setup(hass, V1R)
    coordinator = entry.runtime_data.coordinator
    coordinator._pw = None

    def refuse(self, *args, **kwargs):
        raise TypeError(UNEXPECTED)

    with patch.object(FakePowerwall, "__init__", refuse), caplog.at_level(logging.ERROR):
        for _ in range(12):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
    assert coordinator.last_update_success is False
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert caplog.text.count("failed before reaching the gateway") == 1
    # Any other failure to connect still counts (the login works: the key).
    with patch.object(FakePowerwall, "fail_local", True):
        for _ in range(10):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
