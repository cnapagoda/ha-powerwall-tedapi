"""2.6.4: several Powerwall 3s. On the signed connection pypowerwall reads a
follower Powerwall 3 over the Wi-Fi fallback only; a repair says so."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.tesla_tedapi.const import DOMAIN
from custom_components.tesla_tedapi.coordinator import (
    FOLLOWER_MISSING_POLLS,
    _extract_fans,
    _extract_pinv_value,
    _extract_strings,
    _missing_powerwalls,
    _pack_count,
)
from custom_components.tesla_tedapi.diagnostics import async_get_config_entry_diagnostics

from .conftest import DIN, FakePowerwall, local
from .test_storm import V1R, WIFI, _refresh, _setup

FOLLOWER = "1707000-11-J--TG9876543210ZZ"
BLOCKS = [{"vin": DIN, "type": "Powerwall3"}, {"vin": FOLLOWER, "type": "Powerwall3"}]
FOLLOWER_VITALS = {f"TEPINV--{FOLLOWER}": {"PINV_Fout": 50.0}}


def test_missing_powerwalls() -> None:
    vitals = {f"TEPINV--{DIN}": {}}
    assert _missing_powerwalls(None, vitals) is None
    assert _missing_powerwalls({}, vitals) is None
    assert _missing_powerwalls({"battery_blocks": BLOCKS[:1]}, vitals) is None  # one Powerwall 3
    assert _missing_powerwalls({"battery_blocks": [*BLOCKS, {"vin": "x--Y", "type": "Powerwall2"}]},
                               vitals) == ["TG9876543210ZZ"]
    assert _missing_powerwalls({"battery_blocks": BLOCKS}, {**vitals, **FOLLOWER_VITALS}) == []
    assert _missing_powerwalls({"battery_blocks": [*BLOCKS, "junk", {"type": "Powerwall3"}]},
                               vitals) == ["TG9876543210ZZ"]
    # The vitals read failed (no Powerwall 3 in it at all): not a missing follower.
    assert _missing_powerwalls({"battery_blocks": BLOCKS}, {}) is None
    assert _missing_powerwalls({"battery_blocks": BLOCKS}, {"TEPOD--x--y": {}}) is None


def test_pack_count() -> None:
    assert _pack_count(None) is None and _pack_count({}) is None
    assert _pack_count({"battery_blocks": ["junk"]}) is None
    blocks = [{"vin": DIN, "type": "Powerwall3", "battery_expansions": [{"din": "1807000-20-B--X1"}, "junk"]},
              {"vin": FOLLOWER, "type": "Powerwall3", "battery_expansions": None}, "junk"]
    assert _pack_count({"battery_blocks": blocks}) == 3  # leader, its expansion pack, follower


def test_the_leader_comes_first() -> None:
    """The follower's serial sorts before the leader's: the unnumbered fans,
    solar inputs and grid readings are still the leader's, with the follower
    in the readings or not."""
    leader, follower = "1707000-11-J--TG5", "1707000-11-J--TG1"
    vitals = {f"TEPINV--{follower}": {"PCH_FanSpeed_A": 1200, "PINV_Vout": 239.0},
              f"TEPINV--{leader}": {"PCH_FanSpeed_A": 1395, "PINV_Vout": 241.0},
              f"PVAC--{follower}": {"PVAC_PVMeasuredPower_A": 10.0},
              f"PVAC--{leader}": {"PVAC_PVMeasuredPower_A": 2000.0}}
    fans = _extract_fans(vitals, leader)
    assert fans["A"]["rpm"] == 1395.0 and fans["2A"]["rpm"] == 1200.0
    assert _extract_pinv_value(vitals, "PINV_Vout", din=leader) == 241.0
    strings = _extract_strings(vitals, leader)
    assert strings["A"]["power"] == 2000.0 and strings["2A"]["power"] == 10.0
    alone = {name: block for name, block in vitals.items() if name.endswith(leader)}
    assert _extract_fans(alone, leader)["A"]["rpm"] == 1395.0
    assert _extract_strings(alone, leader)["A"]["power"] == 2000.0
    # Without the gateway's DIN: by serial number, as before.
    assert _extract_fans(vitals)["A"]["rpm"] == 1200.0
    assert _extract_pinv_value(vitals, "PINV_Vout") == 239.0


async def _poll(hass: HomeAssistant, entry, times: int) -> None:
    for _ in range(times):
        await _refresh(hass, entry)


def _issue(hass: HomeAssistant, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"powerwall_follower_hidden_{entry.entry_id}")


async def test_hidden_follower_raises_a_repair_then_clears(hass: HomeAssistant, fake_powerwall) -> None:
    FakePowerwall.battery_blocks = BLOCKS
    entry = await _setup(hass, V1R)
    await _poll(hass, entry, FOLLOWER_MISSING_POLLS - 2)
    assert _issue(hass, entry) is None  # a Wi-Fi hiccup skips a follower now and then
    await _poll(hass, entry, 2)
    issue = _issue(hass, entry)
    assert issue is not None and issue.translation_key == "powerwall_follower_hidden"
    assert issue.translation_placeholders["count"] == "1"
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["data"]["missing_powerwalls"] == 1  # a count, not the serial
    assert "TG9876543210ZZ" not in str(diagnostics)

    FakePowerwall.extra_packs = FOLLOWER_VITALS  # read over the Wi-Fi fallback now
    await _refresh(hass, entry)
    assert _issue(hass, entry) is None
    assert entry.runtime_data.coordinator.data["missing_powerwalls"] == []


async def test_follower_repair_with_wifi_fallback_set(hass: HomeAssistant, fake_powerwall) -> None:
    FakePowerwall.battery_blocks = BLOCKS
    entry = await _setup(hass, {**V1R, "wifi_host": "192.168.91.1"})
    await _poll(hass, entry, FOLLOWER_MISSING_POLLS)
    issue = _issue(hass, entry)
    assert issue.translation_key == "powerwall_follower_wifi"
    assert issue.translation_placeholders["wifi_host"] == "192.168.91.1"


async def test_follower_repair_on_the_wifi_password_connection(hass: HomeAssistant, fake_powerwall) -> None:
    FakePowerwall.battery_blocks = BLOCKS
    entry = await _setup(hass, WIFI)
    await _poll(hass, entry, FOLLOWER_MISSING_POLLS)
    assert _issue(hass, entry).translation_key == "powerwall_follower_missing"
    await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, entry) is None


async def test_single_powerwall_never_raises_it(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, V1R)
    await _poll(hass, entry, FOLLOWER_MISSING_POLLS + 1)
    assert _issue(hass, entry) is None
    assert entry.runtime_data.coordinator.data["missing_powerwalls"] is None


async def test_signed_on_the_hotspot_reads_followers_over_it(hass: HomeAssistant, fake_powerwall) -> None:
    """The signed connection uses the Powerwall's own Wi-Fi: pypowerwall gets
    that address as wifi_host too (it reads followers through it only), with
    no failover, and the repair doesn't ask for a fallback address."""
    FakePowerwall.battery_blocks = BLOCKS
    entry = await _setup(hass, {**V1R, "host": "192.168.91.1"})
    kwargs = local(fake_powerwall).kwargs
    assert kwargs["wifi_host"] == "192.168.91.1"
    assert kwargs["failover"] is False and "wifi_v1r_fallback" not in kwargs
    await _poll(hass, entry, FOLLOWER_MISSING_POLLS)
    issue = _issue(hass, entry)
    assert issue.translation_key == "powerwall_follower_wifi"
    assert issue.translation_placeholders["wifi_host"] == "192.168.91.1"


async def test_home_network_address_alone_sets_no_wifi_host(hass: HomeAssistant, fake_powerwall) -> None:
    await _setup(hass, V1R)
    assert "wifi_host" not in local(fake_powerwall).kwargs


async def test_failed_vitals_read_is_not_a_missing_follower(hass: HomeAssistant, fake_powerwall) -> None:
    FakePowerwall.battery_blocks = BLOCKS
    entry = await _setup(hass, V1R)
    with patch.object(FakePowerwall, "vitals", side_effect=ConnectionError("gateway hiccup")):
        await _poll(hass, entry, FOLLOWER_MISSING_POLLS + 1)
        assert entry.runtime_data.coordinator.data["missing_powerwalls"] is None
    assert _issue(hass, entry) is None
