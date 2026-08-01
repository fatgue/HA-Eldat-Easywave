"""Cover platform: Easywave roller shutter and blind actuators."""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EldatConfigEntry
from .const import (
    CONF_KEY_CLOSE,
    CONF_KEY_OPEN,
    CONF_KEY_STOP,
    CONF_POSITION,
    DEFAULT_KEY_CLOSE,
    DEFAULT_KEY_OPEN,
    DEFAULT_KEY_STOP,
    SUBENTRY_COVER,
)
from .entity import EldatTransmitEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EldatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one cover per configured cover subentry."""
    hub = entry.runtime_data
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_COVER:
            continue
        async_add_entities(
            [EldatCover(hub, subentry)], config_subentry_id=subentry.subentry_id
        )


class EldatCover(EldatTransmitEntity, CoverEntity):
    """A roller shutter driven by three Easywave key codes.

    Position feedback does not exist on Easywave, so no position attribute is
    offered -- only open, close and stop.
    """

    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )

    def __init__(self, hub, subentry) -> None:
        super().__init__(hub, subentry, subentry.data[CONF_POSITION])
        data = subentry.data
        self._key_open = data.get(CONF_KEY_OPEN, DEFAULT_KEY_OPEN)
        self._key_close = data.get(CONF_KEY_CLOSE, DEFAULT_KEY_CLOSE)
        self._key_stop = data.get(CONF_KEY_STOP, DEFAULT_KEY_STOP)
        # Unknown until we are told to move: the shutter's real position is
        # simply not observable.
        self._attr_is_closed: bool | None = None

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_send(self._key_open)
        self._attr_is_closed = False
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_send(self._key_close)
        self._attr_is_closed = True
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._async_send(self._key_stop)
        # After a stop the shutter is somewhere in between; claiming either
        # extreme would be a lie.
        self._attr_is_closed = None
        self.async_write_ha_state()
