# Cloud backup

Everything normally runs locally. A cloud backup is used **only** when the local connection can't read or change a setting:

- the gateway can't be reached, or
- you use the Wi-Fi password connection, which can't change settings locally.

It also provides the few things that only exist in Tesla's cloud: **Storm Watch**, **grid services**, the **off-grid vehicle charging reserve** and the **[Tesla tariff](tesla-tariff.md)**.

A cloud backup is optional. Without one, everything that runs locally still works.

## The two backups

| | Owner API | Fleet API |
|---|---|---|
| What it uses | A Tesla account linked here (refresh token or browser sign-in) | A sign-in made here, to Tesla's official Fleet API: **Tesla Fleet** (your own Tesla developer app) or **Teslemetry** (an access token) |
| You need | A Tesla sign-in ([how](setup.md#signing-in-to-tesla)) | Tesla Fleet: a developer app ([details](setup.md#tesla-fleet-your-own-developer-app)). Teslemetry: a Teslemetry plan. Either way the energy permissions: `energy_device_data` to read, `energy_cmds` to change settings |
| Reads | Tesla's site settings, at most every 5 minutes while needed | The same; Storm Watch and the other cloud-only values every 30 minutes |
| Token | Kept and refreshed here (`config/tesla_tedapi/.pypowerwall.auth`) | Kept in this integration's entry. A Tesla Fleet token is refreshed here; a Teslemetry access token ([from its console](https://teslemetry.com/console)) isn't: when it stops working, sign in again with a new one |
| Cost | None | Calls count on your Tesla developer account (Tesla Fleet) or Teslemetry plan |
| Risk | Tesla may stop accepting Owner API tokens one day | Tesla Fleet needs a developer app and key hosting ([details](setup.md#registering-with-the-fleet-api-developers)) |

The Fleet API is Tesla's official API. Add the Owner API as a second backup if you like. If you also use Home Assistant's Tesla Fleet or Teslemetry integration, the sign-in here is separate: neither changes the other.

## Setting it up

- **Owner API:** sign in to Tesla during setup (path A does it anyway), or later with **Configure → Link a Tesla account (Owner API)**.
- **Fleet API:** sign in to Tesla Fleet or Teslemetry during setup (path A offers both), or later with **Configure → Sign in to Tesla Fleet or Teslemetry**. That continues as a reauthentication on the integration's card. The energy site with this gateway is picked automatically.

**Configure → Cloud backup** offers the choices that are available: *None (local only)*, *Owner API*, *Fleet API*, *Fleet API, then Owner API* and *Owner API, then Fleet API*. Without a choice, a linked Tesla account is the backup.

**Configure → Unlink a cloud backup** stops using one and removes its sign-in here.

**Upgrading from 2.5 or earlier:** the Tesla Fleet backup used to borrow the sign-in of Home Assistant's Tesla Fleet or Teslemetry integration. It now needs its own. A repair, *Sign in to Tesla Fleet or Teslemetry*, asks for it; your cloud backup choice is kept, and a linked Owner API account keeps working meanwhile.

## How it behaves

- Settings are read from the first backup that answers. The setting entities' `cloud_backend` attribute says which one did, and `source` says `local`, `cloud` or `mixed`.
- A change the gateway doesn't take goes to the first backup that accepts it. If Tesla doesn't answer in time, it isn't sent to the other backup as well (it may still have been applied); the action reports an error instead.
- **A change made through the cloud is read back** for up to 5 minutes. If the Powerwall then reports a different value, a repair says so.
- **Tesla's cloud caps the backup reserve at 80 %.** Locally any value up to 100 % works. To charge to full and hold it, use a [max backup](actions.md#tesla_tedapistart_max_backup).
- Each backup is checked with a real read about once an hour (10 minutes after a failure). This keeps the Owner API sign-in fresh, so a broken sign-in shows up before you need it.
- When the gateway can't be reached but the cloud keeps the settings working, this is logged once as a warning, and again when the gateway answers.

## When a backup stops working

**Setup health** lists each backup under `cloud_backends` (`ok`, `can_change_settings`, `failures`, `last_ok`). It only shows *Needs attention* when **no** backup can change settings. A backup that needs attention gets its own repair under **Settings → Repairs**:

| Repair | What to do |
|---|---|
| *Sign in to Tesla again* | Select it and sign in again (refresh token or browser). Tesla tokens stop working when you change your password, or when another app refreshes the same token. |
| *Fleet API cloud backup is not working* | Select it to sign in to Tesla Fleet or Teslemetry again. |
| *Fleet API cloud backup can't change settings* | Select it to sign in again, and allow the energy commands (`energy_cmds`) permission. |
| *Sign in to Tesla Fleet or Teslemetry* | Set up before 2.6 through another integration's sign-in: select it to sign in here. |

## What never goes through the cloud

Battery dispatch, `charge_to` / `discharge_to`, max backup, going off-grid and reconnecting are **local only** (signed connection). They have no cloud fallback.
