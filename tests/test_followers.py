"""2.6.4: several Powerwall 3s. On the signed connection pypowerwall reads a
follower Powerwall 3 over the Wi-Fi fallback only; a repair says so."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.tesla_tedapi.const import DOMAIN
from custom_components.tesla_tedapi.coordinator import FOLLOWER_MISSING_POLLS, _missing_powerwalls
from custom_components.tesla_tedapi.diagnostics import async_get_config_entry_diagnostics

from .conftest import DIN, FakePowerwall
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
