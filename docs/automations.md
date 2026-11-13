# Automations and blueprints

## Blueprints

Ready-made automations. Select **Import** to add one to your Home Assistant, then create an automation from it under **Settings → Automations & scenes → Blueprints**.

The price blueprints work with any price sensor (Amber, Octopus, Nord Pool, Tibber, ComEd and others). All of them change the Powerwall only through this integration's entities and actions.

**Prices and tariffs**

| Blueprint | What it does | |
|---|---|---|
| **Stop exporting at negative feed-in prices** | While the feed-in price stays below a threshold for a few minutes, sets **Grid export mode** to *Disabled*; when it's back above, sets it back to the value you choose. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Fnegative_feed_in.yaml) |
| **Charge when import is cheap** ⚠️ | When the import price falls below a threshold, `charge_to` a target level; hands the battery back when the price rises. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Fcheap_import_charge.yaml) |
| **Export on a price spike** ⚠️ | When the feed-in price rises above a threshold, `discharge_to` a floor; hands the battery back when the price falls. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Fprice_spike_discharge.yaml) |
| **Time-of-use charge and discharge** ⚠️ | For fixed time-of-use tariffs without a price feed: charge in the off-peak window, discharge in the peak window. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Ftime_of_use.yaml) |
| **Dynamic price tariff** | Keeps Tesla's tariff in step with a price forecast (Amber by default), so the Powerwall can stay in Time-Based Control. No dispatch. Needs the [Tesla tariff](tesla-tariff.md) option. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Fdynamic_price_tariff.yaml) |
| **Backup reserve schedule** | Sets **Backup reserve** to one value during a time window and to another after it, on the days you choose, for example to keep the battery for an expensive evening. No dispatch. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Fbackup_reserve_schedule.yaml) |

**Outages and storms**

| Blueprint | What it does | |
|---|---|---|
| **Grid outage alert** | A notification when the grid goes down and when it comes back. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Fgrid_outage_alert.yaml) |
| **Low battery alert** | A notification when the battery falls below a level while the grid is down. It can also alert while the grid is up. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Flow_battery_alert.yaml) |
| **Shed loads during a grid outage** | Turns off the loads you choose when the grid has been down for a few minutes, or once the battery falls below a level, and puts them back when the grid returns. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Foutage_load_shedding.yaml) |
| **Full battery before a storm** | When a weather warning turns on, starts a [max backup](actions.md#tesla_tedapistart_max_backup); ends it when the warning clears. Needs a signed connection. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Fstorm_backup.yaml) |

**Solar and alerts**

| Blueprint | What it does | |
|---|---|---|
| **Run a load on surplus solar** | Turns a load on while the battery is nearly full and the house still exports, and off when the house imports or the battery falls below a level. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Fsolar_surplus_load.yaml) |
| **Problem alert** | A notification when the **Problem** sensor turns on, with what the Powerwall reports, and when it clears. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcnapagoda%2Fha-powerwall-tedapi%2Fblob%2Fv2.8.0%2Fblueprints%2Fautomation%2Ftesla_tedapi%2Fproblem_alert.yaml) |

⚠️ These use battery dispatch: see [Safety](safety.md) about the warranty.

### Blueprint inputs

<details>
<summary><b>Stop exporting at negative feed-in prices</b></summary>

| Input | Default | |
|---|---|---|
| Feed-in price | | Your feed-in price sensor. |
| Below this price | 0 | |
| For at least (minutes) | 5 | Avoids flapping on short dips. |
| Powerwall Grid export | | `select.powerwall_tedapi_grid_export_mode`. |
| Then set export back to | `battery_ok` | |

With solar on a Powerwall 3's own inputs, the Powerwall then limits its solar instead of exporting. A separate solar inverter keeps exporting; limit it through its own integration.

</details>

<details>
<summary><b>Charge when import is cheap</b></summary>

| Input | Default | |
|---|---|---|
| Powerwall | | Choose the integration entry. |
| Import price | | Your import price sensor. |
| Below this price | 0 | |
| Charge to | 90 % | |
| Charging power | 5000 W | |
| For at most | 120 min | |
| Powerwall Dispatch active | | `binary_sensor.powerwall_tedapi_dispatch_active`. |
| Powerwall Operation mode | | `select.powerwall_tedapi_operation_mode`. |
| Leave Tesla's own optimisation alone | On | Does nothing while the Powerwall is in Time-Based Control. |

Charging from the grid needs **Grid charging** on.

</details>

<details>
<summary><b>Export on a price spike</b></summary>

