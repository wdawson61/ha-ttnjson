"""Config flow for TTN JSON integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
from homeassistant.components import mqtt
from homeassistant.core import callback

from .const import (
    CONF_EUI,
    CONF_F_PORT,
    CONF_MAP,
    CONF_NAME,
    CONF_SELECTS,
    CONF_STATE_PATH,
    CONF_TOPIC,
    CONF_VALUES,
    DEFAULT_F_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers — sensor values text
# ---------------------------------------------------------------------------

def _parse_values_text(text: str) -> dict[str, str] | None:
    """Parse multi-line  path/to/field:unit  into a dict.
    Returns None on malformed input."""
    result = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            return None
        path, _, unit = line.rpartition(":")
        path = path.strip()
        if not path:
            return None
        result[path] = unit.strip()
    return result or None


def _values_to_text(values: dict[str, str]) -> str:
    return "\n".join(f"{k}:{v}" for k, v in values.items())


# ---------------------------------------------------------------------------
# Helpers — select map text
# ---------------------------------------------------------------------------

def _parse_map_text(text: str) -> dict[str, int] | None:
    """Parse multi-line  NAME:uint8  into a dict.
    Returns None on malformed input."""
    result = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            return None
        name, _, val = line.rpartition(":")
        name = name.strip()
        if not name:
            return None
        try:
            int_val = int(val.strip(), 0)   # accepts 0x01 or 1
            if not 0 <= int_val <= 255:
                return None
        except ValueError:
            return None
        result[name] = int_val
    return result or None


def _map_to_text(mode_map: dict[str, int]) -> str:
    return "\n".join(f"{k}:{v}" for k, v in mode_map.items())


def _parse_selects_text(text: str) -> list[dict] | None:
    """Parse the compound select block text into a list of select configs.

    Format — each select is separated by a blank line:
        name=mode
        f_port=1
        state_path=uplink_message/decoded_payload/mode
        AUTO:0
        STOW:1
        SNOW:2

    Returns None on malformed input.
    """
    if not text.strip():
        return []   # no selects is valid

    selects = []
    for block in text.strip().split("\n\n"):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        cfg: dict[str, Any] = {}
        map_lines = []
        for line in lines:
            if line.startswith("name="):
                cfg[CONF_NAME] = line[5:].strip()
            elif line.startswith("f_port="):
                try:
                    cfg[CONF_F_PORT] = int(line[7:].strip())
                except ValueError:
                    return None
            elif line.startswith("state_path="):
                cfg[CONF_STATE_PATH] = line[11:].strip()
            else:
                map_lines.append(line)

        if not cfg.get(CONF_NAME) or not cfg.get(CONF_STATE_PATH):
            return None
        if not cfg.get(CONF_F_PORT):
            cfg[CONF_F_PORT] = DEFAULT_F_PORT

        mode_map = _parse_map_text("\n".join(map_lines))
        if mode_map is None:
            return None
        cfg[CONF_MAP] = mode_map
        selects.append(cfg)

    return selects


def _selects_to_text(selects: list[dict]) -> str:
    blocks = []
    for s in selects:
        lines = [
            f"name={s[CONF_NAME]}",
            f"f_port={s.get(CONF_F_PORT, DEFAULT_F_PORT)}",
            f"state_path={s[CONF_STATE_PATH]}",
        ]
        for sym, val in s.get(CONF_MAP, {}).items():
            lines.append(f"{sym}:{val}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

class TtnJsonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for TTN JSON."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1 — device EUI, uplink topic, sensor fields."""
        errors: dict[str, str] = {}

        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            return self.async_abort(reason="mqtt_unavailable")

        if user_input is not None:
            eui = user_input[CONF_EUI].strip()
            topic = user_input[CONF_TOPIC].strip()

            await self.async_set_unique_id(eui)
            self._abort_if_unique_id_configured()

            values = _parse_values_text(user_input[CONF_VALUES])
            if values is None:
                errors[CONF_VALUES] = "bad_values"
            elif not values:
                errors[CONF_VALUES] = "empty_values"

            if not errors:
                self._data = {
                    CONF_EUI:    eui,
                    CONF_TOPIC:  topic,
                    CONF_VALUES: values,
                }
                return await self.async_step_selects()

        default_topic  = (user_input or {}).get(CONF_TOPIC,  "v3/your-app@ttn/devices/<EUI>/up")
        default_eui    = (user_input or {}).get(CONF_EUI,    "")
        default_values = (user_input or {}).get(CONF_VALUES,
            "uplink_message/rx_metadata/rssi:dB\n"
            "uplink_message/decoded_payload/battery:V"
        )

        multiline = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True))

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_EUI,    default=default_eui):    str,
                vol.Required(CONF_TOPIC,  default=default_topic):  str,
                vol.Required(CONF_VALUES, default=default_values): multiline,
            }),
            errors=errors,
        )

    async def async_step_selects(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 2 — optional select (mode command) entities."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selects = _parse_selects_text(user_input.get(CONF_SELECTS, ""))
            if selects is None:
                errors[CONF_SELECTS] = "bad_selects"
            else:
                self._data[CONF_SELECTS] = selects
                eui = self._data[CONF_EUI]
                return self.async_create_entry(
                    title=f"TTN — {eui}",
                    data=self._data,
                )

        default_selects = (user_input or {}).get(CONF_SELECTS,
            "name=mode\n"
            "f_port=1\n"
            "state_path=uplink_message/decoded_payload/mode\n"
            "AUTO:0\n"
            "STOW:1\n"
            "SNOW:2\n"
            "HAIL:3"
        )

        return self.async_show_form(
            step_id="selects",
            data_schema=vol.Schema({
                vol.Optional(CONF_SELECTS, default=default_selects): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)),
            }),
            errors=errors,
            description_placeholders={
                "eui": self._data[CONF_EUI],
            },
        )

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Import from configuration.yaml."""
        eui = import_data[CONF_EUI]
        await self.async_set_unique_id(eui)
        self._abort_if_unique_id_configured()
        _LOGGER.info("TTN JSON: importing yaml entry for EUI '%s'", eui)
        return self.async_create_entry(title=f"TTN — {eui}", data=import_data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> TtnJsonOptionsFlow:
        return TtnJsonOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------

class TtnJsonOptionsFlow(config_entries.OptionsFlow):
    """Edit topic, sensor fields, and select entities after setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._updated: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1 — topic and sensor fields."""
        errors: dict[str, str] = {}
        current = self._config_entry.data

        if user_input is not None:
            values = _parse_values_text(user_input[CONF_VALUES])
            if values is None:
                errors[CONF_VALUES] = "bad_values"
            elif not values:
                errors[CONF_VALUES] = "empty_values"

            if not errors:
                self._updated = {
                    **current,
                    CONF_TOPIC:  user_input[CONF_TOPIC].strip(),
                    CONF_VALUES: values,
                }
                return await self.async_step_selects()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_TOPIC,  default=current.get(CONF_TOPIC,  "")): str,
                vol.Required(CONF_VALUES, default=_values_to_text(current.get(CONF_VALUES, {}))): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)),
            }),
            errors=errors,
        )

    async def async_step_selects(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 2 — select entities."""
        errors: dict[str, str] = {}
        current_selects = self._config_entry.data.get(CONF_SELECTS, [])

        if user_input is not None:
            selects = _parse_selects_text(user_input.get(CONF_SELECTS, ""))
            if selects is None:
                errors[CONF_SELECTS] = "bad_selects"
            else:
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    data={**self._updated, CONF_SELECTS: selects},
                )
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="selects",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_SELECTS,
                    default=_selects_to_text(current_selects),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)),
            }),
            errors=errors,
        )
