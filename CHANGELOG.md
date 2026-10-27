# Changelog

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
