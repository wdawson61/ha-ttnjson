"""The TTN JSON integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import CONF_EUI, CONF_SELECTS, CONF_TOPIC, CONF_VALUES, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "select", "button"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Import existing configuration.yaml entries into config entries."""
    if DOMAIN not in config:
        return True

    configured_euis = {
        entry.data[CONF_EUI]
        for entry in hass.config_entries.async_entries(DOMAIN)
    }

    for platform_config in config.get(DOMAIN, []):
        values = platform_config.get(CONF_VALUES)
        if isinstance(values, list) and values:
            values = values[0]

        eui = platform_config[CONF_EUI]
        if eui in configured_euis:
            _LOGGER.debug(
                "TTN JSON: skipping import for '%s' — already configured", eui
            )
            continue

        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "import"},
                data={
                    CONF_EUI:     eui,
                    CONF_TOPIC:   platform_config[CONF_TOPIC],
                    CONF_VALUES:  values,
                    CONF_SELECTS: platform_config.get(CONF_SELECTS, []),
                },
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a TTN JSON device from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)
