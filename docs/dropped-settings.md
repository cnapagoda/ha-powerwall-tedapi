# Dropped settings

The Powerwall sometimes drops two settings from its own configuration:

- the **grid export rule**, and
- **grid charging**.

The Tesla app and Tesla's cloud still show the value you chose, but the gateway falls back to Tesla's default until the setting is written again: export *Battery & Solar* (`battery_ok`), and grid charging **on**. On one Powerwall 3 (firmware 26.x) this happened about once an hour, and grid charging turned itself on right after it was switched off.

## What the integration does

It reads the gateway's own settings on every poll. When a value you set (or the one the cloud has) is missing:

- **Grid export mode** or **Grid charging** keeps showing **your** value, with `missing_on_powerwall: true`.
- Over a signed connection, the value is **written back**, at most every 2 minutes per setting. Each restore is logged at info level; only a failed restore is a warning.
- `restored_count` and `last_restored` record it.

A value that is the same as Tesla's default is left alone, and nothing is written over a Wi-Fi password connection.

## Attributes

On **Grid export mode** and **Grid charging**:

| Attribute | |
|---|---|
| `missing_on_powerwall` | `true` while the gateway's own config is missing the setting (and it couldn't be written back yet). |
| `restored_count` | How many times it was written back since Home Assistant started. |
| `last_restored` | When it was last written back. |

## Turning it off

**Configure → Settings → Restore settings when the Powerwall drops them.** Turn it off if another app or your energy retailer is meant to manage these settings, so the integration doesn't fight it.

## Is this a problem with my Powerwall?

It seems to be firmware behaviour, not a fault. If you see `restored_count` climbing on a Powerwall 2 or Powerwall+, or on firmware other than 26.x, a [hardware report](https://github.com/cnapagoda/ha-powerwall-tedapi/issues/new?template=hardware_report.yml) helps.
