"""Fixtures: a fake pypowerwall so no test talks to a real Powerwall.

The fake follows pypowerwall 0.22's behaviour where the integration depends on
it: v1r refuses to start without a password, Powerwall 3 vitals use
pypowerwall's PVAC_*_A..F field names, and an unset export rule in the gateway
config reads as "battery_ok"."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

DIN = "1707000-11-J--TG0123456789AB"


class FakePowerwall:
    """Stands in for pypowerwall.Powerwall (local and cloud)."""

    instances: list[FakePowerwall] = []
    fail_local = False
    soe = 52.5  # raw gateway scale -> 50.0 on the app scale
    tedapi_mode = "v1r"
    alert_list = ["SystemConnectedToGrid"]
    # The export rule in the gateway's own config (None = not set).
    export_rule: str | None = "pv_only"
    extra_packs: dict[str, dict] = {}

    def __init__(self, host: str = "", cloudmode: bool = False, **kwargs: Any) -> None:
        self.kwargs = {"host": host, "cloudmode": cloudmode, **kwargs}
        self.mode = "cloud" if cloudmode else "local"
        if not cloudmode and kwargs.get("rsa_key_path") and not (kwargs.get("password") or kwargs.get("gw_pwd")):
            # pypowerwall: connect() logs this and leaves no client.
            raise ValueError("v1r mode requires password or gw_pwd")
        self.client = SimpleNamespace(tedapi=SimpleNamespace(get_config=self._config))
        self.tedapi_mode = None if cloudmode else (
            "v1r" if kwargs.get("rsa_key_path") else "wifi")
        if not cloudmode and FakePowerwall.fail_local:
            raise ConnectionError("unreachable")
        self.settings = {"mode": "self_consumption", "reserve": 20.0,
                         "grid_charging": False, "grid_export": "never"}
        self.dispatches: list[tuple[float, int]] = []
        self.resumes = 0
        self.writes: list[tuple[str, Any]] = []
        FakePowerwall.instances.append(self)

    # reads
    def power(self):
        return {"solar": 3000.0, "battery": -500.0, "site": 100.0, "load": 2600.0}

    def level(self):
        return FakePowerwall.soe

    def grid_status(self):
        return "UP"

    def _config(self, force=False):
        site_info = {"backup_reserve_percent": 24.0}
        if FakePowerwall.export_rule is not None:
            site_info["customer_preferred_export_rule"] = FakePowerwall.export_rule
        return {"site_info": site_info, "default_real_mode": "self_consumption"}

    def vitals(self):
        # Shape of pypowerwall's TEDAPI vitals for a Powerwall 3 (get_pw3_vitals).
        return {
            "TEPINV--1707000-11-J--TG0123456789AB": {"PINV_Fout": 50.01, "PINV_Vout": 241.2,
                                                     "PCH_AmbientTemp": 25.0, "PCH_heatsinkTemp": 30.0},
            "TEPOD--1707000-11-J--TG0123456789AB": {"POD_nom_full_pack_energy": 13900,
                                                    "POD_nom_energy_remaining": 7000,
                                                    "HVP_PackTempMax": 24.0, "HVP_PackTempMin": 22.0},
            **FakePowerwall.extra_packs,
            "PVAC--1707000-11-J--TG0123456789AB": {
                "PVAC_PvState_A": "PV_Active", "PVAC_PVMeasuredVoltage_A": 380.4,
                "PVAC_PVCurrent_A": 5.21, "PVAC_PVMeasuredPower_A": 1982.0,
                "PVAC_PvState_B": "Pv_Standby", "PVAC_PVMeasuredVoltage_B": 0.0,
                "PVAC_PVCurrent_B": 0.0, "PVAC_PVMeasuredPower_B": 0.0,
                # Unused input: standing voltage, standby.
                "PVAC_PvState_C": "Pv_Standby", "PVAC_PVMeasuredVoltage_C": 15,
                "PVAC_PVCurrent_C": 0, "PVAC_PVMeasuredPower_C": 0,
                "PVAC_Fout": 50.0, "PVAC_Pout": 1982.0,
            },
        }

    def version(self):
        return "26.34.1 dcee474d"

    def din(self):
        return DIN

    def poll(self, path):
        return {"site": {"energy_imported": 1_000_000, "energy_exported": 2_000_000},
                "battery": {"energy_imported": 500_000, "energy_exported": 450_000},
                "solar": {"energy_exported": 3_000_000}, "load": {"energy_imported": 1_500_000}}

    def system_status(self):
        return {"nominal_full_pack_energy": 13900, "nominal_energy_remaining": 7000}

    def status(self):
        return {"start_time": "2026-10-22 07:26:37 +1030"}

    def alerts(self):
        return list(FakePowerwall.alert_list)

    def get_time_remaining(self):
        return 12.5

    def get_mode(self):
        return self.settings["mode"]

    def get_reserve(self):
        return self.settings["reserve"]

    def get_grid_charging(self):
        return self.settings["grid_charging"]

    def get_grid_export(self):
        if self.mode == "cloud":
            return self.settings["grid_export"]
        return FakePowerwall.export_rule or "battery_ok"

    # writes
    def set_mode(self, mode):
        self.writes.append(("mode", mode))
        self.settings["mode"] = mode
        return {"ok": True}

    def set_reserve(self, level):
        self.writes.append(("reserve", level))
        self.settings["reserve"] = level
        return {"ok": True}

    def set_grid_charging(self, mode):
        self.writes.append(("grid_charging", mode))
        self.settings["grid_charging"] = mode
        return {"ok": True}

    def set_grid_export(self, mode):
        self.writes.append(("grid_export", mode))
        self.settings["grid_export"] = mode
        if self.mode == "local":
            FakePowerwall.export_rule = mode
        return {"ok": True}

    def dispatch_battery_power(self, power_watts, duration_seconds):
        self.dispatches.append((power_watts, duration_seconds))
        return {"accepted": True}

    def resume_battery_operation(self):
        self.resumes += 1
        return {"accepted": True}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def fake_powerwall():
    FakePowerwall.instances = []
    FakePowerwall.fail_local = False
    FakePowerwall.soe = 52.5
    FakePowerwall.alert_list = ["SystemConnectedToGrid"]
    FakePowerwall.export_rule = "pv_only"
    FakePowerwall.extra_packs = {}
    with patch("pypowerwall.Powerwall", FakePowerwall):
        yield FakePowerwall


class FakeGateway:
    """Stands in for the gateway's customer login (gateway.py)."""

    password = "ABCDE12345"
    reachable = True
    din = DIN


@pytest.fixture(autouse=True)
def fake_gateway():
    from custom_components.tesla_tedapi import gateway

    FakeGateway.password, FakeGateway.reachable, FakeGateway.din = "ABCDE12345", True, DIN

    def login(host, gw_pwd):
        if not FakeGateway.reachable:
            raise gateway.GatewayUnreachable("no route")
        if gateway.customer_password(gw_pwd) != gateway.customer_password(FakeGateway.password):
            raise gateway.GatewayPasswordRejected
        return "token"

    def din(host, gw_pwd):
        login(host, gw_pwd)
        return FakeGateway.din

    with patch.object(gateway, "login", login), patch.object(gateway, "din", din):
        yield FakeGateway


def local(fake) -> FakePowerwall:
    return [p for p in fake.instances if p.mode == "local"][-1]


def cloud(fake) -> FakePowerwall | None:
    found = [p for p in fake.instances if p.mode == "cloud"]
    return found[-1] if found else None
