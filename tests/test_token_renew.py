"""2.3.1: the Owner API access token is renewed before it expires, so no call
gets HTTP 401 (and pypowerwall's refresh warning) first."""
from __future__ import annotations

import time
from types import SimpleNamespace

from custom_components.tesla_tedapi.cloud import CloudBackend, OwnerSettings
from custom_components.tesla_tedapi.const import OWNER_RENEW_SECONDS


class FakeTesla:
    auto_refresh_url = "https://auth.tesla.com/oauth2/v3/token"
    auto_refresh_kwargs = {"client_id": "ownerapi"}

    def __init__(self, expires_in: float, refresh_token: str | None = "rt", fail: bool = False) -> None:
        self.token = {"access_token": "at", "expires_at": time.time() + expires_in}
        if refresh_token:
            self.token["refresh_token"] = refresh_token
        self.fail = fail
        self.calls: list[tuple] = []

    def refresh_token(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail:
            raise ValueError("refused")
        self.token["expires_at"] = time.time() + 8 * 3600
        return self.token


def _owner(tesla: FakeTesla) -> OwnerSettings:
    return OwnerSettings(SimpleNamespace(client=SimpleNamespace(tesla=tesla)))


def test_renews_only_near_expiry() -> None:
    fresh = FakeTesla(OWNER_RENEW_SECONDS + 600)
    assert _owner(fresh).renew_token() is False
    assert fresh.calls == []

    due = FakeTesla(OWNER_RENEW_SECONDS - 600)
    assert _owner(due).renew_token() is True
    assert due.calls == [(FakeTesla.auto_refresh_url, {"refresh_token": "rt", "client_id": "ownerapi"})]

    expired = FakeTesla(-60)
    assert _owner(expired).renew_token() is True


def test_no_refresh_token_or_client() -> None:
    assert _owner(FakeTesla(60, refresh_token=None)).renew_token() is False
    assert OwnerSettings(SimpleNamespace(client=None)).renew_token() is False


def test_failed_renewal_waits_before_trying_again() -> None:
    tesla = FakeTesla(60, fail=True)
    owner = _owner(tesla)
    assert owner.renew_token() is False
    assert owner.renew_token() is False
    assert len(tesla.calls) == 1  # not again until OWNER_RENEW_RETRY_SECONDS
    owner._renew_after = 0
    tesla.fail = False
    assert owner.renew_token() is True


def test_backend_renews_before_each_use() -> None:
    tesla = FakeTesla(60)
    backend = CloudBackend("owner_api", lambda: _owner(tesla))
    backend.get_client()
    assert len(tesla.calls) == 1
    backend.get_client()  # now fresh: no second refresh
    assert len(tesla.calls) == 1
    # Clients without a token (Tesla Fleet) are used as they are.
    assert CloudBackend("tesla_fleet", lambda: "fleet").get_client() == "fleet"
