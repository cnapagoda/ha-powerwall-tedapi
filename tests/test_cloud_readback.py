"""2.4.0: a change made through a cloud backup is read back, and a value
Tesla changed (the 80 % reserve cap) raises a repair."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.tesla_tedapi.const import DOMAIN

from .conftest import cloud, local
from .test_storm import ACCOUNT, V1R, _eid, _refresh, _setup


async def _entry_with_failing_local_writes(hass: HomeAssistant, fake_powerwall):
    """Readings stay local; every local write fails, so changes go through
    the Owner API backup."""
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    pw = local(fake_powerwall)
    for name in ("set_mode", "set_reserve", "set_grid_charging", "set_grid_export"):
        setattr(pw, name, lambda *_a, **_k: None)
    return entry, pw


def _due(entry, give_up: bool = False) -> None:
    for write in entry.runtime_data.coordinator.cloud_writes.values():
        write.check_at = 0
        if give_up:
            write.give_up_at = 0


def _issue(hass: HomeAssistant, entry, key: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"cloud_write_{key}_{entry.entry_id}")


async def _set_reserve(hass: HomeAssistant, value: float) -> None:
    await hass.services.async_call("number", "set_value",
                                   {"entity_id": _eid(hass, "number", "backup_reserve"), "value": value},
                                   blocking=True)


async def test_capped_reserve_raises_repair(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    entry, pw = await _entry_with_failing_local_writes(hass, fake_powerwall)
    await _set_reserve(hass, 95)
    coordinator = entry.runtime_data.coordinator
    assert cloud(fake_powerwall).writes[-1] == ("reserve", 95)
    assert "reserve" in coordinator.cloud_writes

    # Tesla applies 80 instead of 95.
    pw.settings["reserve"] = 80.0
    await _refresh(hass, entry)
    assert "reserve" in coordinator.cloud_writes  # not checked before CLOUD_VERIFY_SECONDS
    _due(entry)
    await _refresh(hass, entry)
    assert "reserve" in coordinator.cloud_writes  # still waiting until the give-up time
    assert all(b.cache is None for b in coordinator.backends)  # read fresh meanwhile
    assert _issue(hass, entry, "reserve") is None

    _due(entry, give_up=True)
    with caplog.at_level(logging.WARNING):
        await _refresh(hass, entry)
    issue = _issue(hass, entry, "reserve")
    assert issue.translation_key == "cloud_reserve_capped"
    assert issue.translation_placeholders == {"wanted": "95", "actual": "80.0", "backend": "Owner API"}
    assert "but the Powerwall reports 80.0" in caplog.text
    assert coordinator.cloud_writes == {}

    # Setting a value Tesla does apply clears it.
    await _set_reserve(hass, 50)
    pw.settings["reserve"] = 50.0
    _due(entry)
    await _refresh(hass, entry)
    assert _issue(hass, entry, "reserve") is None


async def test_confirmed_write_is_logged(hass: HomeAssistant, fake_powerwall, caplog) -> None:
    entry, pw = await _entry_with_failing_local_writes(hass, fake_powerwall)
    with caplog.at_level(logging.INFO, logger="custom_components.tesla_tedapi"):
        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": _eid(hass, "select", "operation_mode"), "option": "backup"}, blocking=True)
        assert "sent through Owner API, checking" in caplog.text
        assert "confirmed" not in caplog.text
        pw.settings["mode"] = "backup"
        _due(entry)
        await _refresh(hass, entry)
    assert "Operation mode set to backup: confirmed (Owner API)" in caplog.text
    assert entry.runtime_data.coordinator.cloud_writes == {}


@pytest.mark.parametrize(("call", "key"), [
    (("switch", "turn_on", "grid_charging", {}), "grid_charging"),
    (("select", "select_option", "grid_export", {"option": "never"}), "grid_export"),
])
async def test_other_mismatch(hass: HomeAssistant, fake_powerwall, call, key) -> None:
    """The gateway keeps reporting its old value: the general repair."""
    entry, _pw = await _entry_with_failing_local_writes(hass, fake_powerwall)
    platform, service, entity_key, data = call
    eid = _eid(hass, platform, entity_key)
    assert eid is not None
    await hass.services.async_call(platform, service, {"entity_id": eid, **data}, blocking=True)
    _due(entry, give_up=True)
    await _refresh(hass, entry)
    issue = _issue(hass, entry, key)
    assert issue.translation_key == "cloud_write_mismatch"


async def test_lower_reserve_is_not_a_cap(hass: HomeAssistant, fake_powerwall) -> None:
    """Below 80 a different value isn't the cap: the general repair."""
    entry, pw = await _entry_with_failing_local_writes(hass, fake_powerwall)
    await _set_reserve(hass, 60)
    pw.settings["reserve"] = 30.0
    _due(entry, give_up=True)
    await _refresh(hass, entry)
    assert _issue(hass, entry, "reserve").translation_key == "cloud_write_mismatch"


