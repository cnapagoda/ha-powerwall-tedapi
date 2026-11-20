"""Masking identifiers in text that goes to the log.

Error messages from libraries and Tesla's servers can carry the gateway's
DIN (which holds its serial number), battery serials, MAC and IP addresses,
energy site IDs, email addresses or tokens in a URL. Diagnostics are redacted
separately (diagnostics.py); logs are often pasted into public issues, so
warning and error texts go through scrub() before they're logged.
"""
from __future__ import annotations

import re
from typing import Any

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Tokens or keys in a URL query or a key=value text.
    (re.compile(r"(?i)\b(access_token|refresh_token|id_token|token|code|password|secret)=[^&\s'\"]+"),
     r"\1=<redacted>"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    # A DIN (part number, revision, then the serial) and Tesla serials.
    (re.compile(r"\b\d{7}-\d{2}-[A-Z0-9]--[A-Z0-9]+\b"), "<din>"),
    (re.compile(r"\bT[A-Z]\d[A-Z0-9]{8,}\b"), "<serial>"),
    (re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b"), "<mac>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "<email>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    # Energy site and other account IDs.
    (re.compile(r"\b\d{9,}\b"), "<id>"),
)


def scrub(value: Any) -> str:
    """`value` as text with identifiers masked."""
    text = str(value)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
