"""Support for TTN decoded_payload fields — config-entry and yaml path."""

from __future__ import annotations

import json
import logging

import voluptuous as vol

from homeassistant.components import mqtt
from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util

from .const import CONF_EUI, CONF_TOPIC, CONF_VALUES, DOMAIN

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legacy configuration.yaml schema (kept for backward compatibility)
# ---------------------------------------------------------------------------
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_EUI): cv.string,
        vol.Required(CONF_TOPIC): cv.string,
        vol.Required(CONF_VALUES): cv.ensure_list,
    }
)

# Validate that the TTN uplink envelope is present but allow any payload shape.
# The previous GPS_JSON_PAYLOAD_SCHEMA name was a leftover from another project
# and the strict sub-schema was rejecting legitimate TTN messages.
TTN_ENVELOPE_SCHEMA = vol.Schema(
    {vol.Required("uplink_message"): dict},
    extra=vol.ALLOW_EXTRA,
)


# ---------------------------------------------------------------------------
# Config-entry setup (called by __init__.async_setup_entry)
# ---------------------------------------------------------------------------
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TTN JSON sensors from a config entry."""
    eui: str = entry.data[CONF_EUI]
    topic: str = entry.data[CONF_TOPIC].replace("<EUI>", eui)
    values: dict[str, str] = entry.data[CONF_VALUES]

    sensors = [
        TtnJsonSensor(
            hass=hass,
            topic=topic,
            eui=eui,
            name=key.split("/")[-1],
            unit=unit,
            mqtt_key=key,
            entry_id=entry.entry_id,
        )
        for key, unit in values.items()
    ]

    async_add_entities(sensors, update_before_add=True)
    _subscribe(hass, topic, sensors)


# ---------------------------------------------------------------------------
# Legacy yaml-platform setup
# ---------------------------------------------------------------------------
async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up TTN JSON sensors from configuration.yaml (legacy path)."""
    if not await mqtt.async_wait_for_mqtt_client(hass):
        _LOGGER.error("MQTT integration is not available")
        raise ConnectionError()

    # config[CONF_VALUES] arrives wrapped in a list by vol.ensure_list
    raw_values = config[CONF_VALUES]
    values: dict[str, str] = raw_values[0] if isinstance(raw_values, list) else raw_values

    eui: str = config[CONF_EUI]
    topic: str = config[CONF_TOPIC].replace("<EUI>", eui)

    sensors = [
        TtnJsonSensor(
            hass=hass,
            topic=topic,
            eui=eui,
            name=key.split("/")[-1],
            unit=unit,
            mqtt_key=key,
            entry_id=None,   # no config entry for yaml path
        )
        for key, unit in values.items()
    ]

    async_add_entities(sensors, update_before_add=True)
    _subscribe(hass, topic, sensors)


# ---------------------------------------------------------------------------
# Shared MQTT subscription helper
# ---------------------------------------------------------------------------
def _subscribe(hass: HomeAssistant, topic: str, sensors: list[TtnJsonSensor]) -> None:
    """Subscribe to the MQTT topic and fan out updates to all sensors."""

    @callback
    async def async_message_received(msg):
        try:
            data = TTN_ENVELOPE_SCHEMA(json.loads(msg.payload))
        except vol.MultipleInvalid:
            _LOGGER.warning(
                "TTN JSON: skipping message — missing 'uplink_message' envelope: %s",
                msg.payload,
            )
            return
        except (ValueError, json.JSONDecodeError):
            _LOGGER.error("TTN JSON: could not parse JSON payload: %s", msg.payload)
            return

        for sensor in sensors:
            sensor.do_update(data)

    hass.async_create_task(
        mqtt.async_subscribe(hass, topic, async_message_received, qos=0)
    )
    _LOGGER.debug("TTN JSON: subscribed to %s (%d sensor(s))", topic, len(sensors))


# ---------------------------------------------------------------------------
# Sensor entity
# ---------------------------------------------------------------------------
class TtnJsonSensor(SensorEntity):
    """A single field extracted from a TTN uplink MQTT message."""

    def __init__(
        self,
        hass: HomeAssistant,
        topic: str,
        eui: str,
        name: str,
        unit: str,
        mqtt_key: str,
        entry_id: str | None,
    ) -> None:
        self._hass = hass
        self._topic = topic
        self._eui = eui
        self._field_name = name
        self._unit = unit
        self._mqtt_key = mqtt_key
        self._state = None
        self._updated: dt_util.dt.datetime = dt_util.utcnow()

        # Entity display name: "efence2-001 temperature"
        self._attr_name = f"{eui} {name}"

        # Unique ID enables entity registry, renaming, and customisation in UI.
        # Scoped to entry_id when available so yaml and UI entries don't clash.
        scope = entry_id if entry_id else "yaml"
        self._attr_unique_id = f"{DOMAIN}_{scope}_{eui}_{mqtt_key}"

        self._attr_should_poll = False

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def native_value(self):
        """Return the current sensor value."""
        return self._state

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the unit of measurement."""
        return self._unit

    @property
    def extra_state_attributes(self) -> dict:
        """Return extra attributes."""
        return {
            "eui": self._eui,
            "mqtt_key": self._mqtt_key,
            "last_updated": self._updated.isoformat(),
        }

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    @callback
    def do_update(self, data: dict) -> None:
        """Navigate the slash-delimited path and update state."""
        try:
            node = data
            for part in self._mqtt_key.split("/"):
                node = node[part]
                # rx_metadata and similar fields are lists — take first element
                if isinstance(node, list):
                    node = node[0]
            self._state = node
        except (KeyError, IndexError, TypeError) as err:
            _LOGGER.warning(
                "TTN JSON: could not extract '%s' from payload: %s",
                self._mqtt_key,
                err,
            )
            return

        self._updated = dt_util.utcnow()
        self.async_write_ha_state()
