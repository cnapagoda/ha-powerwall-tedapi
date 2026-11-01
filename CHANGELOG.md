# Changelog

## 2.5.0

- **Blueprint "Dynamic price tariff":** every 30 minutes it turns a price forecast (Amber by default; Octopus, Nord Pool and others by setting the attribute and key names) into Tesla's tariff for the next 24 hours and sends it with `set_tariff`, so the Powerwall can stay in Time-Based Control. It skips unchanged rates, refuses a forecast with more than 10 of 48 half hours missing, and has a dry run.
- **`set_tariff`:** new `read_back` (`full`, the default; `once`, one read after 15 seconds, for scheduled writes; `none`) and `only_if_changed` (skip when these are the rates last sent or the ones Tesla shows; the dry run says so as `unchanged`).
- **Tariff read-back:** a new write no longer postpones the check of an earlier one Tesla doesn't show yet, and no longer delays the 6-hourly tariff read. Tesla showing an earlier write counts as catching up; the repair comes only for a write still not shown 35 minutes after it was sent.
- **Diagnostics:** an option (off by default) adds the gateway's own configuration to diagnostics downloads, with serial numbers, location, names and credentials removed. Diagnostics also include the Tesla tariff when the Tesla tariff option is on.

## 2.4.4

- **Battery dispatch:** a stop (target reached, `resume_battery_operation` or unloading) could reach the gateway before a renewal already on its way, so the Powerwall kept charging or discharging for up to 5 minutes past the target, ignoring `min_soc`. Dispatch commands now reach the gateway in order.
- **`set_tariff`** no longer refuses a tariff with an empty season written as `{}`, a shape Tesla accepts. The dry run still warns that an empty season is inactive.
- **Discovery:** a Powerwall set up over its Wi-Fi hotspot is no longer offered as a new device when the gateway shows up on the home network. Its stored address is left alone.
- **`set_tariff` read-back** no longer logs in or renews the Owner API token on Home Assistant's event loop, where it could block Home Assistant while waiting on the network.
- **Grid outages:** an outage across a month end counts in both months, each with its own time up to or from local midnight. Before, the new month missed the outage and got all its time.

## 2.4.3

- **`set_tariff` through Tesla Fleet / Teslemetry** sent the tariff wrapped twice (`tariff_content_v2` inside `tariff_content_v2`). It now sends it once, and never sends an empty tariff. The Owner API path was not affected.
- **Tesla tariff sensor:** fields Tesla leaves out of a tariff now count as 0, as Tesla means them. A period ending on Monday no longer matches the rest of the week, and an empty season is no longer active all year.
- **Read-back after `set_tariff`:** Tesla is given about 25 seconds to show the new tariff, and rates are compared the way Tesla stores them, so it no longer raises false repairs. On the Owner API, a write Tesla doesn't show yet is reported as `pending` and checked again in 35 minutes. The response has a new `status`: `confirmed`, `pending` or `mismatch`.
- **`set_tariff` dry run** lists what Tesla would change or leave unpriced (`warnings`). Only `tariff_content_v2` can be sent.
- **`import_battery_history`:** its description now suggests a backup first.

## 2.4.2

- **charge_to / discharge_to** stop on the reading that reaches the target, instead of at the next 60-second renewal.
- The hourly restore of a dropped export rule or grid-charging setting is logged at info level; a warning only when the restore fails.
- **Backup check failed** (shown for a few minutes most days when the battery fills, and after restarts) only becomes a Problem and repair after 30 minutes; it still shows in the alerts meanwhile.
- **Tesla tariff (cloud, optional, off by default):** a sensor with the tariff Tesla uses for Time-Based Control (current period and rates), and `get_tariff` / `set_tariff` actions (dry run by default, rates read back after a write). Needs a cloud backup.
- **`import_battery_history`:** one-off copy of Tesla Fleet or Teslemetry's earlier battery charged/discharged history into the battery energy statistics, joined seamlessly (dry run by default).

## 2.4.1

