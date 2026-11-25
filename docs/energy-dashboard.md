# Energy dashboard

The integration's lifetime energy sensors are ready for Home Assistant's **Energy dashboard**: they are `total_increasing` counters in kWh, read from the gateway's own meters.

## Set it up

Open **Settings → Dashboards → Energy** (or select the ⚙️ on the Energy dashboard).

[![Energy configuration](https://my.home-assistant.io/badges/config_energy.svg)](https://my.home-assistant.io/redirect/config_energy/)

| Energy dashboard section | Field | Sensor |
|---|---|---|
| **Electricity grid** | Grid consumption | **Grid energy imported** (`sensor.powerwall_tedapi_grid_energy_imported`) |
| | Return to grid | **Grid energy exported** (`sensor.powerwall_tedapi_grid_energy_exported`) |
| **Solar panels** | Solar production | **Solar energy produced** (`sensor.powerwall_tedapi_solar_energy_produced`) |
| **Home battery storage** | Energy going in to the battery | **Battery energy charged (net)** (`sensor.powerwall_tedapi_battery_energy_charged_net`) |
| | Energy coming out of the battery | **Battery energy discharged (net)** (`sensor.powerwall_tedapi_battery_energy_discharged_net`) |

The **(net)** battery sensors count the battery the way the Tesla app does. The gateway's own **Battery energy charged** and **Battery energy discharged** count some energy both ways at once (a few tenths of a kWh a day each way on some systems), so they read a little higher in both directions than the app. If your Energy dashboard already uses them, they still work: the net figure (in minus out) is the same. To switch, pick the (net) sensors; the dashboard then shows battery figures from the day you switch, unless you copy the earlier history into them with [`import_battery_history`](actions.md#tesla_tedapiimport_battery_history) and `net: true` (for example from the gateway's counters, or from Tesla Fleet's or Teslemetry's battery sensors).

You don't need **Home energy consumed** here: the Energy dashboard works out your home's use from the others. Keep it for your own cards, or for comparison.

Statistics are built hourly, so the dashboard fills in from the next full hour.

### Costs

Add your tariff to the grid entries as usual: a fixed price, a price sensor from your retailer's integration, or this integration's own. With the **Tesla tariff (cloud)** option, **Tariff buy price** and **Tariff sell price** follow the tariff Tesla uses: pick them as the price entities for grid consumption and return to grid. See [Tesla tariff](tesla-tariff.md#price-sensors-and-calendars).

## Which sensors not to use

| Sensor | Why not |
|---|---|
| The **… today** sensors | They reset at midnight. They're for cards and automations; the Energy dashboard builds its own daily totals from the lifetime counters. |
| **Solar input *A–F* power**, **Battery power** and other power (W) sensors | Power, not energy. The energy sections need kWh counters. |
| **Pack lifetime discharge** | Per-pack wear counter, not a meter for the whole system. |
| **Energy flows** (*Solar to home energy*, …) | For cards and automations. The Energy dashboard works out these flows itself from the lifetime counters. |

## Live power

The power sensors follow Home Assistant's sign convention, so they fit anywhere Home Assistant asks for a live power reading, including the Energy dashboard's power settings in Home Assistant versions that have them:

| | Combined sensor | Or the two split sensors |
|---|---|---|
| Grid | **Grid power**: + importing, − exporting | **Grid import power** / **Grid export power** |
| Battery | **Battery power**: + discharging, − charging | **Battery discharge power** / **Battery charge power** |
| Solar | **Solar power** | |
| Home | **Home power** | |

If a card shows the flow the wrong way round, it expects the opposite sign: use the split sensors, or the card's own invert option. See [Dashboards](dashboards.md).

## Keeping your history

- **Coming from another integration** (the core Tesla Powerwall integration, Tesla Fleet, Teslemetry): don't remove the old sensors from the Energy dashboard. See [Migrating](migration.md#keep-your-energy-dashboard-history).
- **Battery history from Tesla Fleet or Teslemetry** can be copied into this integration's battery sensors with [`import_battery_history`](actions.md#tesla_tedapiimport_battery_history).

## Checking the numbers

- **Grid counter check** (diagnostic) compares today's grid counters with the grid power readings. *OK* means they agree within 10 %.
- Readings missed while the gateway was unreachable stay missing in the power history, but the lifetime counters catch up: the energy used meanwhile appears in the hour the gateway answers again.
- A reading that is missing, zero or lower than the last one is ignored, so a glitch never shows up as a huge spike or a meter reset. A lower reading that stays for 10 readings in a row is taken as a real counter reset.
