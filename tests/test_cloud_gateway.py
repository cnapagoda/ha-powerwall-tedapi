"""The blocking helpers: the gateway's customer login (gateway.py) and the
Tesla account and key registration calls (tesla_cloud.py). No network: the
gateway's urlopen and pypowerwall's Tesla calls are replaced."""
from __future__ import annotations

import io
import json
import os
from unittest.mock import patch
import urllib.error

import pytest

from custom_components.tesla_tedapi import gateway, tesla_cloud

from .conftest import DIN

HOST = "192.168.91.1"


@pytest.fixture(autouse=True)
def fake_gateway():
    """Test the real gateway.py here (conftest replaces it everywhere else)."""


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status


def _urlopen(answers: dict[str, tuple[int, bytes] | Exception], seen: list):
    """urlopen that answers by path; records (path, body, headers)."""

    def urlopen(req, context=None, timeout=None):
        path = req.full_url.split(HOST, 1)[1]
        seen.append((path, json.loads(req.data) if req.data else None, dict(req.header_items())))
        answer = answers[path]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "error", {}, io.BytesIO(body))
        return FakeResponse(status, body)

    return urlopen


def test_gateway_login_and_din() -> None:
    seen: list = []
    answers = {"/api/login/Basic": (200, b'{"token": "abc"}'), "/tedapi/din": (200, f" {DIN}\n".encode())}
    with patch("urllib.request.urlopen", _urlopen(answers, seen)):
        assert gateway.din(HOST, " xxxxxABCDE ") == DIN
    login, din = seen
    assert login[1]["username"] == "customer" and login[1]["password"] == "ABCDE"
    assert din[2]["Authorization"] == "Bearer abc"


def test_gateway_din_not_reported() -> None:
    answers = {"/api/login/Basic": (200, b'{"token": "abc"}'), "/tedapi/din": (404, b"not found")}
    with patch("urllib.request.urlopen", _urlopen(answers, [])):
        assert gateway.din(HOST, "ABCDE") is None


@pytest.mark.parametrize(("answer", "error"), [
    ((401, b""), gateway.GatewayPasswordRejected),
    ((403, b""), gateway.GatewayPasswordRejected),
    ((500, b""), gateway.GatewayUnreachable),
    ((200, b"<html>"), gateway.GatewayUnreachable),
    ((200, b"{}"), gateway.GatewayUnreachable),
    (OSError("no route"), gateway.GatewayUnreachable),
])
def test_gateway_login_failures(answer, error) -> None:
    with patch("urllib.request.urlopen", _urlopen({"/api/login/Basic": answer}, [])), pytest.raises(error):
        gateway.login(HOST, "ABCDE")


def test_gateway_ignores_its_certificate() -> None:
    ctx = gateway._context()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == gateway.ssl.CERT_NONE


def test_start_login() -> None:
    with patch("pypowerwall.tesla_auth._build_auth_url", return_value=("https://auth", "verifier", "state")):
        assert tesla_cloud.start_login() == tesla_cloud.LoginStart("https://auth", "verifier", "state")


def test_parse_callback() -> None:
    url = "https://auth.tesla.com/void/callback?code=CODE&state=S1&issuer=x"
    assert tesla_cloud.parse_callback(url, "S1") == "CODE"
    assert tesla_cloud.parse_callback("code=CODE&state=S1", None) == "CODE"
    assert tesla_cloud.parse_callback("  BARECODE ", "S1") == "BARECODE"
    for text in ("", "not a code", "https://auth.tesla.com/void/callback?state=S1&code="):
        with pytest.raises(tesla_cloud.TeslaAuthError):
            tesla_cloud.parse_callback(text, "S1")
    with pytest.raises(tesla_cloud.TeslaAuthError, match="different login"):
        tesla_cloud.parse_callback(url, "S2")


def test_is_refresh_token() -> None:
    assert tesla_cloud.is_refresh_token(" eyJ" + "a" * 300 + " ")
    assert not tesla_cloud.is_refresh_token("eyJshort")
    assert not tesla_cloud.is_refresh_token("https://x/?code=eyJ" + "a" * 300)


def test_refresh_login() -> None:
    with patch("pypowerwall.tesla_auth._refresh_access_token", return_value={"access_token": "a"}):
        assert tesla_cloud.refresh_login(" r ") == {"access_token": "a", "refresh_token": "r"}
    with patch("pypowerwall.tesla_auth._refresh_access_token", side_effect=RuntimeError("401")), \
            pytest.raises(tesla_cloud.TeslaAuthError):
        tesla_cloud.refresh_login("r")


def test_exchange_code_and_email() -> None:
    with patch("pypowerwall.tesla_auth._exchange_code", return_value={"id_token": "t"}):
        assert tesla_cloud.exchange_code("c", "v") == {"id_token": "t"}
    with patch("pypowerwall.tesla_auth._exchange_code", side_effect=RuntimeError("bad code")), \
            pytest.raises(tesla_cloud.TeslaAuthError):
        tesla_cloud.exchange_code("c", "v")
    with patch("pypowerwall.tesla_auth._extract_email_from_token", return_value=None) as extract:
        assert tesla_cloud.token_email({}) == ""
    extract.assert_called_once_with("")


