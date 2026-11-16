# EMHASS

[EMHASS](https://github.com/davidusb-geek/emhass) plans when to charge and discharge the battery from your prices, solar forecast and load. It publishes the plan as sensors; the **Follow an energy plan** blueprint turns it into [battery dispatches](actions.md#battery-dispatch-start-here), at the planned power.

> ⚠️ Battery dispatch may affect your warranty. Read [Safety](safety.md) first.

## Setup

1. **Signed connection.** Battery dispatch needs the signed (v1r) connection; see [Setup](setup.md).
2. **In EMHASS's configuration:**

   | Setting | Value |
   |---|---|
   | `sensor_power_photovoltaics` | `sensor.powerwall_tedapi_solar_power` |
   | `sensor_power_load_no_var_loads` | `sensor.powerwall_tedapi_home_power` |
   | `set_use_battery` | `true` |
   | `battery_nominal_energy_capacity` | 13500 Wh per Powerwall or expansion pack |
   | `battery_charge_power_max`, `battery_discharge_power_max` | One Powerwall 3: 10000 W; Powerwall 2: 5000 W each |
   | `battery_minimum_state_of_charge` | At least your backup reserve, as a fraction (20 % → 0.2) |

   When you run an optimisation, pass the battery level as `soc_init`: `{{ states('sensor.powerwall_tedapi_battery_level') | float / 100 }}`.
3. **Keep the published plan current:** turn on `continual_publish` in EMHASS's configuration, or run `publish-data` every optimisation time step (30 minutes by default). `sensor.p_batt_forecast` and `sensor.p_grid_forecast` then hold the plan for the current time step. Published only after an optimisation, they keep the first time step's value until the next one.
4. **Import the blueprint** [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Ffollow_energy_plan.yaml) and create an automation from it: **Planned battery power** `sensor.p_batt_forecast`, **Planned grid power** `sensor.p_grid_forecast`, and **Plan too old after** a little longer than the time between two publishes (for example 45 minutes when publishing every 30).
5. **Turn on Grid charging**, so a planned charge can draw from the grid, and check **Largest dispatch power** in the integration options (**Configure → Settings → Battery dispatch**) is at least the blueprint's **Largest battery power**.

Use the Powerwall in **Self-Powered** mode, and turn off other automations that dispatch the battery or change its mode.

## What the blueprint does

| The plan | The Powerwall |
|---|---|
| Charges the battery while importing | Charges at the planned power |
| Exports while discharging the battery | Discharges at the planned power (or the largest, with **Export at full power**) |
| Imports while the battery is idle | Holds the battery at 0 W, keeping it for later |
| Anything else, or no current plan | Normal operation, following the real solar and load |

The dispatch is re-sent every 5 minutes and lasts 10, so the Powerwall returns to normal on its own if Home Assistant stops. A discharge stops at the backup reserve (option **Discharges stop at the backup reserve**).
