"""Network status, site limits, battery calibration delay and registered keys
(extras.py, through aiopytedapi)."""
from __future__ import annotations

from unittest.mock import patch

from aiopytedapi import GatewayConnectionError, MessageFaultError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.tesla_tedapi import extras as extras_mod
from custom_components.tesla_tedapi.const import DOMAIN
from custom_components.tesla_tedapi.diagnostics import async_get_config_entry_diagnostics

from .conftest import DIN, FakeTedapi

# The real key loader (the fake_tedapi fixture replaces it in every test).
LOAD_KEY = extras_mod.load_key

V1R = {"connection_mode": "v1r", "host": "192.168.1.50", "wifi_host": "192.168.91.1",
       "rsa_key_path": "/tmp/key.pem", "gw_pwd": "ABCDE12345", "scan_interval": 30}
V1R_NO_WIFI = {**V1R, "wifi_host": ""}
WIFI = {"connection_mode": "wifi", "host": "192.168.91.1", "gw_pwd": "pw", "scan_interval": 30}


async def _setup(hass: HomeAssistant, data: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data, title="Powerwall")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _state(hass: HomeAssistant, platform: str, key: str):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DIN}_{key}")
    return hass.states.get(entity_id) if entity_id else None


def _calls(name: str, signed: bool | None = None) -> list[tuple]:
    return [call for client in FakeTedapi.instances
            if signed is None or client.signed_connection == signed
            for call in client.calls if call[0] == name]


