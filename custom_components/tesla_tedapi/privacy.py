"""Masking identifiers in text that goes to the log.

Error messages from libraries and Tesla's servers can carry the gateway's
DIN (which holds its serial number), battery serials, MAC and IP addresses,
energy site IDs, email addresses or tokens in a URL. Diagnostics are redacted
separately (diagnostics.py); logs are often pasted into public issues, so
warning and error texts go through scrub() before they're logged, and the
libraries' own log messages (pypowerwall's include the gateway's address)
pass through a filter that does the same.
"""
from __future__ import annotations

import importlib
import logging
import os
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


# The libraries whose own log messages are masked.
_LIBRARIES = ("pypowerwall", "aiopytedapi")


class _MaskFilter(logging.Filter):
    """Masks identifiers in a library's log message (and its traceback)."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        masked = scrub(message)
        if masked != message:
            record.msg, record.args = masked, None
        if record.exc_info and not record.exc_text:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
        return True


_FILTER = _MaskFilter()


def mask_library_logs() -> None:
    """Add the filter to every logger of the libraries, also those of modules
    imported later (pypowerwall imports its signed client only when it
    connects): a filter on a logger doesn't cover its children, so each
    module's logger is made now. Reads the packages' folders: call it in the
    executor."""
    for library in _LIBRARIES:
        try:
            package = importlib.import_module(library)
        except ImportError:
            continue
        names = {library} | {name for name in logging.root.manager.loggerDict if name.startswith(f"{library}.")}
        for folder in package.__path__:
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith(("_", "."))]
                relative = os.path.relpath(root, folder)
                prefix = library if relative == "." else f"{library}.{relative.replace(os.sep, '.')}"
                names.add(prefix)
                names.update(f"{prefix}.{file[:-3]}" for file in files
                             if file.endswith(".py") and file != "__init__.py")
        for name in names:
            logger = logging.getLogger(name)
            if _FILTER not in logger.filters:
                logger.addFilter(_FILTER)
