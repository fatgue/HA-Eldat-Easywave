"""Event platform: Easywave hand-held and wall transmitters.

The ``event`` platform fits a transmitter far better than a binary sensor: a key
press is a moment, not a state, and Home Assistant's event entities carry both
the key and the kind of press for use in automations.

One entity is created per key code, so a four-key transmitter becomes four
entities that can be wired up independently.
"""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EldatConfigEntry
from .const import ATTR_REPEATS, ATTR_RSSI, KEYS, SUBENTRY_TRANSMITTER
from .eldat.telegrams import Action, TelegramEvent
from .entity import EldatReceiveEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EldatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TRANSMITTER:
            continue
        async_add_entities(
            [EldatTransmitterKey(hub, subentry, key) for key in KEYS],
            config_subentry_id=subentry.subentry_id,
        )


class EldatTransmitterKey(EldatReceiveEntity, EventEntity):
    """One key of one Easywave transmitter."""

    _attr_name = None
    _attr_event_types = [str(action) for action in Action]

    def __init__(self, hub, subentry, key: str) -> None:
        super().__init__(hub, subentry)
        self._key = key
        self._attr_translation_key = "transmitter_key"
        self._attr_translation_placeholders = {"key": key}
        self._attr_unique_id = f"{subentry.subentry_id}_{key.lower()}"
        # Distinguishes the four keys in the UI without repeating the device name.
        self._attr_name = f"Key {key}"

    @property
    def available(self) -> bool:
        """Silence is normal for a battery transmitter; only the hub matters."""
        return self._hub.available

    @callback
    def _process_telegram(self, event: TelegramEvent) -> None:
        if event.key != self._key:
            return
        self._trigger_event(
            str(event.action),
            {ATTR_RSSI: event.rssi, ATTR_REPEATS: event.repeats},
        )
        self.async_write_ha_state()