async def _read_again(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    coordinator = entry.runtime_data.coordinator
    coordinator.extras._read_at = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_signed_connection_entities(hass: HomeAssistant) -> None:
    await _setup(hass, V1R)
    signed = [c for c in FakeTedapi.instances if c.signed_connection]
    # One signed client, on the gateway's address, with the customer login
    # password (last 5 characters) and the DIN already known.
    assert len(signed) == 1
    assert (signed[0].host, signed[0].password, signed[0].din) == ("192.168.1.50", "12345", DIN)

    network = _state(hass, "sensor", "network_connection")
    assert network.state == "wifi"
    assert network.attributes["interfaces"]["wifi"] == {
        "enabled": True, "connected": True, "internet": True, "tesla": True, "signal": 61, "state": "ready"}
    assert _state(hass, "sensor", "wifi_signal").state == "61"
    assert _state(hass, "sensor", "cellular_signal").state == "40"
    assert _state(hass, "binary_sensor", "wifi_internet").state == "on"
    assert _state(hass, "binary_sensor", "ethernet_internet").state == "off"
    cellular = _state(hass, "binary_sensor", "cellular_internet")
    assert cellular.state == "on" and cellular.attributes["tesla"] is True
    # Nothing that identifies the home: no SSID, no MAC address.
    assert "HomeNet" not in str(network.attributes) and "02:00" not in str(network.attributes)

    keys = _state(hass, "sensor", "registered_keys")
    assert keys.state == "2"
    assert keys.attributes["this_key"] == "verified"
    assert keys.attributes["states"] == {"verified": 1, "pending_verification_timeout": 1}
    assert keys.attributes["keys"][1] == {"fingerprint": "b" * 16, "type": "customer_mobile_app",
                                          "state": "pending_verification_timeout",
                                          "added": "2026-11-01T00:00:00+00:00", "this_key": False}
    assert "Phone" not in str(keys.attributes)

    calibration = _state(hass, "sensor", "calibration_delays_left")
    assert calibration.state == "3"
    assert calibration.attributes == {**calibration.attributes, "max_calibration_delays": 5,
                                      "delay_remaining_seconds": 0}
    # Site limits come from the gateway config the poll already reads: none set.
    assert _state(hass, "sensor", "site_import_limit").state == "unknown"
    export = _state(hass, "sensor", "site_export_limit")
    assert export.state == "unknown" and export.attributes["limited"] is False
    assert not _calls("limits")


async def test_site_limits_from_the_gateway_config(hass: HomeAssistant, fake_powerwall) -> None:
    original = fake_powerwall._config

    def config(self, force=False):
        result = original(self, force)
        result["site_info"].update({"max_site_meter_power_ac": 20, "min_site_meter_power_ac": -5})
        return result

    with patch.object(fake_powerwall, "_config", config):
        await _setup(hass, V1R)
    assert _state(hass, "sensor", "site_import_limit").state == "20"
    export = _state(hass, "sensor", "site_export_limit")
    assert export.state == "5" and export.attributes["limited"] is True


async def test_wifi_connection_reads_only(hass: HomeAssistant) -> None:
    """The Wi-Fi password connection: no signed client, the site limits are
    read through aiopytedapi (the poll doesn't read the config without a
    way to change settings), no calibration status."""
    await _setup(hass, WIFI)
    assert [c.signed_connection for c in FakeTedapi.instances] == [False]
    wifi = FakeTedapi.instances[0]
    assert (wifi.host, wifi.password) == ("192.168.91.1", "pw")
    assert _state(hass, "sensor", "network_connection").state == "wifi"
    assert _state(hass, "sensor", "site_export_limit").state == "5"
    assert _state(hass, "sensor", "registered_keys").attributes["this_key"] is None
    assert _state(hass, "sensor", "calibration_delays_left") is None


async def test_reads_are_spaced(hass: HomeAssistant) -> None:
    entry = await _setup(hass, V1R)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(_calls("network")) == 1
    await _read_again(hass, entry)
    assert len(_calls("network")) == 2


async def test_powerwall_2_network_status_over_gateway_wifi(hass: HomeAssistant) -> None:
    """A Powerwall 2 answers the network status only over the Gateway Wi-Fi."""
    FakeTedapi.signed_network_error = MessageFaultError("MESSAGEFAULT_ERROR_TIMEOUT")
    entry = await _setup(hass, V1R)
    assert _state(hass, "sensor", "network_connection").state == "wifi"
    assert len(_calls("network", signed=True)) == 1
    assert len(_calls("network", signed=False)) == 1
    await _read_again(hass, entry)
    # The signed connection isn't asked again.
    assert len(_calls("network", signed=True)) == 1
    assert len(_calls("network", signed=False)) == 2


async def test_powerwall_2_without_gateway_wifi(hass: HomeAssistant) -> None:
    FakeTedapi.signed_network_error = MessageFaultError("MESSAGEFAULT_ERROR_TIMEOUT")
    entry = await _setup(hass, V1R_NO_WIFI)
    assert _state(hass, "sensor", "network_connection") is None
    assert _state(hass, "binary_sensor", "wifi_internet") is None
    await _read_again(hass, entry)
    assert len(_calls("network")) == 1
    # The rest still works.
    assert _state(hass, "sensor", "registered_keys").state == "2"


async def test_wifi_fault_is_only_logged(hass: HomeAssistant) -> None:
    FakeTedapi.signed_network_error = MessageFaultError("MESSAGEFAULT_ERROR_TIMEOUT")
    entry = await _setup(hass, V1R)
    extras = entry.runtime_data.coordinator.extras
    with patch.object(FakeTedapi, "get_network_status", side_effect=MessageFaultError("busy")):
        await _read_again(hass, entry)
    assert extras.network_supported and extras.network is not None


async def test_calibration_not_supported(hass: HomeAssistant) -> None:
    FakeTedapi.calibration_supported = False
    entry = await _setup(hass, V1R)
    assert _state(hass, "sensor", "calibration_delays_left") is None
    await _read_again(hass, entry)
    assert len(_calls("calibration")) == 1
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, "delay_battery_calibration", {"delay_hours": 24}, blocking=True)
    assert err.value.translation_key == "not_supported_by_firmware"


async def test_failed_reads_keep_the_last_values(hass: HomeAssistant) -> None:
    entry = await _setup(hass, WIFI)
    FakeTedapi.error = GatewayConnectionError("no route")
    await _read_again(hass, entry)
    assert _state(hass, "sensor", "network_connection").state == "wifi"
    assert _state(hass, "sensor", "registered_keys").state == "2"
    assert _state(hass, "sensor", "site_export_limit").state == "5"


