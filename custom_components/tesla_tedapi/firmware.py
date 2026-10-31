"""Gateway firmware updates.

The version the gateway reports is remembered. When it changes, the update
is announced (event, logbook, log), and on a signed (v1r) entry the first
reading on the new firmware shows whether signed local access still works:
Tesla can change or close local access with any update."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util


def split_version(full: str | None) -> tuple[str | None, str | None]:
    """"26.34.3 f2e0e391" -> ("26.34.3", "f2e0e391")."""
    parts = str(full or "").split()
    if not parts:
        return None, None
    return parts[0], parts[1] if len(parts) > 1 else None


@dataclass
class FirmwareChange:
    previous: str
    version: str
    when: datetime


class FirmwareTracker:
    def __init__(self) -> None:
        self.version: str | None = None
        self.previous: str | None = None
        self.updated_at: datetime | None = None
        # Signed access on the current firmware: None until known.
        self.signed_ok: bool | None = None

    def update(self, full: str | None, now: datetime) -> FirmwareChange | None:
        """A reading's firmware string. The first one only sets the baseline."""
        if not full or full == self.version:
            return None
        previous, self.version = self.version, full
        if previous is None:
            return None
        self.previous, self.updated_at, self.signed_ok = previous, now, None
        return FirmwareChange(previous, full, now)

    def note_signed(self, works: bool) -> bool:
        """Whether signed access works on this firmware; True when that is news."""
        if works == self.signed_ok:
            return False
        self.signed_ok = works
        return True

    def attributes(self) -> dict[str, Any]:
        return {
            "previous_version": split_version(self.previous)[0],
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "previous": self.previous,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
                "signed_ok": self.signed_ok}

    def restore(self, stored: dict[str, Any]) -> None:
        self.version = stored.get("version")
        self.previous = stored.get("previous")
        updated = stored.get("updated_at")
        self.updated_at = dt_util.parse_datetime(updated) if isinstance(updated, str) else None
        self.signed_ok = stored.get("signed_ok")