- Renamed to **Tesla Powerwall TEDAPI** (HACS, the integration list, setup and notices). The device keeps its name, so entity names and ids don't change.
- Tests for failure paths (gateway reads failing one by one, a restore that fails, unreadable backup events, signed-only commands on a Wi-Fi entry). Coverage 99 %; CI now fails below 99 %.

## 2.4.0

- **Warranty warning** on battery dispatch (README Safety, and the descriptions of `dispatch_battery_power`, `charge_to` and `discharge_to`): Tesla's warranty may cover unlimited cycles only for self-consumption, time-based control and backup.
- A dispatch session logs one warning when it starts. pypowerwall's warning for every accepted command (each renewal, every minute) is dropped.
- pypowerwall **0.23.1** (`>=0.23.1,<0.24`): a reconnect that fails keeps the working client, and the battery level no longer goes below 0 % under the reserve.
- Setup: the Powerwall 3 confirmation uses the **On/Off switch (left side)**.
- **Grid outage tracking**: **Last grid outage** (with `ongoing` and `ended`), **Last grid outage duration**, **Grid outages this month** and **Grid outage time this month** (reset monthly, kept across restarts), and a `tesla_tedapi_grid_outage` event when an outage starts or ends. Off-grid runs started here and an unreachable gateway are not outages.
- **Firmware updates**: a `tesla_tedapi_firmware_updated` event and log line when the gateway reports a new version, and **Firmware version** keeps `previous_version` and `updated_at`. On a signed connection, the first reading on the new firmware checks signed access; if it stopped working, a repair says so (and clears when it works again).
- **Logbook** entries for firmware updates, grid outages, and the end of dispatch sessions and off-grid runs.
- **Battery health per pack.** A new per-pack **health** sensor (capacity against the rated 13.5 kWh, like **Battery health**; a new pack reads above 100 %), with the warranty level (70 % of 13.5 kWh) as an attribute. **Battery health** adds `warranty_kwh` and `lifetime_discharged_kwh`. On a Powerwall 3 (pypowerwall 0.23+), per-pack **lifetime discharge** and **cycles** (lifetime discharge ÷ 13.5 kWh, with the battery's own full-cycle and over-discharge counts as attributes).
- **Experimental: On-grid solar curtailment** switch (signed connection only, disabled by default): reads and writes Tesla's `on_grid_solar_curtailment_enabled` flag in the gateway's config. Undocumented by Tesla and not tested on a real Powerwall. README: negative feed-in prices with built-in solar are handled by setting export to *never*.
- **Blueprints** (README → Blueprints, one-click import): stop exporting at negative feed-in prices, charge when import is cheap, export on a price spike, time-of-use charge and discharge, and a grid outage alert. They take any price sensor and change the Powerwall only through this integration.
- Project: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `SECURITY.md`, a pull request template and a feature request form.
- **Cloud changes are read back.** A setting changed through a cloud backup is logged as sent, then checked against the readings for up to 5 minutes. If the Powerwall reports something else, a repair says so. When Tesla's cloud caps the backup reserve at 80 %, the repair suggests a max backup or a local write.
- **`tesla_tedapi.charge_to`** and **`tesla_tedapi.discharge_to`** actions (signed connection): charge or discharge at a set power until the battery reaches a target level, or for at most a time limit. They run as a dispatch session, end with the new reason `target_reached`, and are refused when the battery is already at the target. **Dispatch active** shows the `target_soc`.

## 2.3.1

- **`tesla_tedapi.go_off_grid`** and **`tesla_tedapi.reconnect_grid`** actions (signed connection): disconnect the house from the grid and reconnect it, the same command as *Go off-grid* in the Tesla app (Tesla's `setIslandMode`, through pypowerwall).
- Going off-grid needs `confirm: true`. It is refused unless the Powerwall is on the grid and the battery is above the reconnect level (`min_soc`, default the backup reserve).
- Each command is checked against the gateway's grid status for up to 90 seconds. A go off-grid that isn't confirmed is taken back, so the contactor can't open later on.
- An off-grid run reconnects at the end of `duration_minutes` or when the battery falls to `min_soc`. The run is saved and watched again after a restart or reload. A failed automatic reconnect is retried every minute and raises a repair.
- New event `tesla_tedapi_off_grid_ended` (`reason`: `completed`, `min_soc`, `requested`, `reconnected_elsewhere`). **Grid connected** shows the run in its attributes.
- Texts in all eight languages.
- **Register the key through Tesla Fleet or Teslemetry**, in setup and with **Configure → Register a new signed key**.
- The Owner API access token is renewed when it has less than 75 minutes left, at the next cloud read (at least hourly). Before, it ran out every 8 hours and the next call got HTTP 401, which pypowerwall logged while it refreshed and retried.

## 2.3.0

- **Two cloud backups.** Besides a Tesla account linked here (Owner API), the cloud backup can now use Home Assistant's **Tesla Fleet** or **Teslemetry** integration (official Fleet API). **Configure → Cloud backup** chooses *None*, *Owner API*, *Tesla Fleet (or Teslemetry)*, or both in either order. Without a choice, a linked account stays the backup, as before.
- The Tesla Fleet backup reads the site data that integration already fetches every minute, so it adds no Tesla calls and never holds up local readings. Changes go through its API object, which then refreshes. Its token is never refreshed here, so it stays signed in.
- With two backups, settings are read from the first that answers and a change goes to the first that accepts it. A change Tesla doesn't answer in time is not sent to the other backup too. The setting entities have a new `cloud_backend` attribute.
- Each backup has its own check, cache, failure count and repair. **Setup health** lists them under `cloud_backends` and shows *Needs attention* only when none can change settings. New repairs: *Tesla Fleet cloud backup is not working* and *Tesla Fleet cloud backup can't change settings* (sign-in without `energy_cmds`).
- Tesla Fleet still loading after a restart counts as *starting* for up to 10 minutes. A disabled Tesla Fleet entry, or one that keeps failing, counts as not working.
- The Tesla Fleet site is matched to this Powerwall by the site id, gateway and battery serials; a site with another Powerwall is never used, and Tesla Fleet is only offered when a site matches.
- The Owner API export rule is read from the site's own setting. pypowerwall reported "never" whenever the site has `non_export_configured`, which stays set on sites that do export with "battery_ok".
- Linking or unlinking a Tesla account keeps a Tesla Fleet backup that was chosen.
- The `fleet_backup` option left by a 2.2.1 test build is removed when the entry is updated.
- **Backup event** binary sensor (signed connection): on while the Powerwall runs a backup event, whether a max backup or one Tesla's Storm Watch scheduled, with its kind, start and end. Read locally from the gateway.
- **`tesla_tedapi.start_max_backup` / `cancel_max_backup`** actions (signed connection): charge to full and hold for a set time, locally, the same command Storm Watch uses.
- **Storm Watch** switch and **Storm Watch active** binary sensor, through the cloud backup (Tesla Fleet data, or the Owner API every 30 minutes), on sites Tesla reports as Storm Watch capable.
- **Grid services active** binary sensor and **Off-grid vehicle charging reserve** number, through the cloud backup, only on sites that use them.
- **Grid connected** binary sensor: on while on the grid, off during an outage, *Go off-grid* or while syncing back. Read locally.
- **Grid import / export power** and **Battery charge / discharge power** sensors: the signed power readings split by direction, never negative.
- **Fan speed** sensors (diagnostic): the Powerwall 3 inverter's two fans (with drive duty), or each Powerwall 2's fan (with target speed), from the readings already fetched.
- **Help text** under the setup fields: gateway address and password, key file, sign-in token or callback address, energy site, and the reconfigure fields.
- **Clearer failures.** A failed key registration now says why: Tesla refused the sign-in token, the energy site is wrong, or Tesla can't reach the Powerwall, each with what to do (anything else lists all three). Connection, key, account and action errors also say what to check.
- **Translations:** German, French, Dutch, Spanish, Italian, Polish and Brazilian Portuguese.
- When the gateway can't be reached but the cloud backup keeps the settings working, this is logged once as a warning, and once more when the gateway answers again (it was only a debug message).
- Setting, dispatch and max backup errors are translated, and say what to check. A change that the Powerwall and the cloud backup both refuse logs each reason.
- `PARALLEL_UPDATES` is set on every platform: setting changes go one at a time.
- **Tesla sign-in link stays put** unless a code from it reached Tesla. A paste from an older link, a mistyped address or a refused refresh token no longer replaces the link, so a sign-in tab that is still open keeps working. New messages say which case it was: a callback that isn't from the current link, or a refresh token Tesla rejected.
- **Tesla sign-in link** now includes `locale=en` and `prompt=login`, like the Tesla app's own sign-in. With `prompt=login` Tesla always shows its sign-in form instead of reusing a browser session; codes from links without it were refused by the Owner API (HTTP 403) in testing, while the same steps with it worked.
- **Tesla account sign-in works again from Home Assistant.** Tesla fingerprints the TLS handshake that mints a token, and tokens minted with pypowerwall's TLS 1.3 handshake in Home Assistant's runtime were refused by the Owner API (HTTP 403), for both pasted refresh tokens and browser sign-ins. The integration now caps pypowerwall's Tesla connections at TLS 1.2 (HTTP/2 stays), as the Tesla Custom integration does (alandtse/tesla #1202): sign-in, token refresh, the Owner API calls and the Owner API cloud backup.
- Linking a Tesla account (or unlinking it) now sets up the Owner API backup straight away. The account and the new cloud backup choice were saved in two steps, and the reload after the first one kept the old choice until the next reload.
- **Unlink a cloud backup** (Configure): choose which backup to stop using. *Owner API* removes the Tesla account linked here; *Tesla Fleet* only stops this integration using it (the Tesla Fleet or Teslemetry integration is left as it is). The menu offers it whenever a backup is in use, next to *Link a Tesla account* when no account is linked.
- The sign-in screens, errors and README no longer say browser sign-ins are blocked: the browser sign-in and a refresh token both work again, in all eight languages.
- `quality_scale.yaml` records each rule of Home Assistant's integration quality scale: Bronze to Gold done, except dependency transparency (pypowerwall's PyPI releases), and Platinum still to do.
- README: new sections *Data updates*, *Use cases*, *Known limitations* and *Removing*, and a troubleshooting row for the backup reserve that Tesla's cloud caps at 80 %.
- Tests: the config flow, the gateway login and the Tesla account helpers are fully covered (96 % overall), including the Tesla Fleet fallback for every setting.

## 2.2.1

- The Powerwall also drops its **grid charging** setting about once an hour, and grid charging then turns on by itself (seen on a Powerwall 3 right after it was switched off). Like the export rule, the setting you chose is now shown and written back over a signed connection. The Grid charging switch has the same `missing_on_powerwall`, `restored_count` and `last_restored` attributes as the export select. The existing restore option covers both.
- The Tesla cloud backup is checked with a real read about once an hour (sooner after a failure) instead of only when it is needed. This keeps the sign-in refreshed, and **Setup health** shows `degraded` as soon as the backup stops working rather than when the local connection is already down. New `cloud_last_ok` attribute.
- **Tesla sign-in with a refresh token.** The Tesla Owner API now answers *403 Forbidden* to tokens from a sign-in in a normal browser (the pasted callback address), while tokens from app-style sign-ins keep working. Every sign-in screen (setup, *Link a Tesla account*, the sign-in repair) also accepts a refresh token from an app-style sign-in such as *Auth app for Tesla*, which the Owner API still accepts. A sign-in that Tesla refuses (HTTP 403) now says so and points to this, instead of "Tesla did not accept that code".
- Failed cloud reads are cached for 5 minutes, and once the sign-in repair is up only the hourly check tries again (each failed attempt was retried every poll).
- README: new section *Getting a Tesla refresh token* (token apps for iOS, Android and desktop, how to paste the token, why each integration needs its own), why registering a key still needs a Tesla account, and how to register through the Fleet API with `pypowerwall register` when you have a developer app.
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
