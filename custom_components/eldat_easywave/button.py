"""Button platform: fire a single Easywave key code.

Useful both for scene-style control and for teaching a receiver, since pressing
the button emits exactly the telegram the receiver needs to learn.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EldatConfigEntry
from .const import CONF_KEY, CONF_POSITION, DEFAULT_KEY_ON, SUBENTRY_BUTTON
from .entity import EldatTransmitEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EldatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_BUTTON:
            continue
        async_add_entities(
            [EldatButton(hub, subentry)], config_subentry_id=subentry.subentry_id
        )


class EldatButton(EldatTransmitEntity, ButtonEntity):
    """Sends one telegram when pressed."""

    # A button has no state to assume.
    _attr_assumed_state = False

    def __init__(self, hub, subentry) -> None:
        super().__init__(hub, subentry, subentry.data[CONF_POSITION])
        self._key = subentry.data.get(CONF_KEY, DEFAULT_KEY_ON)

    async def async_press(self) -> None:
        await self._async_send(self._key)
