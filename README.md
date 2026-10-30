# Powerwall TEDAPI for Home Assistant

[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/cnapagoda/ha-powerwall-tedapi/actions/workflows/validate.yml/badge.svg)](https://github.com/cnapagoda/ha-powerwall-tedapi/actions/workflows/validate.yml)

Local monitoring and control of a Tesla Powerwall (Powerwall 3, and Powerwall 2 / Powerwall+ where the gateway supports it) through the gateway's TEDAPI. It's built on [pypowerwall](https://github.com/jasonacox/pypowerwall).

- **Local readings** every 30 seconds: power flows, battery level, lifetime energy counters, capacity and health, temperatures, grid voltage and frequency, per-input solar power (Powerwall 3), alerts.
- **Local control** with a signed (v1r) key: operation mode, backup reserve, grid charging and grid export.
- **Battery dispatch**: charge or discharge at a set power for a set time, with state-of-charge limits. Use it for price-based charging, peak export and similar jobs.
- **Go off-grid and reconnect** (signed connection): like *Go off-grid* in the Tesla app, with a time limit and a battery floor at which the integration reconnects by itself.
- **Tesla cloud as a backup** (optional): if the gateway can't be reached, the settings entities keep working through your Tesla account.

> **Tesla sign-in works again (2.3.0).** From late October 2026 the Tesla Owner API answered *403 Forbidden* to sign-ins made through this integration (2.2.x): Tesla fingerprints the TLS connection that gets the token. 2.3.0 connects to Tesla over TLS 1.2, as the Tesla Custom integration does, so both a **browser sign-in** and a **refresh token** from a token app work. A key that is already registered never needed either.

- **Setup wizard**: finds the Powerwall on your network, checks the gateway password, registers the key with your Powerwall and notices the confirmation by itself. No command line needed.
- **Keeps your export and grid charging settings**: the Powerwall sometimes drops them from its own settings; the integration writes them back.

> **Not affiliated with Tesla.** This uses undocumented interfaces that Tesla can change at any time. A dispatch overrides your export setting (and, if you ask it to, the backup reserve), and going off-grid disconnects your house from the grid, so use them with care. See [Safety](#safety).

---

## Requirements

| | |
|---|---|
| Home Assistant | 2025.1 or newer. 2026.3 or newer shows the integration icon. |
| Powerwall | A Powerwall 3 is tested. Powerwall 2 / Powerwall+ work for signed access if the firmware supports it, and for Wi-Fi password monitoring. |
| Network | Home Assistant must reach the Powerwall, either at its **home-network address** (signed connection) or on its **own Wi-Fi hotspot** `192.168.91.1`. |
| Tesla account | Needed once, to register the key. Optional afterwards, as the [cloud backup](#cloud-backup) (Owner API, or the Tesla Fleet / Teslemetry integration). |
| Languages | English, German, French, Dutch, Spanish, Italian, Polish, Brazilian Portuguese (follows your Home Assistant language). |

### Which connection?

| | Signed (v1r) | Wi-Fi password |
|---|---|---|
| Readings | ✅ local | ✅ local |
| Change mode, reserve, grid charging, export | ✅ local (cloud backup if set up) | ☁️ only through a [cloud backup](#cloud-backup) |
| Battery dispatch | ✅ | ❌ |
| Reaches the Powerwall at | Home-network IP, or hotspot | Hotspot only (HA must join `TeslaPW_…`) |
| Needs | The gateway password (QR sticker) and a key registered once (wizard), with a switch toggle at the Powerwall | The password on the QR sticker inside the Powerwall's cover |

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

_Screenshots below come from a test Home Assistant with example data (no real Powerwall or account)._

Go to **Settings → Devices & services → Add integration → Powerwall TEDAPI**. If Home Assistant has already spotted the gateway on your network, it is listed under **Discovered**; select **Add** there instead.

You need the **gateway password** for every path: it is printed on the QR sticker inside the Powerwall's cover (Powerwall 3: behind the front glass, lower left). Home Assistant logs in to the gateway with its last 5 characters, also for signed access.

### 1. Find the gateway

- **Search my network** scans your network for Tesla gateways (about 20 seconds) and lists what it finds, or
- **Enter the gateway address** yourself. Your router lists the gateway as `teslapw_…`.

Then enter the **gateway password**. It is checked straight away, before anything else. A wrong password or address is reported here, not after the Tesla sign-in.

**Wi-Fi fallback** (optional): `192.168.91.1` if Home Assistant can also reach the Powerwall's own hotspot; leave it empty otherwise.

<details>
<summary>Screenshots: finding the gateway</summary>

<img src="https://raw.githubusercontent.com/cnapagoda/ha-powerwall-tedapi/main/docs/images/setup-01-find-gateway.png" alt="Start: enter the address or search the network" width="640">

<img src="https://raw.githubusercontent.com/cnapagoda/ha-powerwall-tedapi/main/docs/images/setup-02-scan-results.png" alt="Network search results" width="640">

<img src="https://raw.githubusercontent.com/cnapagoda/ha-powerwall-tedapi/main/docs/images/setup-03-gateway.png" alt="Gateway address and password" width="640">

</details>

You don't need to reserve the gateway's IP: when your router gives it a new address, Home Assistant notices the gateway (DHCP discovery), checks it is the same Powerwall and updates the address.

### 2. Choose how to connect

<img src="https://raw.githubusercontent.com/cnapagoda/ha-powerwall-tedapi/main/docs/images/setup-04-access.png" alt="Choose how to connect" width="640">

> **Which Tesla API registers the key?** A new key is registered by one command sent through Tesla's cloud. The Powerwall then waits for the switch toggle at the unit. Either Tesla API can send that command:
>
> | | Owner API | Fleet API |
> |---|---|---|
> | Who it's for | Everyone | Developers |
> | You need | A Tesla account: a browser sign-in in the wizard, or a refresh token from a token app ([how](#getting-a-tesla-refresh-token)) | A developer app at [developer.tesla.com](https://developer.tesla.com), with its public key hosted on a domain you own, set up as for Home Assistant's [Tesla Fleet](https://www.home-assistant.io/integrations/tesla_fleet) integration ([steps](#b-upload-a-key-that-is-already-registered)) |
> | Where | This wizard, path **A** | `python -m pypowerwall register` (option 2) on a computer, then path **B** |
>
> Neither one exposes Home Assistant to the internet: after the key is verified, everything is local. A key can't be registered over the local network alone. The Powerwall accepts it there, but never verifies it.

#### A. Register a new key (recommended)

1. **Sign in to Tesla**, in the browser or with a refresh token:
   - **Browser sign-in.** The wizard shows a sign-in link. Tesla's login ends by sending the browser to a `tesla://auth/callback?code=…` address that browsers can't open, so you copy it by hand:
     1. On a computer, open the browser's developer tools (F12) on the **Network** tab.
     2. Open the link and sign in, including MFA. Tesla always shows its full sign-in form.
     3. In the Network list, find the request that starts with `callback?code=`, or the last redirect's `Location` header. Copy the full address.
     4. Paste it into the wizard. Codes expire within minutes and work only once. If Tesla rejects a code, the wizard makes a new link; a paste that isn't from the current link leaves the link unchanged.
   - **Refresh token.** Paste a refresh token from a token app instead (see [Getting a Tesla refresh token](#getting-a-tesla-refresh-token)). No developer tools needed.
2. The energy site with this gateway is picked automatically (you choose only if it can't tell).
3. **Confirm at the Powerwall.** The wizard makes an RSA-4096 key in `config/tesla_tedapi/` and registers it with your Powerwall. Within about **10 minutes**:
   - **Powerwall 3:** switch the **On/Off switch** (left side, under the red rapid-shutdown flap) **OFF for about 15 seconds, then back ON**. A quick flick is ignored. Don't leave it off until the unit powers down.
   - **Other models:** switching the Powerwall's AC breaker off for about 15 seconds is reported to work on some units.

   The wizard checks every 10 seconds and continues on its own once the Powerwall confirms. If the window closes, it registers the key again once by itself; after that it asks you to try again.

<details>
<summary>Screenshots: Tesla sign-in, confirmation, done</summary>

<img src="https://raw.githubusercontent.com/cnapagoda/ha-powerwall-tedapi/main/docs/images/setup-05-tesla-sign-in.png" alt="Tesla sign-in step" width="640">

<img src="https://raw.githubusercontent.com/cnapagoda/ha-powerwall-tedapi/main/docs/images/setup-06-confirm-key.png" alt="Waiting for the switch toggle at the Powerwall" width="640">

<img src="https://raw.githubusercontent.com/cnapagoda/ha-powerwall-tedapi/main/docs/images/setup-07-done.png" alt="Setup complete" width="640">

</details>

Your Tesla login is saved in `config/tesla_tedapi/.pypowerwall.auth` and used as the cloud backup.

#### B. Upload a key that is already registered

If you registered a key with `python -m pypowerwall register`, upload its `tedapi_rsa_private.pem`. It is checked (it must be an RSA private key) and copied into `config/tesla_tedapi/` with owner-only permissions. You can also link a Tesla account here for the cloud backup.

**Registering with the Fleet API**

The Fleet API needs a Tesla developer app whose public key is hosted on a domain you own. If you already use Home Assistant's [Tesla Fleet](https://www.home-assistant.io/integrations/tesla_fleet) integration, you have both and can reuse the same developer app. Otherwise, set them up the way that integration's documentation describes:

1. **Developer app.** At [developer.tesla.com/request](https://developer.tesla.com/request), create an app with:
   - OAuth grant type: *Authorization Code and Machine-to-Machine*.
   - Allowed origin: your domain, for example `https://yourdomain.com/`.
   - Allowed redirect URIs: `https://my.home-assistant.io/redirect/oauth` (for Tesla Fleet), and also one for this command, for example `https://yourdomain.com/callback`. That page doesn't need to exist, because you only copy its address.
   - Scopes: **Energy Product Information** and **Energy Product Settings**. The energy APIs are free; no billing is needed.

   Note the **Client ID** and **Client Secret** under *View Details → Credentials & APIs*.
2. **Host the public key** at `https://yourdomain.com/.well-known/appspecific/com.tesla.3p.public-key.pem`, with a valid certificate. Options listed in the Tesla Fleet documentation:
   - The [NGINX Home Assistant SSL proxy](https://github.com/home-assistant/addons/blob/master/nginx_proxy/DOCS.md) app (recommended there), with a custom `location` for the `.well-known` path.
   - A hosting service: [FleetKey.net](https://fleetkey.net) or [MyTeslamate](https://app.myteslamate.com/fleet).

   Tesla may silently reject some domains (for example `ddns.net` hostnames). China isn't supported.

   The Tesla Fleet integration's setup registers this domain with Tesla (the partner registration). Without that integration, pypowerwall's Fleet path expects the partner registration to be done already; see [Tesla's Fleet API guide](https://developer.tesla.com/docs/fleet-api/getting-started/what-is-fleet-api).
3. **Register the Powerwall key** on a computer with Python:
   ```bash
   pip install pypowerwall
   export TESLA_CLIENT_ID="your-client-id"
   export TESLA_CLIENT_SECRET="your-client-secret"
   export TESLA_REDIRECT_URI="https://yourdomain.com/callback"
   # optional, otherwise it asks: NA, EU or CN region
   export TESLA_FLEET_API_BASE="https://fleet-api.prd.na.vn.cloud.tesla.com"
   python -m pypowerwall register      # choose [2] Fleet API
   ```
   Sign in, allow the energy scopes, then paste the address the browser ends on. This sign-in is separate from the Tesla Fleet integration's, so it doesn't log that integration out.
4. **Switch toggle** at the Powerwall within about 10 minutes (as in path A, step 3). The command waits until the key is `VERIFIED`.
5. **Upload** the `tedapi_rsa_private.pem` it wrote, here in path B. This is the Powerwall key, not the Fleet app's key.

If you register with the Fleet API, local control works fully. For the cloud backup you can then use the Tesla Fleet integration (see [Cloud backup](#cloud-backup)), link a Tesla account (refresh token), or both.

<details>
<summary>Screenshot: key upload</summary>

<img src="https://raw.githubusercontent.com/cnapagoda/ha-powerwall-tedapi/main/docs/images/setup-08-key-upload.png" alt="Upload an existing key" width="640">

</details>

> Registered a key with `python -m pypowerwall register` on another computer? Use this path. (That command signs in through Tesla too. If it gets a *403*, register the key with path A instead.)

#### C. Wi-Fi password only

Home Assistant must be connected to the Powerwall's `TeslaPW_…` Wi-Fi (address `192.168.91.1`). You get local readings. Link a Tesla account to change settings through the cloud.

<details>
<summary>Screenshot: Wi-Fi only</summary>

<img src="https://raw.githubusercontent.com/cnapagoda/ha-powerwall-tedapi/main/docs/images/setup-09-wifi-only.png" alt="Wi-Fi password only" width="640">

</details>

### Getting a Tesla refresh token

A refresh token is the alternative to the browser sign-in: no developer tools, and it can be pasted from a phone. Like the [Tesla Custom](https://github.com/alandtse/tesla) integration, this integration takes a **refresh token** made by an app that runs Tesla's own login page. Use one of these:

| App | Platforms |
|---|---|
| **Auth app for Tesla** | iOS, macOS (App Store) and Android |
| **Tesla Tokens** | Android (Google Play) |
| [**tesla_auth**](https://github.com/adriankumpf/tesla_auth) | Windows, macOS, Linux (command line, opens a login window) |

1. Open the app and sign in to Tesla with your email, password and MFA. The sign-in happens on Tesla's page; the app only receives the tokens.
2. The app shows an **access token** and a **refresh token**. Copy the **refresh token**: a long string, usually starting with `eyJ`. The access token runs out after 8 hours, so it won't do.
3. Paste it into the Tesla sign-in step in the setup wizard, *Link a Tesla account* (**Configure**), or the sign-in repair.

The integration swaps the token for a fresh pair straight away and keeps refreshing it, saved in `config/tesla_tedapi/.pypowerwall.auth`.

- **Make a new token for this integration.** Each refresh replaces the token. If two integrations share one, the one that refreshes second is logged out. Don't reuse the token from Tesla Custom, Tesla Fleet or another app.
- **Treat it like a password.** A refresh token gives full access to your Tesla account until you change your password. Get it only from an app you trust; changing your Tesla password revokes it.
- **Why a Tesla account at all?** Only to register the key. The Powerwall does accept a new key over its own Wi-Fi with just the gateway password, but in a test (Powerwall 3, firmware 26.34.1) a key added that way never verified with the switch toggle. Keys only verify when they are registered through Tesla's cloud, via the Owner API (this integration) or the Fleet API (see path B). After the key is verified, local control never uses the Tesla account; the account then only serves as the cloud backup.

### Changing things later

- **⋮ → Reconfigure:** change the addresses, the gateway password or the key file. Giving a key file switches a Wi-Fi entry to signed.
- **Configure → Settings:**

  | Option | Default | |
  |---|---|---|
  | Polling interval | 30 s | 10–300 s. |
  | Restore settings when the Powerwall drops them | on | See [Dropped settings](#dropped-settings). |
  | Alert events for status alerts too | off | See [Alerts](#alerts). |
  | Problem delay | 10 min | How long a warning or fault lasts before **Problem** turns on. |
  | Discharges stop at the backup reserve | on | Applies to dispatches without `min_soc`. |
  | Largest dispatch power | 10,000 W | Dispatch calls above this are refused. |

<details>
<summary>Screenshots: options</summary>

<img src="https://raw.githubusercontent.com/cnapagoda/ha-powerwall-tedapi/main/docs/images/setup-11-options-menu.png" alt="Options menu" width="640">

<img src="https://raw.githubusercontent.com/cnapagoda/ha-powerwall-tedapi/main/docs/images/setup-12-options-settings.png" alt="Settings" width="640">

</details>

- **Configure → Cloud backup:** choose the cloud backup and its order. See [Cloud backup](#cloud-backup).
- **Configure → Link a Tesla account:** add the Owner API backup without setting the Powerwall up again (browser sign-in or refresh token). A *Cloud backup* choice that includes Tesla Fleet keeps it.
- **Configure → Unlink a cloud backup:** stop using one backup and keep the other. **Owner API** removes the Tesla account linked here. **Tesla Fleet** only stops this integration using it; the Tesla Fleet or Teslemetry integration itself is not changed. Either way the change applies straight away.
- If the Powerwall starts refusing the password or the key (for example the key was removed in the Tesla app), Home Assistant asks you to fix it (**Reauthenticate**): enter the password again, register the key again or upload another key. If the Tesla sign-in stops working, **Settings → Repairs** offers to sign in again.

---

## Entities

All entities belong to one device, **Powerwall TEDAPI**.

**Entity IDs** follow the format set in **Settings → System → Entity ID format** (Home Assistant 2026.8 and newer). By default it is *area + device + entity name*, so with the device in an area called *Garage*:

| Device area | Example entity ID |
|---|---|
| Garage | `sensor.garage_powerwall_tedapi_battery_level` |
| No area (or area left out of the format) | `sensor.powerwall_tedapi_battery_level` |

The examples on this page use the *Garage* form; replace `garage_` with your area, or drop it. The format applies when an entity is created; existing entity IDs keep their names (reconfiguring the integration doesn't change them). To apply a new format, use **Recreate entity IDs** on the device page, and update automations and dashboards that use the old IDs.

<details>
<summary>Screenshot: device page</summary>

<img src="https://raw.githubusercontent.com/cnapagoda/ha-powerwall-tedapi/main/docs/images/setup-10-device.png" alt="Device page with controls and sensors" width="640">

</details>

| Entity | Notes |
|---|---|
| Solar / Battery / Grid / Home power (W) | Battery: + discharging, − charging. Grid: + importing, − exporting. |
| Grid import / export power, Battery charge / discharge power (W) | The same readings split by direction, never negative (0 while the power flows the other way). |
| **Battery level** (%) | Same scale as the Tesla app. |
| Battery state of energy (%) | The gateway's raw value, about 2–4 points higher than the app (the app hides the bottom 5 %). |
| Grid status, frequency, voltage | |
| **Grid connected** (binary sensor) | On while the Powerwall is on the grid. Off during an outage or *Go off-grid*, and while it syncs to reconnect. `grid_status` attribute: `UP`, `DOWN` or `SYNCING`. With a signed connection, it also shows an [off-grid run](#tesla_tedapigo_off_grid) started here: `off_grid_run`, `off_grid_since`, `reconnect_at`, `reconnect_below` and `last_end_reason`. |
| Lifetime energy: grid imported/exported, battery charged/discharged, solar produced, home consumed (kWh) | `total_increasing`, ready for the **Energy dashboard**. |
| Battery full capacity, energy remaining (kWh), health (%) | Health = full capacity ÷ (13.5 kWh × packs). |
| Per-pack capacity and temperature | One pair per Powerwall / expansion pack. |
| Solar input *A–F* power (W), voltage, current | Powerwall 3 built-in inverter, one set per input in use (voltage and current are disabled by default). Not created when your solar has its own inverter. Packs and inputs added later appear by themselves. |
| Backup time remaining (h), inverter temperatures, gateway last started, firmware, connection | Diagnostics. |
| Fan *A*/*B* speed (rpm) | Diagnostics. Powerwall 3: the inverter's two fans, with the drive `duty` (%) as an attribute. Powerwall 2: one fan per Powerwall (*Fan 1*), with its `target_rpm`. Only created when the gateway reports fan speeds. |
| **Setup health** | `OK` or `Needs attention`. Attributes list the checks (password set, signed connection, gateway reachable, energy counters, cloud backup working) and when the Owner API token expires. Each cloud backup is checked with a real read about once an hour (`cloud_backends`, `cloud_last_ok`). |
| Alert level, Active alerts, Alerts (sensors), Problem (binary sensor), Alert (event) | See [Alerts](#alerts). |
| **Operation mode**, **Grid export mode** (select) | Settings. These need a signed connection or a linked Tesla account. Grid export mode also shows `missing_on_powerwall`, `restored_count` and `last_restored` (see [Dropped settings](#dropped-settings)). |
| **Backup reserve** (number), **Grid charging** (switch) | Settings. The `source` attribute shows whether the value came from `local` or `cloud`. Grid charging also has the [dropped settings](#dropped-settings) attributes. |
| **Dispatch active** (binary sensor) | Signed connection only. Attributes: power, start, end, limits, last end reason. |
| **Backup event** (binary sensor) | Signed connection only, read from the gateway about every 5 minutes. On while a backup event runs: a [max backup](#tesla_tedapistart_max_backup) started here or in the Tesla app, or one that Tesla's Storm Watch scheduled. The Powerwall then charges to full and holds it. Attributes: `kind` (`manual` or the event name, such as Storm Watch), `start`, `end`; when none is running, the next scheduled one. |
| **Storm Watch** (switch), **Storm Watch active** (binary sensor) | Tesla's Storm Watch service, a Tesla cloud setting: needs a [cloud backup](#cloud-backup), and only appears on sites Tesla reports as capable. `cloud_backend` says which backup answered. With Tesla Fleet the values come from its data; with the Owner API they are read every 30 minutes. |
| Grid services active (binary sensor), **Off-grid vehicle charging reserve** (number) | Tesla cloud values, through the cloud backup. Each appears only when Tesla reports the site uses it (grid services enrolled, off-grid vehicle charging supported). |

If the gateway can't be reached, the sensors show *unavailable* instead of stale values. The settings stay usable through the cloud if a [cloud backup](#cloud-backup) is set up.

---

## Actions

### `tesla_tedapi.dispatch_battery_power`

Hold the battery at a fixed power for a set time.

| Field | |
|---|---|
| `power_watts` | **+** discharge, **−** charge, `0` holds the battery idle. Up to ±10,000 W. |
| `duration_seconds` | 1 s to 24 h. |
| `min_soc` | Optional. End a discharge when the **Battery level** falls to this. Default: the backup reserve (option). |
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

### `tesla_tedapi.start_max_backup`

Signed connection only. Starts a backup event, the same command Storm Watch uses: the Powerwall charges to full (from the grid if solar isn't enough) and keeps the battery full for the duration, then returns to normal. It works without Storm Watch turned on, for example from a weather warning or before a planned outage. A new call replaces a running max backup. **Backup event** shows it.

| Field | |
|---|---|
| `duration_minutes` | 1–2880 (48 hours). |
| `config_entry_id` | Only needed with more than one Powerwall. |

```yaml
# Keep the battery full for 6 hours when a severe weather warning arrives
action: tesla_tedapi.start_max_backup
data:
  duration_minutes: 360
```

### `tesla_tedapi.cancel_max_backup`

Ends a max backup started here or in the Tesla app. Events Tesla scheduled (Storm Watch) are not affected; turn the **Storm Watch** switch off to stop those.

### `tesla_tedapi.go_off_grid`

Signed connection only. **Use with care: this disconnects your house from the grid.** It sends the same command as *Go off-grid* in the Tesla app: the Powerwall opens its grid contactor and the house runs on the battery and solar. Solar stops for about 30 seconds while the contactor switches.

| Field | |
|---|---|
| `confirm` | Required, and must be `true`. |
| `duration_minutes` | Optional, 1–1440 (24 hours). Reconnect after this long. |
| `min_soc` | Optional. Reconnect when the **Battery level** falls to this. Default: the backup reserve. |
| `config_entry_id` | Only needed with more than one Powerwall. |

How an off-grid run works:

- The action is refused unless the Powerwall is on the grid and the battery is above `min_soc`.
- It then waits up to 90 seconds for the gateway to report the grid as down. If that doesn't happen, it takes the request back (sends a reconnect) and fails, so the contactor can't open later on.
- The integration reconnects when the time is up or the battery falls to `min_soc`. The run is saved, so this also works after a restart. If the integration isn't running, nothing reconnects: the Powerwall stays off-grid.
- If an automatic reconnect fails, it is tried again every minute and a repair says so.
- The `tesla_tedapi_off_grid_ended` event carries a `reason`: `completed`, `min_soc`, `requested`, or `reconnected_elsewhere` (for example in the Tesla app).
- **Grid connected** turns off, and its attributes show the run.

```yaml
# Test the backup for 15 minutes, back on the grid early if the battery falls to 50 %
action: tesla_tedapi.go_off_grid
data:
  confirm: true
  duration_minutes: 15
  min_soc: 50
```

### `tesla_tedapi.reconnect_grid`

Signed connection only. Reconnects the house to the grid, also after *Go off-grid* in the Tesla app. It waits up to 90 seconds for the Powerwall to start syncing to the grid (**Grid connected** turns back on once it has synced). It fails during a real outage, when there is no grid to sync to.

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

- **Alert level** (sensor) shows the most serious active alert: `OK` (status alerts only), `Notice`, `Warning` or `Fault`.
- **Active alerts** (sensor) lists the active alerts as short text, most serious first, for example "Built-in solar inputs off · Connected to the grid · Firmware update succeeded".
- **Problem** (binary sensor) turns on when a warning or fault has lasted **10 minutes or more** (option). Its `problems` attribute lists them.
- **Alert** (event entity) records every notice, warning or fault raised or cleared, with its description. The changes appear in the device's activity log and the logbook, and automations can trigger on them. Status alerts are left out by default because some (such as *Export limited*) come and go every few minutes; turn on **Alert events for status alerts too** to include them.
- **Settings → Repairs** shows each lasting warning or fault with an explanation. The entry disappears when the alert clears.
- **Alerts** (sensor) shows the number of alerts. Its `details` attribute lists each alert's severity, description, start time and source (gateway or battery pack). The gateway doesn't report when an alert started, so the start time is when Home Assistant first saw it. It is kept across restarts.

Dashboard card:

```yaml
type: markdown
title: Powerwall alerts
content: >-
  {% set icon = {'fault': '🔴', 'warning': '🟠', 'notice': '🔵', 'status': '⚪'} %}
  {% for a in state_attr('sensor.garage_powerwall_tedapi_alerts', 'details') or [] %}
  {{ icon[a.severity] }} **{{ a.description }}** · `{{ a.alert }}` · since {{ as_timestamp(a.since) | timestamp_custom('%d %b %H:%M') }}
  {% else %}
  No alerts.
  {% endfor %}
```

Notify on a lasting problem:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.garage_powerwall_tedapi_problem
    to: "on"
actions:
  - action: notify.notify
    data:
      title: Powerwall problem
      message: >-
        {{ state_attr('binary_sensor.garage_powerwall_tedapi_problem', 'problems')
           | map(attribute='description') | join(', ') }}
```

If you see a code that the integration describes wrongly or doesn't know, please open an issue with the code and what the Powerwall was doing at the time.

---

## Dropped settings

The Powerwall sometimes drops two settings from its own configuration: the grid export rule and grid charging. The Tesla app and the cloud still show the value you chose. On one Powerwall 3 (firmware 26.x) this happened about once an hour. Until the setting is written again, the gateway falls back to Tesla's default: export *battery_ok*, and grid charging **on**.

The integration reads the gateway's own settings on every poll. When a value you set (or the one the cloud has) is missing:

- **Grid export mode** or **Grid charging** keeps showing your value, with `missing_on_powerwall: true`.
- Over a signed connection the value is written back (at most every 2 minutes) and logged as a warning; `restored_count` and `last_restored` record it.

Turn this off with **Restore settings when the Powerwall drops them** if another app is meant to manage these settings. A value that is the same as Tesla's default is left alone.

## Safety

- **A dispatch ignores your grid export setting, and the Powerwall itself ignores the backup reserve.** On a Powerwall 3 a discharge exported to the grid with export set to *never*. The integration stops a discharge at the backup reserve unless you pass `min_soc` (option), and refuses calls above the largest dispatch power you set.
- **Your retailer or VPP may also control the Powerwall.** They can change modes or grid charging from the cloud, and their changes can fight yours.
- The **Battery level** sensor and the limits use the Tesla app scale.
- **Going off-grid switches the house to the battery.** If the battery runs out before the house is reconnected, the power goes off. Always set `min_soc` or `duration_minutes` high enough, and don't rely on it while Home Assistant may be down. See [`go_off_grid`](#tesla_tedapigo_off_grid).

## Cloud backup

When the local connection can't read or change a setting (the gateway is unreachable, or a Wi-Fi password entry, which can't change settings locally), it goes through the Tesla cloud. There are two backups:

| | Owner API | Tesla Fleet |
|---|---|---|
| What it uses | A Tesla account linked here (**Link a Tesla account**: browser sign-in or refresh token) | Home Assistant's [Tesla Fleet](https://www.home-assistant.io/integrations/tesla_fleet) or [Teslemetry](https://www.home-assistant.io/integrations/teslemetry) integration (official Fleet API) |
| You need | A Tesla sign-in: in the browser, or a refresh token from a token app ([how](#getting-a-tesla-refresh-token)) | That integration enabled and loaded, on the Tesla account with this Powerwall, with the energy permissions (`energy_device_data` to read, `energy_cmds` to change settings) |
| Reads | Tesla's site settings, every 5 minutes at most while needed | The site data that integration already reads every minute: no extra Tesla calls |
| Token | Kept and refreshed here (`config/tesla_tedapi/.pypowerwall.auth`) | That integration's own; used, never refreshed here, so it stays signed in |
| Cost | None | A change of setting is a command on your Tesla developer account (Tesla Fleet) or Teslemetry subscription |
| Risk | Tesla may refuse Owner API tokens altogether one day | Tesla Fleet needs the developer app and key hosting described in path B |

**Configure → Cloud backup** offers the choices that are set up: *None*, *Owner API*, *Tesla Fleet (or Teslemetry)*, *Tesla Fleet, then Owner API* and *Owner API, then Tesla Fleet*. Tesla Fleet is offered only when a loaded Tesla Fleet or Teslemetry entry has this Powerwall's site (matched by the gateway serial; a single site with no serials listed is used as well). Without a choice, a linked Tesla account is the backup, as in 2.2.

With two backups:

- Settings are read from the first one that answers. The setting entities' `cloud_backend` attribute says which one did.
- A change the gateway doesn't take goes to the first backup that accepts it. If Tesla doesn't answer in time, it isn't sent to the other backup as well, because it may still have been applied; the action reports an error instead.
- Each backup is checked about once an hour. **Setup health** lists each one under `cloud_backends` (`ok`, `starting`, `can_change_settings`, `failures`, `last_ok`) and only shows *Needs attention* when none can change settings. A backup that needs attention gets its own repair: *Sign in to Tesla again* (Owner API), *Tesla Fleet cloud backup is not working*, or *Tesla Fleet cloud backup can't change settings* (no `energy_cmds` permission).
- After a restart, Tesla Fleet can load after this integration. For up to 10 minutes it counts as *starting*, not as failing. A disabled entry, or one that keeps failing to load, counts as not working.

Registering a new key still uses the Owner API (path A) or `pypowerwall register` (path B). Registering through the Tesla Fleet integration's sign-in is planned.

## Data updates

The integration polls the gateway every 30 seconds by default (**Polling interval**, 10–300 s). Each poll reads the power flows, battery level, grid status, settings, energy counters, battery packs, temperatures, fans and alerts in one go. Some values are read less often:

| What | How often |
|---|---|
| Backup events (signed connection) | Every 5 minutes, and straight after a max backup is started or cancelled |
| Settings from the cloud backup, when the gateway can't give them | Owner API: cached for 5 minutes. Tesla Fleet: from that integration's own data, which it reads every minute |
| Storm Watch, grid services, off-grid vehicle charging reserve | Tesla Fleet: every minute (its data). Owner API: every 30 minutes |
| Cloud backup health check | About once an hour, or 10 minutes after a failure |
| Restoring a dropped setting | At most every 2 minutes per setting |

After you change a setting, the integration reads the Powerwall again straight away, so the entity shows the new value within seconds. If the gateway can't be reached, readings show *unavailable* until it answers again; nothing is filled in for the missed time.

## Use cases

- **Charge from the grid when power is cheap or free.** Set the operation mode, backup reserve and grid charging from your tariff (for example Amber prices or a free-power window), and switch back afterwards. See [`examples/automations.yaml`](examples/automations.yaml).
- **Export on a price spike.** Use [`dispatch_battery_power`](#tesla_tedapidispatch_battery_power) to discharge at a set power while the price is high, and stop when it falls.
- **Keep the settings you chose.** The [dropped settings](#dropped-settings) restore keeps your export rule and grid charging in place, even when the Powerwall drops them.
- **Get ready for a storm.** Turn on **Storm Watch**, or start a [max backup](#tesla_tedapistart_max_backup) to charge to full and hold it.
- **Test your backup.** [Go off-grid](#tesla_tedapigo_off_grid) for a few minutes, with a battery floor, to see that the house runs on the Powerwall.
- **Know when the power goes out.** **Grid connected** turns off within one poll of an outage. Use it for notifications or to switch off big loads.
- **Track energy.** Add the lifetime energy sensors to the **Energy dashboard** (grid, solar, battery, home).
- **Watch the hardware.** [Alerts](#alerts), the **Problem** sensor, battery health, temperatures and fan speeds show issues early, with the gateway's own alert codes.

## Known limitations

- **Undocumented interfaces.** TEDAPI and the signed (v1r) protocol are not public. A Powerwall firmware update can change or break them without notice.
- **Registering a key needs the Tesla cloud** once: a Tesla sign-in (path A) or `pypowerwall register` (path B). The gateway doesn't confirm a key registered only over the local network.
- **The Wi-Fi password connection can't change settings locally**, and has no battery dispatch or max backup. Settings changes need a cloud backup.
- **Cloud-only settings:** Storm Watch, grid services and the off-grid vehicle charging reserve need a [cloud backup](#cloud-backup).
- **Through the cloud, backup reserve stops at 80 %.** Tesla's cloud caps it. Locally, any value up to 100 % works, and [max backup](#tesla_tedapistart_max_backup) charges to full; neither is available through the cloud.
- **Battery dispatch, max backup, going off-grid and reconnecting are local only** (signed connection), with no cloud fallback. Going off-grid is new in 2.3.1; pypowerwall's authors tested the command on a Powerwall 3.
- **A registered key can't be removed from here.** Remove it in the Tesla app.
- **Tested mainly on Powerwall 3.** Powerwall 2 / Powerwall+ are less tested, and some readings (built-in solar inputs, fans, inverter temperatures) depend on the model and firmware. On some firmware the inverter heatsink temperature doesn't change.
- **No history backfill.** Readings missed while the gateway was unreachable stay missing.

## Privacy and security

- The RSA private key and the Tesla login cache are stored in `config/tesla_tedapi/` with owner-only permissions. Treat them like passwords: anyone with the key on your network can control the Powerwall. They are included in Home Assistant backups.
- This integration can't remove a registered key from the Powerwall yet. Deleting the key files stops Home Assistant from using it.
- Diagnostics downloads hide the password, email, site ID and serial numbers.

## Troubleshooting

| Symptom | Try |
|---|---|
| *The gateway refused this password* | Use the password from the QR sticker inside the Powerwall's cover (upper case). Only the last 5 characters are used. |
| *The Powerwall did not accept the key* | The key isn't VERIFIED, or it belongs to another Powerwall. Run the **Register a new key** path again and do the switch step within 10 minutes. |
| *Could not reach the Powerwall* | Check the address. `ping` it from the HA host. Reserve the IP in your router. |
| Readings stop now and then | Add the hotspot as **Wi-Fi fallback** if HA can reach it, or improve the network path. Download diagnostics and look at the `tedapi_mode` field. |
| Settings change back by themselves | Usually a retailer, VPP or third-party app with access to your Tesla account. For the export rule and grid charging, see [Dropped settings](#dropped-settings). |
| **Setup health** says *Needs attention* | Its `problems` attribute names the failing check. For the cloud backup, `cloud_backends` shows which backup fails. |
| *Tesla Fleet cloud backup is not working* | Check that the Tesla Fleet or Teslemetry integration is enabled, loaded and signed in (re-authenticate it if it asks), and has this Powerwall on its account. |
| *Tesla Fleet cloud backup can't change settings* | Sign in to that integration again and allow the energy commands (`energy_cmds`) permission. |
| Backup reserve shows 80 % after setting more | The gateway didn't take the change, so it went through Tesla's cloud, which caps backup reserve at 80 %. Check the gateway connection. To charge to full and hold it, use [max backup](#tesla_tedapistart_max_backup) (signed connection). |
| Sign-in code rejected | Codes are single-use and expire quickly. After Tesla rejects a code, the form shows a new link: sign in with that one and copy the callback address straight away. A paste that isn't from the current link (*not a sign-in address from the link above*) leaves the link unchanged. |
| *The Tesla Owner API refused the token (HTTP 403)* | Update to 2.3.0 or newer: 2.2.x connects to Tesla in a way it now refuses. On 2.3.0, sign in again; if the 403 keeps coming back, Tesla may have changed its rules again, so please open an issue. |

Debug logging:

```yaml
logger:
  logs:
    custom_components.tesla_tedapi: debug
    pypowerwall: debug
```

## Removing

1. **Settings → Devices & services → Powerwall TEDAPI → ⋮ → Delete.** This removes the entities and the integration's saved state (alert times, the export rule it restores).
2. The key and the Tesla sign-in stay in `config/tesla_tedapi/`, because another entry may use them. If you don't set the Powerwall up again, delete that folder.
3. The key stays registered on the Powerwall. Remove it in the Tesla app if you don't want it any more.
4. If you installed through HACS: **HACS → Powerwall TEDAPI → ⋮ → Remove**, then restart Home Assistant. For a manual install, delete `config/custom_components/tesla_tedapi/` and restart.
5. Check your automations, scripts and dashboards for entities that no longer exist.

## Quality

[`quality_scale.yaml`](custom_components/tesla_tedapi/quality_scale.yaml) tracks Home Assistant's [integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) rule by rule. It is a self-assessment (the scale is only reviewed for core integrations): Bronze, Silver and Gold rules are done, except dependency transparency, which depends on how pypowerwall publishes its releases. Platinum needs an async library.

## Credits

- [pypowerwall](https://github.com/jasonacox/pypowerwall) by Jason Cox and contributors: TEDAPI, v1r signing, dispatch and Tesla login.
- [Tesla-API](https://github.com/Matthew1471/Tesla-API) research by Matthew1471 into the gateway's battery dispatch fields.

## License

MIT
