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
    "IslandChecksFailed": (SEVERITY_WARNING, "Backup readiness check failed"),
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

    def problems(self, now: datetime) -> list[str]:
        """Warnings and faults that have lasted at least PROBLEM_AFTER."""
        return sorted(
            name for name, since in self.since.items()
            if describe(name)[0] in PROBLEM_SEVERITIES and now - since >= PROBLEM_AFTER
        )
