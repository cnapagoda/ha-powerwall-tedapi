"""2.9.0: a reauthentication because the gateway refuses the key has its own
step; an entry without a Tesla sign-in (local only) is asked whether to keep
the sign-in made to register its new key."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi.const import AUTH_FILE, DOMAIN, REAUTH_KEY_REFUSED, STORAGE_FOLDER

from .conftest import DIN, TESLEMETRY
from .test_config_flow import _keep_signin
from .test_local_only import OWNER, V1R, _owner_patches


def _entry(hass: HomeAssistant, data: dict, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data, options=options or {}, minor_version=2,
                            title="Powerwall")
    entry.add_to_hass(hass)
    return entry


async def _reauth_key(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    result = await entry.start_reauth_flow(hass, data={**entry.data, REAUTH_KEY_REFUSED: True})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "reauth_key"
    return result


async def _register(hass: HomeAssistant, tmp_path: Path, result: dict) -> dict:
    """Register a new key through an Owner API sign-in (the token is saved
    for real, in the integration's folder)."""
    patches = _owner_patches(tmp_path)
    for p in patches:
        p.start()
    try:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "login"})
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback": "tesla://auth/callback?code=abc&state=state1"})
    finally:
        for p in reversed(patches):
            p.stop()


def _tokens(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(STORAGE_FOLDER)) / AUTH_FILE


async def test_refused_key_starts_its_own_reauth(hass: HomeAssistant, fake_powerwall) -> None:
    entry = _entry(hass, V1R)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    fake_powerwall.fail_signed = True
    for _ in range(10):
        coordinator._pw = None
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1 and flows[0]["step_id"] == "reauth_key"
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"])
    assert result["menu_options"] == ["register", "key_upload", "reauth_password"]


async def test_refused_password_keeps_the_general_reauth(hass: HomeAssistant, fake_powerwall,
                                                         fake_gateway) -> None:
    entry = _entry(hass, V1R)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    fake_powerwall.fail_signed = True
    fake_gateway.password = "ABCDE99999"
    for _ in range(3):
        coordinator._pw = None
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1 and flows[0]["step_id"] == "reauth_confirm"


async def test_reauth_key_offers_the_fleet_sign_in_it_has(hass: HomeAssistant) -> None:
    entry = _entry(hass, {**V1R, **TESLEMETRY})
    result = await _reauth_key(hass, entry)
    assert result["menu_options"] == ["register", "key_upload", "reauth_password", "fleet_signin"]


async def test_local_only_entry_forgets_the_sign_in_again(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    entry = _entry(hass, V1R, {"cloud_backends": "none"})
    with patch("custom_components.tesla_tedapi.async_setup_entry", return_value=True):
        result = await _register(hass, tmp_path, await _reauth_key(hass, entry))
        result = await _keep_signin(hass, result, "local_only")
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data["rsa_key_path"].endswith("k.pem")
    assert not {"owners_email", "owners_authpath", "site_id"} & entry.data.keys()
    assert entry.options == {"cloud_backends": "none"}
    assert not _tokens(hass).exists()


async def test_local_only_entry_can_keep_it_as_the_cloud_backup(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    entry = _entry(hass, V1R, {"cloud_backends": "none"})
    with patch("custom_components.tesla_tedapi.async_setup_entry", return_value=True):
        result = await _register(hass, tmp_path, await _reauth_key(hass, entry))
        result = await _keep_signin(hass, result, "keep_cloud")
    assert result["reason"] == "reauth_successful"
    assert entry.data["owners_email"] == OWNER and entry.data["site_id"] == 42
    # Turned on as the backup, not left at None.
    assert entry.options == {"cloud_backends": "owner_api"}
    assert OWNER in json.loads(_tokens(hass).read_text())


async def test_entry_with_a_sign_in_isnt_asked(hass: HomeAssistant, tmp_path: Path) -> None:
    """As before 2.9.0: the new sign-in replaces the account's."""
    hass.config.config_dir = str(tmp_path)
    entry = _entry(hass, {**V1R, "owners_email": "old@example.com", "owners_authpath": str(tmp_path),
                          "site_id": 7}, {"cloud_backends": "owner_api"})
    with patch("custom_components.tesla_tedapi.async_setup_entry", return_value=True):
        result = await _register(hass, tmp_path, await _reauth_key(hass, entry))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data["owners_email"] == OWNER and entry.data["site_id"] == 42
    assert entry.options == {"cloud_backends": "owner_api"}


async def test_start_over_goes_back_to_the_key_menu(hass: HomeAssistant, tmp_path: Path, fake_powerwall) -> None:
    entry = _entry(hass, V1R)
    key = tmp_path / "new.pem"
    key.write_text("x")
    result = await _reauth_key(hass, entry)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "key_upload"})
    fake_powerwall.fail_local = True
    with patch("custom_components.tesla_tedapi.config_flow._store_key", return_value=str(key)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"key_file": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"})
    assert result["step_id"] == "reauth_connect_failed"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next": "start_over"})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "reauth_key"
