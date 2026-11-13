# Alerts

The gateway reports status and fault codes such as `SystemConnectedToGrid`, `BackfeedLimited` or `BatteryFault`. Tesla doesn't document them, so the integration has a built-in list that gives each known code a plain-language description (in Home Assistant's language) and a severity. A code it doesn't know is shown by its raw name as a `notice`.

## Severities

| Severity | Meaning | Do I need to act? |
|---|---|---|
| `status` | Normal operation: connected to the grid, battery full, export limited. | No. |
| `notice` | Unusual but harmless, or a code not in the list yet. | No. If it's an unknown code, please [report it](#help-improve-the-list). |
| `warning` | Expected for a few minutes while the Powerwall restarts or its On/Off switch is used. | Only if it lasts: then **Problem** turns on. |
| `fault` | The Powerwall reports a fault. | If it lasts, check the Tesla app and contact your installer or Tesla. |

## Where alerts appear

| Where | What it shows |
|---|---|
| **Problem** (binary sensor) | On when a warning or fault has lasted the **Problem delay** (10 minutes by default; at least 30 minutes for *Backup check failed*, which is often brief). Its `problems` attribute lists them. **This is the one to notify on.** |
| **Settings → Repairs** | One entry per lasting warning or fault, with an explanation. It disappears when the alert clears. |
| **Alert level** (sensor) | The most serious active alert: *OK* (only status alerts), *Notice*, *Warning* or *Fault*. |
| **Active alerts** (sensor) | A short readable list, most serious first, for example "Built-in solar inputs off · Connected to the grid · Firmware update succeeded". |
| **Alerts** (sensor) | The number of alerts. Attribute `details`: each alert's severity, description, start time and source (gateway or battery pack). |
| **Alert** (event entity) | Every notice, warning or fault raised or cleared. Shown in the device's activity and the logbook; automations can trigger on it. Status alerts are left out unless you turn on **Alert events for status alerts too**: some (such as *Export limited*) come and go every few minutes. |

The gateway doesn't say when an alert started, so the start time is when Home Assistant first saw it. It's kept across restarts.

## Notify on a lasting problem

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.powerwall_tedapi_problem
    to: "on"
actions:
  - action: notify.notify
    data:
      title: Powerwall problem
      message: >-
        {{ state_attr('binary_sensor.powerwall_tedapi_problem', 'problems')
           | map(attribute='description') | join(', ') }}
```

## Dashboard card

```yaml
type: markdown
title: Powerwall alerts
content: >-
  {% set icon = {'fault': '🔴', 'warning': '🟠', 'notice': '🔵', 'status': '⚪'} %}
  {% for a in state_attr('sensor.powerwall_tedapi_alerts', 'details') or [] %}
  {{ icon[a.severity] }} **{{ a.description }}** · `{{ a.alert }}` · since {{ as_timestamp(a.since) | timestamp_custom('%d %b %H:%M') }}
  {% else %}
  No alerts.
  {% endfor %}
```

More cards in [Dashboards](dashboards.md).

## Known codes

| Code | Severity | Short label | Description |
|---|---|---|---|
| `SystemConnectedToGrid` | status | Connected to the grid | Connected to the grid |
| `FWUpdateSucceeded` | status | Firmware update succeeded | The last firmware update installed successfully |
| `BAGGR_a007_EndOfCharge` | status | Battery is full | Battery is full |
| `BackfeedLimited` | status | Export limited | Export to the grid is being limited (export setting or export limit) |
| `ChargeOnlyFromSolarOverride` | status | Solar-only charging | Charging from the grid is not allowed; charging from solar only |
| `SelfConsumptionReservedLimit` | status | At backup reserve | Battery is at the backup reserve |
| `PCH_a054_pwsDisabledMpptEnableLine` | status | Built-in solar inputs off | The Powerwall's built-in solar inputs (MPPT) are off, normal when solar uses a separate inverter |
| `BMS_a045_SW_SOC_Change` | status | Charge level recalibrated | Battery recalibrated its state of charge |
| `BatteryCalibration` | status | Battery calibrating | Battery is calibrating its charge level (also **Battery calibrating**) |
| `RealPowerConfigLimited` | status | Power limited by settings | Power is held below a configured limit |
| `GridCodesWrite` | status | Writing grid settings | The gateway is writing its grid-code settings |
| `PodCommissionTime` | status | Battery commissioning | A battery is recording its commissioning time |
| `BMS_a089_LifeModel_StoDcrGrowth_Change` | status | Ageing estimate updated | Battery updated its ageing estimate |
| `EnergyAccumulatorIrrational` | notice | Energy counter jump | An energy counter jumped implausibly |
| `IslandChecksFailed` | warning | Backup check failed | Backup readiness check failed (normal for a few minutes when the battery fills or after a restart) |
| `BatteryComms` | warning | Battery comms lost | The gateway can't communicate with a battery |
| `PVInverterComms` | warning | Solar inverter comms lost | Lost communication with the solar inverter |
| `SolarMeterComms` | warning | Solar meter comms lost | Lost communication with the solar meter (brief dropouts are common) |
| `IEEE1547Trip` | warning | Grid protection trip | Grid-connection protection tripped (grid voltage or frequency out of range) |
| `DisallowService` | warning | Gateway not ready | Gateway is not ready for service |
| `PCH_a053_pwsDisabledInvEnableLine` | warning | Inverter disabled | Inverter is disabled |
| `PCH_a055_pwsDisabledDcdcEnableLine` | warning | DC-DC converter disabled | Battery DC-DC converter is disabled |
| `PWS_a014_HvpNotActive` | warning | Battery controller inactive | Battery high-voltage controller is not active |
| `PWS_a021_HvpMia` | warning | Battery controller not responding | Battery high-voltage controller is not responding |
| `BAGGR_a005_NumBatteriesExpectedMismatch` | warning | Battery packs missing | Fewer battery packs found than expected |
| `BMS_a037_SW_NVRAM_Config_Error` | warning | Battery settings error | Battery management settings could not be read |
| `BatteryFault` | fault | Battery fault | Battery fault |
| `SystemShutdown` | fault | System shut down | System shut down |
| `PWS_a009_Rsd` | fault | Rapid shutdown active | Rapid shutdown is active (On/Off switch or rapid-shutdown button) |
| `PWS_a014_HvpFault` | fault | Battery controller fault | Battery high-voltage controller fault |
| `PWS_a044_HvpRsdFault` | fault | Rapid-shutdown fault | Battery rapid-shutdown fault |
| `PWS_a045_BattVRationality` | fault | Battery voltage implausible | Battery voltage reading is implausible |

Descriptions come from the code names and from how the alerts behave on real systems (pypowerwall and community reports). They are a best effort, not Tesla's definitions.

## Help improve the list

If you see a code that the integration describes wrongly or doesn't know, please [open an issue](https://github.com/cnapagoda/ha-powerwall-tedapi/issues/new/choose) with:

- the code (from the **Alerts** sensor's `alerts` attribute),
- what the Powerwall was doing at the time (charging, a restart, an outage, a firmware update), and
- how long it lasted.
