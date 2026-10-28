"""Direct checks against the gateway's web API: customer login and DIN.

Used by the setup wizard (before the slower pypowerwall connection) and by
the coordinator to tell a wrong password from an unreachable gateway. All
blocking; run in the executor."""
from __future__ import annotations

import json
import ssl
from typing import Any
import urllib.error
import urllib.request

TIMEOUT = 10


class GatewayUnreachable(Exception):
    """No answer from the gateway at that address."""


class GatewayPasswordRejected(Exception):
    """The gateway answered and refused the password."""


def customer_password(gw_pwd: str) -> str:
    """The gateway's customer login uses the last 5 characters of the QR-sticker
    password (pypowerwall derives it the same way)."""
    return gw_pwd.strip()[-5:]


def _context() -> ssl.SSLContext:
    # The gateway uses a self-signed certificate.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _request(host: str, path: str, body: dict[str, Any] | None = None,
             token: str | None = None) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"https://{host}{path}", data=json.dumps(body).encode() if body is not None else None,
        headers=headers)
    try:
        with urllib.request.urlopen(req, context=_context(), timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except (OSError, ValueError) as err:
        raise GatewayUnreachable(str(err)) from err


def login(host: str, gw_pwd: str) -> str:
    """Customer login; returns the bearer token."""
    status, body = _request(host, "/api/login/Basic", {
        "username": "customer",
        "password": customer_password(gw_pwd),
        "email": "customer@customer.domain",
        "clientInfo": {"timezone": "UTC"},
    })
    if status in (401, 403):
        raise GatewayPasswordRejected
    if status != 200:
        raise GatewayUnreachable(f"login HTTP {status}")
    try:
        return json.loads(body)["token"]
    except (ValueError, KeyError) as err:
        raise GatewayUnreachable("unexpected login answer") from err


def din(host: str, gw_pwd: str) -> str | None:
    """Log in and read the gateway DIN (None if this firmware does not say)."""
    token = login(host, gw_pwd)
    status, body = _request(host, "/tedapi/din", token=token)
    text = body.decode(errors="replace").strip()
    return text if status == 200 and "--" in text else None
