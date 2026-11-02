# How it works and limitations

- [Architecture](#architecture)
- [Data updates](#data-updates)
- [What is stored](#what-is-stored)
- [Privacy and security](#privacy-and-security)
- [Known limitations](#known-limitations)
- [Quality](#quality)

## Architecture

```text
                 home network                                 internet (optional)
┌────────────────┐   signed TEDAPI (v1r)    ┌──────────────┐
│ Home Assistant │ ───────────────────────▶ │   Powerwall  │
│  tesla_tedapi  │   readings + commands    │   gateway    │
│  (pypowerwall) │ ─ ─ Wi-Fi fallback ─ ─ ▶ │ 192.168.91.1 │
└───────┬────────┘                          └──────────────┘
        │  only when local can't answer,
        │  and for cloud-only features
        ▼
┌──────────────────────────────────┐
│ Cloud backup                     │
│  • Owner API (Tesla account)     │
│  • Tesla Fleet / Teslemetry      │
└──────────────────────────────────┘
```

- **Readings** always come from the gateway on your network.
- **Settings** are written to the gateway over the signed connection. The [cloud backup](cloud-backup.md) is only used when that fails, or on a Wi-Fi password connection.
- **Dispatch, max backup and go off-grid** are local only.
- **Storm Watch, grid services, the off-grid vehicle charging reserve and the tariff** exist only in Tesla's cloud, so they always use the cloud backup.
- The **signed connection** uses an RSA key registered once with your Powerwall. Each command is signed with it; the gateway also needs the password from the QR sticker.

## Data updates

The integration polls the gateway every 30 seconds by default (**Polling interval**, 10–300 s). Each poll reads power flows, battery level, grid status, settings, energy counters, battery packs, temperatures, fans and alerts in one go. Some values are read less often:

| What | How often |
|---|---|
| Backup events (signed connection) | Every 5 minutes, and straight after a max backup is started or cancelled |
| Settings from the cloud backup, when the gateway can't give them | Owner API: cached for 5 minutes. Tesla Fleet: from that integration's own data, which it reads every minute |
| Storm Watch, grid services, off-grid vehicle charging reserve | Tesla Fleet: every minute (its data). Owner API: every 30 minutes |
| Tesla tariff (option) | Every 6 hours, and after each write |
| Cloud backup health check | About once an hour, or 10 minutes after a failure |
| Restoring a dropped setting | At most every 2 minutes per setting |

After you change a setting, the integration reads the Powerwall again straight away, so the entity shows the new value within seconds. A change made through the cloud is read back for up to 5 minutes.

If the gateway can't be reached, readings show *unavailable* until it answers again. Nothing is filled in for the missed time, except that the lifetime energy counters catch up.

**Choosing a polling interval:** 30 s suits most people. Shorter intervals give smoother power graphs and quicker dispatch limits, but more load on the gateway and a bigger database. If you also run Powerwall-Dashboard or another poller, consider a longer interval.

## What is stored

| Where | What |
|---|---|
| Config entry | Gateway address and password, key path, connection type, Tesla account email and site (if linked), options. |
| `config/tesla_tedapi/` | `tedapi_rsa_private.pem` (the key), `tedapi_rsa_public.der`, `.pypowerwall.auth` (Owner API tokens). Owner-only permissions. |
| `.storage/tesla_tedapi.<entry>` | Alert start times, the settings to restore, outage history, firmware history, today's energy baselines, grid counter check. Removed when you delete the entry. |
| `.storage/tesla_tedapi.<entry>.off_grid` | A running off-grid run, so the reconnect survives a restart. |
| Recorder | States, and the attributes that matter in history. Attributes that change on nearly every reading or are only detail (pack temperatures and remaining energy, the grid counter check figures, Setup health checks, fan duty, alert details) are shown live but not recorded. |

## Privacy and security

- **No data leaves your network** unless you set up a cloud backup, and then only to Tesla (directly, or through Tesla Fleet / Teslemetry).
- The key and the Tesla sign-in are secrets: anyone with the key on your network can control the Powerwall. They're included in Home Assistant backups.
- **Diagnostics downloads** hide the password, email, site ID and serial numbers. The optional gateway configuration in diagnostics also removes location, names and credentials. Check a download before you share it.
- See [SECURITY.md](../SECURITY.md).

## Known limitations

- **Undocumented interfaces.** TEDAPI and the signed (v1r) protocol aren't public. A Powerwall firmware update can change or break them without notice.
- **Registering a key needs the Tesla cloud** once: a Tesla sign-in, Tesla Fleet / Teslemetry, or `pypowerwall register`. The gateway doesn't confirm a key registered only over the local network.
- **The Wi-Fi password connection can't change settings locally**, and has no battery dispatch or max backup. Settings changes need a cloud backup.
- **Cloud-only settings:** Storm Watch, grid services and the off-grid vehicle charging reserve need a cloud backup.
- **Through the cloud, backup reserve stops at 80 %.** Tesla's cloud caps it. Locally any value up to 100 % works, and max backup charges to full; neither is available through the cloud.
- **Battery dispatch, max backup, going off-grid and reconnecting are local only**, with no cloud fallback.
- **A registered key can't be removed from here.** Remove it in the Tesla app.
- **Tested on Powerwall 3, with and without Expansion Packs.** Powerwall 2 / Powerwall+ are less tested; some readings depend on the model and firmware. See [Compatibility](compatibility.md).
- **No history backfill** for readings missed while the gateway was unreachable (battery history from Tesla Fleet / Teslemetry can be [imported](actions.md#tesla_tedapiimport_battery_history)).
- **Changing the Tesla tariff option, the cloud backup, a linked account or the connection reloads the integration**, which ends a running battery dispatch. Other settings apply without a reload.
- **Outages are only seen while Home Assistant and your network have power.**

## Quality

[`quality_scale.yaml`](../custom_components/tesla_tedapi/quality_scale.yaml) tracks Home Assistant's [integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) rule by rule. It is a self-assessment (the scale is only reviewed for core integrations): Bronze, Silver and Gold rules are done, except dependency transparency, which depends on how pypowerwall publishes its releases. Platinum needs an async library.
