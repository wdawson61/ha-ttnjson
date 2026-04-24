"""Config flow for TTN JSON integration."""

from __future__ import annotations

import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import mqtt
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
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

# hass.data key for captured MQTT payload during discovery
_DISCOVERY_KEY = f"{DOMAIN}_discovery"

# Unit guesses based on field name substrings
_UNIT_GUESSES = {
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
}

# ---------------------------------------------------------------------------
# Helpers — JSON flattening
# ---------------------------------------------------------------------------

def _flatten(obj: Any, prefix: str = "") -> list[str]:
    """Recursively flatten a JSON object into slash-delimited paths.
    List nodes use the first element. Skips non-scalar leaves."""
    paths = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = f"{prefix}/{k}" if prefix else k
            paths.extend(_flatten(v, child))
    elif isinstance(obj, list):
        if obj:
            paths.extend(_flatten(obj[0], prefix))
    else:
        # Scalar — this is a leaf
        if prefix:
            paths.append(prefix)
    return paths


def _guess_unit(path: str) -> str:
    """Return a unit guess based on the field name, or empty string."""
    field = path.split("/")[-1].lower()
    for keyword, unit in _UNIT_GUESSES.items():
        if keyword in field:
            return unit
    return ""


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
    """Parse multi-line  NAME:uint8  into a dict."""
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
    """Parse compound select block text into a list of select configs."""
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
        self._discovered_paths: list[str] = []
        self._unsubscribe = None

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

            self._data = {CONF_EUI: eui, CONF_TOPIC: topic}

            # Clear any previous discovery payload for this flow
            self.hass.data.pop(_DISCOVERY_KEY, None)

            # Subscribe to the topic and capture the first message
            resolved_topic = topic.replace("<EUI>", eui)

            @callback
            def _on_message(msg):
                if _DISCOVERY_KEY not in self.hass.data:
                    try:
                        self.hass.data[_DISCOVERY_KEY] = json.loads(msg.payload)
                        _LOGGER.debug("TTN JSON discovery: captured payload")
                    except (ValueError, json.JSONDecodeError):
                        pass

            self._unsubscribe = await mqtt.async_subscribe(
                self.hass, resolved_topic, _on_message, qos=0
            )

            return await self.async_step_listen()

        default_topic = "v3/your-app@ttn/devices/<EUI>/up"
        default_eui   = ""

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_EUI,   default=default_eui):   str,
                vol.Required(CONF_TOPIC, default=default_topic): str,
            }),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2 — Wait for a message
    # ------------------------------------------------------------------
    async def async_step_listen(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Show a 'send a message now' prompt. On submit, check if one arrived."""
        errors: dict[str, str] = {}

        if user_input is not None:
            payload = self.hass.data.get(_DISCOVERY_KEY)

            if payload is None:
                # Nothing arrived yet — redisplay with error
                errors["base"] = "no_message"
            else:
                # Got one — flatten the payload into selectable paths
                self._discovered_paths = _flatten(payload)
                if self._unsubscribe:
                    self._unsubscribe()
                    self._unsubscribe = None
                self.hass.data.pop(_DISCOVERY_KEY, None)

                if not self._discovered_paths:
                    errors["base"] = "empty_payload"
                else:
                    return await self.async_step_pick_fields()

        eui   = self._data[CONF_EUI]
        topic = self._data[CONF_TOPIC].replace("<EUI>", eui)

        return self.async_show_form(
            step_id="listen",
            data_schema=vol.Schema({
                vol.Required("ready", default=False): bool,
            }),
            errors=errors,
            description_placeholders={
                "eui":   eui,
                "topic": topic,
            },
        )

    # ------------------------------------------------------------------
    # Step 3 — Pick fields from discovered paths
    # ------------------------------------------------------------------
    async def async_step_pick_fields(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Multi-select checkboxes of all discovered JSON paths."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get("fields", [])
            if not selected:
                errors["fields"] = "no_fields_selected"
            else:
                self._data["_selected_fields"] = selected
                return await self.async_step_assign_units()

        return self.async_show_form(
            step_id="pick_fields",
            data_schema=vol.Schema({
                vol.Required("fields"): SelectSelector(
                    SelectSelectorConfig(
                        options=self._discovered_paths,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }),
            errors=errors,
            description_placeholders={"count": str(len(self._discovered_paths))},
        )

    # ------------------------------------------------------------------
    # Step 4 — Assign units to selected fields
    # ------------------------------------------------------------------
    async def async_step_assign_units(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """One unit field per selected path, pre-populated with guesses."""
        selected = self._data.get("_selected_fields", [])
        errors: dict[str, str] = {}

        if user_input is not None:
            # Build the values dict from submitted units
            values = {
                path: user_input.get(f"unit_{i}", "")
                for i, path in enumerate(selected)
            }
            self._data[CONF_VALUES] = values
            self._data.pop("_selected_fields", None)
            return await self.async_step_selects()

        # Build schema with one unit field per selected path,
        # pre-populated with guessed units
        schema_dict = {}
        for i, path in enumerate(selected):
            guess = _guess_unit(path)
            field_name = path.split("/")[-1]
            schema_dict[
                vol.Optional(f"unit_{i}", default=guess, description={"suggested_value": guess})
            ] = str

        return self.async_show_form(
            step_id="assign_units",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "fields": "\n".join(
                    f"{path.split('/')[-1]} ({path})" for path in selected
                )
            },
        )

    # ------------------------------------------------------------------
    # Step 5 — Select entities (mode commands)
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
                eui = self._data[CONF_EUI]
                return self.async_create_entry(
                    title=f"TTN — {eui}",
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
                vol.Optional(CONF_SELECTS, default=default_selects): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
                ),
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
                vol.Required(CONF_TOPIC,  default=current.get(CONF_TOPIC, "")): str,
                vol.Required(CONF_VALUES, default=_values_to_text(current.get(CONF_VALUES, {}))): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
                ),
            }),
            errors=errors,
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
                ): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
                ),
            }),
            errors=errors,
        )
