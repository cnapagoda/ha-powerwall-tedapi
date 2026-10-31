# Security policy

## Supported versions

Security fixes go into the latest release. Please update before reporting.

## Reporting a vulnerability

Report it privately through [GitHub security advisories](https://github.com/cnapagoda/ha-powerwall-tedapi/security/advisories/new). Please don't open a public issue.

Include what is affected, how to reproduce it, and the integration and Home Assistant versions. You'll get a reply as soon as possible.

## Keep your secrets out of reports

Never post these in an issue, pull request, log or screenshot:

- the private key (`tedapi_rsa_private.pem`),
- the Tesla refresh or access token (`.pypowerwall.auth`),
- the gateway password (from the QR sticker).

The integration's diagnostics hide passwords, email addresses and serial numbers, but check them before you share them. If a secret has been exposed, register a new key (**Configure → Register a new signed key**) and sign in to Tesla again.

## How the integration stores secrets

- **Signed key:** the RSA private key is stored as a file in your Home Assistant configuration folder (`tesla_tedapi/` by default, readable by the owner only). Anyone with this file and access to your network can control the Powerwall.
- **Tesla sign-in:** the Owner API token cache is stored in the same folder. Tesla Fleet and Teslemetry keep their own sign-in in their integrations.
- **Gateway password:** stored in the config entry.

All of these are included in Home Assistant backups. Keep your backups private.