async def test_failed_signed_reads_keep_the_last_values(hass: HomeAssistant) -> None:
    entry = await _setup(hass, V1R)
    FakeTedapi.error = GatewayConnectionError("no route")
    await _read_again(hass, entry)
    assert _state(hass, "sensor", "calibration_delays_left").state == "3"


async def test_unexpected_error_doesnt_fail_the_poll(hass: HomeAssistant) -> None:
    entry = await _setup(hass, V1R)
    with patch.object(extras_mod.GatewayExtras, "async_read", side_effect=RuntimeError("bug")):
        await _read_again(hass, entry)
    assert entry.runtime_data.coordinator.last_update_success
    assert _state(hass, "sensor", "network_connection").state == "wifi"


async def test_not_read_while_the_gateway_is_down(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, "owners_email": "a@b.c", "owners_authpath": "/tmp", "site_id": 42})
    before = len(_calls("network"))
    fake_powerwall.fail_local = True
    entry.runtime_data.coordinator._pw = None
    await _read_again(hass, entry)
    assert entry.runtime_data.coordinator.last_update_success
    assert len(_calls("network")) == before


async def test_key_file_unreadable(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    """Without the key, reads use the Gateway Wi-Fi; commands fail."""
    FakeTedapi.key_fail = True
    entry = await _setup(hass, V1R)
    assert "signing key can't be read" in caplog.text
    assert [c.signed_connection for c in FakeTedapi.instances] == [False]
    assert _state(hass, "sensor", "registered_keys").state == "2"
    await _read_again(hass, entry)
    assert caplog.text.count("signing key can't be read") == 1
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, "set_site_limit", {"direction": "export", "limit_kw": 3,
                                                                  "confirm": True}, blocking=True)
    assert err.value.translation_key == "gateway_command_failed"


async def test_no_client_at_all(hass: HomeAssistant) -> None:
    """Signed mode without a readable key or a Gateway Wi-Fi password: nothing
    to read with, nothing is asked."""
    FakeTedapi.key_fail = True
    extras = extras_mod.GatewayExtras(hass, mode="v1r", host="192.168.1.50", gw_pwd="", rsa_key_path="/x")
    await extras.async_read(limits=True)
    assert FakeTedapi.instances == []
    assert not extras.network_supported
    assert (extras.network, extras.keys, extras.site_limits) == (None, None, None)


async def test_gateway_on_its_own_wifi_address(hass: HomeAssistant) -> None:
    """Signed v1r to 192.168.91.1: that address is the Gateway Wi-Fi too."""
    entry = await _setup(hass, {**V1R, "host": "192.168.91.1", "wifi_host": ""})
    assert entry.runtime_data.coordinator.extras.has_wifi


async def test_set_site_limit(hass: HomeAssistant) -> None:
    await _setup(hass, V1R)
    result = await hass.services.async_call(
        DOMAIN, "set_site_limit", {"direction": "import", "limit_kw": 3.5, "confirm": True},
        blocking=True, return_response=True)
    assert result == {"import_limit_kw": 3.5, "export_limit_kw": 5}
    assert _calls("set_import") == [("set_import", 3.5)]
    assert _state(hass, "sensor", "site_import_limit").state == "3.5"
    # Left out: no limit.
    result = await hass.services.async_call(DOMAIN, "set_site_limit", {"direction": "export", "confirm": True},
                                            blocking=True, return_response=True)
    assert result == {"import_limit_kw": 3.5, "export_limit_kw": None}
    assert _calls("set_export") == [("set_export", None)]


async def test_set_site_limit_on_a_fast_poll(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {**V1R, "scan_interval": 300})
    coordinator = entry.runtime_data.coordinator
    coordinator._full = {"site_limits": None}
    await hass.services.async_call(DOMAIN, "set_site_limit", {"direction": "import", "limit_kw": 7,
                                                              "confirm": True}, blocking=True)
    assert coordinator._full["site_limits"]["import_limit_kw"] == 7


async def test_set_site_limit_needs_confirm(hass: HomeAssistant) -> None:
    await _setup(hass, V1R)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "set_site_limit", {"direction": "import", "limit_kw": 3,
                                                                  "confirm": False}, blocking=True)
    assert err.value.translation_key == "command_not_confirmed"
    assert not _calls("set_import")


