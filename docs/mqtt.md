# MQTT

The integration can publish the Powerwall's readings, settings, status and events to MQTT, for tools outside Home Assistant (Node-RED, Grafana or Telegraf, evcc, your own scripts). It's **off by default** and optional: nothing changes unless you turn it on.

- [Turning it on](#turning-it-on)
- [Topics](#topics)
- [Payloads](#payloads)
- [Examples](#examples)
- [Notes](#notes)

## Turning it on

1. Set up Home Assistant's [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) with your broker (for example the Mosquitto add-on). This integration publishes through it: there are no broker settings of its own.
2. **Settings → Devices & services → Tesla Powerwall TEDAPI → Configure → MQTT.** Turn on **Publish to MQTT** and choose a **Base topic** (default `tesla_tedapi/powerwall`; a second Powerwall gets `tesla_tedapi/powerwall_2`).

Turning it off, or changing the base topic, removes the retained messages under the old base topic. So does removing the integration. If the MQTT integration isn't set up, a repair says so; everything else keeps working.

## Topics

| Topic | Retained | Sent |
|---|---|---|
| `<base>/availability` | yes | `online` while the gateway answers, `offline` otherwise and when the integration stops (plain text) |
| `<base>/power` | yes | every reading (with fast polling, every fast poll) |
| `<base>/battery` | yes | every full reading |
| `<base>/energy` | yes | every full reading |
| `<base>/settings` | yes | every reading that has them, also after a change |
| `<base>/status` | yes | every reading |
| `<base>/event/<name>` | no | when it happens: `alert`, `dispatch_ended`, `off_grid_ended`, `grid_outage`, `firmware_updated` |

A retained message is only sent again when something in it changed.

## Payloads

JSON. Every payload has `schema` (1), `ts` (when it was sent, UTC) and, except events, `source`: how the reading came (`signed`, `wifi_fallback`, `wifi_password` or `cloud`, as the **Connection path** sensor). Units and signs are the entities' own: W, kWh, % on the Tesla app's scale; grid `+` is importing, battery `+` is discharging. A value the gateway didn't give is `null`.

**power**
```json
{"solar_w": 3120, "battery_w": -1500, "grid_w": -820, "home_w": 800, "battery_level": 64.0,
 "grid_status": "up", "source": "signed", "schema": 1, "ts": "2026-11-26T09:30:00+00:00"}
```

**battery**: `level`, `soe` (raw state of energy), `full_kwh`, `remaining_kwh`, `usable_kwh` (above the app's 0 %), `backup_hours`, and `packs`: per pack `index`, `full_kwh`, `remaining_kwh`, `temp_max`, `temp_min`.

**energy**: `lifetime` (the lifetime counters, kWh, as the energy sensors), `today`, `flows` (solar to home, grid to battery, …) and `battery_net`.

**settings**: `operation_mode`, `backup_reserve`, `grid_charging`, `grid_export`, `site_limits`, `storm_watch` (with a cloud backup) and `settings_source` (`local`, `cloud` or `mixed`).

**status**: `alert_level`, `alerts` (the active alert codes), `network` (the gateway's internet connection), `firmware_version`, `health` (Setup health), `dispatch` (`active`, `power_watts`, `ends`; signed connection only) and `outage`.

**event/alert**: `kind` (`raised` or `cleared`), `alert`, `severity`, `description`. The other events carry the same data as the [Home Assistant events](events.md).

Nothing in the topics or payloads identifies your Powerwall or network: no gateway ID, serial numbers, pack names or addresses.

## Examples

**Mosquitto command line:**
```bash
mosquitto_sub -h <broker> -u <user> -P <password> -t 'tesla_tedapi/powerwall/#' -v
```

**Node-RED:** an *mqtt in* node on `tesla_tedapi/powerwall/power` with output *a parsed JSON object*; `msg.payload.battery_level` is the battery level.

**Telegraf** (to InfluxDB, for Grafana):
```toml
[[inputs.mqtt_consumer]]
  servers = ["tcp://<broker>:1883"]
  topics = ["tesla_tedapi/powerwall/power", "tesla_tedapi/powerwall/battery"]
  data_format = "json"
  json_time_key = "ts"
  json_time_format = "2006-01-02T15:04:05Z07:00"
```

## Notes

- Messages go out through Home Assistant's MQTT client: the broker, credentials and TLS are the MQTT integration's.
- `availability` stays `online` if Home Assistant itself stops without a clean shutdown (the broker's last will is Home Assistant's, not this integration's). Check `ts` to tell a stale message.
- With fast polling at 5 seconds, `power` is sent up to every 5 seconds, and only when it changed.