def test_save_token_and_site(tmp_path) -> None:
    folder = str(tmp_path / "auth")
    with patch("pypowerwall.tesla_auth.save_token") as save:
        tesla_cloud.save_token({"a": 1}, folder, "a@b.c")
    save.assert_called_once_with({"a": 1}, path=os.path.join(folder, ".pypowerwall.auth"), email="a@b.c")
    tesla_cloud.save_site(folder, 42)
    assert (tmp_path / "auth" / ".pypowerwall.site").read_text() == "42"


@pytest.mark.parametrize(("code", "resp", "error"), [
    (403, {}, tesla_cloud.TeslaRefused),
    (401, {}, tesla_cloud.TeslaAuthError),
    (404, {"error": "not found"}, tesla_cloud.TeslaCloudError),
    (200, "not json", tesla_cloud.TeslaCloudError),
])
def test_owner_api_errors(code, resp, error) -> None:
    with patch("pypowerwall.v1r_register.api_call", return_value=(code, resp)), pytest.raises(error) as err:
        tesla_cloud.energy_sites("token")
    assert err.value.status == code


def test_energy_sites() -> None:
    products = {"response": [
        {"energy_site_id": 42, "gateway_id": DIN, "site_name": "Home"},
        {"energy_site_id": 43},
        {"vin": "5YJ3"},  # a car
    ]}
    with patch("pypowerwall.v1r_register.api_call", return_value=(200, products)) as call:
        sites = tesla_cloud.energy_sites("token")
    assert sites == [tesla_cloud.EnergySite(42, DIN, "Home"), tesla_cloud.EnergySite(43, "", "43")]
    assert call.call_args.args[0] == f"{tesla_cloud.OWNER_API}/api/1/products"
    assert call.call_args.kwargs["token"] == "token"


def test_ensure_rsa_key_creates_once(tmp_path) -> None:
    folder = str(tmp_path / "keys")
    private_path, public_der = tesla_cloud.ensure_rsa_key(folder)
    assert os.stat(private_path).st_mode & 0o777 == 0o600
    # The second call reuses the key.
    assert tesla_cloud.ensure_rsa_key(folder) == (private_path, public_der)
    assert len(tesla_cloud.fingerprint(public_der)) == 64


def test_register_key_and_state() -> None:
    calls = []

    def api_call(url, method, data, token):
        calls.append((url, data))
        return 200, {"response": {}}

    with patch("pypowerwall.v1r_register.api_call", api_call), \
            patch("pypowerwall.v1r_register._check_key_state", return_value=tesla_cloud.KEY_PENDING) as check:
        assert tesla_cloud.register_key(42, "token", b"der") == tesla_cloud.KEY_PENDING
        assert tesla_cloud.key_state(42, "token", b"der") == tesla_cloud.KEY_PENDING
    url, data = calls[0]
    assert url.endswith("/api/1/energy_sites/42/command")
    assert data["command_type"] == "grpc_command"
    request = data["command_properties"]["message"]["authorization"]["add_authorized_client_request"]
    assert request["public_key"] == "ZGVy"  # base64 of b"der"
    assert "list_authorized_clients_request" in calls[1][1]["command_properties"]["message"]["authorization"]
    check.assert_called_with({"response": {}}, pubkey_der=b"der")


def test_wait_for_key_state() -> None:
    states = iter([tesla_cloud.KEY_PENDING, None, tesla_cloud.KEY_VERIFIED])
    with patch.object(tesla_cloud, "key_state", lambda *args: next(states)), \
            patch.object(tesla_cloud.time, "sleep") as sleep:
        assert tesla_cloud.wait_for_key_state(42, "t", b"k", timeout=60, delay=10) == tesla_cloud.KEY_VERIFIED
    assert sleep.call_count == 2
    # Still pending when the time runs out.
    with patch.object(tesla_cloud, "key_state", return_value=tesla_cloud.KEY_PENDING), \
            patch.object(tesla_cloud.time, "sleep") as sleep:
        assert tesla_cloud.wait_for_key_state(42, "t", b"k", timeout=5, delay=10) == tesla_cloud.KEY_PENDING
    sleep.assert_not_called()


def test_redact_token_file(tmp_path) -> None:
    path = tmp_path / ".pypowerwall.auth"
    assert tesla_cloud.redact_token_file(str(path)) == {"present": False}
    path.write_text(json.dumps({"a@b.c": {"sso": {"expires_at": 123, "refresh_token": "secret"}}}))
    summary = tesla_cloud.redact_token_file(str(path))
    assert summary == {"present": True, "accounts": 1, "expires_at": [123]}
    assert "secret" not in json.dumps(summary)
