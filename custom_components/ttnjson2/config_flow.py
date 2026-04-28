"""Config flow for TTN JSON integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import mqtt
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

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

_MULTILINE = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True))


# ---------------------------------------------------------------------------
# Helpers — select map
# ---------------------------------------------------------------------------

def _parse_map_text(text: str) -> dict[str, int] | None:
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
            int_val = int(val.strip(), 0)
            if not 0 <= int_val <= 255:
                return None
        except ValueError:
            return None
        result[name] = int_val
    return result or None


def _parse_selects_text(text: str) -> list[dict] | None:
    if not text.strip():
        return []
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
        cfg.setdefault(CONF_F_PORT, DEFAULT_F_PORT)
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
    """Two-step flow: (1) EUI + topic, (2) optional select entities."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step 1 — EUI + topic
    # ------------------------------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            return self.async_abort(reason="mqtt_unavailable")

        if user_input is not None:
            eui   = user_input[CONF_EUI].strip()
            topic = user_input[CONF_TOPIC].strip()

            await self.async_set_unique_id(eui)
            self._abort_if_unique_id_configured()

            self._data = {
                CONF_EUI:    eui,
                CONF_TOPIC:  topic,
                CONF_VALUES: {},    # populated automatically on first uplink
            }
            return await self.async_step_selects()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_EUI,   default=""): str,
                vol.Required(CONF_TOPIC, default="v3/your-app@ttn/devices/<EUI>/up"): str,
            }),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2 — Optional select entities
    # ------------------------------------------------------------------
    async def async_step_selects(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            selects = _parse_selects_text(user_input.get(CONF_SELECTS, ""))
            if selects is None:
                errors[CONF_SELECTS] = "bad_selects"
            else:
                self._data[CONF_SELECTS] = selects
                return self.async_create_entry(
                    title=f"TTN — {self._data[CONF_EUI]}",
                    data=self._data,
                )

        default_selects = (
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
                vol.Optional(CONF_SELECTS, default=default_selects): _MULTILINE,
            }),
            errors=errors,
            description_placeholders={"eui": self._data[CONF_EUI]},
        )

    # ------------------------------------------------------------------
    # Import from configuration.yaml
    # ------------------------------------------------------------------
    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> config_entries.FlowResult:
        eui = import_data[CONF_EUI]
        await self.async_set_unique_id(eui)
        self._abort_if_unique_id_configured()
        _LOGGER.info("TTN JSON: importing yaml entry for '%s'", eui)
        return self.async_create_entry(title=f"TTN — {eui}", data=import_data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> TtnJsonOptionsFlow:
        return TtnJsonOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow — edit topic, units, selects after setup
# ---------------------------------------------------------------------------

class TtnJsonOptionsFlow(config_entries.OptionsFlow):
    """Edit topic, discovered sensor units, and select entities."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._updated: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Edit topic and unit overrides for discovered sensors."""
        errors: dict[str, str] = {}
        current = self._config_entry.data

        # Build editable text from currently discovered values
        current_values: dict[str, str] = current.get(CONF_VALUES, {})
        values_text = "\n".join(
            f"{path.split('/')[-1]}:{unit}"
            for path, unit in current_values.items()
        )

        if user_input is not None:
            # Parse edited units back — match by field name order
            new_units = {}
            for line in user_input.get("units", "").splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                name, _, unit = line.rpartition(":")
                # Find the matching full path
                for path in current_values:
                    if path.split("/")[-1] == name.strip():
                        new_units[path] = unit.strip()
                        break

            self._updated = {
                **current,
                CONF_TOPIC:  user_input[CONF_TOPIC].strip(),
                CONF_VALUES: new_units if new_units else current_values,
            }
            return await self.async_step_selects()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_TOPIC, default=current.get(CONF_TOPIC, "")): str,
                vol.Optional("units", default=values_text): _MULTILINE,
            }),
            errors=errors,
            description_placeholders={
                "hint": "Edit units for each discovered field (fieldname:unit). "
                        "Fields are re-discovered automatically from the next uplink."
            },
        )

    async def async_step_selects(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
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
                ): _MULTILINE,
            }),
            errors=errors,
        )