async def test_local_write_is_not_read_back(hass: HomeAssistant, fake_powerwall) -> None:
    entry = await _setup(hass, {**V1R, **ACCOUNT})
    await _set_reserve(hass, 95)
    assert local(fake_powerwall).writes[-1] == ("reserve", 95)
    assert entry.runtime_data.coordinator.cloud_writes == {}


async def test_new_write_replaces_pending_check(hass: HomeAssistant, fake_powerwall) -> None:
    entry, _pw = await _entry_with_failing_local_writes(hass, fake_powerwall)
    await _set_reserve(hass, 95)
    await _set_reserve(hass, 70)
    assert entry.runtime_data.coordinator.cloud_writes["reserve"].wanted == 70


async def test_unload_clears_write_repairs(hass: HomeAssistant, fake_powerwall) -> None:
    entry, pw = await _entry_with_failing_local_writes(hass, fake_powerwall)
    await _set_reserve(hass, 95)
    pw.settings["reserve"] = 80.0
    _due(entry, give_up=True)
    await _refresh(hass, entry)
    assert _issue(hass, entry, "reserve") is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, entry, "reserve") is None


@pytest.mark.parametrize(("key", "a", "b", "same"), [
    ("reserve", 80.0, 80, True),
    ("reserve", 79.6, 80, True),
    ("reserve", 80, 95, False),
    ("grid_charging", 1, True, True),
    ("grid_export", "never", "never", True),
    ("grid_export", "never", "pv_only", False),
    ("operation_mode", None, "backup", False),
])
def test_same(key, a, b, same) -> None:
    from custom_components.tesla_tedapi.coordinator import _same  # noqa: PLC0415

    assert _same(key, a, b) is same


async def test_cloud_refuses_too(hass: HomeAssistant, fake_powerwall) -> None:
    from homeassistant.exceptions import HomeAssistantError  # noqa: PLC0415

    entry, _pw = await _entry_with_failing_local_writes(hass, fake_powerwall)
    owner = cloud(fake_powerwall)
    owner.set_reserve = lambda *_a: None
    with pytest.raises(HomeAssistantError) as err:
        await _set_reserve(hass, 40)
    assert err.value.translation_key == "write_failed"
    assert entry.runtime_data.coordinator.cloud_writes == {}


async def test_starting_backup_is_skipped(hass: HomeAssistant, fake_powerwall) -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from homeassistant.exceptions import HomeAssistantError  # noqa: PLC0415

    entry, _pw = await _entry_with_failing_local_writes(hass, fake_powerwall)
    owner = entry.runtime_data.coordinator.backends[0]
    with patch.object(owner, "starting", return_value=True), pytest.raises(HomeAssistantError) as err:
        await _set_reserve(hass, 40)
    assert err.value.translation_key == "write_failed"


async def test_wifi_without_cloud_cannot_write(hass: HomeAssistant, fake_powerwall) -> None:
    from homeassistant.exceptions import HomeAssistantError  # noqa: PLC0415

    from .test_storm import WIFI  # noqa: PLC0415

    entry = await _setup(hass, WIFI)
    with pytest.raises(HomeAssistantError) as err:
        await hass.async_add_executor_job(entry.runtime_data.coordinator._set_reserve, 40)
    assert err.value.translation_key == "settings_need_control"
