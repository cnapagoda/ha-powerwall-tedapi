"""2.9.0: unlinking the Tesla account also removes its saved tokens."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import stat
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_tedapi import tesla_cloud
from custom_components.tesla_tedapi.const import AUTH_FILE, DOMAIN, SITE_FILE, STORAGE_FOLDER

from .conftest import DIN

SETUP = "custom_components.tesla_tedapi.async_setup_entry"
TOKENS = {"url": "https://auth.tesla.com/", "sso": {"access_token": "at", "refresh_token": "rt"}}


def _cache(folder: Path, *emails: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / AUTH_FILE
    path.write_text(json.dumps({email: TOKENS for email in emails}))
    (folder / SITE_FILE).write_text("42")
    return path


# ---- the helper ----

def test_removes_only_this_account(tmp_path: Path) -> None:
    path = _cache(tmp_path, "me@example.com", "other@example.com")
    assert tesla_cloud.forget_account(str(tmp_path), "me@example.com", [])
    assert json.loads(path.read_text()) == {"other@example.com": TOKENS}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / f"{AUTH_FILE}.tmp").exists()


def test_last_account_removes_the_file(tmp_path: Path) -> None:
    path = _cache(tmp_path, "Me@Example.com")
    assert tesla_cloud.forget_account(str(tmp_path), "me@example.com ", [])
    assert not path.exists()
    assert not (tmp_path / SITE_FILE).exists()


def test_an_account_another_entry_uses_is_kept(tmp_path: Path) -> None:
    path = _cache(tmp_path, "me@example.com")
    others = [(str(tmp_path / "." / ""), "ME@example.com")]
    assert not tesla_cloud.forget_account(str(tmp_path), "me@example.com", others)
    assert json.loads(path.read_text()) == {"me@example.com": TOKENS}
    assert (tmp_path / SITE_FILE).exists()


def test_same_account_in_another_folder_doesnt_count(tmp_path: Path) -> None:
    path = _cache(tmp_path / "a", "me@example.com")
    assert tesla_cloud.forget_account(str(tmp_path / "a"), "me@example.com",
                                      [(str(tmp_path / "b"), "me@example.com")])
    assert not path.exists()


def test_site_pin_kept_while_another_entry_uses_the_folder(tmp_path: Path) -> None:
    path = _cache(tmp_path, "me@example.com", "other@example.com")
    assert tesla_cloud.forget_account(str(tmp_path), "me@example.com", [(str(tmp_path), "other@example.com")])
    assert json.loads(path.read_text()) == {"other@example.com": TOKENS}
    assert (tmp_path / SITE_FILE).exists()


@pytest.mark.parametrize("content", ["not json", "[1, 2]", None])
def test_a_cache_that_cant_be_read_is_left_alone(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / AUTH_FILE
    if content is not None:
        path.write_text(content)
    assert not tesla_cloud.forget_account(str(tmp_path), "me@example.com", [])
    assert (path.read_text() == content) if content is not None else not path.exists()


def test_no_email_or_unknown_account_changes_nothing(tmp_path: Path) -> None:
    path = _cache(tmp_path, "other@example.com")
    before = path.read_text()
    assert not tesla_cloud.forget_account(str(tmp_path), "", [])
    assert not tesla_cloud.forget_account(str(tmp_path), "me@example.com", [])
    assert path.read_text() == before


# ---- Unlink account ----

async def _unlink(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "unlink_account"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"backup": "owner_api"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "owners_email" not in entry.data
    assert "owners_authpath" not in entry.data
    assert "site_id" not in entry.data


def _entry(hass: HomeAssistant, folder: str, email: str = "me@example.com", unique_id: str = DIN) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=unique_id, data={
        "connection_mode": "v1r", "host": "192.168.1.50", "gw_pwd": "ABCDE12345", "rsa_key_path": "/k",
        "owners_email": email, "owners_authpath": folder, "site_id": 42})
    entry.add_to_hass(hass)
    return entry


async def test_unlink_removes_the_saved_tokens(hass: HomeAssistant, tmp_path: Path,
                                               caplog: pytest.LogCaptureFixture) -> None:
    hass.config.config_dir = str(tmp_path)
    folder = tmp_path / STORAGE_FOLDER
    path = _cache(folder, "me@example.com")
    entry = _entry(hass, str(folder))
    with caplog.at_level(logging.INFO, logger="custom_components.tesla_tedapi"):
        await _unlink(hass, entry)
    assert not path.exists()
    assert "saved tokens were removed" in caplog.text
    assert "me@example.com" not in caplog.text


async def test_unlink_keeps_another_entrys_account(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    folder = tmp_path / STORAGE_FOLDER
    path = _cache(folder, "me@example.com", "other@example.com")
    entry = _entry(hass, str(folder))
    _entry(hass, str(folder), "other@example.com", unique_id="1707000-11-J--TG0000000000OTHER")
    await _unlink(hass, entry)
    assert json.loads(path.read_text()) == {"other@example.com": TOKENS}
    assert (folder / SITE_FILE).exists()


async def test_unlink_leaves_a_cache_outside_the_integrations_folder(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path / "config")
    path = _cache(tmp_path / "elsewhere", "me@example.com")
    entry = _entry(hass, str(tmp_path / "elsewhere"))
    await _unlink(hass, entry)
    assert json.loads(path.read_text()) == {"me@example.com": TOKENS}


async def test_unlink_finishes_when_the_tokens_cant_be_removed(hass: HomeAssistant, tmp_path: Path,
                                                               caplog: pytest.LogCaptureFixture) -> None:
    hass.config.config_dir = str(tmp_path)
    folder = tmp_path / STORAGE_FOLDER
    _cache(folder, "me@example.com")
    entry = _entry(hass, str(folder))
    with patch.object(tesla_cloud, "forget_account", side_effect=PermissionError(13, "denied")):
        await _unlink(hass, entry)
    assert "saved tokens couldn't be removed" in caplog.text
    assert os.path.exists(folder / AUTH_FILE)
