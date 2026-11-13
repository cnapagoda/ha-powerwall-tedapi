# Troubleshooting

**Start with Setup health.** Open the device page and look at **Setup health**. If it says *Needs attention*, its `problems` attribute names the failing check, and **Settings → Repairs** usually has an entry explaining what to do.

- [Setup](#setup)
- [Tesla sign-in](#tesla-sign-in)
- [Readings](#readings)
- [Settings and control](#settings-and-control)
- [Cloud backup](#cloud-backup)
- [Repairs explained](#repairs-explained)
- [Collecting information for a bug report](#collecting-information-for-a-bug-report)

## Setup

| Symptom | Try |
|---|---|
| *The gateway refused this password* | Use the password from the QR sticker inside the Powerwall's cover, in upper case. Usually only the last 5 characters are used, so you can also enter just those. A Backup Gateway 1 (part number `1118431-…`) needs the full password. |
| *The Powerwall didn't accept the dispatch: …* | The text after the colon is the gateway's own reason, with its status code. One Powerwall 3 refuses more than 10,000 W. Lower the power, or **Largest dispatch power**. |
| *pypowerwall … is loaded in Home Assistant, but this integration needs … or newer* (setup stops, or the integration shows *Failed to set up*) | Home Assistant runs one pypowerwall for all integrations, and another custom integration that pins an older one loaded it first (for example `ha-pypowerwall`). Remove that integration (disabling it isn't enough: delete it in HACS or from `custom_components`), restart Home Assistant and try again. See [which integrations can stay](migration.md#which-integrations-can-stay). |
| *The gateway is refusing logins after too many attempts* | The gateway locks its login for a few minutes after several wrong passwords. Wait about 15 minutes before you try again. |
| *Could not reach the Powerwall* | Check the address. `ping` it from the Home Assistant host. Home Assistant and the gateway must be on the same network (or routed). The gateway uses HTTPS on port 443. |
| **Search my network** finds nothing (*No Powerwall answered on this network*) | Enter the address yourself. Your router lists the gateway as `teslapw_…` (or a Tesla part number such as `1707000-…`). The scan only covers Home Assistant's own network. |
| The wizard keeps waiting at *Confirm at the Powerwall* | Powerwall 3: leave the **On/Off switch** off for a full **15 seconds** before switching it back on. A quick flick is ignored. If the 10-minute window closes, the wizard registers the key again once; after that it asks you to try again. |
| *The Powerwall did not confirm the key* | Try again and do the switch step promptly. Other models: try the AC breaker off for 15 seconds. |
| *The Powerwall did not accept the key* | The key isn't verified, or it belongs to another Powerwall. Register a new key and do the switch step within 10 minutes. |
| Key registration fails | The error says why: Tesla refused the sign-in token, the energy site is wrong, or Tesla can't reach the Powerwall (it needs its internet connection to receive the registration). |
| *No Powerwall was found on this Tesla account* | Sign in with the Tesla account that owns the Powerwall (as shown in the Tesla app). |
| The wrong Powerwall is chosen | The site is matched by the gateway's serial. With several sites the wizard asks; choose the one with this gateway. |

## Tesla sign-in

| Symptom | Try |
|---|---|
| *Tesla did not accept that code* | Codes are single-use and expire quickly. After Tesla rejects a code, the form shows a new link: sign in with that one and copy the callback address straight away. |
| *That is not a sign-in address from the link above* | The pasted address is from an older link or was cut short. The link is left unchanged, so you can sign in again with it. |
| *Tesla did not accept that refresh token* | Make a new token in the token app. Don't reuse a token another integration uses: each refresh replaces it. |
| *The Tesla Owner API refused the token (HTTP 403)* | Update to 2.3.0 or newer (2.2.x connects to Tesla in a way it now refuses), then sign in again. If the 403 keeps coming back on 2.3.0+, Tesla may have changed its rules again: please open an issue. Alternatively use [Tesla Fleet / Teslemetry](cloud-backup.md) for the cloud backup. |
| Signed out of another Tesla integration after setting this one up | You shared one refresh token between two integrations. Give each its own. |

## Readings

| Symptom | Try |
|---|---|
| All sensors *unavailable* | The gateway can't be reached. Check the network; settings keep working through the cloud backup if you have one. **Setup health** shows `gateway_reachable: false`. |
| Power sensors *unknown* for a moment | The gateway's meters didn't answer that poll. The next reading fills it in; a missed reading is no longer recorded as 0 W. |
| Readings stop now and then | Add the Powerwall's Wi-Fi as **Wi-Fi fallback address** if Home Assistant can reach it, or improve the network path (Ethernet for the gateway, a better access point). Try a longer **Polling interval** if another app also polls the gateway. |
| **Battery level** differs from the Tesla app by a point | Normal rounding and timing: the app updates less often. **Battery state of energy** is the raw value and reads 2–4 points higher. |
| No **Solar input** sensors | Normal when your solar has its own inverter: they only appear for the Powerwall 3's built-in inputs in use. Inputs that start producing later appear by themselves. |
| **Solar input** sensors that always read about 0 W, or are *unavailable* | Inputs with nothing connected (for example with an external inverter) get no sensors. Sensors made for them by older versions are removed at startup when their recorded power never reached 20 W. If one stays, delete it on the entity's settings page. |
| A pack or expansion pack is missing | Packs appear when the gateway first reports them. Check the **Alerts** sensor for *Battery packs missing*. |
| Energy counters don't move | **Setup health** checks `energy_counters`. Readings that are missing or zero are ignored, and so is a lower reading unless it stays lower for 10 readings in a row (then it's taken as a counter reset, for example after a gateway replacement). |
| **Grid counter check** says *Counters differ* | The gateway's grid meters disagree with the grid power readings by more than 10 %. An occasional day can come from gaps in readings. If it lasts, mention it in an issue with diagnostics. |
| No **Grid voltage L1–L3** or **Meter** sensors | They're created for the phases and current transformers the gateway reports and that are in use. Ones that start carrying current later appear by themselves. |
| **Battery capacity trend** and **Battery warranty level date** show *unknown* | They build up from one sample a day and show once there are 30 samples over 90 days. |
| **Battery round-trip efficiency** shows *unknown* | It shows once 50 kWh has been charged into the battery. It also stays *unknown* if the counters give more energy out than in, which can happen when solar charges the battery without being counted as charge. |
| No **Wall Connector** sensors | They come from the cloud backup and appear once the site's Wall Connector shows up there. |
| **Tariff buy price** / **Tariff sell price** missing | Turn on the **Tesla tariff (cloud)** option. The sell price shows for tariffs with feed-in rates. |
| **Inverter heatsink temperature** never changes | Known on some firmware. |
| Entity IDs start with an area name | Home Assistant 2026.8+ includes the area by default. Replace `powerwall_tedapi_` in the examples with yours, or change the format and use **Recreate entity IDs**. See [conventions](README.md#conventions-in-these-pages). |

## Settings and control

| Symptom | Try |
|---|---|
| Settings change back by themselves | Usually a retailer, VPP or another app with access to your Tesla account. For the export rule and grid charging, see [Dropped settings](dropped-settings.md): the Powerwall itself drops them. |
| Grid charging turns itself on | The Powerwall dropped the setting. With **Restore settings when the Powerwall drops them** on, it's written back within a couple of minutes (signed connection). |
| Backup reserve shows 80 % after setting more | The gateway didn't take the change, so it went through Tesla's cloud, which caps the reserve at 80 %. A repair says so. Check the gateway connection. To charge to full and hold it, use [max backup](actions.md#tesla_tedapistart_max_backup). |
| Settings entities missing | A Wi-Fi password connection without a cloud backup is read-only, so there are no settings entities. Add a cloud backup, or switch to signed (**⋮ → Reconfigure** with a key). |
| *Battery dispatch needs the signed (v1r) connection* | Dispatch, max backup and go off-grid only work over a signed connection. |
| A dispatch is refused | Check the message: power above **Largest dispatch power**, battery already past the limit, or already at the target. |
| A dispatch ended early | Check `last_end_reason` on **Dispatch active**. `min_soc` without your own `min_soc` means the backup reserve was reached (option **Discharges stop at the backup reserve**). `unloaded` means the integration reloaded, for example after changing the Tesla tariff option, the cloud backup or the connection. |
| A discharge exports although export is *Disabled* | Expected: a dispatch overrides the export setting. See [Safety](safety.md). |
| `go_off_grid` is refused | The Powerwall must be on the grid and the battery above the reconnect level (default: the backup reserve). |
| `reconnect_grid` fails | During a real outage there's no grid to sync to. Otherwise check the gateway connection. |

## Cloud backup

| Symptom | Try |
|---|---|
| *Sign in to Tesla again* repair | Select it and sign in again. Tokens stop working when you change your Tesla password, or when another app refreshes the same token. |
| *Fleet API cloud backup is not working* | Select it to sign in to Tesla Fleet or Teslemetry again. |
| *Fleet API cloud backup can't change settings* | Select it to sign in again, and allow the energy commands (`energy_cmds`) permission. |
| *Sign in to Tesla Fleet or Teslemetry* repair | Set up before 2.6 through another integration's sign-in. Select it to sign in here. |
| The Fleet API isn't offered under **Cloud backup** | Sign in first: **Configure → Sign in to Tesla Fleet or Teslemetry**. |
| Tesla Fleet sign-in: *Tesla refused the developer app (HTTP 412)* | The app's domain isn't registered with Tesla in your account's region. See [Setup](setup.md#registering-with-the-fleet-api-developers). (A wrong region is found by itself.) |
| Tesla Fleet sign-in: *Tesla Fleet refused the new sign-in* | The app needs the energy scopes and a domain registered with Tesla. See [Setup](setup.md#registering-with-the-fleet-api-developers). |
| **Setup health** says *Needs attention* about the cloud | No backup can change settings. `cloud_backends` shows which backup fails and why. |
| *A cloud change did not apply as set* | Tesla accepted it but the Powerwall reports another value after 5 minutes. Check the Tesla app; your retailer or VPP may override it. |

## Repairs explained

| Repair | Meaning |
|---|---|
| *Powerwall alert: …* | A warning or fault has lasted the Problem delay. It disappears when the alert clears. See [Alerts](alerts.md). |
| *Sign in to Tesla again* | The Owner API backup failed 3 checks in a row. Fixable: select it to sign in. |
| *Fleet API cloud backup is not working* / *can't change settings* | See above. |
| *Signed local access stopped after a firmware update* | The first reading on new firmware couldn't use the signed connection. Wait for a few polls; if it stays, register a new key (**Configure → Register a new signed key**) and open an issue with the firmware version. |
| *The Powerwall couldn't reconnect to the grid* | The automatic reconnect failed; it's retried every minute. Check the gateway connection, or reconnect in the Tesla app. |
| *Tesla's cloud capped the backup reserve* / *A cloud change did not apply as set* | See [Settings and control](#settings-and-control). |
| *Tesla stored a different tariff* | A tariff write wasn't shown by Tesla after 35 minutes (or a Fleet API read-back showed other rates). See [Tesla tariff](tesla-tariff.md). |
| *A Powerwall 3 is missing from the readings* | With several Powerwall 3s, one stayed out of the readings for 10 polls. On the signed connection the followers are read over the **Wi-Fi fallback address** only: set it to `192.168.91.1` if Home Assistant can reach the Powerwall's Wi-Fi (if the connection already uses `192.168.91.1`, check that it still answers). Totals and **Battery health** still count every Powerwall. See [Several Powerwalls](setup.md#several-powerwalls). |

The integration's card can also show **Reauthenticate**: the gateway keeps refusing the password or the key. See [Configuration](configuration.md#when-the-powerwall-stops-accepting-home-assistant).

## Collecting information for a bug report

1. **Diagnostics:** **Settings → Devices & services → Tesla Powerwall TEDAPI → ⋮ → Download diagnostics.** Passwords, email, site ID and serial numbers are hidden; check it anyway before sharing. For connection problems, the `tedapi_mode` field shows the connection actually in use. Turn on **Gateway configuration in diagnostics** in the options if you're asked for it.
2. **Debug log:** add this to `configuration.yaml` and restart, or use **Enable debug logging** on the integration's page:

   ```yaml
   logger:
     logs:
       custom_components.tesla_tedapi: debug
       pypowerwall: debug
   ```

3. Reproduce the problem, then **Disable debug logging** (the log downloads automatically) or copy the relevant lines.
4. [Open an issue](https://github.com/cnapagoda/ha-powerwall-tedapi/issues/new/choose) with the versions (integration, Home Assistant, Powerwall model and firmware), your connection type, the diagnostics and the log.

**Never post** the key (`tedapi_rsa_private.pem`), Tesla tokens (`.pypowerwall.auth`) or the gateway password.
