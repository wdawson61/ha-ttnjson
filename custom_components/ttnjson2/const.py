"""Constants for the TTN JSON integration."""

DOMAIN = "ttnjson2"

CONF_EUI        = "eui"
CONF_TOPIC      = "topic"
CONF_VALUES     = "values"       # dict of discovered path → unit (persisted after first message)
CONF_SELECTS    = "selects"
CONF_NAME       = "name"
CONF_F_PORT     = "f_port"
CONF_MAP        = "map"
CONF_STATE_PATH = "state_path"

DEFAULT_F_PORT  = 1

# Paths auto-included from outside decoded_payload
EXTRA_PATHS = [
    "uplink_message/rx_metadata/rssi",
    "uplink_message/rx_metadata/snr",
]

# Unit guesses keyed by field name substring
UNIT_GUESSES = {
    "rssi":        "dB",
    "snr":         "dB",
    "battery":     "V",
    "voltage":     "V",
    "temperature": "°F",
    "temp":        "°F",
    "humidity":    "%",
    "pressure":    "hPa",
    "elevation":   "°",
    "azimuth":     "°",
    "altitude":    "m",
    "speed":       "mph",
    "current":     "A",
    "power":       "W",
    "pulse":       "mV",
    "mode":        "",
    "fault":       "",
}