async def test_commands_need_the_signed_connection(hass: HomeAssistant) -> None:
    await _setup(hass, WIFI)
    for service, data in (("set_site_limit", {"direction": "import", "limit_kw": 3, "confirm": True}),
                          ("delay_battery_calibration", {"delay_hours": 24}),
                          ("remove_registered_key", {"fingerprint": "b" * 8, "confirm": True})):
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(DOMAIN, service, data, blocking=True)
        assert err.value.translation_key == "command_needs_v1r"


async def test_command_failure(hass: HomeAssistant) -> None:
    await _setup(hass, V1R)
    FakeTedapi.error = GatewayConnectionError("no route")
    for service, data in (("set_site_limit", {"direction": "import", "limit_kw": 3, "confirm": True}),
                          ("delay_battery_calibration", {"delay_hours": 24}),
                          ("remove_registered_key", {"fingerprint": "b" * 8, "confirm": True})):
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(DOMAIN, service, data, blocking=True)
        assert err.value.translation_key == "gateway_command_failed"
        assert err.value.translation_placeholders == {"error": "no route"}


async def test_delay_battery_calibration(hass: HomeAssistant) -> None:
    await _setup(hass, V1R)
    result = await hass.services.async_call(DOMAIN, "delay_battery_calibration", {"delay_hours": 24},
                                            blocking=True, return_response=True)
    assert _calls("delay") == [("delay", 24)]
    assert result == {"status": {"calibration_delays_left": 2, "calibration_delay_remaining_seconds": 86400,
                                 "max_calibration_delays": 5}}
    assert _state(hass, "sensor", "calibration_delays_left").state == "2"
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "delay_battery_calibration", {"delay_hours": 169}, blocking=True)


async def test_remove_registered_key(hass: HomeAssistant) -> None:
    await _setup(hass, V1R)
    result = await hass.services.async_call(
        DOMAIN, "remove_registered_key", {"fingerprint": " BBBBBBBB ", "confirm": True},
        blocking=True, return_response=True)
    assert _calls("remove") == [("remove", "bbbbbbbb")]
    assert result["registered_keys"]["count"] == 2
    # Its own key, or a prefix that matches no key: refused, with the reason.
    for prefix in ("aaaaaaaa", "cccccccc"):
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(DOMAIN, "remove_registered_key", {"fingerprint": prefix,
                                                                             "confirm": True}, blocking=True)
        assert err.value.translation_key == "key_not_removed"
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "remove_registered_key", {"fingerprint": "bbbbbbbb",
                                                                         "confirm": False}, blocking=True)
    assert err.value.translation_key == "command_not_confirmed"
    # Not a fingerprint (too short, not hex).
    for bad in ("bbbb", "not-a-fingerprint"):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(DOMAIN, "remove_registered_key", {"fingerprint": bad,
                                                                             "confirm": True}, blocking=True)


async def test_diagnostics_redact_fingerprints(hass: HomeAssistant) -> None:
    entry = await _setup(hass, V1R)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    keys = diagnostics["data"]["registered_keys"]
    assert keys["count"] == 2
    assert "bbbbbbbb" not in str(diagnostics) and "aaaaaaaa" not in str(diagnostics)
    assert diagnostics["data"]["network"]["active"] == "wifi"


def test_load_key(tmp_path) -> None:
    from aiopytedapi import RsaKey

    key = RsaKey.generate(1024)
    path = tmp_path / "key.pem"
    path.write_bytes(key.private_pem())
    assert LOAD_KEY(str(path)).fingerprint == key.fingerprint


def test_network_values_leave_out_missing_interfaces() -> None:
    from aiopytedapi import NetworkInterfaceStatus, NetworkStatus

    values = extras_mod.network_values(NetworkStatus(eth=NetworkInterfaceStatus(enabled=True)))
    assert values == {"active": "none", "interfaces": {"ethernet": {
        "enabled": True, "connected": False, "internet": False, "tesla": False, "signal": None, "state": "invalid"}}}
