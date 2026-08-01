"""Binary sensor platform: Easywave contacts and detectors.

Built for transmitters that encode a state in two different key codes. The ELDAT
RTS16 window contact is the reference case: per its manual, the EIN/AUS variant
sends Easywave code A when the contact opens and code B when it closes.

State is restored across restarts on purpose. The RTS16E5001B01 has no periodic
status telegram -- only the STATUS variants re-send every 24 hours -- so a
restart would otherwise leave the sensor unknown until the window is next moved.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import EldatConfigEntry
from .const import (
    ATTR_RSSI,
    CONF_DEVICE_CLASS,
    CONF_KEY_STATE_OFF,
    CONF_KEY_STATE_ON,
    DEFAULT_KEY_CLOSE,
    DEFAULT_KEY_OPEN,
    SUBENTRY_CONTACT,
)
from .eldat.telegrams import TelegramEvent
from .entity import EldatReceiveEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EldatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_CONTACT:
            continue
        async_add_entities(
            [EldatContact(hub, subentry)], config_subentry_id=subentry.subentry_id
        )


class EldatContact(EldatReceiveEntity, BinarySensorEntity, RestoreEntity):
    """A contact whose two key codes mean 'on' and 'off'."""

    def __init__(self, hub, subentry) -> None:
        super().__init__(hub, subentry)
        data = subentry.data
        self._key_on = data.get(CONF_KEY_STATE_ON, DEFAULT_KEY_OPEN)
        self._key_off = data.get(CONF_KEY_STATE_OFF, DEFAULT_KEY_CLOSE)
        if device_class := data.get(CONF_DEVICE_CLASS):
            self._attr_device_class = BinarySensorDeviceClass(device_class)
        self._attr_is_on: bool | None = None
        self._rssi: int | None = None

    @property
    def available(self) -> bool:
        """A battery transmitter is not reachable, so only the hub matters.

        Reporting unavailable when the transmitter is merely quiet would be
        wrong -- silence is its normal state.
        """
        return self._hub.available

    @property
    def extra_state_attributes(self) -> dict[str, int | None] | None:
        if self._rssi is None:
            return None
        return {ATTR_RSSI: self._rssi}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (
            self._attr_is_on is None
            and (last := await self.async_get_last_state())
            and last.state in ("on", "off")
        ):
            self._attr_is_on = last.state == "on"

    @callback
    def _process_telegram(self, event: TelegramEvent) -> None:
        if event.key == self._key_on:
            self._attr_is_on = True
        elif event.key == self._key_off:
            self._attr_is_on = False
        else:
            # Another key from the same transmitter, e.g. a second channel.
            return
        self._rssi = event.rssi
        self.async_write_ha_state()
