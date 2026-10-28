"""Constants for the Powerwall TEDAPI integration."""

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
CONF_DISPATCH_FLOOR_RESERVE = "dispatch_floor_reserve"
CONF_DISPATCH_MAX_WATTS = "dispatch_max_watts"
# Use the core Tesla Fleet integration's sign-in as the cloud backup.
CONF_FLEET_BACKUP = "fleet_backup"

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
# A restore of each setting is written at most this often.
RESTORE_MIN_SECONDS = 120

# Cloud reads of settings are cached this long (Owner API rate limits).
CLOUD_CACHE_SECONDS = 300
# The cloud backup is otherwise idle while the gateway answers. It is read this
# often so a sign-in that stopped working shows up (and the token is refreshed)
# before the backup is needed; a failed check is retried sooner.
CLOUD_CHECK_SECONDS = 3600
CLOUD_RETRY_SECONDS = 600
# Consecutive failures before asking the user to sign in or re-register.
CLOUD_AUTH_FAILURES = 3
KEY_REFUSED_POLLS = 10
PASSWORD_REFUSED_POLLS = 3

# Battery dispatch
DISPATCH_MAX_WATTS = 10000
DISPATCH_MAX_SECONDS = 86400
# Each command to the Powerwall covers at most this long and is renewed while a
# session runs, so the battery returns to normal within this time if Home
# Assistant stops.
DISPATCH_CHUNK_SECONDS = 300
DISPATCH_RENEW_SECONDS = 60
DISPATCH_MAX_FAILURES = 3

EVENT_DISPATCH_ENDED = f"{DOMAIN}_dispatch_ended"

SERVICE_DISPATCH = "dispatch_battery_power"
SERVICE_RESUME = "resume_battery_operation"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_POWER_WATTS = "power_watts"
ATTR_DURATION_SECONDS = "duration_seconds"
ATTR_MIN_SOC = "min_soc"
ATTR_MAX_SOC = "max_soc"


def app_soc(raw: float | None) -> float | None:
    """Convert the gateway's state of energy to the Tesla app scale.

    The app hides the bottom 5 %: app = (raw - 5) / 0.95.
    """
    if raw is None:
        return None
    return round(max(0.0, min(100.0, (raw - 5) / 0.95)), 1)
