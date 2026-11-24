# Predbat

[Predbat](https://github.com/springfall2008/batpred) plans when to charge, hold and export the battery from your prices and solar forecast. With this integration it controls the Powerwall locally: it sends a [battery dispatch](actions.md#battery-dispatch-start-here) every 5 minutes and never changes the operation mode, export rule, grid charging or backup reserve. If Predbat or Home Assistant stops, the Powerwall returns to its normal operation within 10 minutes.

> ⚠️ Every charge, hold and export Predbat makes is a battery dispatch, which may affect your warranty. Read [Safety](safety.md) first.

## Setup

1. **Signed connection.** Battery dispatch needs the signed (v1r) connection; see [Setup](setup.md).
2. **Enable the four *today* energy sensors** (disabled by default) on each entity's settings page: **Home energy consumed today**, **Grid energy imported today**, **Grid energy exported today** and **Solar energy produced today**.
3. **Create two Number helpers** (**Settings → Devices & services → Helpers → Create helper → Number**), minimum 0, maximum 10000, step 100, unit W: `input_number.predbat_powerwall_charge_rate` and `input_number.predbat_powerwall_discharge_rate`. Predbat writes the power it chose into them.
4. **Turn on Grid charging.** A dispatch charges from the grid only while it's on; otherwise from solar only.
5. **Check the integration options** (**Configure → Settings → Battery dispatch**): **Largest dispatch power** at least Predbat's `battery_rate_max`, and with **Discharges stop at the backup reserve** on, a backup reserve at or below the lowest level you want Predbat to use.
6. **Install Predbat** ([installation](https://springfall2008.github.io/batpred/install/)) and use [`examples/predbat_apps.yaml`](../examples/predbat_apps.yaml) as its `apps.yaml`. Adjust the entity IDs if yours differ (see [conventions](README.md#conventions-in-these-pages)), the time zone, currency and `battery_rate_max` (one Powerwall 3: 10000 W; Powerwall 2: 5000 W each), add your [energy rates](https://springfall2008.github.io/batpred/energy-rates/), then delete `template: True`.

Use the Powerwall in **Self-Powered** mode, so its own Time-Based Control doesn't act between Predbat's dispatches. Turn off other automations that dispatch the battery or change its mode while Predbat is in control.

## Notes

- **State of charge** comes from **Battery energy remaining** and **Battery full capacity**, which already add up every pack. They include the bottom 5 % the Tesla app hides, hence `battery_min_soc: 5`.
- **Freeze modes use a 0 W dispatch,** which holds the battery in both directions. For Predbat's *freeze charging* that's close: the battery doesn't discharge, but surplus solar is exported instead of charging it. For *freeze exporting* it isn't: Predbat expects the battery to keep powering the home (without charging), but a 0 W hold leaves the home on the grid. If you'd rather Predbat didn't plan export freezes, set `support_discharge_freeze: False`.
- **While the gateway can't be reached** the readings are *unavailable* and dispatches fail; Predbat retries every cycle. The running dispatch ends on its own within 10 minutes.
- **Dispatch active** and [`tesla_tedapi_dispatch_ended`](events.md#tesla_tedapi_dispatch_ended) show what Predbat is doing. A dispatch Predbat re-sends at the same power is logged once, not every 5 minutes.
- **More than one Powerwall set up:** add `config_entry_id` to each hook in `apps.yaml`.
