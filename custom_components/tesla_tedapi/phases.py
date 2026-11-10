"""Per-phase readings from the gateway's vitals.

Where the gateway reports them (it depends on the model: a Backup Gateway's
sync board and its meters do; a Powerwall 3 on its own may not), vitals carry:

- ISLAND_VL{1,2,3}N_Main / _Load: the voltage of each phase on the grid side
  and on the home side of the gateway's contactor;
- METER_{X,Y,Z}_CT{A,B,C}_I / _InstRealPower / _InstReactivePower: the current
  (A), real power (W) and reactive power (var) at each current transformer of
  the gateway's meters. Which circuit a meter measures (grid, solar, ...) is
  set at installation, so the meters keep Tesla's letters here.

Nothing is assumed about which block of vitals carries a signal: every block
is searched, and the first number found for a signal wins.

No Home Assistant imports: the extraction is tested on its own.
"""
from __future__ import annotations

from typing import Any

PHASES = ("1", "2", "3")
METERS = ("X", "Y", "Z")
CTS = ("A", "B", "C")
# What each current transformer reports: our name -> the signal's ending.
CT_SIGNALS = {"power": "InstRealPower", "current": "I", "reactive_power": "InstReactivePower"}

# A phase that isn't wired reads 0 V (or a few volts of noise); a current
# transformer that isn't fitted reads 0. Only what is in use gets sensors.
IN_USE_VOLTS = 20.0
IN_USE_AMPS = 0.05
IN_USE_WATTS = 1.0


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _find(vitals: dict[str, Any], signal: str) -> float | None:
    for block in vitals.values():
        if isinstance(block, dict) and _number(block.get(signal)):
            return float(block[signal])
    return None


def extract(vitals: dict[str, Any] | None) -> dict[str, Any]:
    """{"grid_voltage": {"1": V, ...}, "home_voltage": {...},
    "meters": {"X": {"A": {"power": W, "current": A, "reactive_power": var}}}}
    with only the signals the gateway reports a number for."""
    result: dict[str, Any] = {"grid_voltage": {}, "home_voltage": {}, "meters": {}}
    if not isinstance(vitals, dict):
        return result
    for phase in PHASES:
        for key, side in (("grid_voltage", "Main"), ("home_voltage", "Load")):
            value = _find(vitals, f"ISLAND_VL{phase}N_{side}")
            if value is not None:
                result[key][phase] = round(value, 1)
    for meter in METERS:
        for ct in CTS:
            values = {name: _find(vitals, f"METER_{meter}_CT{ct}_{ending}") for name, ending in CT_SIGNALS.items()}
            values = {name: round(value, 2) for name, value in values.items() if value is not None}
            if values:
                result["meters"].setdefault(meter, {})[ct] = values
    return result


def voltage_in_use(value: Any) -> bool:
    return _number(value) and abs(value) >= IN_USE_VOLTS


def ct_in_use(values: dict[str, Any]) -> bool:
    """Whether a current transformer measures anything now."""
    current, power = values.get("current"), values.get("power")
    return bool((_number(current) and abs(current) >= IN_USE_AMPS) or (_number(power) and abs(power) >= IN_USE_WATTS))
