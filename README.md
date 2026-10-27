# Powerwall TEDAPI for Home Assistant

[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/cnapagoda/ha-powerwall-tedapi/actions/workflows/validate.yml/badge.svg)](https://github.com/cnapagoda/ha-powerwall-tedapi/actions/workflows/validate.yml)

Local monitoring and control of a Tesla Powerwall (Powerwall 3, and Powerwall 2 / Powerwall+ where the gateway supports it) through the gateway's TEDAPI. It's built on [pypowerwall](https://github.com/jasonacox/pypowerwall).

- **Local readings** every 30 seconds: power flows, battery level, lifetime energy counters, capacity and health, temperatures, grid voltage and frequency, PV string voltages, alerts.
- **Local control** with a signed (v1r) key: operation mode, backup reserve, grid charging and grid export.
- **Battery dispatch**: charge or discharge at a set power for a set time, with state-of-charge limits. Use it for price-based charging, peak export and similar jobs.
- **Tesla cloud as a backup** (optional): if the gateway can't be reached, the settings entities keep working through your Tesla account.
- **Setup wizard**: registers the key with your Powerwall from Home Assistant, with no command line needed.

> **Not affiliated with Tesla.** This uses undocumented interfaces that Tesla can change at any time. A dispatch overrides your backup reserve and export settings, so use it with care. See [Safety](#safety).

---

## Requirements

| | |
|---|---|
| Home Assistant | 2025.1 or newer. 2026.3 or newer shows the integration icon. |
| Powerwall | A Powerwall 3 is tested. Powerwall 2 / Powerwall+ work for signed access if the firmware supports it, and for Wi-Fi password monitoring. |
| Network | Home Assistant must reach the Powerwall, either at its **home-network address** (signed connection) or on its **own Wi-Fi hotspot** `192.168.91.1`. |
| Tesla account | Needed once, to register the key. Optional afterwards, as the cloud backup. |

### Which connection?

| | Signed (v1r) | Wi-Fi password |
|---|---|---|
| Readings | ✅ local | ✅ local |
| Change mode, reserve, grid charging, export | ✅ local (cloud backup if linked) | ☁️ only through a linked Tesla account |
| Battery dispatch | ✅ | ❌ |
| Reaches the Powerwall at | Home-network IP, or hotspot | Hotspot only (HA must join `TeslaPW_…`) |
| Needs | A key registered once (wizard), with a switch toggle at the Powerwall | The password on the QR sticker inside the Powerwall's cover |

**Signed (v1r) is recommended.** It works over your normal home network, so you don't need a second Wi-Fi adapter on the Home Assistant host.

---

## Installation

### HACS (recommended)

1. In Home Assistant open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/cnapagoda/ha-powerwall-tedapi` with type **Integration**.
3. Search for **Powerwall TEDAPI**, open it and select **Download**.
4. **Restart Home Assistant.**

Or use this button:

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=cnapagoda&repository=ha-powerwall-tedapi&category=integration)

### Manual

1. Download the latest release and copy `custom_components/tesla_tedapi` into your Home Assistant `config/custom_components/` folder.
2. Restart Home Assistant.

---

## Setup

Go to **Settings → Devices & services → Add integration → Powerwall TEDAPI** and pick a path.

### A. Register a new key (recommended)

1. **Find the Powerwall's address.** Look in your router's client list for the Tesla gateway, often named after its serial number. **Reserve that IP in DHCP** so it doesn't change.
2. **Sign in to Tesla.** The wizard shows a sign-in link. Tesla's login ends by sending the browser to a `tesla://auth/callback?code=…` address that browsers can't open, so you copy it by hand:
   1. On a computer, open the browser's developer tools (F12) on the **Network** tab.
   2. Open the link and sign in, including MFA.
   3. In the Network list, find the request that starts with `callback?code=`, or the last redirect's `Location` header. Copy the full address.
   4. Paste it into the wizard. Codes expire within minutes and work only once. If one is rejected, the wizard makes a new link.
3. **Choose your energy site** if your account has several.
4. **Confirm at the Powerwall.** The wizard makes an RSA-4096 key in `config/tesla_tedapi/` and registers it with your Powerwall. Within about **10 minutes**:
   - **Powerwall 3:** switch the **On/Off switch** (left side, under the red rapid-shutdown flap) **OFF for about 15 seconds, then back ON**. A quick flick is ignored. Don't leave it off until the unit powers down.
   - **Other models:** switching the Powerwall's AC breaker off for about 15 seconds is reported to work on some units.

   Then select **Submit**. The check takes up to a minute. If the window has closed, submitting again re-registers the same key and opens a new window.
5. **Connect.**
   - **Gateway address:** the home-network IP from step 1.
   - **Wi-Fi fallback:** `192.168.91.1` if Home Assistant can also reach the Powerwall's hotspot; otherwise leave it empty.
   - **Gateway password:** optional.

Your Tesla login is saved in `config/tesla_tedapi/.pypowerwall.auth` and used as the cloud backup.

### B. Use a key that is already registered

If you registered a key with `python -m pypowerwall register`, copy `tedapi_rsa_private.pem` into your config folder (for example `config/tesla_tedapi/`) and enter its path. You can also link a Tesla account here for the cloud backup.

> The sign-in step doesn't work for you? Run `pip install pypowerwall && python -m pypowerwall register` on any computer with a screen. It opens a proper login window. Then use this path.

### C. Wi-Fi password only

Home Assistant must be connected to the Powerwall's `TeslaPW_…` Wi-Fi. Enter the full password from the QR sticker. You get local readings. Link a Tesla account to change settings through the cloud.

### Changing things later

- **⋮ → Reconfigure:** change the addresses, the gateway password or the key file. Giving a key file switches a Wi-Fi entry to signed.
- **Configure:** change the polling interval (10–300 s, default 30).

---

## Entities

All entities belong to one device, **Powerwall TEDAPI**.

| Entity | Notes |
|---|---|
| Solar / Battery / Grid / Home power (W) | Battery: + discharging, − charging. Grid: + importing, − exporting. |
| **Battery level** (%) | Same scale as the Tesla app. |
| Battery state of energy (%) | The gateway's raw value, about 2–4 points higher than the app (the app hides the bottom 5 %). |
| Grid status, frequency, voltage | |
| Lifetime energy: grid imported/exported, battery charged/discharged, solar produced, home consumed (kWh) | `total_increasing`, ready for the **Energy dashboard**. |
| Battery full capacity, energy remaining (kWh), health (%) | Health = full capacity ÷ (13.5 kWh × packs). |
| Per-pack capacity and temperature | One pair per Powerwall / expansion pack. |
| String *n* voltage | One per active PV string. |
| Backup time remaining (h), inverter temperatures, gateway last started, firmware, connection | Diagnostics. |
| Alerts (sensor), Problem (binary sensor), Alert (event) | See [Alerts](#alerts). |
| **Operation mode**, **Grid export mode** (select) | Settings. These need a signed connection or a linked Tesla account. |
| **Backup reserve** (number), **Grid charging** (switch) | Settings. The `source` attribute shows whether the value came from `local` or `cloud`. |
| **Dispatch active** (binary sensor) | Signed connection only. Attributes: power, start, end, limits, last end reason. |

If the gateway can't be reached, the sensors show *unavailable* instead of stale values. The settings stay usable through the cloud if an account is linked.

---

## Actions

### `tesla_tedapi.dispatch_battery_power`

Hold the battery at a fixed power for a set time.

| Field | |
|---|---|
| `power_watts` | **+** discharge, **−** charge, `0` holds the battery idle. Up to ±10,000 W. |
| `duration_seconds` | 1 s to 24 h. |
| `min_soc` | Optional. End a discharge when the **Battery level** falls to this. |
| `max_soc` | Optional. End a charge when the **Battery level** reaches this. |
| `config_entry_id` | Only needed with more than one Powerwall. |

How a dispatch runs:

- The integration sends the command in steps of at most **5 minutes** and renews it every minute. If Home Assistant stops, the Powerwall goes back to normal within 5 minutes.
- When the time is up or a limit is reached, it hands the battery back. The `tesla_tedapi_dispatch_ended` event carries a `reason`: `completed`, `min_soc`, `max_soc`, `stopped`, `connection_lost` or `unloaded`.
- A new call replaces the running dispatch.
- Charging from the grid may need **Grid charging** switched on.

```yaml
# Charge at 5 kW for 2 hours, stop at 90 %
action: tesla_tedapi.dispatch_battery_power
data:
  power_watts: -5000
  duration_seconds: 7200
  max_soc: 90
```

### `tesla_tedapi.resume_battery_operation`

End a dispatch now and return to the Powerwall's normal mode. It also clears a dispatch left over from before a restart.

More examples are in [`examples/automations.yaml`](examples/automations.yaml).

---

## Alerts

The gateway reports status and fault codes such as `SystemConnectedToGrid`, `BackfeedLimited` or `BatteryFault`. Tesla doesn't document them, so the integration has a built-in list that gives each known code a plain-language description and a severity. Codes it doesn't know are shown by their raw name as `notice`.

| Severity | Meaning |
|---|---|
| `status` | Normal operation, for example connected to grid, battery full, export limited. |
| `notice` | Unusual but harmless, or a code not in the list yet. |
| `warning` | Expected for a few minutes while the Powerwall restarts or its On/Off switch is used; a problem if it lasts. |
| `fault` | The Powerwall reports a fault. |

Where they appear:

- **Problem** (binary sensor) turns on when a warning or fault has lasted **10 minutes or more**. Its `problems` attribute lists them.
- **Alert** (event entity) records every alert raised or cleared, with its description. The changes appear in the device's activity log and the logbook, and automations can trigger on them.
- **Settings → Repairs** shows each lasting warning or fault with an explanation. The entry disappears when the alert clears.
- **Alerts** (sensor) shows the number of alerts. Its `details` attribute lists each alert's severity, description, start time and source (gateway or battery pack). The gateway doesn't report when an alert started, so the start time is when Home Assistant first saw it; it resets when Home Assistant restarts.

Dashboard card:

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

Notify on a lasting problem:

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

If you see a code that the integration describes wrongly or doesn't know, please open an issue with the code and what the Powerwall was doing at the time.

---

## Safety

- **A dispatch ignores your backup reserve and grid export setting.** On a Powerwall 3 a discharge exported to the grid with export set to *never*, and pypowerwall's tests show it discharging below the reserve. Always pass `min_soc` for discharges you automate.
- **Your retailer or VPP may also control the Powerwall.** They can change modes or grid charging from the cloud, and their changes can fight yours.
- The **Battery level** sensor and the limits use the Tesla app scale.
- Contactor control (going off-grid) is **not** exposed on purpose.

## Privacy and security

- The RSA private key and the Tesla login cache are stored in `config/tesla_tedapi/` with owner-only permissions. Treat them like passwords: anyone with the key on your network can control the Powerwall. They are included in Home Assistant backups.
- This integration can't remove a registered key from the Powerwall yet. Deleting the key files stops Home Assistant from using it.
- Diagnostics downloads hide the password, email, site ID and serial numbers.

## Troubleshooting

| Symptom | Try |
|---|---|
| *The Powerwall did not accept the key* | The key isn't VERIFIED, or it belongs to another Powerwall. Run the **Register a new key** path again and do the switch step within 10 minutes. |
| *Could not reach the Powerwall* | Check the address. `ping` it from the HA host. Reserve the IP in your router. |
| Readings stop now and then | Add the hotspot as **Wi-Fi fallback** if HA can reach it, or improve the network path. Download diagnostics and look at the `tedapi_mode` field. |
| Settings change back by themselves | Usually a retailer, VPP or third-party app with access to your Tesla account. |
| Sign-in code rejected | Codes are single-use and expire quickly. Use the fresh link and copy the callback address straight away. |

Debug logging:

```yaml
logger:
  logs:
    custom_components.tesla_tedapi: debug
    pypowerwall: debug
```

## Credits

- [pypowerwall](https://github.com/jasonacox/pypowerwall) by Jason Cox and contributors: TEDAPI, v1r signing, dispatch and Tesla login.
- [Tesla-API](https://github.com/Matthew1471/Tesla-API) research by Matthew1471 into the gateway's battery dispatch fields.

## License

MIT
