# Changelog

## 2.2.1

- The Powerwall also drops its **grid charging** setting about once an hour, and grid charging then turns on by itself (seen on a Powerwall 3 right after it was switched off). Like the export rule, the setting you chose is now shown and written back over a signed connection. The Grid charging switch has the same `missing_on_powerwall`, `restored_count` and `last_restored` attributes as the export select. The existing restore option covers both.
- The Tesla cloud backup is checked with a real read about once an hour (sooner after a failure) instead of only when it is needed. This keeps the sign-in refreshed, and **Setup health** shows `degraded` as soon as the backup stops working rather than when the local connection is already down. New `cloud_last_ok` attribute.
- **Cloud backup through the Tesla Fleet integration** (new option). The Tesla Owner API now answers *403 Forbidden* to tokens from a sign-in in a normal browser (tokens from app-style sign-ins still work), so a Tesla account linked here through the pasted callback address no longer works as the backup. When the core Tesla Fleet integration is set up, its sign-in can be used instead: settings are read and written through its energy site API, and the token stays with Tesla Fleet (never refreshed here).
- Failed cloud reads are cached for 5 minutes, and once the sign-in repair is up only the hourly check tries again (each failed attempt was retried every poll).
- A cloud read that logs in but returns nothing (for example a refreshed token the Tesla API rejects) now counts as a failure, so the sign-in repair appears after three failed checks.

## 2.2.0

Fixes
- Signed (v1r) setup no longer fails with a misleading "key not accepted" when the gateway password is left empty: the password is now required (pypowerwall logs in with its last 5 characters) and checked first, with its own error.
- Solar input sensors read the field names pypowerwall actually produces (`PVAC_PVMeasured*_A..F`), so they now appear on Powerwall 3 with DC solar. They are named **Solar input A–F** power / voltage / current.
- The cloud backup uses this entry's energy site instead of the shared `.pypowerwall.site` file (several Powerwalls on one account).
- The **Alert** event entity follows the integration's availability.
- Battery packs and solar inputs are added when they first appear (also after starting with the gateway unreachable, or after adding an expansion pack).

Grid export setting
- The Powerwall sometimes drops its export rule from its own settings while the cloud keeps it (seen hourly on a Powerwall 3). The select now shows the rule you set instead of pypowerwall's "battery_ok" guess, and over a signed connection the rule is written back. New attributes `missing_on_powerwall`, `restored_count`, `last_restored`; option to turn it off.

Alerts
- Status alerts (such as *Export limited*) no longer create Alert events by default; an option brings them back.
- Alert start times are kept across restarts.
- The Problem delay is an option (default 10 minutes).

Setup
- The wizard finds the gateway: network scan, or DHCP discovery (`teslapw_*` and Tesla gateway part numbers).
- When the router gives a known gateway a new address, the entry follows it (the gateway is identified by logging in and reading its DIN).
- Gateway address and password come first and are checked straight away.
- Key confirmation is detected automatically (checked every 10 seconds, re-registered once if the window closes).
- An existing key is uploaded instead of typing a path, checked and stored with owner-only permissions.
- Reauthentication when the gateway keeps refusing the password or the key; settings keep working through the cloud meanwhile.
- A fixable repair to sign in to Tesla again when the cloud backup stops working.
- Options: link or unlink a Tesla account after setup; export restore; status alert events; Problem delay; dispatch floor and power cap.
- New **Setup health** sensor.

Other
- Cloud reads of settings are cached for 5 minutes (Owner API rate limits).
- A discharge dispatch without `min_soc` now stops at the backup reserve (option, on by default). Pass `min_soc` to go lower.
- pypowerwall is capped below 0.23 (tested with 0.22.x).

## 2.1.0

- Powerwall alerts are now explained: each known code has a plain-language description and a severity (status, notice, warning, fault).
- New **Alert level** sensor (OK / Notice / Warning / Fault) and **Active alerts** sensor (short readable list), so the device page shows what is active without opening attributes.
- New **Problem** binary sensor: on when a warning or fault has lasted 10 minutes or more.
- New **Alert** event entity: every alert raised or cleared appears in the device's activity log and the logbook, and can trigger automations.
- Lasting warnings and faults appear under **Settings → Repairs** and disappear when they clear.
- The **Alerts** sensor has a new `details` attribute listing each alert's severity, description, start time and source.

## 2.0.1

- Firmware version sensor shows just the version (for example `26.34.1`); the gateway's build hash moves to the `build` attribute.
- Tests wait for background timer tasks on current Home Assistant; CI actions updated to Node 24 releases.

## 2.0.0

First public release.

- Setup wizard: Tesla sign-in, RSA key registration and physical confirmation from Home Assistant; existing keys and Wi-Fi password setups also supported.
- Signed (v1r) local connection on the home network, with the Powerwall hotspot as an optional fallback.
- Settings (operation mode, backup reserve, grid charging, grid export) written locally, with the Tesla cloud as an optional backup.
- Managed battery dispatch sessions: renewed in 5-minute steps, `min_soc` / `max_soc` limits, `Dispatch active` binary sensor and `tesla_tedapi_dispatch_ended` event.
- New `Battery level` sensor on the Tesla app scale.
- Translated entity names and select options, icons, diagnostics, options flow (polling interval), reconfigure flow.
- Select options are now keys (`self_consumption`, `backup`, `autonomous`; `battery_ok`, `pv_only`, `never`) instead of display labels. Update automations that set or compare them.
