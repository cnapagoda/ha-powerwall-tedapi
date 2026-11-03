# Dashboards

Ready-to-paste cards. Add a card, choose **Manual** (or **Show code editor**), and paste. Replace the entity IDs if yours differ ([how to find them](README.md#conventions-in-these-pages)).

A complete dashboard with all of these is in [`examples/dashboard.yaml`](../examples/dashboard.yaml).

- [Battery at a glance](#battery-at-a-glance)
- [Settings](#settings)
- [Action buttons](#action-buttons)
- [Power flow](#power-flow)
- [Today's energy](#todays-energy)
- [Alerts](#alerts)
- [Grid outages](#grid-outages)
- [Battery health](#battery-health)

## Battery at a glance

```yaml
type: gauge
entity: sensor.powerwall_tedapi_battery_level
name: Powerwall
min: 0
max: 100
needle: true
severity:
  red: 0
  yellow: 20
  green: 40
```

```yaml
type: glance
columns: 4
entities:
  - entity: sensor.powerwall_tedapi_solar_power
    name: Solar
  - entity: sensor.powerwall_tedapi_battery_power
    name: Battery
  - entity: sensor.powerwall_tedapi_grid_power
    name: Grid
  - entity: sensor.powerwall_tedapi_home_power
    name: Home
```

## Settings

Tile cards with controls. The select options show the Tesla app's names (*Self-Powered*, *Time-Based Control*, …).

```yaml
type: grid
columns: 2
square: false
cards:
  - type: tile
    entity: select.powerwall_tedapi_operation_mode
    features:
      - type: select-options
  - type: tile
    entity: select.powerwall_tedapi_grid_export_mode
    features:
      - type: select-options
  - type: tile
    entity: number.powerwall_tedapi_backup_reserve
    features:
      - type: numeric-input
        style: slider
  - type: tile
    entity: switch.powerwall_tedapi_grid_charging
```

If your Home Assistant version doesn't know a tile feature, use an entities card instead:

```yaml
type: entities
title: Powerwall settings
entities:
  - select.powerwall_tedapi_operation_mode
  - number.powerwall_tedapi_backup_reserve
  - switch.powerwall_tedapi_grid_charging
  - select.powerwall_tedapi_grid_export_mode
```

## Action buttons

With a signed connection the integration has three [buttons](entities.md#buttons): **Stop battery dispatch**, **Cancel max backup** and **Reconnect to the grid**. The simplest card lists them, each with a *Press* button:

```yaml
type: entities
title: Powerwall commands
entities:
  - button.powerwall_tedapi_stop_battery_dispatch
  - button.powerwall_tedapi_cancel_max_backup
  - button.powerwall_tedapi_reconnect_to_the_grid
```

Or as large buttons, each asking for confirmation first. *Max backup 6 h* calls the [action](actions.md#tesla_tedapistart_max_backup), because starting one needs a duration. *Go off-grid* is left out on purpose: run it from an automation with a duration and a battery floor (see [Safety](safety.md)).

```yaml
type: grid
columns: 3
square: false
cards:
  - type: button
    entity: button.powerwall_tedapi_stop_battery_dispatch
    name: Stop dispatch
    tap_action:
      action: perform-action
      perform_action: button.press
      target:
        entity_id: button.powerwall_tedapi_stop_battery_dispatch
      confirmation:
        text: Hand the battery back to the Powerwall?
  - type: button
    name: Max backup 6 h
    icon: mdi:shield-home
    tap_action:
      action: perform-action
      perform_action: tesla_tedapi.start_max_backup
      data:
        duration_minutes: 360
      confirmation:
        text: Charge to full and hold it for 6 hours?
  - type: button
    entity: button.powerwall_tedapi_cancel_max_backup
    name: Cancel max backup
    tap_action:
      action: perform-action
      perform_action: button.press
      target:
        entity_id: button.powerwall_tedapi_cancel_max_backup
      confirmation:
        text: End the max backup?
```

Show the running dispatch next to them:

```yaml
type: conditional
conditions:
  - condition: state
    entity: binary_sensor.powerwall_tedapi_dispatch_active
    state: "on"
card:
  type: markdown
  content: >-
    {% set e = 'binary_sensor.powerwall_tedapi_dispatch_active' %}
    **Dispatch running:** {{ state_attr(e, 'power_watts') | int }} W
    until {{ as_timestamp(state_attr(e, 'ends')) | timestamp_custom('%H:%M') }}
    {%- if state_attr(e, 'target_soc') %}, target {{ state_attr(e, 'target_soc') }} %{% endif %}.
```

## Power flow

### Home Assistant's own energy cards

Once the [Energy dashboard](energy-dashboard.md) is set up, its cards work on any dashboard:

```yaml
type: energy-distribution
link_dashboard: true
```

### Power Flow Card Plus (HACS)

[Power Flow Card Plus](https://github.com/flixlix/power-flow-card-plus) shows live power. Install it from HACS (**Frontend**), then:

```yaml
type: custom:power-flow-card-plus
entities:
  grid:
    entity: sensor.powerwall_tedapi_grid_power
  solar:
    entity: sensor.powerwall_tedapi_solar_power
  battery:
    entity: sensor.powerwall_tedapi_battery_power
    state_of_charge: sensor.powerwall_tedapi_battery_level
  home:
    entity: sensor.powerwall_tedapi_home_power
watt_threshold: 1000
```

The combined sensors use Home Assistant's convention (battery + discharging, grid + importing). If an arrow points the wrong way, add `invert_state: true` under that entity, or give the card the split sensors (**Battery charge power** / **Battery discharge power**, **Grid import power** / **Grid export power**) as described in its documentation.

## Today's energy

```yaml
type: entities
title: Today
entities:
  - entity: sensor.powerwall_tedapi_solar_energy_produced_today
    name: Solar
  - entity: sensor.powerwall_tedapi_home_energy_consumed_today
    name: Home
  - entity: sensor.powerwall_tedapi_grid_energy_imported_today
    name: Imported
  - entity: sensor.powerwall_tedapi_grid_energy_exported_today
    name: Exported
  - entity: sensor.powerwall_tedapi_battery_energy_charged_today
    name: Battery in
  - entity: sensor.powerwall_tedapi_battery_energy_discharged_today
    name: Battery out
```

```yaml
type: history-graph
title: Last 24 hours
hours_to_show: 24
entities:
  - entity: sensor.powerwall_tedapi_solar_power
    name: Solar
  - entity: sensor.powerwall_tedapi_home_power
    name: Home
  - entity: sensor.powerwall_tedapi_grid_power
    name: Grid
  - entity: sensor.powerwall_tedapi_battery_power
    name: Battery
```

## Alerts

Shows nothing while all is well:

```yaml
type: conditional
conditions:
  - condition: state
    entity: sensor.powerwall_tedapi_alert_level
    state_not: ok
card:
  type: markdown
  title: Powerwall alerts
  content: >-
    {% set icon = {'fault': '🔴', 'warning': '🟠', 'notice': '🔵', 'status': '⚪'} %}
    {% for a in state_attr('sensor.powerwall_tedapi_alerts', 'details') or []
       if a.severity != 'status' %}
    {{ icon[a.severity] }} **{{ a.description }}** · since {{ as_timestamp(a.since) | timestamp_custom('%d %b %H:%M') }}
    {% endfor %}
```

The full list, status alerts included, is in [Alerts](alerts.md#dashboard-card).

## Grid outages

```yaml
type: entities
title: Grid
entities:
  - binary_sensor.powerwall_tedapi_grid_connected
  - sensor.powerwall_tedapi_backup_time_remaining
  - sensor.powerwall_tedapi_last_grid_outage
  - sensor.powerwall_tedapi_last_grid_outage_duration
  - sensor.powerwall_tedapi_grid_outages_this_month
  - sensor.powerwall_tedapi_grid_outage_time_this_month
```

## Battery health

```yaml
type: entities
title: Battery health
entities:
  - sensor.powerwall_tedapi_battery_health
  - sensor.powerwall_tedapi_battery_full_capacity
  - sensor.powerwall_tedapi_powerwall_3_cycles
  - sensor.powerwall_tedapi_powerwall_3_temperature
  - sensor.powerwall_tedapi_setup_health
```

Per-pack entity IDs follow the pack's name (*Powerwall 3*, *Expansion Pack*, …); check yours on the device page.
