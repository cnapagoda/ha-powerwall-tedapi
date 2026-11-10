# Entities

All entities belong to one device per gateway, **Powerwall TEDAPI**. Entity IDs below assume the default name with no area; see [conventions](README.md#conventions-in-these-pages) if yours differ.

**Which entities you get depends on your setup:**

| Marker | Meaning |
|---|---|
| 🔑 | Signed connection only. |
| ⚙️ | Needs a signed connection **or** a [cloud backup](cloud-backup.md). |
| ☁️ | Needs a cloud backup, and only appears when Tesla reports your site uses the feature. |
| 🔍 | Created only when the gateway reports it (model and firmware dependent). Added by itself when it first appears, also later. |
| 🚫 | Disabled by default: enable it on the entity's settings page. |
| 🩺 | Diagnostic: shown in the device page's *Diagnostic* section. |

**When the gateway can't be reached,** readings show *unavailable* (never stale values). Settings stay usable through the cloud backup if you have one. Outage history and **Setup health** stay available.

- [Power](#power)
- [Battery](#battery)
- [Energy](#energy)
- [Grid](#grid)
- [Settings](#settings)
- [Backup, dispatch and cloud states](#backup-dispatch-and-cloud-states)
- [Buttons](#buttons)
- [Alerts](#alerts)
- [Hardware](#hardware)
- [Setup and connection](#setup-and-connection)

## Power

Signs follow Home Assistant's convention: **battery + discharging / − charging; grid + importing / − exporting.**

| Entity | Entity ID | Unit | Notes |
|---|---|---|---|
| Solar power | `sensor.powerwall_tedapi_solar_power` | W | |
| Battery power | `sensor.powerwall_tedapi_battery_power` | W | + discharging, − charging. |
| Grid power | `sensor.powerwall_tedapi_grid_power` | W | + importing, − exporting. |
| Home power | `sensor.powerwall_tedapi_home_power` | W | |
| Grid import power | `sensor.powerwall_tedapi_grid_import_power` | W | Never negative: 0 while exporting. |
| Grid export power | `sensor.powerwall_tedapi_grid_export_power` | W | Never negative: 0 while importing. |
| Battery discharge power | `sensor.powerwall_tedapi_battery_discharge_power` | W | Never negative: 0 while charging. |
| Battery charge power | `sensor.powerwall_tedapi_battery_charge_power` | W | Never negative: 0 while discharging. |
| Solar input *A–F* power 🔍 | `sensor.powerwall_tedapi_solar_input_a_power` … | W | Powerwall 3 built-in inverter, one per input in use. Attribute `state` (the input's state). With solar on its own inverter they aren't created: an input counts as in use from 20 W, 60 V or an active state. Sensors that older versions created for unused inputs are removed at startup if their recorded power never reached 20 W. An input in use keeps its sensors after a restart at night (0 W until morning). With several inverters, later ones are numbered (`2A`). |
| Solar input *A–F* voltage / current 🔍🚫 | `…_solar_input_a_voltage`, `…_solar_input_a_current` | V, A | Same inputs. |

## Battery

| Entity | Entity ID | Unit | Notes |
|---|---|---|---|
| **Battery level** | `sensor.powerwall_tedapi_battery_level` | % | **Same scale as the Tesla app.** Use this one in automations; dispatch limits use it too. |
| Battery state of energy | `sensor.powerwall_tedapi_battery_state_of_energy` | % | The gateway's raw value, about 2–4 points higher than the app (the app hides the bottom 5 %: app = (raw − 5) ÷ 0.95). |
| Battery energy remaining | `sensor.powerwall_tedapi_battery_energy_remaining` | kWh | |
| Battery full capacity | `sensor.powerwall_tedapi_battery_full_capacity` | kWh | Attributes: each pack's `serial` and `full_kwh`. |
| Backup time remaining | `sensor.powerwall_tedapi_backup_time_remaining` | h | The gateway's estimate at the current load. |
| Battery health 🩺 | `sensor.powerwall_tedapi_battery_health` | % | The packs' full capacity ÷ (13.5 kWh × packs). While a follower Powerwall 3 is missing from the readings: the system's full capacity ÷ (13.5 kWh × every pack in the gateway's configuration), so it still covers the whole system. A new battery reads above 100 %. Attributes: `nominal_kwh_per_pack`, `packs` (in the system), `packs_read` (in the readings), `warranty_kwh` (70 % of 13.5 kWh per pack), `lifetime_discharged_kwh` (of the packs read). |

### Per battery pack 🔍

One set per Powerwall and expansion pack, named after the pack (*Powerwall 3*, *Expansion Pack*, or *Battery 1234*).

| Entity | Example entity ID | Unit | Notes |
|---|---|---|---|
| *Pack* capacity | `sensor.powerwall_tedapi_powerwall_3_capacity` | kWh | Attributes: `remaining_kwh`, `part_number`, `serial`. |
| *Pack* temperature | `sensor.powerwall_tedapi_powerwall_3_temperature` | °C | The hottest cell reading. Attributes: `temp_min`, `shunt_temp`, `serial`. |
| *Pack* health 🩺 | `sensor.powerwall_tedapi_powerwall_3_health` | % | Capacity ÷ 13.5 kWh. Attributes: `nominal_kwh`, `warranty_kwh`. |
| *Pack* lifetime discharge 🩺 | `sensor.powerwall_tedapi_powerwall_3_lifetime_discharge` | kWh | Powerwall 3. |
| *Pack* cycles 🩺 | `sensor.powerwall_tedapi_powerwall_3_cycles` | | Powerwall 3. Equivalent full cycles: lifetime discharge ÷ 13.5 kWh. Attributes: `bms_full_cycles` and `over_discharges` (the battery's own counters). |

## Energy

### Lifetime (for the Energy dashboard)

`total_increasing` counters from the gateway's meters, in kWh. These are the ones to use in the [Energy dashboard](energy-dashboard.md).

| Entity | Entity ID |
|---|---|
| Grid energy imported | `sensor.powerwall_tedapi_grid_energy_imported` |
| Grid energy exported | `sensor.powerwall_tedapi_grid_energy_exported` |
| Battery energy charged | `sensor.powerwall_tedapi_battery_energy_charged` |
| Battery energy discharged | `sensor.powerwall_tedapi_battery_energy_discharged` |
| Solar energy produced | `sensor.powerwall_tedapi_solar_energy_produced` |
| Home energy consumed | `sensor.powerwall_tedapi_home_energy_consumed` |

A reading that is missing or zero is ignored, and so is a reading lower than the last one, so a glitch never looks like a meter reset in your statistics. Only a lower reading that stays for 10 readings in a row is taken as a real counter reset (for example a replaced gateway).

### Today

The same six values for **today**, resetting at local midnight (like Tesla Fleet's daily sensors): `sensor.powerwall_tedapi_grid_energy_imported_today`, `…_grid_energy_exported_today`, `…_battery_energy_charged_today`, `…_battery_energy_discharged_today`, `…_solar_energy_produced_today`, `…_home_energy_consumed_today`.

They're meant for cards and automations. For the Energy dashboard, use the lifetime sensors.

| Entity | Entity ID | Notes |
|---|---|---|
| Grid counter check 🩺 | `sensor.powerwall_tedapi_grid_counter_check` | Whether today's grid import and export counters agree with the grid power readings: *OK*, *Counters differ* (more than 10 % and 0.5 kWh apart) or *Not enough data yet* (needs an hour of readings and 0.5 kWh). Attributes: `counter_import_kwh`, `counter_export_kwh`, `power_import_kwh`, `power_export_kwh`, `import_difference_pct`, `export_difference_pct`, `covered_minutes`, `gaps`. |

## Grid

| Entity | Entity ID | Notes |
|---|---|---|
| **Grid connected** | `binary_sensor.powerwall_tedapi_grid_connected` | On while the Powerwall is on the grid. Off during an outage, *Go off-grid*, and while it syncs to reconnect. Attribute `grid_status`: `UP`, `DOWN` or `SYNCING`. With a signed connection it also shows an [off-grid run](actions.md#tesla_tedapigo_off_grid) started here: `off_grid_run`, `off_grid_since`, `reconnect_at`, `reconnect_below`, `last_end_reason`. |
| Grid status | `sensor.powerwall_tedapi_grid_status` | `up`, `down` or `syncing` (shown as *Up*, *Down*, *Syncing* in your language). Before 2.5.1 the states were upper case. |
| Grid frequency | `sensor.powerwall_tedapi_grid_frequency` | Hz. |
| Grid voltage | `sensor.powerwall_tedapi_grid_voltage` | V. |
| Last grid outage | `sensor.powerwall_tedapi_last_grid_outage` | When the last (or current) outage started. Attributes: `ended`, `ongoing`. |
| Last grid outage duration | `sensor.powerwall_tedapi_last_grid_outage_duration` | Minutes. |
| Grid outages this month | `sensor.powerwall_tedapi_grid_outages_this_month` | Resets at the start of each month. |
| Grid outage time this month | `sensor.powerwall_tedapi_grid_outage_time_this_month` | Minutes, including an outage in progress. |

**Outages** count only what the gateway reports. An off-grid run started here and an unreachable gateway don't count. An outage across a month end counts in both months, each with its own share of the time. Outages are only seen while Home Assistant and your network have power (a UPS helps).

## Settings

These need a signed connection or a cloud backup ⚙️. Their `source` attribute says whether the value came from the gateway (`local`), the cloud (`cloud`) or both (`mixed`), and `cloud_backend` says which cloud backup answered.

| Entity | Entity ID | Values |
|---|---|---|
| **Operation mode** ⚙️ | `select.powerwall_tedapi_operation_mode` | `self_consumption` (*Self-Powered*), `backup` (*Backup only*), `autonomous` (*Time-Based Control*). |
| **Grid export mode** ⚙️ | `select.powerwall_tedapi_grid_export_mode` | `battery_ok` (*Battery & Solar*), `pv_only` (*Solar only*), `never` (*Disabled*). |
| **Backup reserve** ⚙️ | `number.powerwall_tedapi_backup_reserve` | 0–100 %, Tesla app scale. Through the cloud, Tesla caps it at 80 %. |
| **Grid charging** ⚙️ | `switch.powerwall_tedapi_grid_charging` | Allow charging from the grid. |
| On-grid solar curtailment (experimental) 🔑🚫 | `switch.powerwall_tedapi_on_grid_solar_curtailment_experimental` | Tesla's `on_grid_solar_curtailment_enabled` flag in the gateway's configuration; its exact effect isn't known. Not tested on a real Powerwall: enable it only to experiment, and watch the solar output. |
| **Storm Watch** ☁️ | `switch.powerwall_tedapi_storm_watch` | Tesla's Storm Watch service. |
| Off-grid vehicle charging reserve ☁️ | `number.powerwall_tedapi_off_grid_vehicle_charging_reserve` | Battery kept for the house when charging a vehicle during an outage. |

In automations, use the **option keys** (`self_consumption`, `battery_ok`, …), not the translated labels:

```yaml
action: select.select_option
target:
  entity_id: select.powerwall_tedapi_grid_export_mode
data:
  option: never
```

**Grid export mode** and **Grid charging** also have `missing_on_powerwall`, `restored_count` and `last_restored`. See [Dropped settings](dropped-settings.md).

## Backup, dispatch and cloud states

| Entity | Entity ID | Notes |
|---|---|---|
| **Dispatch active** 🔑 | `binary_sensor.powerwall_tedapi_dispatch_active` | On while a [battery dispatch](actions.md#tesla_tedapidispatch_battery_power) started here runs. Attributes: `power_watts`, `started`, `ends`, `min_soc`, `max_soc`, `target_soc` (for `charge_to` / `discharge_to`), and `last_end_reason` when idle. |
| **Backup event** 🔑 | `binary_sensor.powerwall_tedapi_backup_event` | On while a backup event runs: a [max backup](actions.md#tesla_tedapistart_max_backup) started here or in the Tesla app, or one Storm Watch scheduled. The Powerwall then charges to full and holds it. Attributes: `kind` (`manual` or the event's name), `start`, `end`; when none is running, the next scheduled one. Read every 5 minutes. |
| Storm Watch active ☁️ | `binary_sensor.powerwall_tedapi_storm_watch_active` | Tesla's Storm Watch is preparing for severe weather. |
| Grid services active ☁️ | `binary_sensor.powerwall_tedapi_grid_services_active` | Tesla reports grid services (for example a virtual power plant) as active on the site. Only on sites enrolled in grid services. |
| Tesla tariff | `sensor.powerwall_tedapi_tesla_tariff` | Only with the **Tesla tariff (cloud)** option. See [Tesla tariff](tesla-tariff.md). |

Cloud states come from the cloud backup every 30 minutes. Their `cloud_backend` attribute says which.

## Buttons

Signed connection only 🔑. Each does the same as the matching [action](actions.md), so you can put it on a dashboard without a script. They're unavailable while the gateway can't be reached, because these commands only work locally. An error (for example the Powerwall refusing the command) is shown when you press.

| Entity | Entity ID | Same as |
|---|---|---|
| **Stop battery dispatch** 🔑 | `button.powerwall_tedapi_stop_battery_dispatch` | [`resume_battery_operation`](actions.md#tesla_tedapiresume_battery_operation): ends a dispatch and hands the battery back. Also clears a dispatch left over from before a restart. |
| **Cancel max backup** 🔑 | `button.powerwall_tedapi_cancel_max_backup` | [`cancel_max_backup`](actions.md#tesla_tedapicancel_max_backup): ends a max backup started here or in the Tesla app. Storm Watch events aren't affected. |
| **Reconnect to the grid** 🔑 | `button.powerwall_tedapi_reconnect_to_the_grid` | [`reconnect_grid`](actions.md#tesla_tedapireconnect_grid): reconnects the house, also after *Go off-grid* in the Tesla app. |

There's no button to go off-grid or to start a dispatch or a max backup, on purpose: those need a duration and limits, so use the [actions](actions.md).

## Alerts

See [Alerts](alerts.md) for what they mean.

| Entity | Entity ID | Notes |
|---|---|---|
| **Problem** | `binary_sensor.powerwall_tedapi_problem` | On when a warning or fault has lasted the problem delay (10 minutes by default). Attributes: `problems` (each with `alert`, `description`, `severity`), `minutes_before_problem`. |
| Alert level 🩺 | `sensor.powerwall_tedapi_alert_level` | *OK*, *Notice*, *Warning* or *Fault*: the most serious active alert. |
| Active alerts 🩺 | `sensor.powerwall_tedapi_active_alerts` | Short readable list, most serious first. Attribute `details`. |
| Alerts 🩺 | `sensor.powerwall_tedapi_alerts` | Number of alerts. Attributes: `alerts` (raw codes), `pack_alerts`, `details` (each with `alert`, `severity`, `description`, `since`, `sources`). |
| Alert | `event.powerwall_tedapi_alert` | Event types `raised` and `cleared`, with `alert`, `severity`, `description`, `sources`. |

## Hardware

All diagnostic 🩺.

| Entity | Entity ID | Notes |
|---|---|---|
| Inverter ambient temperature | `sensor.powerwall_tedapi_inverter_ambient_temperature` | °C. |
| Inverter heatsink temperature | `sensor.powerwall_tedapi_inverter_heatsink_temperature` | °C. Doesn't change on some firmware. |
| Fan *A*/*B* speed 🔍 | `sensor.powerwall_tedapi_fan_a_speed` | rpm. Powerwall 3: the inverter's two fans, with the drive `duty` (%) as an attribute. With more than one Powerwall 3, *Fan A* and *B* are the leader's, and the next inverters' fans are *Fan 2A*, *Fan 2B* and so on. Powerwall 2: one fan per Powerwall (*Fan 1*, *Fan 2*, …), with `target_rpm`. |
| Firmware version | `sensor.powerwall_tedapi_firmware_version` | For example `26.34.1`. Attributes: `build`, `previous_version`, `updated_at`. The last two stay empty until the integration has seen a firmware update. |
| Gateway last started | `sensor.powerwall_tedapi_gateway_last_started` | When the gateway last restarted. |

## Setup and connection

| Entity | Entity ID | Notes |
|---|---|---|
| **Setup health** 🩺 | `sensor.powerwall_tedapi_setup_health` | *OK* or *Needs attention*. Stays available while the gateway is down. See below. |
| Connection 🩺🚫 | `sensor.powerwall_tedapi_connection` | *Signed (v1r)* or *Wi-Fi password*: the connection actually in use. |

**Setup health attributes:**

| Attribute | |
|---|---|
| `checks` | Each check and whether it passes: `password_set`, `signed_connection` (signed setups), `gateway_reachable`, `energy_counters`, `cloud_backup` (when a cloud backup is set up). |
| `problems` | The checks that fail. |
| `wifi_fallback` | Whether a Wi-Fi fallback address is set (signed setups). |
| `cloud_linked` | Whether a cloud backup is set up. |
| `cloud_backends` | Per backup: `ok`, `starting`, `can_change_settings`, `failures`, `last_ok`. |
| `cloud_token_expires` | When the Owner API access token expires (it's renewed automatically). |
| `cloud_last_ok` | The last successful cloud read. |

*Needs attention* about the cloud only appears when **no** backup can change settings; a single failing backup gets its own repair instead.
