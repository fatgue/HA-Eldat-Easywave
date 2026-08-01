"""The ELDAT Easywave integration.

Talks to an ELDAT Easywave USB transceiver through the companion bridge add-on,
which owns the hardware and exposes the stick as a TCP stream. That split keeps
this integration free of third-party requirements and, more importantly, makes it
work on Home Assistant OS at all: the Linux ``cp210x`` driver does not recognise
every ELDAT product id, and the Home Assistant container ships neither libusb nor
a way to add udev rules.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry

from .const import (
    ATTR_KEY,
    ATTR_ON,
    ATTR_POSITION,
    DOMAIN,
    KEYS,
    MANUFACTURER,
    SERVICE_SEND_TELEGRAM,
    SERVICE_SET_LED,
)
from .eldat.protocol import EldatError
from .hub import EldatHub

_LOGGER = logging.getLogger(__name__)

type EldatConfigEntry = ConfigEntry[EldatHub]

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.COVER,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.SWITCH,
]

_SEND_TELEGRAM_SCHEMA = vol.Schema(
    {
        # 0-based; the stick rejects anything past its own count, and a
        # permissive bound here keeps larger future firmwares working.
        vol.Required(ATTR_POSITION): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Required(ATTR_KEY): vol.In(KEYS),
    }
)

_SET_LED_SCHEMA = vol.Schema({vol.Required(ATTR_ON): cv.boolean})


async def async_setup_entry(hass: HomeAssistant, entry: EldatConfigEntry) -> bool:
    """Connect to the bridge and set up the platforms."""
    hub = EldatHub(hass, entry.entry_id, entry.data)
    try:
        await hub.async_setup()
    except EldatError as err:
        raise ConfigEntryNotReady(
            f"cannot reach the Easywave transceiver ({hub.unique_id}): {err}"
        ) from err

    entry.runtime_data = hub
    _async_register_hub_device(hass, entry, hub)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EldatConfigEntry) -> bool:
    """Tear down platforms and drop the connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: EldatConfigEntry) -> None:
    """Reload so that added or removed devices take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_hub_device(
    hass: HomeAssistant, entry: EldatConfigEntry, hub: EldatHub
) -> None:
    """Register the transceiver itself, so devices can hang off it."""
    identification = hub.identification
    device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, hub.unique_id)},
        manufacturer=MANUFACTURER,
        name=entry.title,
        model=(
            hub.info.fields[0] if hub.info and hub.info.fields else "Easywave transceiver"
        ),
        sw_version=identification.version_string if identification else None,
        # Where the bridge is, rather than a USB path: the integration never
        # touches the hardware itself.
        configuration_url=None,
    )


def _hubs(hass: HomeAssistant) -> list[EldatHub]:
    """Every connected hub."""
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        if getattr(entry, "runtime_data", None) is not None
    ]


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the manual-control services once.

    ``send_telegram`` is what makes pairing possible at all on sticks whose
    firmware lacks ``RDP?``: with no way to read a position's serial number, the
    only route is to put the receiver into learning mode and fire a telegram from
    a chosen position.
    """
    if hass.services.has_service(DOMAIN, SERVICE_SEND_TELEGRAM):
        return

    async def handle_send_telegram(call: ServiceCall) -> None:
        hubs = _hubs(call.hass)
        if not hubs:
            raise HomeAssistantError("no Easywave bridge is connected")
        for hub in hubs:
            try:
                await hub.async_transmit(call.data[ATTR_POSITION], call.data[ATTR_KEY])
            except EldatError as err:
                raise HomeAssistantError(f"cannot send telegram: {err}") from err

    async def handle_set_led(call: ServiceCall) -> None:
        hubs = _hubs(call.hass)
        if not hubs:
            raise HomeAssistantError("no Easywave bridge is connected")
        for hub in hubs:
            try:
                await hub.async_set_led(call.data[ATTR_ON])
            except EldatError as err:
                raise HomeAssistantError(f"cannot set the LED: {err}") from err

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_TELEGRAM, handle_send_telegram, schema=_SEND_TELEGRAM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_LED, handle_set_led, schema=_SET_LED_SCHEMA
    )
