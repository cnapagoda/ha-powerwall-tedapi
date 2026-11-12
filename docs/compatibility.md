# Compatibility

This integration uses interfaces Tesla doesn't document, so what works depends on the model and firmware. This page lists what is known. **It grows with your reports:** if you run the integration on hardware or firmware not listed here, please send a [hardware report](https://github.com/cnapagoda/ha-powerwall-tedapi/issues/new?template=hardware_report.yml).

## Models

| Model | Status | Notes |
|---|---|---|
| **Powerwall 3** | ✅ Tested by the maintainer | Signed connection over the home network; firmware 26.x. Built-in solar inputs, fans, per-pack wear counters. |
| **Powerwall 3 with Expansion Packs** | ✅ Tested by the maintainer | Each Expansion Pack gets its own capacity, temperature and health sensors, and **Battery health** and **Battery full capacity** count every pack. Packs added later appear by themselves. |
| **Powerwall 2 / Powerwall+** | 🟡 Less tested | Signed access works if the firmware supports it; reported working on firmware 26.34.2 with a Backup Gateway 1. The Wi-Fi password connection gives readings. Some readings differ (see below). |
| Backup Gateway 1 (part number `1118431-…`) | 🟡 Less tested | Recognised at setup: it logs in with the full gateway password instead of its last 5 characters. Reported working with a Powerwall 2 on firmware 26.34.2: signed connection, readings, backup reserve, battery dispatch and `charge_to` (thanks [@BJReplay](https://github.com/BJReplay)). Grid charging and export not confirmed yet. |
| Several Powerwalls on one gateway | 🟡 Expected to work | One entry per gateway; packs appear by themselves. |
| Several gateways | 🟡 Expected to work | Add the integration once per gateway. |
| Powerwall 1 | ❓ Unknown | |

✅ tested · 🟡 expected or reported, not verified by the maintainer · ❓ unknown · ❌ doesn't work

## Features by model

| Feature | Powerwall 3 (with or without Expansion Packs) | Powerwall 2 / + |
|---|---|---|
| Power, battery level, energy counters, grid status | ✅ | ✅ |
| Settings over a signed connection | ✅ | 🟡 firmware dependent; backup reserve reported working on 26.34.2 |
| Battery dispatch, `charge_to` / `discharge_to` | ✅ | 🟡 firmware dependent; dispatch and `charge_to` reported working on 26.34.2 |
| Max backup | ✅ | 🟡 |
| Go off-grid / reconnect | ✅ | ❓ |
| Solar input *A–F* sensors | ✅ built-in inverter | ❓ a Powerwall+ solar inverter may report them; reports welcome |
| Fan speeds | ✅ inverter fans A/B, with duty | 🟡 one fan per Powerwall, with target speed |
| Per-pack lifetime discharge and cycles | ✅ | ❓ only if the gateway reports the counters |
| Inverter temperatures | ✅ (heatsink doesn't change on some firmware) | ❌ not reported (no sensors); each battery's temperature comes from its thermal controller |
| Energy flows, round-trip efficiency, capacity trend | ✅ | ✅ |
| Per-phase voltage and meter sensors | ✅ where the gateway reports them | ✅ where the gateway reports them |
| Wall Connector and tariff sensors (cloud backup) | ✅ | ✅ |
| Dropped-settings restore | ✅ seen and restored on firmware 26.x | ❓ |
| On-grid solar curtailment (experimental) | ❓ not tested on a real Powerwall | ❓ |

## Connection types

| Connection | Works on |
|---|---|
| Signed (v1r) over the home network | Powerwall 3; Powerwall 2 / + where the firmware supports it. |
| Signed over the Powerwall's Wi-Fi (fallback) | Same, when Home Assistant can reach `192.168.91.1`. |
| Wi-Fi password only | Powerwall 3 and Powerwall 2 / +, over the Powerwall's own Wi-Fi. Readings only, unless you add a cloud backup. |

## Regions

Nothing in the local connection depends on the region. The Tesla cloud parts (key registration, cloud backup, tariff) do:

| Region | Notes |
|---|---|
| North America, Europe, Asia-Pacific | No region-specific limits known. Please report what works for you, so this table can say more. |
| China | Tesla's Fleet API isn't available there, so Tesla Fleet / Teslemetry can't be used. The Owner API path is untested. |

## Home Assistant

| Version | |
|---|---|
| 2025.11 or newer | Required. |
| 2026.3 or newer | Shows the integration's icon. |
| 2026.8 or newer | Entity IDs follow **Settings → System → Entity ID format** (area + device + entity by default). |

Only Home Assistant releases from the last 12 months are supported. The minimum version moves up over time, and older releases may stop working with a new version of this integration.

## Firmware

Tesla updates firmware on its own schedule. When the gateway reports a new version, the integration fires [`tesla_tedapi_firmware_updated`](events.md#tesla_tedapi_firmware_updated) and, on a signed connection, checks that signed access still works. If it stops working, a repair says so.

| Firmware | Model | Status | Reported by |
|---|---|---|---|
| 26.34.3 | Powerwall 3 | ✅ Works (signed connection, settings, battery dispatch) | Maintainer |
| 26.34.2 | Powerwall 3 | ✅ Works (signed connection) | Maintainer |
| 26.34.1 | Powerwall 3 | ✅ Works (key registration verified through the Tesla cloud) | Maintainer |
| 26.x | Powerwall 3 | ✅ Works; drops export and grid charging settings about hourly ([restored](dropped-settings.md)) | Maintainer |

Add your firmware with a [hardware report](https://github.com/cnapagoda/ha-powerwall-tedapi/issues/new?template=hardware_report.yml).
