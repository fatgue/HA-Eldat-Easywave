"""Light platform: Easywave receivers driving lighting.

Only on/off. Easywave gives no feedback, so a dimmer's brightness could never be
tracked -- offering a brightness slider would be a fiction.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EldatConfigEntry
from .const import (
    CONF_KEY_OFF,
    CONF_KEY_ON,
    CONF_POSITION,
    DEFAULT_KEY_OFF,
    DEFAULT_KEY_ON,
    SUBENTRY_LIGHT,
)
from .entity import EldatTransmitEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EldatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_LIGHT:
            continue
        async_add_entities(
            [EldatLight(hub, subentry)], config_subentry_id=subentry.subentry_id
        )


class EldatLight(EldatTransmitEntity, LightEntity):
    """An Easywave-switched light."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, hub, subentry) -> None:
        super().__init__(hub, subentry, subentry.data[CONF_POSITION])
        self._key_on = subentry.data.get(CONF_KEY_ON, DEFAULT_KEY_ON)
        self._key_off = subentry.data.get(CONF_KEY_OFF, DEFAULT_KEY_OFF)
        self._attr_is_on: bool | None = None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_send(self._key_on)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_send(self._key_off)
        self._attr_is_on = False
        self.async_write_ha_state()
