"""Constants for the TTN JSON integration."""

DOMAIN = "ttnjson"

CONF_EUI      = "eui"
CONF_TOPIC    = "topic"
CONF_VALUES   = "values"
CONF_SELECTS  = "selects"      # list of select entity configs
CONF_NAME     = "name"
CONF_F_PORT   = "f_port"
CONF_MAP      = "map"          # symbolic-name → uint8 value mapping
CONF_STATE_PATH = "state_path" # uplink JSON path for state feedback

DEFAULT_F_PORT = 1