| Input | Default | |
|---|---|---|
| Powerwall | | Choose the integration entry. |
| Feed-in price | | Your feed-in price sensor. |
| Above this price | 1 | |
| Keep at least | 30 % | |
| Discharging power | 5000 W | |
| For at most | 60 min | |
| Powerwall Dispatch active | | `binary_sensor.powerwall_tedapi_dispatch_active`. |
| Powerwall Operation mode | | `select.powerwall_tedapi_operation_mode`. |
| Leave Tesla's own optimisation alone | On | Does nothing while the Powerwall is in Time-Based Control. |

</details>

<details>
<summary><b>Time-of-use charge and discharge</b></summary>

| Input | Default |
|---|---|
| Powerwall | |
| Off-peak charging starts / ends | 01:00 / 05:00 |
| Charge to | 90 % |
| Charging power | 3000 W |
| Peak starts / ends | 16:00 / 21:00 |
| Keep at least | 20 % |
| Discharging power | 3000 W |

Tesla's own Time-Based Control may suit you better: it needs no dispatch. Put your tariff in the Tesla app, or use the [Tesla tariff](tesla-tariff.md) actions.

</details>

<details>
<summary><b>Dynamic price tariff</b></summary>

Every 30 minutes it turns a price forecast into Tesla's tariff for the next 24 hours (48 half hours) and sends it with `set_tariff`. It skips unchanged rates and refuses a forecast with more than 10 of 48 half hours missing.

| Input | Default | |
|---|---|---|
| Powerwall | | |
| Buy price forecast | `sensor.amber_general_forecast` | |
| Sell (feed-in) price forecast | `sensor.amber_feed_in_forecast` | |
| Fixed feed-in price | 0 | Used when there's no sell forecast. |
| Forecast attribute(s) | `forecasts` | Octopus Energy: `rates`. Nord Pool: `raw_today, raw_tomorrow`. |
| Start key / End key / Price key | `start_time` / `end_time` / `per_kwh` | Octopus Energy: `start`, `end`, `value_inc_vat`. Nord Pool: `start`, `end`, `value`. |
| Price scale | 1 | Multiplies every price, for example `0.01` for a feed in cents per kWh. |
| Tariff name | Dynamic prices | |
| Dry run | On | Shows a notification with what would be sent. Turn it off once it looks right. |

Put the Powerwall in Time-Based Control yourself. Don't combine it with the dispatch blueprints.

</details>

<details>
<summary><b>Backup reserve schedule</b></summary>

| Input | Default | |
|---|---|---|
| Powerwall Backup reserve | | `number.powerwall_tedapi_backup_reserve`. |
| Window starts / ends | 14:00 / 17:00 | The window may run past midnight. |
| Reserve during the window | 80 % | |
| Reserve after the window | 20 % | |
| On these days | Every day | The days the window starts on. On other days nothing changes. |

A reserve you set yourself during the window is replaced when the window ends. Through the cloud, Tesla caps the reserve at 80 %.

</details>

<details>
<summary><b>Grid outage alert</b></summary>

| Input | Default | |
|---|---|---|
| Notify action | `notify.notify` | For example `notify.mobile_app_your_phone`. |
| Powerwall Battery level | | Optional, added to the message. |

Home Assistant and your network need power during the outage (for example a UPS) to send it.

</details>

<details>
<summary><b>Low battery alert</b></summary>

| Input | Default | |
|---|---|---|
| Powerwall Battery level | | `sensor.powerwall_tedapi_battery_level`. |
| Powerwall Grid connected | | `binary_sensor.powerwall_tedapi_grid_connected`. |
| Below | 20 % | |
| Only while the grid is down | On | Off: also alerts while the grid is up. |
| Notify action | `notify.notify` | For example `notify.mobile_app_your_phone`. |

It's also sent when an outage starts with the battery already below the level. Home Assistant and your network need power during the outage to send it.

</details>

<details>
<summary><b>Shed loads during a grid outage</b></summary>

| Input | Default | |
|---|---|---|
| Powerwall Grid connected | | `binary_sensor.powerwall_tedapi_grid_connected`. |
| Powerwall Battery level | | `sensor.powerwall_tedapi_battery_level`. |
| Loads to turn off | | Switches, lights, fans, climate, water heaters, humidifiers and toggles. |
| After the grid has been down for | 1 min | Ignores short interruptions. |
| Only when the battery is below | 100 % | At 100 % the loads go off as soon as the time above has passed. |
| Put the loads back when the grid returns | On | Loads that were on are turned on again; loads that were off stay off. |

Off-grid runs you started yourself don't count. If Home Assistant restarts during the outage, the loads stay off when the grid returns.

</details>

<details>
<summary><b>Full battery before a storm</b></summary>

| Input | Default | |
|---|---|---|
| Powerwall | | Choose the integration entry. |
| Weather warning | | Any entity that is on while you want a full battery: a warning binary sensor, a calendar with planned outages, a schedule or a toggle. |
| Keep the battery full for at most | 12 h | A warning that lasts longer isn't renewed. |
| End the max backup when the warning clears | On | This also ends a max backup started in the Tesla app. |

