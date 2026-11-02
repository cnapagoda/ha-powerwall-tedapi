# Cloud backup

Everything normally runs locally. A cloud backup is used **only** when the local connection can't read or change a setting:

- the gateway can't be reached, or
- you use the Wi-Fi password connection, which can't change settings locally.

It also provides the few things that only exist in Tesla's cloud: **Storm Watch**, **grid services**, the **off-grid vehicle charging reserve** and the **[Tesla tariff](tesla-tariff.md)**.

A cloud backup is optional. Without one, everything that runs locally still works.

## The two backups

| | Owner API | Tesla Fleet |
|---|---|---|
| What it uses | A Tesla account linked here (refresh token or browser sign-in) | Home Assistant's [Tesla Fleet](https://www.home-assistant.io/integrations/tesla_fleet) or [Teslemetry](https://www.home-assistant.io/integrations/teslemetry) integration (Tesla's official Fleet API) |
| You need | A Tesla sign-in ([how](setup.md#signing-in-to-tesla)) | That integration enabled and loaded, on the Tesla account with this Powerwall, with the energy permissions (`energy_device_data` to read, `energy_cmds` to change settings) |
| Reads | Tesla's site settings, at most every 5 minutes while needed | The site data that integration already reads every minute: no extra Tesla calls |
| Token | Kept and refreshed here (`config/tesla_tedapi/.pypowerwall.auth`) | That integration's own; used but never refreshed here, so it stays signed in |
| Cost | None | A change of setting is a command on your Tesla developer account (Tesla Fleet) or Teslemetry subscription |
| Risk | Tesla may stop accepting Owner API tokens one day | Tesla Fleet needs a developer app and key hosting ([details](setup.md#registering-with-the-fleet-api-developers)) |

**If you already use Teslemetry or Tesla Fleet, use it.** It adds no extra Tesla calls and uses Tesla's official API. Add the Owner API as a second backup if you like.

## Setting it up

- **Owner API:** sign in to Tesla during setup (path A does it anyway), or later with **Configure → Link a Tesla account (Owner API)**.
- **Tesla Fleet / Teslemetry:** set up that integration first, then choose it under **Configure → Cloud backup**.

**Configure → Cloud backup** offers the choices that are available: *None (local only)*, *Owner API*, *Tesla Fleet (or Teslemetry)*, *Tesla Fleet, then Owner API* and *Owner API, then Tesla Fleet*. Tesla Fleet is only offered when a loaded Tesla Fleet or Teslemetry entry has this Powerwall's site (matched by the gateway's serial; a single site with no serials listed is used too). Without a choice, a linked Tesla account is the backup.

**Configure → Unlink a cloud backup** stops using one. *Owner API* removes the Tesla account linked here. *Tesla Fleet* only stops this integration using it; the Tesla Fleet or Teslemetry integration isn't changed.

## How it behaves

- Settings are read from the first backup that answers. The setting entities' `cloud_backend` attribute says which one did, and `source` says `local`, `cloud` or `mixed`.
- A change the gateway doesn't take goes to the first backup that accepts it. If Tesla doesn't answer in time, it isn't sent to the other backup as well (it may still have been applied); the action reports an error instead.
- **A change made through the cloud is read back** for up to 5 minutes. If the Powerwall then reports a different value, a repair says so.
- **Tesla's cloud caps the backup reserve at 80 %.** Locally any value up to 100 % works. To charge to full and hold it, use a [max backup](actions.md#tesla_tedapistart_max_backup).
- Each backup is checked with a real read about once an hour (10 minutes after a failure). This keeps the Owner API sign-in fresh, so a broken sign-in shows up before you need it.
- When the gateway can't be reached but the cloud keeps the settings working, this is logged once as a warning, and again when the gateway answers.
- After a restart, Tesla Fleet can load after this integration. For up to 10 minutes it counts as *starting*, not as failing. A disabled entry, or one that keeps failing to load, counts as not working.

## When a backup stops working

**Setup health** lists each backup under `cloud_backends` (`ok`, `starting`, `can_change_settings`, `failures`, `last_ok`). It only shows *Needs attention* when **no** backup can change settings. A backup that needs attention gets its own repair under **Settings → Repairs**:

| Repair | What to do |
|---|---|
| *Sign in to Tesla again* | Select it and sign in again (refresh token or browser). Tesla tokens stop working when you change your password, or when another app refreshes the same token. |
| *Tesla Fleet cloud backup is not working* | Check that the Tesla Fleet or Teslemetry integration is enabled, loaded and signed in (re-authenticate it if it asks), and has this Powerwall on its account. |
| *Tesla Fleet cloud backup can't change settings* | Sign in to that integration again and allow the energy commands (`energy_cmds`) permission. |

## What never goes through the cloud

Battery dispatch, `charge_to` / `discharge_to`, max backup, going off-grid and reconnecting are **local only** (signed connection). They have no cloud fallback.
