# Tesla tariff

In **Time-Based Control** (`autonomous`), the Powerwall decides when to charge, discharge and export from the **tariff** stored in Tesla's cloud: your buy and sell rates by time of day and season. Normally you set it in the Tesla app. This integration can read it, and write it from Home Assistant, for example to follow a dynamic price feed while Tesla's own optimisation keeps control. No battery dispatch is involved.

## Turn it on

1. Set up a [cloud backup](cloud-backup.md): **Configure → Link a Tesla account** (Owner API) or **Configure → Sign in to Tesla Fleet or Teslemetry**. The tariff lives only in Tesla's cloud, so the option isn't shown before this.
2. **Configure → Settings**, open the **Advanced** section (collapsed) and turn on **Tesla tariff (cloud)**.

You then get:

- the **Tesla tariff** sensor (`sensor.powerwall_tedapi_tesla_tariff`),
- the `tesla_tedapi.get_tariff` and `tesla_tedapi.set_tariff` actions,
- the tariff in diagnostics downloads.

The tariff is read every 6 hours, and after each write.

## The Tesla tariff sensor

| | |
|---|---|
| State | The tariff's name. |
| `utility` | The utility Tesla has for it. |
| `season`, `period` | The season and time-of-use period in effect now. |
| `buy_rate`, `sell_rate` | The rates in effect now. |
| `backend` | Which cloud backup answered. |
| `tariff` | Tesla's raw tariff (not stored in the recorder). |

Fields Tesla leaves out of a tariff count as 0, as Tesla means them: a period without a rate is free.

## Reading: `get_tariff`

```yaml
action: tesla_tedapi.get_tariff
response_variable: result
```

The response has `backend` and `tariff`, Tesla's raw tariff in its **read** format (`energy_charges`, `seasons`, `sell_tariff`, …).

## Writing: `set_tariff`

> [!IMPORTANT]
> Writes use a **different format** from what `get_tariff` returns. `tou_settings` must be an object with only `tariff_content_v2`. A payload in the read format is refused with a message saying so.

### The write format

```yaml
tariff_content_v2:
  name: My tariff
  utility: My utility
  seasons:
    Summer:                     # any season name
      fromMonth: 1
      toMonth: 12
      fromDay: 1
      toDay: 31
      tou_periods:
        OFF_PEAK:
          periods:
            - {fromDayOfWeek: 0, toDayOfWeek: 6, fromHour: 21, fromMinute: 0, toHour: 16, toMinute: 0}
        ON_PEAK:
          periods:
            - {fromDayOfWeek: 0, toDayOfWeek: 6, fromHour: 16, fromMinute: 0, toHour: 21, toMinute: 0}
  energy_charges:
    ALL:
      rates: {ALL: 0}
    Summer:
      rates:
        OFF_PEAK: 0.15          # buy price per kWh
        ON_PEAK: 0.45
  sell_tariff:
    energy_charges:
      ALL:
        rates: {ALL: 0}
      Summer:
        rates:
          OFF_PEAK: 0.05        # sell (feed-in) price per kWh
          ON_PEAK: 0.10
```

How the fields work:

| Field | |
|---|---|
| `seasons.<name>` | `fromMonth`/`toMonth` (1–12) and `fromDay`/`toDay`. A season can wrap over the new year. |
| `tou_periods.<period>.periods` | A list of time ranges. Days of the week count from **Monday = 0** to Sunday = 6. A range whose end is before its start runs over midnight; a range with the same start and end covers the whole day. |
| `energy_charges.<season>.rates.<period>` | The buy price. `ALL` is a fallback for any season or period. |
| `sell_tariff.energy_charges` | The sell prices, in the same layout. Without it, no feed-in rates are sent. |

### Always start with a dry run

`dry_run` is `true` unless you turn it off. A dry run checks the payload and returns:

| Response | |
|---|---|
| `would_send` | The payload. |
| `backend` | The backup it would go through. |
| `warnings` | What Tesla would change or leave unpriced: negative rates (Tesla stores 0), sell rates above the buy rate (Tesla raises the buy rate to match), times of the week in no period (or in two), days of the year in no season (or in two), seasons without months, prices without a period and periods without a buy price, and a missing `sell_tariff`. |
| `unchanged` | Whether these rates are the ones last sent or the ones Tesla already shows. |

```yaml
action: tesla_tedapi.set_tariff
data:
  dry_run: true
  tou_settings:
    tariff_content_v2: ...   # as above
response_variable: result
```

### Sending it

```yaml
action: tesla_tedapi.set_tariff
data:
  dry_run: false
  read_back: full            # full, once or none
  only_if_changed: false
  tou_settings:
    tariff_content_v2: ...
response_variable: result
```

| Option | |
|---|---|
| `read_back: full` (default) | Read the tariff back for up to 25 seconds. |
| `read_back: once` | One read after 15 seconds. Use this for scheduled writes. |
| `read_back: none` | No read; the next tariff read confirms it. |
| `only_if_changed: true` | Skip the write when these are the rates last sent, or the ones Tesla already shows. Good for automations that run every 30 minutes. |

The response's `status`:

| `status` | |
|---|---|
| `confirmed` | Tesla shows the new rates. |
| `pending` | Tesla doesn't show them yet. The Owner API can take up to 30 minutes; it's checked again after 35 minutes, and a repair appears only if it still doesn't show then. |
| `mismatch` | A full read-back through the Fleet API shows other rates. A repair says so. |
| `unchanged` | `only_if_changed` and nothing changed: nothing was sent. |

## Following dynamic prices

The **[Dynamic price tariff](automations.md#blueprints)** blueprint does all of this for you: every 30 minutes it turns a price forecast (Amber by default; Octopus Energy, Nord Pool and others by setting the attribute and key names) into 48 half-hour periods for the next 24 hours, and sends them with `read_back: once` and `only_if_changed: true`. Start it with its dry run on.

## Good to know

- **Put the Powerwall in Time-Based Control yourself.** The tariff has no effect in *Self-Powered* or *Backup only*.
- **Tesla stores negative rates as 0.** A negative price in your feed is sent, but Tesla keeps 0; the read-back allows for this.
- **Tesla raises a buy rate below the sell rate.** Where the sell price is higher, Tesla stores the sell price as the buy price too; the read-back allows for this.
- **Tesla decides what to do with the tariff.** Writing a tariff doesn't force a charge or discharge; it changes what Tesla's optimisation aims for.
- **Your retailer or VPP may also write the tariff.** If yours keeps changing, check whether another service manages it.
- **Writes are commands.** Through Tesla Fleet they count against your developer account; through Teslemetry, against your subscription.
