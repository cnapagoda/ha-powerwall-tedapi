# Events

The integration fires these events on Home Assistant's event bus. Use them as automation triggers (**Trigger → Event**). Each one carries `config_entry_id`, so with several Powerwalls you can tell them apart. All except the alert event also appear in the **Logbook**.

| Event | When |
|---|---|
| [`tesla_tedapi_grid_outage`](#tesla_tedapi_grid_outage) | A grid outage starts or ends. |
| [`tesla_tedapi_dispatch_ended`](#tesla_tedapi_dispatch_ended) | A battery dispatch ends. |
| [`tesla_tedapi_off_grid_ended`](#tesla_tedapi_off_grid_ended) | An off-grid run ends. |
| [`tesla_tedapi_firmware_updated`](#tesla_tedapi_firmware_updated) | The gateway reports a new firmware version. |
| [Alert event entity](#alert-event-entity) | The Powerwall raises or clears an alert. |

To see events as they happen: **Developer tools → Events → Listen to events**, and enter the event type.

---

## `tesla_tedapi_grid_outage`

| Field | |
|---|---|
| `config_entry_id` | The Powerwall. |
| `phase` | `started` or `ended`. |
| `start` | When the outage started (ISO 8601). |
| `duration_seconds` | How long it lasted (`ended` only). |

Only real outages count: an off-grid run started here, or a gateway that can't be reached, doesn't fire it. Home Assistant and your network need power during the outage to see it.

```yaml
triggers:
  - trigger: event
    event_type: tesla_tedapi_grid_outage
    event_data:
      phase: started
actions:
  - action: notify.notify
    data:
      message: >-
        Grid down. Battery at
        {{ states('sensor.powerwall_tedapi_battery_level') }} %.
```

The **[Grid outage alert](automations.md#blueprints)** blueprint does this for you.

---

## `tesla_tedapi_dispatch_ended`

| Field | |
|---|---|
| `config_entry_id` | The Powerwall. |
| `reason` | Why it ended (below). |
| `power_watts` | The dispatch power. |
| `battery_level` | **Battery level** when it ended. |

| `reason` | |
|---|---|
| `completed` | The duration ran out. |
| `target_reached` | `charge_to` / `discharge_to` reached the target level. |
| `min_soc` | A discharge reached `min_soc` (or the backup reserve). |
| `max_soc` | A charge reached `max_soc`. |
| `stopped` | `resume_battery_operation` was called. (A new dispatch that replaces a running one doesn't fire this event.) |
| `connection_lost` | Three renewals in a row failed. The Powerwall returns to normal by itself within 5 minutes. |
| `unloaded` | The integration was unloaded or reloaded, for example after changing the Tesla tariff option, the cloud backup or the connection, or Home Assistant stopped or restarted. |

---

## `tesla_tedapi_off_grid_ended`

| Field | |
|---|---|
| `config_entry_id` | The Powerwall. |
| `reason` | `completed` (the duration ran out), `min_soc` (the battery fell to the reconnect level), `requested` (`reconnect_grid` was called), or `reconnected_elsewhere` (for example in the Tesla app). |
| `battery_level` | **Battery level** when it ended. |

---

## `tesla_tedapi_firmware_updated`

| Field | |
|---|---|
| `config_entry_id` | The Powerwall. |
| `previous_version` | For example `26.30.0`. |
| `version` | For example `26.34.1`. |
| `build` | The build hash. |

On a signed connection, the first reading on the new firmware also checks that signed access still works. If it stopped working, a repair says so (and clears when it works again).

```yaml
triggers:
  - trigger: event
    event_type: tesla_tedapi_firmware_updated
actions:
  - action: persistent_notification.create
    data:
      title: Powerwall firmware updated
      message: >-
        {{ trigger.event.data.previous_version }} → {{ trigger.event.data.version }}
```

---

## Alert event entity

Powerwall alerts are an **event entity**, `event.powerwall_tedapi_alert`, rather than a bus event. Its event types are `raised` and `cleared`, with `alert` (the code), `severity`, `description` and `sources`. Status alerts are left out unless you turn on **Alert events for status alerts too**.

```yaml
triggers:
  - trigger: state
    entity_id: event.powerwall_tedapi_alert
conditions:
  - condition: template
    value_template: >-
      {{ trigger.to_state.attributes.event_type == 'raised'
         and trigger.to_state.attributes.severity in ['warning', 'fault'] }}
actions:
  - action: notify.notify
    data:
      title: Powerwall alert
      message: "{{ trigger.to_state.attributes.description }}"
```

See [Alerts](alerts.md).
