"""TTN JSON — ButtonEntity for one-shot downlink commands (fault clear, reboot etc.)."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BUTTONS,
    CONF_EUI,
    CONF_F_PORT,
    CONF_NAME,
    CONF_PAYLOAD,
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
    """Set up TTN JSON button entities from a config entry."""
    buttons_cfg: list[dict] = entry.data.get(CONF_BUTTONS, [])
    if not buttons_cfg:
        return

    eui: str      = entry.data[CONF_EUI]
    up_topic: str = entry.data[CONF_TOPIC].replace("<EUI>", eui)
    down_topic    = up_topic.replace("/up", "/down/push")

    entities = [
        TtnJsonButton(
            hass=hass,
            eui=eui,
            down_topic=down_topic,
            name=cfg[CONF_NAME],
            f_port=cfg.get(CONF_F_PORT, DEFAULT_F_PORT),
            payload_hex=cfg[CONF_PAYLOAD],
            entry_id=entry.entry_id,
        )
        for cfg in buttons_cfg
    ]

    async_add_entities(entities, update_before_add=False)
    _LOGGER.debug("TTN JSON: created %d button(s) for %s", len(entities), eui)


# ---------------------------------------------------------------------------
# Button entity
# ---------------------------------------------------------------------------

class TtnJsonButton(ButtonEntity):
    """A HA button that sends a fixed byte sequence as a TTN downlink."""

    def __init__(
        self,
        hass: HomeAssistant,
        eui: str,
        down_topic: str,
        name: str,
        f_port: int,
        payload_hex: str,
        entry_id: str,
    ) -> None:
        self._hass       = hass
        self._eui        = eui
        self._down_topic = down_topic
        self._f_port     = f_port
        self._last_pressed: dt_util.dt.datetime | None = None

        # Decode hex string → bytes → base64 once at init
        try:
            raw = bytes.fromhex(payload_hex.replace(" ", "").replace("0x", ""))
        except ValueError:
            _LOGGER.error(
                "TTN JSON button '%s': invalid hex payload '%s' — defaulting to 0x00",
                name, payload_hex,
            )
            raw = bytes([0x00])

        self._b64 = base64.b64encode(raw).decode()
        self._payload_hex = payload_hex

        self._attr_name      = f"{eui} {name}"
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{eui}_{name}"

    async def async_press(self) -> None:
        """Send the downlink when the button is pressed."""
        payload = json.dumps({
            "downlinks": [{
                "f_port":      self._f_port,
                "frm_payload": self._b64,
                "priority":    "NORMAL",
            }]
        })

        await mqtt.async_publish(
            self._hass,
            self._down_topic,
            payload,
            qos=0,
            retain=False,
        )

        self._last_pressed = dt_util.utcnow()
        _LOGGER.info(
            "TTN JSON: button '%s' pressed — sent 0x%s on port %d to %s",
            self._attr_name, self._payload_hex.upper(), self._f_port, self._eui,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "eui":          self._eui,
            "f_port":       self._f_port,
            "payload_hex":  self._payload_hex,
            "last_pressed": self._last_pressed.isoformat() if self._last_pressed else None,
        }
