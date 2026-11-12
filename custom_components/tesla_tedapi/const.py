"""Constants for the Tesla Powerwall TEDAPI integration."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DOMAIN = "tesla_tedapi"

# Config entry data
CONF_CONNECTION_MODE = "connection_mode"
CONF_GW_PWD = "gw_pwd"
CONF_HOST = "host"
CONF_RSA_KEY_PATH = "rsa_key_path"
CONF_WIFI_HOST = "wifi_host"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_OWNERS_EMAIL = "owners_email"
CONF_OWNERS_AUTHPATH = "owners_authpath"
CONF_SITE_ID = "site_id"

# Options
CONF_RESTORE_EXPORT = "restore_export"
CONF_STATUS_EVENTS = "status_alert_events"
CONF_PROBLEM_MINUTES = "problem_minutes"
CONF_TESLA_TARIFF = "tesla_tariff"
# Diagnostics include the gateway's own configuration (redacted). Off by default.
CONF_DIAGNOSTICS_GATEWAY = "diagnostics_gateway_config"
# Fast polling for power, battery level and grid status (seconds; 0 = off:
# everything is read at the polling interval). Changing it reloads the entry.
CONF_POWER_INTERVAL = "power_interval"
MIN_POWER_INTERVAL = 5
MAX_POWER_INTERVAL = 60
# Power and battery level from the cloud backup while the gateway can't be
# reached (off by default: readings then come from Tesla, not the gateway).
CONF_CLOUD_READINGS = "cloud_readings"
CONF_DISPATCH_FLOOR_RESERVE = "dispatch_floor_reserve"
CONF_DISPATCH_MAX_WATTS = "dispatch_max_watts"
# Which cloud backup to use and in what order (one of CLOUD_CHOICES).
CONF_CLOUD_BACKENDS = "cloud_backends"
# Options that take effect without reloading the integration (a reload would
# end a running battery dispatch). Any other change reloads it.
LIVE_OPTIONS = frozenset({
    CONF_SCAN_INTERVAL, CONF_RESTORE_EXPORT, CONF_STATUS_EVENTS, CONF_PROBLEM_MINUTES,
    CONF_DISPATCH_FLOOR_RESERVE, CONF_DISPATCH_MAX_WATTS, CONF_DIAGNOSTICS_GATEWAY, CONF_CLOUD_READINGS,
})

# The gateway's network MAC addresses (lower case, no separators), learnt
# from DHCP discovery: only a discovery with one of them is followed to a
# new address. Saved without a reload.
CONF_MACS = "macs"
# Backup Gateway 1: the customer login takes the full gateway password, not its
# last 5 characters (found from the gateway's DIN at setup).
CONF_FULL_PASSWORD_LOGIN = "full_password_login"
# The oldest pypowerwall this version works with: the same as the lower
# bound in manifest.json (a test keeps them in step).
MIN_PYPOWERWALL = (0, 25, 0)

# Cloud backups: a Tesla account signed in here (Owner API), or a Fleet API
# sign-in made here (Tesla Fleet with the user's own developer app, or
# Teslemetry).
BACKEND_OWNER = "owner_api"
BACKEND_FLEET = "tesla_fleet"
# The Fleet API sign-in, in the entry data. A Tesla Fleet sign-in also keeps
# "auth_implementation" and "token" (Home Assistant's OAuth2 keys).
CONF_FLEET_PROVIDER = "fleet_provider"
CONF_FLEET_SITE_ID = "fleet_site_id"
CONF_FLEET_SERVER = "fleet_server"
CONF_FLEET_TOKEN = "fleet_token"  # Teslemetry access token
CONF_OAUTH_IMPLEMENTATION = "auth_implementation"
CONF_OAUTH_TOKEN = "token"
PROVIDER_TESLA_FLEET = "tesla_fleet"
PROVIDER_TESLEMETRY = "teslemetry"
FLEET_DATA_KEYS = (CONF_FLEET_PROVIDER, CONF_FLEET_SITE_ID, CONF_FLEET_SERVER, CONF_FLEET_TOKEN,
                   CONF_OAUTH_IMPLEMENTATION, CONF_OAUTH_TOKEN)
CLOUD_NONE = "none"
CLOUD_FLEET_FIRST = "fleet_then_owner"
CLOUD_OWNER_FIRST = "owner_then_fleet"
CLOUD_CHOICES: dict[str, tuple[str, ...]] = {
    CLOUD_NONE: (),
    BACKEND_OWNER: (BACKEND_OWNER,),
    BACKEND_FLEET: (BACKEND_FLEET,),
    CLOUD_FLEET_FIRST: (BACKEND_FLEET, BACKEND_OWNER),
    CLOUD_OWNER_FIRST: (BACKEND_OWNER, BACKEND_FLEET),
}
# After linking or unlinking a Tesla account here: the choice that keeps the
# other backup and adds or drops the Owner API.
CLOUD_WITH_OWNER = {CLOUD_NONE: BACKEND_OWNER, BACKEND_FLEET: CLOUD_FLEET_FIRST}
CLOUD_WITHOUT_OWNER = {BACKEND_OWNER: CLOUD_NONE, CLOUD_FLEET_FIRST: BACKEND_FLEET,
                       CLOUD_OWNER_FIRST: BACKEND_FLEET}
CLOUD_WITH_FLEET = {CLOUD_NONE: BACKEND_FLEET, BACKEND_OWNER: CLOUD_OWNER_FIRST}
CLOUD_WITHOUT_FLEET = {BACKEND_FLEET: CLOUD_NONE, CLOUD_FLEET_FIRST: BACKEND_OWNER,
                       CLOUD_OWNER_FIRST: BACKEND_OWNER}


def cloud_choice(data: Mapping[str, Any], options: Mapping[str, Any]) -> str:
    """The cloud backup choice. Without one, a Tesla account linked here is
    the backup (as before 2.3)."""
    return options.get(CONF_CLOUD_BACKENDS) or (BACKEND_OWNER if data.get(CONF_OWNERS_EMAIL) else CLOUD_NONE)


# Connection modes
MODE_WIFI = "wifi"
MODE_V1R = "v1r"

# Defaults
DEFAULT_HOST_WIFI = "192.168.91.1"
DEFAULT_SCAN_INTERVAL = 30
DEFAULT_PROBLEM_MINUTES = 10
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 300
# Folder under the HA config dir that holds the RSA key and the Tesla login
# cache written by the setup wizard.
STORAGE_FOLDER = "tesla_tedapi"
RSA_KEY_FILE = "tedapi_rsa_private.pem"
RSA_PUBLIC_KEY_FILE = "tedapi_rsa_public.der"
AUTH_FILE = ".pypowerwall.auth"

# Operation modes: API value -> option key (translated in strings.json)
OPERATION_MODES = ("self_consumption", "backup", "autonomous")

# Grid export modes: API value -> option key
GRID_EXPORT_MODES = ("battery_ok", "pv_only", "never")
# The gateway sometimes drops settings from its own config (seen hourly on
# Powerwall 3 firmware 26.x while the Tesla cloud keeps them). Setting key ->
# (site_info field, the value pypowerwall reports while the field is missing).
DROPPABLE_SETTINGS = {
    "grid_export": ("customer_preferred_export_rule", "battery_ok"),
    "grid_charging": ("disallow_charge_from_grid_with_solar_installed", True),
}
# Experimental: Tesla's on-grid solar curtailment flag in the gateway's site_info.
SOLAR_CURTAILMENT_KEY = "on_grid_solar_curtailment_enabled"
# A restore of each setting is written at most this often.
RESTORE_MIN_SECONDS = 120

# Cloud reads of settings are cached this long (Owner API rate limits).
CLOUD_CACHE_SECONDS = 300
# The cloud backup is otherwise idle while the gateway answers. It is read this
# often so a sign-in that stopped working shows up (and the token is refreshed)
# before the backup is needed; a failed check is retried sooner.
CLOUD_CHECK_SECONDS = 3600
CLOUD_RETRY_SECONDS = 600
# A change made through a cloud backup is read back: first after this long
# (Tesla takes about a minute to pass it to the gateway), and for at most
# CLOUD_VERIFY_TIMEOUT_SECONDS before a different value is reported.
CLOUD_VERIFY_SECONDS = 60
CLOUD_VERIFY_TIMEOUT_SECONDS = 300
# Tesla's cloud caps the backup reserve it sets (80 % since 2026-10).
CLOUD_RESERVE_CAP = 80
# The Owner API access token (8 hours) is renewed when it has less than this
# left at a cloud read, before Tesla answers HTTP 401 for it. More than
# CLOUD_CHECK_SECONDS, so the hourly check renews it in time; a failed renewal
# is tried again after OWNER_RENEW_RETRY_SECONDS (pypowerwall's own refresh on
# 401 stays as the fallback).
OWNER_RENEW_SECONDS = 4500
OWNER_RENEW_RETRY_SECONDS = 300
# Storm Watch and other cloud-only values from the Owner API are read this
# often (two Tesla calls); Tesla Fleet's are read from its data every poll.
OWNER_EXTRAS_SECONDS = 1800
# The site's live status from the cloud (Wall Connectors; power and battery
# level while the gateway can't be reached) is read at most this often. Only
# read when one of the two needs it. The Owner API is rate limited.
CLOUD_LIVE_SECONDS_FLEET = 60
CLOUD_LIVE_SECONDS_OWNER = 300
# Tesla tariff (cloud, optional): read every 6 hours.
TARIFF_SECONDS = 6 * 3600
# After a write, the tariff is read back after these waits (at 0, 1, 3, 7, 15
# and 25 s): Tesla takes a while to show a new tariff.
TARIFF_READBACK_WAITS = (0, 1, 2, 4, 8, 10)
# The Owner API may show a new tariff only after up to 30 minutes: a write it
# hasn't shown yet is pending, and checked again this long after.
TARIFF_PENDING_SECONDS = 35 * 60
# read_back "once" (for scheduled writes): one read after this wait.
TARIFF_READBACK_ONCE_SECONDS = 15
TARIFF_READ_BACK_MODES = ("full", "once", "none")
# Unconfirmed writes kept for the pending check (the oldest are dropped).
TARIFF_PENDING_MAX = 8
# Backup events (Storm Watch, manual backup) are read from the gateway this
# often, and right after one is started or cancelled here.
BACKUP_EVENTS_SECONDS = 300
MAX_BACKUP_MAX_MINUTES = 2880
# Consecutive failures before asking the user to sign in or re-register.
CLOUD_AUTH_FAILURES = 3
KEY_REFUSED_POLLS = 10
PASSWORD_REFUSED_POLLS = 3
# A lifetime energy counter that reads lower than before is a glitch, unless
# it stays lower for this many readings in a row: then the counter was reset
# (a gateway replaced or reset) and the new value is taken.
ENERGY_RESET_POLLS = 10

# Battery dispatch
# Battery dispatch power ceiling (W) for actions and the Largest dispatch power
# option. Within it the gateway decides: one Powerwall 3 refuses more than
# 10,000 W (status code 11); systems with several can do more.
DISPATCH_MAX_WATTS = 30000
# Largest dispatch power until the user sets it: what one Powerwall 3 accepts.
DISPATCH_DEFAULT_WATTS = 10000
DISPATCH_MAX_SECONDS = 86400
# Each command to the Powerwall covers at most this long and is renewed while a
# session runs, so the battery returns to normal within this time if Home
# Assistant stops.
DISPATCH_CHUNK_SECONDS = 300
DISPATCH_RENEW_SECONDS = 60
DISPATCH_MAX_FAILURES = 3

EVENT_DISPATCH_ENDED = f"{DOMAIN}_dispatch_ended"

# Go off-grid: the longest run, how long to wait for the grid contactor to
# move after a command (it takes about 30 s), and how often to look meanwhile.
OFF_GRID_MAX_MINUTES = 1440
CONTACTOR_WAIT_SECONDS = 90
CONTACTOR_POLL_SECONDS = 5
# An automatic reconnect that fails is tried again this often.
RECONNECT_RETRY_SECONDS = 60
EVENT_OFF_GRID_ENDED = f"{DOMAIN}_off_grid_ended"
# A grid outage started or ended (not an off-grid run started here).
EVENT_GRID_OUTAGE = f"{DOMAIN}_grid_outage"
# The gateway reported a new firmware version.
EVENT_FIRMWARE_UPDATED = f"{DOMAIN}_firmware_updated"

SERVICE_DISPATCH = "dispatch_battery_power"
SERVICE_RESUME = "resume_battery_operation"
SERVICE_GET_TARIFF = "get_tariff"
SERVICE_SET_TARIFF = "set_tariff"
SERVICE_IMPORT_BATTERY_HISTORY = "import_battery_history"
ATTR_SOURCE_CHARGED = "source_charged"
ATTR_SOURCE_DISCHARGED = "source_discharged"
ATTR_TOU_SETTINGS = "tou_settings"
ATTR_DRY_RUN = "dry_run"
ATTR_READ_BACK = "read_back"
ATTR_ONLY_IF_CHANGED = "only_if_changed"
SERVICE_CHARGE_TO = "charge_to"
SERVICE_DISCHARGE_TO = "discharge_to"
SERVICE_START_MAX_BACKUP = "start_max_backup"
SERVICE_CANCEL_MAX_BACKUP = "cancel_max_backup"
SERVICE_GO_OFF_GRID = "go_off_grid"
SERVICE_RECONNECT_GRID = "reconnect_grid"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_POWER_WATTS = "power_watts"
ATTR_DURATION_SECONDS = "duration_seconds"
ATTR_MIN_SOC = "min_soc"
ATTR_MAX_SOC = "max_soc"
ATTR_DURATION_MINUTES = "duration_minutes"
ATTR_CONFIRM = "confirm"
ATTR_TARGET_SOC = "target_soc"
ATTR_MAX_MINUTES = "max_minutes"


def app_soc(raw: float | None) -> float | None:
    """Convert the gateway's state of energy to the Tesla app scale.

    The app hides the bottom 5 %: app = (raw - 5) / 0.95.
    """
    if raw is None:
        return None
    return round(max(0.0, min(100.0, (raw - 5) / 0.95)), 1)
