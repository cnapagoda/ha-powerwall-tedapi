# Safety

This integration controls real electrical equipment through interfaces Tesla doesn't document. Monitoring is harmless. Changing settings is what the Tesla app does too. **Battery dispatch and going off-grid go further.** Read this page before you use them.

> [!WARNING]
> **Not affiliated with Tesla.** Tesla can change these interfaces at any time, and a firmware update may change how commands behave. You use the control features at your own risk.

## Battery dispatch

Applies to `dispatch_battery_power`, `charge_to` and `discharge_to`.

- **A dispatch ignores your grid export setting, and the Powerwall ignores the backup reserve during one.** On a Powerwall 3, a discharge exported to the grid with export set to *never*. The integration stops a discharge at the backup reserve unless you pass `min_soc` (option **Discharges stop at the backup reserve**), and refuses calls above **Largest dispatch power**.
- **Dispatching may affect your warranty.** Tesla's warranty may cover unlimited cycles only for self-consumption, time-based control and backup, and limit other use to a total energy throughput (37.8 MWh is quoted for some units). Dispatching may count as other use. Check your own warranty.
- **If Home Assistant stops, the dispatch stops.** Commands are sent in steps of at most 5 minutes, so the Powerwall returns to normal within 5 minutes.
- **Your retailer or VPP may also control the Powerwall.** They can change modes or grid charging from the cloud, and their changes can fight yours.
- **Limits use the Tesla app scale** (**Battery level**).

Good practice:

- Set **Largest dispatch power** to what your installation can deliver.
- Give every dispatch a time limit and a battery level limit.
- Consider Tesla's own **Time-Based Control** with the [Tesla tariff](tesla-tariff.md) instead: no dispatch at all.

## Going off-grid

`go_off_grid` opens the Powerwall's grid contactor, exactly like *Go off-grid* in the Tesla app.

- **If the battery runs out before the house is reconnected, your power goes off.** Always set `min_soc` or `duration_minutes` high enough.
- **The automatic reconnect needs Home Assistant running.** If Home Assistant (or the host, or the network) is down when the time is up, nothing reconnects; the Powerwall stays off-grid until you reconnect it here or in the Tesla app.
- Solar stops for about 30 seconds while the contactor switches.
- Don't use it for long periods unattended. It's meant for testing your backup, with you nearby.

The integration helps:

- It refuses unless the Powerwall is on the grid and the battery is above the reconnect level (default: the backup reserve).
- `confirm: true` is required.
- If the gateway doesn't report the switch within 90 seconds, it sends a reconnect and fails, so the contactor can't open later on.
- The run is saved, so the reconnect still happens after a restart. A failed reconnect is retried every minute, with a repair.

## Max backup

`start_max_backup` charges the battery to full, from the grid if needed, and holds it. It's the same command Storm Watch uses. Grid charging during a max backup may cost money on your tariff.

## The key and your Tesla sign-in

- The RSA private key and the Tesla sign-in are stored in `config/tesla_tedapi/` with owner-only permissions. **Anyone with the key and access to your network can control the Powerwall.** Treat both like passwords.
- They're included in Home Assistant backups. Keep your backups private.
- A registered key can't be removed from here: remove it in the Tesla app if you stop using it.
- See [SECURITY.md](../SECURITY.md) for reporting vulnerabilities and what to do if a secret leaks.

## Experimental features

The **On-grid solar curtailment (experimental)** switch writes an undocumented flag to the gateway's configuration. It has not been tested on a real Powerwall. It's disabled by default; enable it only to experiment, and watch your solar output.
