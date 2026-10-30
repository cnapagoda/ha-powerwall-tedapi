"""Tesla account login and v1r key registration, used by the config flow.

Everything here is blocking and runs in the executor. The HTTP work goes through
pypowerwall, which carries Tesla's transport quirks (HTTP/2, TLS policy)."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
import time
from typing import Any
import urllib.parse

OWNER_API = "https://owner-api.teslamotors.com"

# Authorized-client key states reported by the gateway.
KEY_PENDING = 1
KEY_TIMEOUT = 2
KEY_VERIFIED = 3
KEY_REMOVED = 4


class TeslaCloudError(Exception):
    """A Tesla account or Owner API call failed. status: the HTTP status,
    if Tesla answered."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TeslaAuthError(TeslaCloudError):
    """The login code or token was rejected."""


class TeslaRefused(TeslaAuthError):
    """Signed in, but the Owner API refuses the token (HTTP 403). Tesla does
    this for tokens whose sign-in was made in a normal browser."""


@dataclass
class LoginStart:
    url: str
    code_verifier: str
    state: str


@dataclass
class EnergySite:
    site_id: int
    din: str
    name: str


# Added to pypowerwall's login URL, as the Tesla app's own sign-in does:
# prompt=login always shows Tesla's sign-in form instead of reusing a browser
# session. Codes from links without it got 403 from the Owner API in testing
# (2026-10-30), while the same steps with it got 200.
LOGIN_URL_EXTRA = {"locale": "en", "prompt": "login"}


def start_login() -> LoginStart:
    """Build the Tesla login URL (OAuth PKCE, same client as the Tesla app)."""
    from pypowerwall.tesla_auth import _build_auth_url  # noqa: PLC0415

    url, verifier, state = _build_auth_url()
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k not in LOGIN_URL_EXTRA]
    query += list(LOGIN_URL_EXTRA.items())
    return LoginStart(urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query))), verifier, state)


def parse_callback(text: str, expected_state: str | None) -> str:
    """Pull the authorization code out of a pasted callback URL (or a bare code)."""
    text = text.strip()
    if "code=" not in text:
        if text and " " not in text and "/" not in text:
            return text
        raise TeslaAuthError("no code in the pasted text")
    query = urllib.parse.urlparse(text).query or text.split("?", 1)[-1]
    params = urllib.parse.parse_qs(query)
    code = (params.get("code") or [""])[0]
    state = (params.get("state") or [""])[0]
    if not code:
        raise TeslaAuthError("no code in the pasted URL")
    if expected_state and state and state != expected_state:
        raise TeslaAuthError("the URL is from a different login attempt")
    return code


def is_refresh_token(text: str) -> bool:
    """A pasted Tesla refresh token (a long JWT) rather than a callback URL
    or an authorization code."""
    text = text.strip()
    return text.startswith("eyJ") and len(text) > 200 and "code=" not in text


def refresh_login(refresh_token: str) -> dict[str, Any]:
    """Tokens from a refresh token made by an app-style sign-in (for example
    the Auth app for Tesla). The Owner API keeps accepting tokens refreshed
    from such a sign-in, unlike one made in a normal browser."""
    from pypowerwall.tesla_auth import _refresh_access_token  # noqa: PLC0415

    refresh_token = refresh_token.strip()
    try:
        data = _refresh_access_token(refresh_token)
    except RuntimeError as err:
        raise TeslaAuthError(str(err)) from err
    data.setdefault("refresh_token", refresh_token)
    return data


def exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    """Swap the authorization code for tokens."""
    from pypowerwall.tesla_auth import _exchange_code  # noqa: PLC0415

    try:
        return _exchange_code(code, code_verifier)
    except RuntimeError as err:
        raise TeslaAuthError(str(err)) from err


def token_email(token_data: dict[str, Any]) -> str:
    """Account email from the id_token (no network)."""
    from pypowerwall.tesla_auth import _extract_email_from_token  # noqa: PLC0415

    return _extract_email_from_token(token_data.get("id_token", "")) or ""


def save_token(token_data: dict[str, Any], authpath: str, email: str) -> None:
    """Write the token cache pypowerwall's cloud mode reads (.pypowerwall.auth)."""
    from pypowerwall.tesla_auth import save_token as _save  # noqa: PLC0415

    os.makedirs(authpath, exist_ok=True)
    _save(token_data, path=os.path.join(authpath, ".pypowerwall.auth"), email=email)


