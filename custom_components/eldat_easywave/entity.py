"""Shared entity behaviour."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import CONF_ADDRESS, DOMAIN, MANUFACTURER
from .eldat.telegrams import TelegramEvent
from .hub import EldatHub


class EldatEntity(Entity):
    """Base class: one entity belonging to one configured device."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, hub: EldatHub, subentry: ConfigSubentry) -> None:
        self._hub = hub
        self._subentry = subentry
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer=MANUFACTURER,
            model=subentry.subentry_type,
            via_device=(DOMAIN, hub.unique_id),
        )

    @property
    def available(self) -> bool:
        return self._hub.available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._hub.add_availability_listener(self._handle_availability)
        )

    @callback
    def _handle_availability(self) -> None:
        self.async_write_ha_state()


class EldatTransmitEntity(EldatEntity):
    """An entity that controls an Easywave receiver.

    Easywave is one-way: a receiver never reports back. State is therefore
    optimistic, and ``assumed_state`` tells the frontend to offer explicit
    buttons instead of a toggle that pretends to know the truth.
    """

    _attr_assumed_state = True

    def __init__(self, hub: EldatHub, subentry: ConfigSubentry, position: int) -> None:
        super().__init__(hub, subentry)
        self._position = position

    async def _async_send(self, key: str) -> None:
        await self._hub.async_transmit(self._position, key)


class EldatReceiveEntity(EldatEntity):
    """An entity fed by telegrams from one transmitter address."""

    def __init__(self, hub: EldatHub, subentry: ConfigSubentry) -> None:
        super().__init__(hub, subentry)
        self._address = subentry.data[CONF_ADDRESS]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal_telegram, self._handle_telegram
            )
        )

    @callback
    def _handle_telegram(self, event: TelegramEvent) -> None:
        if event.address != self._address:
            return
        self._process_telegram(event)

    @callback
    def _process_telegram(self, event: TelegramEvent) -> None:
        raise NotImplementedError
