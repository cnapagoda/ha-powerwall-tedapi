# Documentation

Tesla Powerwall TEDAPI for Home Assistant: local monitoring and control of a Tesla Powerwall.

## Getting started

- **[Quick start](quick-start.md)**: from nothing to a working Powerwall in Home Assistant, the recommended way.
- **[Setup in detail](setup.md)**: every way to connect, the Tesla sign-in, registering through the Fleet API, and Wi-Fi-only setups.
- **[Configuration](configuration.md)**: options, reconfiguring, re-registering the key, and removing the integration.
- **[Migrating from another integration](migration.md)**: coming from the core Tesla Powerwall, Tesla Fleet, Teslemetry or a pypowerwall proxy.

## Using it

- **[Energy dashboard](energy-dashboard.md)**: which sensor goes where.
- **[Dashboards](dashboards.md)**: ready-to-paste cards and a complete example dashboard.
- **[Automations and blueprints](automations.md)**: one-click blueprints and example automations.
- **[Alerts](alerts.md)**: what the Powerwall's alert codes mean, and how to be notified.
- **[Tesla tariff](tesla-tariff.md)**: read and write the tariff Tesla uses for Time-Based Control.

## Reference

- **[Entities](entities.md)**: every sensor, switch, select, number and event, with its attributes.
- **[Actions](actions.md)**: dispatch, charge/discharge to a level, max backup, go off-grid, tariff and history import.
- **[Events](events.md)**: the events the integration fires, with their data.
- **[Cloud backup](cloud-backup.md)**: the Owner API and Fleet API (Tesla Fleet or Teslemetry) backups.
- **[Dropped settings](dropped-settings.md)**: why the Powerwall loses its export and grid charging settings, and how they're restored.
- **[How it works and limitations](how-it-works.md)**: polling, what is read how often, privacy, known limitations and the quality scale.

## Help

- **[Safety](safety.md)**: read this before using battery dispatch or going off-grid.
- **[Troubleshooting](troubleshooting.md)**: symptoms and fixes, and how to collect debug logs.
- **[FAQ](faq.md)**: short answers to common questions.
- **[Compatibility](compatibility.md)**: which Powerwall models and features are tested.

## Conventions in these pages

**Entity IDs** are shown as `sensor.powerwall_tedapi_battery_level`: the device name (*Powerwall TEDAPI*) plus the entity name. Yours may differ:

- From Home Assistant 2026.8, entity IDs follow **Settings → System → Entity ID format**, which is *area + device + entity* by default. With the device in an area, the area comes first, for example `sensor.garage_powerwall_tedapi_battery_level`.
- The format applies when an entity is created: existing entity IDs keep their names. To apply a new format (or a new area), use **Recreate entity IDs** on the device page, then update automations and dashboards that use the old IDs.
- With a second Powerwall, its entities end in `_2`.
- Entities you renamed keep the name you gave them.

To find an entity's ID, open it from the device page (**Settings → Devices & services → Tesla Powerwall TEDAPI → device**) and look under **Settings → Entity ID**.

**Battery level** always means the Tesla app's scale (see [Entities](entities.md#battery)).
