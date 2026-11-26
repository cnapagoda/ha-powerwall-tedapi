"""The ready-made configs for other systems call the integration's actions
with data its schemas accept."""
from __future__ import annotations

from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml
import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.tesla_tedapi import DISPATCH_SCHEMA, FREEZE_SCHEMA, RESUME_SCHEMA
from custom_components.tesla_tedapi.const import DOMAIN

EXAMPLES = Path(__file__).parent.parent / "examples"


async def test_evcc_scripts(hass: HomeAssistant) -> None:
    scripts = load_yaml(EXAMPLES / "evcc_scripts.yaml")
    assert set(scripts) == {"powerwall_evcc_normal", "powerwall_evcc_hold",
                            "powerwall_evcc_charge", "powerwall_evcc_discharge"}
    dispatch = async_mock_service(hass, DOMAIN, "dispatch_battery_power", schema=DISPATCH_SCHEMA)
    resume = async_mock_service(hass, DOMAIN, "resume_battery_operation", schema=RESUME_SCHEMA)
    assert await async_setup_component(hass, "script", {"script": scripts})
    for name in scripts:
        await hass.services.async_call("script", name, blocking=True)
    assert [c.data["power_watts"] for c in dispatch] == [0, -5000, 5000]
    assert all(c.data["duration_seconds"] == 86400 for c in dispatch)
    assert dispatch[1].data["max_soc"] == 100
    assert len(resume) == 1


@pytest.mark.parametrize(("hook", "power"), [("charge_start_service", -5000.0),
                                             ("discharge_start_service", 5000.0)])
def test_predbat_dispatch_hooks(hook: str, power: float) -> None:
    """Predbat drops `service` and `repeat`, fills in {power} and sends the
    rest as the action's data."""
    config = load_yaml(EXAMPLES / "predbat_apps.yaml")["pred_bat"]
    (template,) = config[hook]
    assert template["service"] == f"{DOMAIN}.dispatch_battery_power" and template["repeat"] is True
    data = {k: str(v).replace("{power}", "5000") for k, v in template.items() if k not in ("service", "repeat")}
    assert DISPATCH_SCHEMA(data)["power_watts"] == power


@pytest.mark.parametrize(("hook", "direction"), [("charge_freeze_service", "charge_only"),
                                                 ("discharge_freeze_service", "discharge_only")])
def test_predbat_freeze_hooks(hook: str, direction: str) -> None:
    config = load_yaml(EXAMPLES / "predbat_apps.yaml")["pred_bat"]
    (template,) = config[hook]
    assert template["service"] == f"{DOMAIN}.freeze_battery" and template["repeat"] is True
    data = {k: str(v) for k, v in template.items() if k not in ("service", "repeat")}
    assert FREEZE_SCHEMA(data) == {"direction": direction, "duration_seconds": 600}


def test_predbat_stop_hooks() -> None:
    config = load_yaml(EXAMPLES / "predbat_apps.yaml")["pred_bat"]
    for hook in ("charge_stop_service", "discharge_stop_service"):
        assert config[hook] == [{"service": f"{DOMAIN}.resume_battery_operation"}]
