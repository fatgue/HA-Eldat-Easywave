"""Diagnostics for the ELDAT Easywave integration.

Deliberately verbose about the firmware, because ELDAT ships several OEM builds
whose command sets differ -- ``RDP?`` is missing on some of them, and the ``REC``
telegram layout varies. Knowing exactly which firmware answered is usually the
fastest route to understanding a bug report.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import EldatConfigEntry
from .const import CONF_CONNECTION, CONNECTION_TCP


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EldatConfigEntry
) -> dict[str, Any]:
    hub = entry.runtime_data
    identification = hub.identification

    return {
        "connection": {
            "type": entry.data.get(CONF_CONNECTION, CONNECTION_TCP),
            "identity": hub.unique_id,
            "available": hub.available,
        },
        "transceiver": {
            "usb_ids": (
                f"{identification.vendor_id:04X}:{identification.product_id:04X}"
                if identification
                else None
            ),
            "firmware": identification.version_string if identification else None,
            "info": hub.info.text if hub.info else None,
            "mode": hub.mode,
            "transmit_positions": hub.position_count,
        },
        "transmitters_heard": [
            {
                "address": address,
                "last_key": event.key,
                "last_action": str(event.action),
                "rssi": event.rssi,
            }
            for address, event in reversed(list(hub.seen.items()))
        ],
        "configured_devices": [
            {
                "type": subentry.subentry_type,
                "title": subentry.title,
                "data": dict(subentry.data),
            }
            for subentry in entry.subentries.values()
        ],
    }
