"""TTN JSON sensors — auto-discovered from first uplink message."""

from __future__ import annotations

import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import mqtt
from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EUI,
    CONF_TOPIC,
    CONF_VALUES,
    DOMAIN,
    EXTRA_PATHS,
    UNIT_GUESSES,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legacy yaml schema
# ---------------------------------------------------------------------------
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_EUI):    cv.string,
        vol.Required(CONF_TOPIC):  cv.string,
        vol.Optional(CONF_VALUES): cv.ensure_list,
    }
)

TTN_ENVELOPE_SCHEMA = vol.Schema(
    {vol.Required("uplink_message"): dict},
    extra=vol.ALLOW_EXTRA,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _guess_unit(path: str) -> str:
    field = path.split("/")[-1].lower()
    for keyword, unit in UNIT_GUESSES.items():
        if keyword in field:
            return unit
    return ""


def _extract_decoded_paths(payload: dict) -> list[str]:
    """Return all scalar paths inside decoded_payload, plus EXTRA_PATHS.
    Nested dicts (like 'raw') and lists are skipped — only top-level scalars."""
    paths = []

    # Walk decoded_payload — top-level scalars only
    decoded = (
        payload
        .get("uplink_message", {})
        .get("decoded_payload", {})
    )
    if isinstance(decoded, dict):
        for key, val in decoded.items():
            if isinstance(val, (dict, list)):
                _LOGGER.debug("TTN JSON: skipping nested field '%s'", key)
                continue
            paths.append(f"uplink_message/decoded_payload/{key}")

    # Add extra paths (rssi, snr) if present in payload
    for extra in EXTRA_PATHS:
        node = payload
        try:
            for part in extra.split("/"):
                node = node[part]
                if isinstance(node, list):
                    node = node[0]
            if not isinstance(node, (dict, list)):
                paths.append(extra)
        except (KeyError, IndexError, TypeError):
            pass

    return paths


def _nav(data: dict, path: str) -> Any:
    """Navigate a slash-delimited path, taking first element of lists."""
    node = data
    for part in path.split("/"):
        node = node[part]
        if isinstance(node, list):
            node = node[0]
    return node


# ---------------------------------------------------------------------------
# Config-entry setup
# ---------------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TTN JSON sensors.

    If the config entry already has discovered values (from a previous run),
    create those sensors immediately.  Either way, subscribe to the MQTT topic
    so that on the first message (or any subsequent message) we can create any
    newly discovered fields and update existing ones.
    """
    eui: str   = entry.data[CONF_EUI]
    topic: str = entry.data[CONF_TOPIC].replace("<EUI>", eui)

    # Sensors keyed by mqtt path — shared across callbacks
    sensors: dict[str, TtnJsonSensor] = {}

    # Restore previously discovered sensors if any
    existing_values: dict[str, str] = entry.data.get(CONF_VALUES, {})
    if existing_values:
        for path, unit in existing_values.items():
            sensor = TtnJsonSensor(
                eui=eui,
                name=path.split("/")[-1],
                unit=unit,
                mqtt_key=path,
                entry_id=entry.entry_id,
            )
            sensors[path] = sensor
        async_add_entities(list(sensors.values()), update_before_add=False)
        _LOGGER.debug(
            "TTN JSON: restored %d sensor(s) for %s", len(sensors), eui
        )

    async def async_message_received(msg):
        try:
            data = TTN_ENVELOPE_SCHEMA(json.loads(msg.payload))
        except (vol.MultipleInvalid, ValueError, json.JSONDecodeError) as err:
            _LOGGER.warning("TTN JSON: bad message from %s: %s", eui, err)
            return

        _LOGGER.debug("TTN JSON: message received for %s", eui)

        # Discover any new fields not yet tracked
        discovered_paths = _extract_decoded_paths(data)
        new_sensors = []
        for path in discovered_paths:
            if path not in sensors:
                unit = _guess_unit(path)
                sensor = TtnJsonSensor(
                    eui=eui,
                    name=path.split("/")[-1],
                    unit=unit,
                    mqtt_key=path,
                    entry_id=entry.entry_id,
                )
                sensors[path] = sensor
                new_sensors.append(sensor)
                _LOGGER.info(
                    "TTN JSON: discovered new field '%s' for %s (unit: '%s')",
                    path, eui, unit,
                )

        if new_sensors:
            async_add_entities(new_sensors, update_before_add=False)
            all_values = {path: s._unit for path, s in sensors.items()}
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_VALUES: all_values}
            )

        # Update all sensor states
        for path, sensor in sensors.items():
            try:
                value = _nav(data, path)
                _LOGGER.debug("TTN JSON: %s = %s", path, value)
                sensor.do_update(value)
            except (KeyError, IndexError, TypeError) as err:
                _LOGGER.warning("TTN JSON: failed to nav %s: %s", path, err)
            except Exception as err:
                _LOGGER.error("TTN JSON: error updating %s: %s", path, err)

    await mqtt.async_subscribe(hass, topic, async_message_received, qos=0)
    _LOGGER.debug("TTN JSON: subscribed to %s for %s", topic, eui)


# ---------------------------------------------------------------------------
# Legacy yaml setup
# ---------------------------------------------------------------------------

async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Legacy yaml path — creates sensors from explicit values dict."""
    if not await mqtt.async_wait_for_mqtt_client(hass):
        _LOGGER.error("TTN JSON: MQTT not available")
        raise ConnectionError()

    raw_values = config.get(CONF_VALUES, [])
    values: dict[str, str] = raw_values[0] if isinstance(raw_values, list) and raw_values else {}

    eui: str   = config[CONF_EUI]
    topic: str = config[CONF_TOPIC].replace("<EUI>", eui)

    sensors = {
        path: TtnJsonSensor(
            eui=eui,
            name=path.split("/")[-1],
            unit=unit,
            mqtt_key=path,
            entry_id=None,
        )
        for path, unit in values.items()
    }
    async_add_entities(list(sensors.values()), update_before_add=False)

    async def async_message_received(msg):
        try:
            data = TTN_ENVELOPE_SCHEMA(json.loads(msg.payload))
        except (vol.MultipleInvalid, ValueError, json.JSONDecodeError):
            return
        for path, sensor in sensors.items():
            try:
                sensor.do_update(_nav(data, path))
            except (KeyError, IndexError, TypeError):
                pass

    await mqtt.async_subscribe(hass, topic, async_message_received, qos=0)


# ---------------------------------------------------------------------------
# Sensor entity
# ---------------------------------------------------------------------------

class TtnJsonSensor(SensorEntity):
    """A single TTN uplink field as a HA sensor."""

    def __init__(
        self,
        eui: str,
        name: str,
        unit: str,
        mqtt_key: str,
        entry_id: str | None,
    ) -> None:
        self._eui        = eui
        self._unit       = unit
        self._mqtt_key   = mqtt_key
        self._state      = None
        self._updated    = dt_util.utcnow()

        self._attr_name        = f"{eui} {name}"
        scope                  = entry_id if entry_id else "yaml"
        self._attr_unique_id   = f"{DOMAIN}_{scope}_{eui}_{mqtt_key}"
        self._attr_should_poll = False

    @property
    def native_value(self):
        return self._state

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return None for non-numeric fields — empty string causes HA to
        infer numeric type and reject string values like 'AUTO' or 'False'."""
        return self._unit if self._unit else None

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "eui":          self._eui,
            "mqtt_key":     self._mqtt_key,
            "last_updated": self._updated.isoformat(),
        }

    @callback
    def do_update(self, value: Any) -> None:
        """Set state directly from a pre-navigated value."""
        self._state   = value
        self._updated = dt_util.utcnow()
        _LOGGER.debug(
            "TTN JSON: do_update %s = %r (type=%s, hass=%s)",
            self._mqtt_key, value, type(value).__name__,
            self.hass is not None if hasattr(self, "hass") else "no_hass_attr",
        )
        self.async_write_ha_state()