def save_site(authpath: str, site_id: int) -> None:
    """Pin cloud mode to this site (.pypowerwall.site)."""
    with open(os.path.join(authpath, ".pypowerwall.site"), "w", encoding="utf-8") as f:
        f.write(str(site_id))


def _call(method: str, path: str, token: str, data: dict | None = None) -> dict[str, Any]:
    from pypowerwall.v1r_register import api_call  # noqa: PLC0415

    code, resp = api_call(f"{OWNER_API}{path}", method=method, data=data, token=token)
    if code == 403:
        raise TeslaRefused("Owner API refused the token (HTTP 403)", code)
    if code == 401:
        raise TeslaAuthError(f"Owner API rejected the token (HTTP {code})", code)
    if code != 200 or not isinstance(resp, dict):
        raise TeslaCloudError(f"Owner API HTTP {code}: {str(resp)[:200]}", code)
    return resp


def energy_sites(token: str) -> list[EnergySite]:
    resp = _call("GET", "/api/1/products", token)
    return [
        EnergySite(int(p["energy_site_id"]), str(p.get("gateway_id") or ""),
                   str(p.get("site_name") or p["energy_site_id"]))
        for p in resp.get("response", [])
        if "energy_site_id" in p
    ]


def ensure_rsa_key(folder: str) -> tuple[str, bytes]:
    """Return (private key path, public key DER), creating an RSA-4096 pair if needed."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415

    from .const import RSA_KEY_FILE, RSA_PUBLIC_KEY_FILE  # noqa: PLC0415

    os.makedirs(folder, exist_ok=True)
    private_path = os.path.join(folder, RSA_KEY_FILE)
    if os.path.isfile(private_path):
        with open(private_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        with open(os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
            f.write(pem)
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.PKCS1
    )
    with open(os.path.join(folder, RSA_PUBLIC_KEY_FILE), "wb") as f:
        f.write(public_der)
    return private_path, public_der


def fingerprint(public_der: bytes) -> str:
    return hashlib.sha256(public_der).hexdigest()


def _grpc(site_id: int, token: str, message: dict) -> dict[str, Any]:
    return _call("POST", f"/api/1/energy_sites/{site_id}/command", token, {
        "command_properties": {
            "message": {"authorization": message},
            "identifier_type": 1,
        },
        "command_type": "grpc_command",
    })


def _key_state(resp: dict[str, Any], public_der: bytes) -> int | None:
    from pypowerwall.v1r_register import _check_key_state  # noqa: PLC0415

    return _check_key_state(resp, pubkey_der=public_der)


def register_key(site_id: int, token: str, public_der: bytes) -> int | None:
    """Ask the gateway to trust this key. Returns the key state, if reported.

    Registering the same key again reopens an expired verification window."""
    resp = _grpc(site_id, token, {
        "add_authorized_client_request": {
            "key_type": 1,
            "public_key": base64.b64encode(public_der).decode(),
            "authorized_client_type": 1,
            "description": "Home Assistant",
        }
    })
    return _key_state(resp, public_der)


def key_state(site_id: int, token: str, public_der: bytes) -> int | None:
    """Current state of this key in the gateway's authorized-client list."""
    return _key_state(_grpc(site_id, token, {"list_authorized_clients_request": {}}), public_der)


def wait_for_key_state(site_id: int, token: str, public_der: bytes,
                       timeout: float = 60, delay: float = 10) -> int | None:
    """Poll until the key is verified, times out, or `timeout` seconds pass."""
    deadline = time.monotonic() + timeout
    state = None
    while True:
        state = key_state(site_id, token, public_der)
        if state in (KEY_VERIFIED, KEY_TIMEOUT, KEY_REMOVED):
            return state
        if time.monotonic() + delay > deadline:
            return state
        time.sleep(delay)


def redact_token_file(path: str) -> dict[str, Any]:
    """Summary of a token cache for diagnostics, without secrets."""
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return {"present": False}
    return {
        "present": True,
        "accounts": len(cache),
        "expires_at": [v.get("sso", {}).get("expires_at") for v in cache.values()],
    }
