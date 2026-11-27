# HAEO

[HAEO](https://github.com/hass-energy/haeo) (Home Assistant Energy Optimizer) plans your battery, solar and grid, and publishes the plan as sensors; it doesn't control devices itself. The **Follow an energy plan** blueprint applies the plan to the Powerwall with [battery dispatches](actions.md#battery-dispatch-start-here), based on HAEO's own advice for automating hardware: pick a mode from what the plan does, and let the Powerwall follow the real solar and load the rest of the time.

> ⚠️ Battery dispatch may affect your warranty. Read [Safety](safety.md) first.

## Setup

1. **Signed connection.** Battery dispatch needs the signed (v1r) connection; see [Setup](setup.md).
2. **In HAEO's battery element:** **Current Charge Percentage** `sensor.powerwall_tedapi_battery_level`, **Capacity** the total: 13.5 kWh for each Powerwall and Expansion Pack, added up, and max charge and discharge power in kW (one Powerwall 3: 10 kW; Powerwall 2: 5 kW each). Set the minimum charge to at least your backup reserve.
3. **Import the blueprint** [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Ffollow_energy_plan.yaml) and create an automation from it: **Planned battery power** your battery's `sensor.<battery>_active_power`, **Planned grid power** your grid's `sensor.<grid>_active_power`. HAEO's sensors are in kW; the blueprint converts them.
4. **Turn on Grid charging** (or the option **Turn on grid charging for a charge**), so a planned charge can draw from the grid, and check **Largest dispatch power** in the integration options (**Configure → Settings → Battery dispatch**) is at least the blueprint's **Largest battery power**.

Use the Powerwall in **Self-Powered** mode, and turn off other automations that dispatch the battery or change its mode.

## What the blueprint does

| The plan | The Powerwall |
|---|---|
| Charges the battery while importing | Charges at the planned power |
| Exports while discharging the battery | Discharges at the planned power, or the largest with **Export at full power** (for plans that export at the export limit) |
| Imports while the battery is idle | Holds the battery at 0 W, keeping it for later |
| Anything else, or no current plan | Normal operation, following the real solar and load |

The Powerwall can't limit its discharge in normal operation, so a plan that imports while the battery covers part of the house runs as normal operation. A plan that exports solar without discharging the battery also runs as normal operation, where the Powerwall charges the battery before it exports. While the battery is held at 0 W, solar above the forecast is exported instead of charging it.

The blueprint re-sends the dispatch every 5 minutes. If Home Assistant stops, the Powerwall returns to normal on its own within 2 minutes.
