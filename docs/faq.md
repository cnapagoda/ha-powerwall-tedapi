# FAQ

## Getting started

**Do I need a Tesla account?**
For the signed connection, you need **one** Tesla sign-in, once, to register the access key with your Powerwall: your Tesla account, a Teslemetry token or your own Tesla Fleet developer app ([which one](setup.md#which-connection-and-which-sign-in)). The Wi-Fi password connection needs none. After the key is registered, local control never uses the sign-in. Keeping it linked gives you a [cloud backup](cloud-backup.md) for when Home Assistant can't reach the gateway, and access to Storm Watch and the tariff.

**Why can't the key be registered locally, with just the gateway password?**
The Powerwall accepts a key sent over its own Wi-Fi, but in testing such a key never verified. Keys only verify when the registration comes through Tesla's cloud.

**Do I need to reserve the gateway's IP address?**
Usually not. When your router gives the gateway a new address, Home Assistant notices it (DHCP discovery) and updates the address if it recognises the gateway by its network (MAC) address. It learns that the first time it sees the gateway at the address you set up. Your gateway password is never sent to a newly discovered device. If the gateway shows up under **Discovered** instead, select **Add** and enter the password once: it's then recognised from then on.

**Does my Home Assistant need a second Wi-Fi adapter?**
Not with the recommended signed connection, which works over your normal home network. Only the Wi-Fi password connection needs Home Assistant on the Powerwall's own Wi-Fi.

**Does this expose Home Assistant or my Powerwall to the internet?**
No. Everything runs on your network. The cloud backup, if you set one up, makes outgoing calls to Tesla only.

**Where's the gateway password?**
On the QR sticker inside the Powerwall's cover. On a Powerwall 3 it's behind the front glass, lower left.

## Readings

**Why is Battery level different from Battery state of energy?**
**Battery level** uses the Tesla app's scale, which hides the bottom 5 %. **Battery state of energy** is the gateway's raw value, 2–4 points higher. Use **Battery level** everywhere.

**Why is Battery health above 100 %?**
It compares the measured capacity with Tesla's rated 13.5 kWh per pack. New batteries have more capacity than rated.

**Can I use the sensors in the Energy dashboard?**
Yes: the lifetime energy sensors are made for it. See [Energy dashboard](energy-dashboard.md).

**How often does it update?**
Every 30 seconds by default (10–300 s in the options). See [Data updates](how-it-works.md#data-updates).

**What happens during a power cut?**
**Grid connected** turns off within one poll, the outage is counted, and `tesla_tedapi_grid_outage` fires, as long as Home Assistant and your network have power (a UPS helps).

## Control

**Will it conflict with the Tesla app?**
No: they change the same settings, and the last change wins. The integration shows changes made in the app within one poll.

**Will it conflict with my retailer or virtual power plant (VPP)?**
It can. They control the Powerwall from Tesla's cloud and may change modes, the reserve or grid charging. If your settings keep changing back, that's the likely cause. Keep automations that fight them to a minimum.

**Why does my grid charging setting turn back on by itself?**
The Powerwall sometimes drops the setting from its own configuration. The integration writes it back. See [Dropped settings](dropped-settings.md).

**Why does the backup reserve stop at 80 %?**
When the change goes through Tesla's cloud, Tesla caps it at 80 %. Over the signed local connection any value up to 100 % works. To charge to full, use [max backup](actions.md#tesla_tedapistart_max_backup).

**What's the difference between dispatch and changing the operation mode?**
Changing the mode or reserve is what the Tesla app does: the Powerwall still decides how to run. A dispatch tells it exactly what power to charge or discharge at, overriding the export setting. Dispatching may affect your warranty; see [Safety](safety.md).

**What happens to a dispatch if Home Assistant restarts or crashes?**
Commands are sent in steps of at most 5 minutes, so the Powerwall returns to normal within 5 minutes. A dispatch isn't resumed after a restart.

**What happens to an off-grid run if Home Assistant restarts?**
The run is saved and picked up again, so it still reconnects on time. If Home Assistant stays down, nothing reconnects: reconnect in the Tesla app.

**Can I use it to make money exporting?**
The [blueprints](automations.md#blueprints) can discharge during price spikes. Whether that's worth it depends on your tariff and your warranty. Tesla's own Time-Based Control with a [tariff](tesla-tariff.md) is a dispatch-free alternative.

## Other integrations

**Can I keep Tesla Fleet or Teslemetry?**
Yes. This integration signs in to Tesla Fleet or Teslemetry on its own for the cloud backup and key registration, so the two don't affect each other. See [Migrating](migration.md).

**Can I share a Tesla refresh token with another integration?**
No. Each refresh replaces the token, so whichever refreshes second is signed out. Make a new token for each.

**Does this replace the core Tesla Powerwall integration?**
For local monitoring and control, yes, including on a Powerwall 3. See [Migrating](migration.md) to keep your history.

## Security

**Is it safe to keep the key on my Home Assistant?**
It's stored with owner-only permissions in `config/tesla_tedapi/`. Anyone with the key and access to your network can control the Powerwall, so keep your backups private. See [SECURITY.md](../SECURITY.md).

**How do I remove the key?**
In the Tesla app. The integration can't remove it. See [Removing the integration](configuration.md#removing-the-integration).

**Is it affiliated with Tesla?**
No. It uses interfaces Tesla doesn't document and can change at any time.
