"""TTN JSON — SelectEntity for uint8 mode commands with symbolic name mapping."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import mqtt
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EUI,
    CONF_F_PORT,
    CONF_MAP,
    CONF_NAME,
    CONF_SELECTS,
    CONF_STATE_PATH,
    CONF_TOPIC,
    DEFAULT_F_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config-entry setup
# ---------------------------------------------------------------------------
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TTN JSON select entities from a config entry."""
    selects_cfg: list[dict] = entry.data.get(CONF_SELECTS, [])
    if not selects_cfg:
        return

    eui: str   = entry.data[CONF_EUI]
    up_topic: str = entry.data[CONF_TOPIC].replace("<EUI>", eui)

    # Derive the downlink topic from the uplink topic:
    #   v3/app@ttn/devices/<eui>/up  →  v3/app@ttn/devices/<eui>/down/push
    down_topic = up_topic.replace("/up", "/down/push")

    entities = []
    for cfg in selects_cfg:
        entity = TtnJsonSelect(
            hass=hass,
            eui=eui,
            up_topic=up_topic,
            down_topic=down_topic,
            name=cfg[CONF_NAME],
            f_port=cfg.get(CONF_F_PORT, DEFAULT_F_PORT),
            mode_map=cfg[CONF_MAP],            # {str: int}
            state_path=cfg[CONF_STATE_PATH],   # e.g. "uplink_message/decoded_payload/mode"
            entry_id=entry.entry_id,
        )
        entities.append(entity)

    async_add_entities(entities, update_before_add=True)

    # Subscribe once per entry — fan out to all select entities on this device
    async def async_message_received(msg):
        try:
            data = json.loads(msg.payload)
            if "uplink_message" not in data:
                return
        except (ValueError, json.JSONDecodeError):
            _LOGGER.error("TTN JSON select: could not parse JSON: %s", msg.payload)
            return
        for entity in entities:
            entity.handle_uplink(data)

    await mqtt.async_subscribe(hass, up_topic, async_message_received, qos=0)
    _LOGGER.debug(
        "TTN JSON: subscribed %d select(s) to %s", len(entities), up_topic
    )


# ---------------------------------------------------------------------------
# Select entity
# ---------------------------------------------------------------------------
class TtnJsonSelect(SelectEntity):
    """A HA select entity that sends a uint8 mode byte as a TTN downlink
    and reflects ground-truth state from the device's own uplink reports."""

    def __init__(
        self,
        hass: HomeAssistant,
        eui: str,
        up_topic: str,
        down_topic: str,
        name: str,
        f_port: int,
        mode_map: dict[str, int],
        state_path: str,
        entry_id: str,
    ) -> None:
        self._hass       = hass
        self._eui        = eui
        self._up_topic   = up_topic
        self._down_topic = down_topic
        self._field_name = name
        self._f_port     = f_port
        self._mode_map   = mode_map                          # {"AUTO": 0, "STOW": 1, ...}
        self._rev_map    = {v: k for k, v in mode_map.items()}  # {0: "AUTO", 1: "STOW", ...}
        self._state_path = state_path
        self._updated    = dt_util.utcnow()

        self._attr_name        = f"{eui} {name}"
        self._attr_unique_id   = f"{DOMAIN}_{entry_id}_{eui}_{name}"
        self._attr_options     = list(mode_map.keys())
        self._attr_current_option: str | None = None
        self._attr_should_poll = False

    # ------------------------------------------------------------------
    # SelectEntity interface
    # ------------------------------------------------------------------
    @property
    def current_option(self) -> str | None:
        """Return the currently active option."""
        return self._attr_current_option

    async def async_select_option(self, option: str) -> None:
        """Send a downlink command when the user picks a new option."""
        if option not in self._mode_map:
            _LOGGER.error(
                "TTN JSON select '%s': unknown option '%s'", self._attr_name, option
            )
            return

        byte_val = self._mode_map[option]
        b64 = base64.b64encode(bytes([byte_val])).decode()

        payload = json.dumps({
            "downlinks": [{
                "f_port": self._f_port,
                "frm_payload": b64,
                "priority": "NORMAL",
            }]
        })

        await mqtt.async_publish(
            self._hass,
            self._down_topic,
            payload,
            qos=0,
            retain=False,
        )
        _LOGGER.info(
            "TTN JSON: sent downlink to %s — %s → 0x%02X (port %d)",
            self._eui, option, byte_val, self._f_port,
        )
        # Do NOT update _attr_current_option here — wait for the uplink
        # confirmation from the device before reflecting the state change.

    # ------------------------------------------------------------------
    # Uplink state feedback
    # ------------------------------------------------------------------
    @callback
    def handle_uplink(self, data: dict) -> None:
        """Extract the mode byte from an uplink and update state."""
        try:
            node = data
            for part in self._state_path.split("/"):
                node = node[part]
                if isinstance(node, list):
                    node = node[0]
            raw_value = int(node)
        except (KeyError, IndexError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "TTN JSON select '%s': could not extract '%s': %s",
                self._attr_name, self._state_path, err,
            )
            return

        symbolic = self._rev_map.get(raw_value)
        if symbolic is None:
            _LOGGER.warning(
                "TTN JSON select '%s': received unknown mode value %d — "
                "add it to the map config",
                self._attr_name, raw_value,
            )
            return

        self._attr_current_option = symbolic
        self._updated = dt_util.utcnow()
        self.async_write_ha_state()
        _LOGGER.debug(
            "TTN JSON: '%s' state → %s (0x%02X)", self._attr_name, symbolic, raw_value
        )

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the map and last update time for diagnostics."""
        return {
            "eui":          self._eui,
            "f_port":       self._f_port,
            "mode_map":     self._mode_map,
            "state_path":   self._state_path,
            "last_updated": self._updated.isoformat(),
        }
