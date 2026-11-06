# Migrating from another integration

Most people arrive here from another way of connecting a Powerwall to Home Assistant. This page helps you switch without losing your Energy dashboard history, and explains what can run side by side.

- [Which integrations can stay](#which-integrations-can-stay)
- [Keep your Energy dashboard history](#keep-your-energy-dashboard-history)
- [Update automations and dashboards](#update-automations-and-dashboards)
- [From the core Tesla Powerwall integration](#from-the-core-tesla-powerwall-integration)
- [From Tesla Fleet or Teslemetry](#from-tesla-fleet-or-teslemetry)
- [From a pypowerwall proxy or Powerwall-Dashboard](#from-a-pypowerwall-proxy-or-powerwall-dashboard)
- [Checklist](#checklist)

## Which integrations can stay

| Integration | Keep it? | Why |
|---|---|---|
| **Tesla Fleet** / **Teslemetry** | **Yes, keep it.** | Its battery history can be [imported](actions.md#tesla_tedapiimport_battery_history), and it covers your Tesla vehicles. The same developer app or Teslemetry plan can give this integration its own [cloud backup](cloud-backup.md) sign-in. |
| **Tesla Powerwall** (core, `powerwall`) | Optional. | If it still works for you, it can run alongside. If it stopped working (common on Powerwall 3, which doesn't offer the old local API on the home network), remove it after you've kept its history (below). |
| **Tesla Custom** (`alandtse/tesla`) | Optional. | It's cloud only. Don't share a refresh token between it and this integration: [each needs its own](setup.md#refresh-token-easiest). |
| **pypowerwall proxy / Powerwall-Dashboard** | Optional. | See [below](#from-a-pypowerwall-proxy-or-powerwall-dashboard). |

**Let one place make changes.** Reading from several integrations is fine. But if two of them (or an automation in each) change the operation mode, reserve or grid charging, they will fight. Move your automations to this integration and leave the others read-only.

## Keep your Energy dashboard history

The Energy dashboard keeps the history of every sensor you configure. If you **replace** the old sensors with the new ones, the dashboard stops showing the old history. Instead, **add** the new ones next to the old ones:

1. Open **Settings → Dashboards → Energy**.
2. In each section, **add** this integration's sensor ([which one](energy-dashboard.md#set-it-up)) and **leave the old sensor in place**.
3. When the new sensors are recording, **disable** the old integration (or remove it). Its sensors stop changing, so they add nothing new, but their history stays on the dashboard.

If **Developer tools → Statistics** later lists an old sensor as no longer provided, don't choose to delete its statistics: that removes its history from the dashboard too.

> [!TIP]
> Do the switch close to the top of an hour and check the next hour's bar: if it shows double values, one old sensor is still updating. Disable the old integration.

For **battery** history from Tesla Fleet or Teslemetry there's an alternative: [`import_battery_history`](actions.md#tesla_tedapiimport_battery_history) copies it into this integration's battery sensors, so one sensor holds the whole history. Run it with `dry_run: true` first, and take a backup.

## Update automations and dashboards

1. Find what uses the old entities: open each old entity's page and look at **Related**, or search your automations and dashboards for the old entity IDs.
2. Replace them with this integration's entities. The [Entities](entities.md) page lists them all. Common equivalents:

   | You want | Use |
   |---|---|
   | Battery percentage as in the Tesla app | `sensor.powerwall_tedapi_battery_level` |
   | Solar / battery / grid / home power | `sensor.powerwall_tedapi_solar_power`, `…_battery_power`, `…_grid_power`, `…_home_power` |
   | Grid up or down | `binary_sensor.powerwall_tedapi_grid_connected` |
   | Backup reserve | `number.powerwall_tedapi_backup_reserve` |
   | Operation mode | `select.powerwall_tedapi_operation_mode` (option keys `self_consumption`, `backup`, `autonomous`) |
   | Grid charging, export | `switch.powerwall_tedapi_grid_charging`, `select.powerwall_tedapi_grid_export_mode` |
   | Storm Watch | `switch.powerwall_tedapi_storm_watch` (needs a cloud backup) |

3. **Check sign conventions.** This integration uses Home Assistant's convention: battery **+ discharging**, grid **+ importing**. If your old sensors used another, flip the comparisons in your automations, or use the split sensors (*Battery charge power*, *Grid export power*, …) which are never negative.
4. **Check percentages.** **Battery level** matches the Tesla app. If your old integration showed the gateway's raw value, thresholds may need lowering by 2–4 points.

## From the core Tesla Powerwall integration

- That integration logs in to the gateway's local web API. This one uses TEDAPI with a signed key, which also works on a Powerwall 3 over the home network.
- Use the same **gateway address**. The **gateway password** is the one on the QR sticker: enter it in full (a Backup Gateway 1 needs the full password; other gateways also take just its last 5 characters).
- Its Energy dashboard sensors can stay on the dashboard for their history ([above](#keep-your-energy-dashboard-history)).

## From Tesla Fleet or Teslemetry

You probably want to keep these.

1. Set this integration up with **Register a new key**, and sign in with **Tesla Fleet** (the same developer app) or **Teslemetry**. That sign-in is also the cloud backup.
2. Move automations that change Powerwall settings to this integration's entities: they're local, so they're faster and keep working without the internet.
3. For the Energy dashboard, add this integration's sensors next to theirs, or [import the battery history](actions.md#tesla_tedapiimport_battery_history).

## From a pypowerwall proxy or Powerwall-Dashboard

This integration runs pypowerwall inside Home Assistant, so you don't need a proxy for Home Assistant any more.

- **Can I keep Powerwall-Dashboard?** Yes, for its own graphs. Two clients polling the same gateway is more load on it; if you see readings drop out, lower one of them (this integration's **Polling interval** is 10–300 s).
- **Can I reuse the proxy's key?** Yes: if you registered a key with `python -m pypowerwall register`, upload its `tedapi_rsa_private.pem` with [path B](setup.md#b-upload-a-key-that-is-already-registered). Both can then use the same key.

## Checklist

- [ ] This integration set up, **Setup health** says *OK*.
- [ ] New sensors added to the Energy dashboard, old ones left in place.
- [ ] Automations and dashboards moved to the new entities; signs and percentages checked.
- [ ] Only one integration changes settings.
- [ ] Old integration disabled (or removed) once the new one has recorded for a day.
- [ ] A Tesla Fleet or Teslemetry sign-in here as the cloud backup, if you want one.
