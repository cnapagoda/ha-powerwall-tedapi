"""Powerwall gateway alerts: what they mean and how serious they are.

Tesla does not document these codes. The descriptions below come from the
code names and from how the alerts behave on real systems (pypowerwall and
community reports); anything not listed is shown under its raw name as
"unknown"."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

SEVERITY_STATUS = "status"    # normal operation, nothing to do
SEVERITY_NOTICE = "notice"    # unusual but harmless, or unknown
SEVERITY_WARNING = "warning"  # fine if brief (restarts, switch toggles); a problem if it lasts
SEVERITY_FAULT = "fault"      # the Powerwall reports a fault

SEVERITIES = (SEVERITY_STATUS, SEVERITY_NOTICE, SEVERITY_WARNING, SEVERITY_FAULT)
PROBLEM_SEVERITIES = (SEVERITY_WARNING, SEVERITY_FAULT)
# A warning or fault must last this long before it counts as a problem;
# restarts and the On/Off switch raise many of them for a few minutes.
PROBLEM_AFTER = timedelta(minutes=10)

CATALOGUE: dict[str, tuple[str, str]] = {
    # Status
    "SystemConnectedToGrid": (SEVERITY_STATUS, "Connected to the grid"),
    "FWUpdateSucceeded": (SEVERITY_STATUS, "The last firmware update installed successfully"),
    "BAGGR_a007_EndOfCharge": (SEVERITY_STATUS, "Battery is full"),
    "BackfeedLimited": (SEVERITY_STATUS, "Export to the grid is being limited (export setting or export limit)"),
    "ChargeOnlyFromSolarOverride": (SEVERITY_STATUS, "Charging from the grid is not allowed; charging from solar only"),
    "SelfConsumptionReservedLimit": (SEVERITY_STATUS, "Battery is at the backup reserve"),
    "PCH_a054_pwsDisabledMpptEnableLine": (
        SEVERITY_STATUS,
        "The Powerwall's built-in solar inputs (MPPT) are off, normal when solar uses a separate inverter",
    ),
    "BMS_a045_SW_SOC_Change": (SEVERITY_STATUS, "Battery recalibrated its state of charge"),
    "BMS_a089_LifeModel_StoDcrGrowth_Change": (SEVERITY_STATUS, "Battery updated its ageing estimate"),
    # Notices
    "EnergyAccumulatorIrrational": (SEVERITY_NOTICE, "An energy counter jumped implausibly"),
    # Warnings: expected during restarts or switch toggles, a problem if they last
    "IslandChecksFailed": (
        SEVERITY_WARNING,
        "Backup readiness check failed (normal for a few minutes when the battery fills or after a restart)",
    ),
    "PVInverterComms": (SEVERITY_WARNING, "Lost communication with the solar inverter"),
    "IEEE1547Trip": (SEVERITY_WARNING, "Grid-connection protection tripped (grid voltage or frequency out of range)"),
    "DisallowService": (SEVERITY_WARNING, "Gateway is not ready for service"),
    "PCH_a053_pwsDisabledInvEnableLine": (SEVERITY_WARNING, "Inverter is disabled"),
    "PCH_a055_pwsDisabledDcdcEnableLine": (SEVERITY_WARNING, "Battery DC-DC converter is disabled"),
    "PWS_a014_HvpNotActive": (SEVERITY_WARNING, "Battery high-voltage controller is not active"),
    "PWS_a021_HvpMia": (SEVERITY_WARNING, "Battery high-voltage controller is not responding"),
    "BAGGR_a005_NumBatteriesExpectedMismatch": (SEVERITY_WARNING, "Fewer battery packs found than expected"),
    "BMS_a037_SW_NVRAM_Config_Error": (SEVERITY_WARNING, "Battery management settings could not be read"),
    # Faults
    "BatteryFault": (SEVERITY_FAULT, "Battery fault"),
    "SystemShutdown": (SEVERITY_FAULT, "System shut down"),
    "PWS_a009_Rsd": (SEVERITY_FAULT, "Rapid shutdown is active (On/Off switch or rapid-shutdown button)"),
    "PWS_a014_HvpFault": (SEVERITY_FAULT, "Battery high-voltage controller fault"),
    "PWS_a044_HvpRsdFault": (SEVERITY_FAULT, "Battery rapid-shutdown fault"),
    "PWS_a045_BattVRationality": (SEVERITY_FAULT, "Battery voltage reading is implausible"),
}


# Short labels for the "Active alerts" sensor; codes not listed use their
# description (or raw name).
SHORT: dict[str, str] = {
    "FWUpdateSucceeded": "Firmware update succeeded",
    "BackfeedLimited": "Export limited",
    "ChargeOnlyFromSolarOverride": "Solar-only charging",
    "SelfConsumptionReservedLimit": "At backup reserve",
    "PCH_a054_pwsDisabledMpptEnableLine": "Built-in solar inputs off",
    "BMS_a045_SW_SOC_Change": "Charge level recalibrated",
    "BMS_a089_LifeModel_StoDcrGrowth_Change": "Ageing estimate updated",
    "EnergyAccumulatorIrrational": "Energy counter jump",
    "IslandChecksFailed": "Backup check failed",
    "PVInverterComms": "Solar inverter comms lost",
    "IEEE1547Trip": "Grid protection trip",
    "DisallowService": "Gateway not ready",
    "PCH_a053_pwsDisabledInvEnableLine": "Inverter disabled",
    "PCH_a055_pwsDisabledDcdcEnableLine": "DC-DC converter disabled",
    "PWS_a014_HvpNotActive": "Battery controller inactive",
    "PWS_a021_HvpMia": "Battery controller not responding",
    "BAGGR_a005_NumBatteriesExpectedMismatch": "Battery packs missing",
    "BMS_a037_SW_NVRAM_Config_Error": "Battery settings error",
    "PWS_a009_Rsd": "Rapid shutdown active",
    "PWS_a014_HvpFault": "Battery controller fault",
    "PWS_a044_HvpRsdFault": "Rapid-shutdown fault",
    "PWS_a045_BattVRationality": "Battery voltage implausible",
}

# Alert level sensor states: the most serious active alert ("ok" when only
# status alerts are active).
LEVELS = ("ok", SEVERITY_NOTICE, SEVERITY_WARNING, SEVERITY_FAULT)

# Warnings that are routinely brief need to last longer before they become a
# Problem/repair: "Backup check failed" shows for a few minutes most days when
# the battery reaches full while charging hard, and after restarts.
MIN_PROBLEM_AFTER: dict[str, timedelta] = {
    "IslandChecksFailed": timedelta(minutes=30),
}


def problem_after(name: str, after: timedelta) -> timedelta:
    """How long an alert must last before it counts as a Problem."""
    return max(after, MIN_PROBLEM_AFTER.get(name, timedelta(0)))
MAX_STATE_LENGTH = 255


def short(name: str) -> str:
    return SHORT.get(name) or describe(name)[1]


def level(names: list[str]) -> str:
    worst = "ok"
    for name in names:
        severity = describe(name)[0]
        if severity != SEVERITY_STATUS and LEVELS.index(severity) > LEVELS.index(worst):
            worst = severity
    return worst


def summary(names: list[str]) -> str:
    """Short labels, most serious first, within a state's 255 characters."""
    order = {s: i for i, s in enumerate(reversed(SEVERITIES))}
    labels = [short(n) for n in sorted(names, key=lambda n: (order[describe(n)[0]], short(n)))]
    text = " · ".join(labels) if labels else "None"
    if len(text) > MAX_STATE_LENGTH:
        text = text[: MAX_STATE_LENGTH - 1].rstrip(" ·") + "…"
    return text


