# Actions

The integration adds these actions (formerly called *services*). Try them in **Developer tools → Actions**.

**`config_entry_id`** is optional on every action. You only need it with more than one Powerwall; the action editor then shows a list to pick from.

| Action | Needs | What it does |
|---|---|---|
| [`dispatch_battery_power`](#tesla_tedapidispatch_battery_power) | Signed | Run the battery at a fixed power for a set time. |
| [`charge_to` / `discharge_to`](#tesla_tedapicharge_to--tesla_tedapidischarge_to) | Signed | Charge or discharge until a battery level is reached. |
| [`resume_battery_operation`](#tesla_tedapiresume_battery_operation) | Signed | End a dispatch now. |
| [`start_max_backup`](#tesla_tedapistart_max_backup) | Signed | Charge to full and hold it for a set time. |
| [`cancel_max_backup`](#tesla_tedapicancel_max_backup) | Signed | End a max backup. |
| [`go_off_grid`](#tesla_tedapigo_off_grid) | Signed | Disconnect the house from the grid. |
| [`reconnect_grid`](#tesla_tedapireconnect_grid) | Signed | Reconnect the house to the grid. |
| [`get_tariff` / `set_tariff`](#tesla_tedapiget_tariff--tesla_tedapiset_tariff) | Cloud backup + Tesla tariff option | Read or write Tesla's tariff. |
| [`import_battery_history`](#tesla_tedapiimport_battery_history) | Two energy sensors with history | One-off copy of earlier battery history. |

> [!WARNING]
> Battery dispatch and going off-grid change how your house is powered and may affect your warranty. Read [Safety](safety.md) first.

## Battery dispatch: start here

Charging or discharging on command is done with actions, not with a switch or a setting:

```yaml
action: tesla_tedapi.dispatch_battery_power
data:
  power_watts: 5000       # + discharge, - charge, 0 holds idle
  duration_seconds: 1800  # 30 minutes
```

- **To a battery level instead of for a time:** `tesla_tedapi.charge_to` / `discharge_to`.
- **Stop early:** `tesla_tedapi.resume_battery_operation`, or the **Stop battery dispatch** button.
- **Settings that apply** (**Configure → Settings → Battery dispatch**): whether a discharge stops at the backup reserve, and **Largest dispatch power**.
- **How much power:** no more than **Largest dispatch power** (default 10,000 W, at most 30,000 W). Within that the Powerwall decides: it refuses what it can't deliver, and the action then fails with the gateway's reason and status code in the error. One Powerwall 3 (with or without an expansion pack) refuses more than 10,000 W. With several Powerwalls, raise **Largest dispatch power** only to what your installation can safely carry: wiring, breakers, gateway and backup switch ratings, and your grid connection and export limit ([Safety](safety.md#battery-dispatch)). Try a short dispatch first and watch your grid export.

---

## `tesla_tedapi.dispatch_battery_power`

Hold the battery at a fixed power for a set time.

| Field | Required | |
|---|---|---|
| `power_watts` | Yes | **+** discharge, **−** charge, `0` holds the battery idle. Up to ±30,000 W, and no more than **Largest dispatch power** in the options. The Powerwall refuses more than it can deliver (one Powerwall 3: 10,000 W). |
| `duration_seconds` | Yes | 1 s to 24 h. |
| `min_soc` | No | End a discharge when **Battery level** falls to this. Default: the backup reserve (option **Discharges stop at the backup reserve**). |
| `max_soc` | No | End a charge when **Battery level** reaches this. |
| `config_entry_id` | No | Only with several Powerwalls. |

How a dispatch runs:

- The command is sent in steps of at most **5 minutes** and renewed every minute. If Home Assistant stops, the Powerwall goes back to normal within 5 minutes.
- When the time is up or a limit is reached, the battery is handed back. Limits are checked on every reading.
- A new call replaces a running dispatch.
- The call is refused if the battery is already past the limit.
- Charging from the grid may need **Grid charging** switched on.
- **Dispatch active** shows the running dispatch, and [`tesla_tedapi_dispatch_ended`](events.md#tesla_tedapi_dispatch_ended) fires when it ends.
- Changing the **Tesla tariff (cloud)** option, the cloud backup, a linked account or the connection reloads the integration, which ends a running dispatch. The other settings apply straight away.

```yaml
# Charge at 5 kW for 2 hours, stop at 90 %
action: tesla_tedapi.dispatch_battery_power
data:
  power_watts: -5000
  duration_seconds: 7200
  max_soc: 90
```

**Response** (optional): the dispatch as **Dispatch active** shows it (`active`, `power_watts`, `started`, `ends`, `min_soc`, `max_soc`, `target_soc`), plus the gateway's `response`.

---

## `tesla_tedapi.charge_to` / `tesla_tedapi.discharge_to`

Charge or discharge at a set power until **Battery level** reaches `target_soc`, or until `max_minutes` pass. They run as a dispatch (above) and end with reason `target_reached` or `completed`. They're refused when the battery is already at the target.

| Field | Required | |
|---|---|---|
| `target_soc` | Yes | 1–100 %. |
| `power_watts` | Yes | 100–30,000 W, **without a sign**: the action decides the direction. The same limits apply as for `dispatch_battery_power`. |
| `max_minutes` | Yes | 1–1440. |
| `config_entry_id` | No | Only with several Powerwalls. |

```yaml
# Charge to 80 % at 3 kW, for at most 2 hours
action: tesla_tedapi.charge_to
data:
  target_soc: 80
  power_watts: 3000
  max_minutes: 120
```

```yaml
# Export down to 40 % at 5 kW, for at most an hour
action: tesla_tedapi.discharge_to
data:
  target_soc: 40
  power_watts: 5000
  max_minutes: 60
```

`charge_to` charges from solar and, if **Grid charging** is on, from the grid. `discharge_to` overrides the export setting, so the battery may export.

---

## `tesla_tedapi.resume_battery_operation`

End a dispatch now and hand the battery back to the Powerwall's normal mode. It also clears a dispatch left over from before a restart, so it's safe to call any time. The **Stop battery dispatch** [button](entities.md#buttons) does the same.

```yaml
action: tesla_tedapi.resume_battery_operation
```

---

## `tesla_tedapi.start_max_backup`

Start a backup event, the same command Storm Watch uses: the Powerwall charges to full (from the grid if solar isn't enough) and keeps the battery full for the duration, then returns to normal. It works without Storm Watch, for example from a weather warning or before a planned outage. A new call replaces a running max backup. **Backup event** shows it.

| Field | Required | |
|---|---|---|
| `duration_minutes` | Yes | 1–2880 (48 hours). |
| `config_entry_id` | No | Only with several Powerwalls. |

```yaml
# Keep the battery full for 6 hours when a severe weather warning arrives
action: tesla_tedapi.start_max_backup
data:
  duration_minutes: 360
```

This also charges to 100 %, which a backup reserve set through Tesla's cloud can't (it's capped at 80 %).

---

## `tesla_tedapi.cancel_max_backup`

End a max backup started here or in the Tesla app. Events Tesla scheduled (Storm Watch) aren't affected; turn the **Storm Watch** switch off to stop those. The **Cancel max backup** [button](entities.md#buttons) does the same.

---

## `tesla_tedapi.go_off_grid`

> [!CAUTION]
> **This disconnects your house from the grid.** If the battery runs out before the house is reconnected, your power goes off.

The same command as *Go off-grid* in the Tesla app: the Powerwall opens its grid contactor and the house runs on battery and solar. Solar stops for about 30 seconds while the contactor switches.

| Field | Required | |
|---|---|---|
| `confirm` | Yes | Must be `true`. |
| `duration_minutes` | No | 1–1440 (24 hours). Reconnect after this long. |
| `min_soc` | No | Reconnect when **Battery level** falls to this. Default: the backup reserve. |
| `config_entry_id` | No | Only with several Powerwalls. |

How an off-grid run works:

- It's refused unless the Powerwall is on the grid and the battery is above `min_soc`.
- It then waits up to 90 seconds for the gateway to report the grid as down. If that doesn't happen, it takes the request back (sends a reconnect) and fails, so the contactor can't open later on.
- The integration reconnects when the time is up or the battery falls to `min_soc`. The run is saved, so this also works after a restart.
- **If Home Assistant isn't running, nothing reconnects:** the Powerwall stays off-grid until you reconnect it here or in the Tesla app.
- If an automatic reconnect fails, it's tried again every minute and a repair says so.
- **Grid connected** turns off and its attributes show the run. [`tesla_tedapi_off_grid_ended`](events.md#tesla_tedapi_off_grid_ended) fires when it ends.

```yaml
# Test the backup for 15 minutes, back on the grid early if the battery falls to 50 %
action: tesla_tedapi.go_off_grid
data:
  confirm: true
  duration_minutes: 15
  min_soc: 50
```

**Response** (optional): `off_grid_run`, `off_grid_since`, `reconnect_at`, `reconnect_below`, `grid_status`, and the gateway's `response`.

---

## `tesla_tedapi.reconnect_grid`

Reconnect the house to the grid, also after *Go off-grid* in the Tesla app. It waits up to 90 seconds for the Powerwall to start syncing to the grid (**Grid connected** turns back on once it has synced). It fails during a real outage, when there's no grid to sync to. The **Reconnect to the grid** [button](entities.md#buttons) does the same.

**Response** (optional): `grid_status` and the gateway's `response`.

---

## `tesla_tedapi.get_tariff` / `tesla_tedapi.set_tariff`

Read or write the tariff Tesla uses for Time-Based Control. They need a [cloud backup](cloud-backup.md) and the **Tesla tariff (cloud)** option. See **[Tesla tariff](tesla-tariff.md)** for the format, examples and the read-back.

**`get_tariff`** always returns a response: `backend` (which backup answered) and `tariff` (Tesla's raw tariff).

**`set_tariff`** fields:

| Field | Default | |
|---|---|---|
| `tou_settings` | (required) | Tesla's write format: an object with `tariff_content_v2` only. |
| `dry_run` | `true` | Only check, list what Tesla would change (`warnings`) and show what would be sent. |
| `read_back` | `full` | `full`: read back for up to 25 s. `once`: one read after 15 s, for scheduled writes. `none`: no read; the next tariff read confirms it. |
| `only_if_changed` | `false` | Skip the write when these are the rates last sent, or the ones Tesla shows. |

---

## `tesla_tedapi.import_battery_history`

A one-off copy of the battery charged and discharged history of two energy sensors you pick (for example **Tesla Fleet**'s or **Teslemetry**'s *Battery charged* and *Battery discharged*) into this integration's **Battery energy charged** and **Battery energy discharged** statistics, for the time before this integration's own history starts. The imported hours join the existing statistics without a jump, so the Energy dashboard shows one continuous battery history.

> [!IMPORTANT]
> Take a Home Assistant backup first: an import is hard to undo. Run it with `dry_run: true` (the default) and check the result before you run it for real.

| Field | Default | |
|---|---|---|
| `dry_run` | `true` | Only show what would be imported. |
| `source_charged` | (required) | The energy sensor to copy charged energy from. |
| `source_discharged` | (required) | The energy sensor to copy discharged energy from. |
| `config_entry_id` | | Only with several Powerwalls. |

The response's `summary` lists, per sensor, the `source`, the number of `hours`, the `kwh`, the period (`from`, `to`) and where it joins (`join`).

It's refused when there is nothing to import, or when the history was already imported.

See [Migrating](migration.md) for keeping the rest of your Energy dashboard history.