Needs a signed connection. It doesn't need Tesla's Storm Watch. Grid charging during a max backup may cost money on your tariff ([Safety](safety.md#max-backup)).

</details>

<details>
<summary><b>Run a load on surplus solar</b></summary>

| Input | Default | |
|---|---|---|
| Powerwall Battery level | | `sensor.powerwall_tedapi_battery_level`. |
| Powerwall Grid power | | `sensor.powerwall_tedapi_grid_power`. |
| Load | | What to turn on and off: entities, devices or areas. |
| Start when the battery is above | 95 % | |
| And the house exports more than | 1000 W | About what the load draws. |
| For at least | 5 min | Also used for the import below. Ignores passing clouds. |
| Stop when the house imports more than | 200 W | |
| Or the battery falls below | 80 % | |

It doesn't change the Powerwall. A stop condition also turns off a load you switched on yourself; turn the automation off to run the load by hand.

</details>

<details>
<summary><b>Problem alert</b></summary>

| Input | Default | |
|---|---|---|
| Powerwall Problem | | `binary_sensor.powerwall_tedapi_problem`. |
| Notify action | `notify.notify` | For example `notify.mobile_app_your_phone`. |
| Also when the problem has cleared | On | |

**Problem** turns on when a warning or fault has lasted the problem delay: see [Alerts](alerts.md).

</details>

Price entities and suggested thresholds per retailer are collected in the [wiki](https://github.com/cnapagoda/ha-powerwall-tedapi/wiki).

## Example automations

More examples, ready to copy, are in [`examples/automations.yaml`](../examples/automations.yaml).

### Charge from the grid when power is cheap

```yaml
alias: Powerwall - charge when power is cheap
triggers:
  - trigger: numeric_state
    entity_id: sensor.electricity_price          # your price sensor
    below: 0.10
conditions:
  - condition: numeric_state
    entity_id: sensor.powerwall_tedapi_battery_level
    below: 90
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.powerwall_tedapi_grid_charging
  - action: tesla_tedapi.charge_to
    data:
      target_soc: 90
      power_watts: 5000
      max_minutes: 60
mode: single
```

### Switch modes without dispatching

Changing the operation mode and reserve doesn't count as dispatching. For example, hold the battery during an expensive evening by raising the reserve, and give it back afterwards:

```yaml
alias: Powerwall - save the battery for the evening peak
triggers:
  - trigger: time
    at: "14:00:00"
actions:
  - action: number.set_value
    target:
      entity_id: number.powerwall_tedapi_backup_reserve
    data:
      value: 80
  - wait_for_trigger:
      - trigger: time
        at: "17:00:00"
  - action: number.set_value
    target:
      entity_id: number.powerwall_tedapi_backup_reserve
    data:
      value: 20
mode: single
```

The **Backup reserve schedule** [blueprint](#blueprints) does this for you.

### Prepare for a storm

```yaml
alias: Powerwall - full battery before a storm
triggers:
  - trigger: state
    entity_id: binary_sensor.my_severe_weather_warning   # your weather alert
    to: "on"
actions:
  - action: tesla_tedapi.start_max_backup
    data:
      duration_minutes: 720
mode: single
```

The **Full battery before a storm** [blueprint](#blueprints) does this for you, and ends the max backup when the warning clears.

### Shed load during an outage

```yaml
alias: Powerwall - shed load during an outage
triggers:
  - trigger: state
    entity_id: binary_sensor.powerwall_tedapi_grid_connected
    to: "off"
    for: "00:01:00"
conditions:
  - condition: template
    value_template: >-
      {{ not state_attr('binary_sensor.powerwall_tedapi_grid_connected', 'off_grid_run') }}
actions:
  - action: switch.turn_off
    target:
      entity_id:
        - switch.pool_pump        # your big loads
        - switch.ev_charger
mode: single
```

The condition skips off-grid runs you started yourself. The **Shed loads during a grid outage** [blueprint](#blueprints) does this for you, and puts the loads back when the grid returns.

## Tips

- **Use Battery level**, not *Battery state of energy*, in conditions. It matches the Tesla app.
- **Check `binary_sensor.powerwall_tedapi_dispatch_active`** before starting a dispatch from a second automation: a new call replaces the running one.
- **Use option keys** in automations: `self_consumption`, `backup`, `autonomous`; `battery_ok`, `pv_only`, `never`.
- **Your retailer or VPP may also control the Powerwall** from the cloud. If your settings keep changing back, see [Troubleshooting](troubleshooting.md).
- **End every dispatch automation with a stop**: a time limit (`max_minutes`, `duration_seconds`) and, where it matters, `resume_battery_operation` when the condition ends.