def describe(name: str) -> tuple[str, str]:
    """(severity, description) for an alert code."""
    return CATALOGUE.get(name, (SEVERITY_NOTICE, name))


@dataclass
class AlertChange:
    kind: str  # "raised" or "cleared"
    name: str
    severity: str
    description: str
    sources: list[str]


class AlertTracker:
    """Remembers when each alert appeared, so changes and lasting problems
    can be reported."""

    def __init__(self) -> None:
        self.since: dict[str, datetime] = {}
        self.sources: dict[str, list[str]] = {}
        self._initialised = False

    def update(self, gateway: list[str] | None, packs: dict[str, list[str]], now: datetime) -> list[AlertChange]:
        """Feed one reading; returns what was raised or cleared since the last one.
        The first reading only sets the baseline (no changes reported)."""
        current: dict[str, list[str]] = {}
        for name in gateway or []:
            current.setdefault(name, []).append("gateway")
        for pack, names in packs.items():
            for name in names:
                current.setdefault(name, []).append(pack)

        changes: list[AlertChange] = []
        if self._initialised:
            for name in sorted(current.keys() - self.since.keys()):
                changes.append(AlertChange("raised", name, *describe(name), current[name]))
            for name in sorted(self.since.keys() - current.keys()):
                changes.append(AlertChange("cleared", name, *describe(name), self.sources.get(name, [])))
        self._initialised = True

        self.since = {name: self.since.get(name, now) for name in current}
        self.sources = current
        return changes

    def as_dict(self) -> dict[str, Any]:
        """Saved between restarts (alert -> first seen, and where)."""
        return {name: {"since": since.isoformat(), "sources": self.sources.get(name, [])}
                for name, since in self.since.items()}

    def restore(self, saved: dict[str, Any]) -> None:
        """Start from saved alerts: the first reading after a restart then
        reports what changed while Home Assistant was down, and alerts keep
        their original start time."""
        since: dict[str, datetime] = {}
        for name, item in saved.items():
            try:
                since[name] = datetime.fromisoformat(item["since"])
            except (KeyError, TypeError, ValueError):
                continue
            self.sources[name] = list(item.get("sources") or [])
        self.since = since
        self._initialised = bool(since)

    def details(self) -> list[dict[str, Any]]:
        order = {s: i for i, s in enumerate(reversed(SEVERITIES))}
        rows = []
        for name, since in self.since.items():
            severity, description = describe(name)
            rows.append({
                "alert": name,
                "severity": severity,
                "description": description,
                "since": since.isoformat(),
                "sources": self.sources.get(name, []),
            })
        return sorted(rows, key=lambda r: (order[r["severity"]], r["alert"]))

    def problems(self, now: datetime, after: timedelta = PROBLEM_AFTER) -> list[str]:
        """Warnings and faults that have lasted at least `after` (longer for
        alerts in MIN_PROBLEM_AFTER)."""
        return sorted(
            name for name, since in self.since.items()
            if describe(name)[0] in PROBLEM_SEVERITIES and now - since >= problem_after(name, after)
        )
