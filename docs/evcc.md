# evcc

[evcc](https://evcc.io) charges your car from surplus solar and cheap prices, and can keep the home battery from emptying into the car or charge it when the grid is cheap. Its Home Assistant meter controls the battery through four scripts, which drive the Powerwall with [battery dispatches](actions.md#battery-dispatch-start-here).

> ⚠️ Battery dispatch may affect your warranty. Read [Safety](safety.md) first.

## Setup

1. **Signed connection.** Battery dispatch needs the signed (v1r) connection; see [Setup](setup.md).
2. **Add the scripts** from [`examples/evcc_scripts.yaml`](../examples/evcc_scripts.yaml) to `scripts.yaml` and reload scripts. Set `power_watts` in the charge and discharge scripts to what your Powerwall can do (one Powerwall 3: 10,000 W), and no more than **Largest dispatch power** in the integration options.
3. **Turn on Grid charging**, so evcc's battery charge can draw from the grid.
4. **Connect evcc to Home Assistant** as described in [evcc's Home Assistant docs](https://docs.evcc.io/en/meters/home-assistant), and add the meters to `evcc.yaml`:

```yaml
meters:
  - name: grid
    type: template
    template: homeassistant
    usage: grid
    uri: http://homeassistant.local:8123
    power: sensor.powerwall_tedapi_grid_power          # + importing
  - name: pv
    type: template
    template: homeassistant
    usage: pv
    uri: http://homeassistant.local:8123
    power: sensor.powerwall_tedapi_solar_power
  - name: powerwall
    type: template
    template: homeassistant
    usage: battery
    uri: http://homeassistant.local:8123
    power: sensor.powerwall_tedapi_battery_power       # + discharging
    soc: sensor.powerwall_tedapi_battery_level
    capacity: 13.5                                     # kWh per Powerwall or expansion pack
    modeNormal: script.powerwall_evcc_normal
    modeHold: script.powerwall_evcc_hold
    modeCharge: script.powerwall_evcc_charge
    modeDischarge: script.powerwall_evcc_discharge

site:
  meters:
    grid: grid
    pv: [pv]
    battery: [powerwall]
```

Adjust the entity IDs if yours differ (see [conventions](README.md#conventions-in-these-pages)).

## Notes

- **Hold** keeps the battery idle at 0 W: it neither discharges nor charges, so surplus solar goes to the car, the house or the grid while it lasts.
- **evcc's *hold charge* mode isn't offered:** the Powerwall can't stop charging while still discharging to the house.
- evcc calls a script only when its mode changes, so each dispatch runs for up to 24 hours until the next mode ends it. A charge also ends at 100 % and a discharge at the backup reserve. If evcc stops while a mode is on, run **Powerwall - evcc normal** or press **Stop battery dispatch**.
