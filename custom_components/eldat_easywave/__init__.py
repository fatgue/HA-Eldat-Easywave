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
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry

from .const import (
    ATTR_COMMAND,
    ATTR_KEY,
    ATTR_ON,
    ATTR_POSITION,
    DOMAIN,
    KEYS,
    MANUFACTURER,
    SERVICE_SEND_COMMAND,
    SERVICE_SEND_TELEGRAM,
    SERVICE_SET_LED,
)
from .eldat.protocol import EldatCommandError, EldatError
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

_SEND_COMMAND_SCHEMA = vol.Schema({vol.Required(ATTR_COMMAND): cv.string})

#: Starting the bootloader leaves the stick answering nothing until it is
#: physically reset, so it is the one command this service will not pass on.
_REFUSED_COMMAND = "bootloader"


async def async_setup_entry(hass: HomeAssistant, entry: EldatConfigEntry) -> bool:
    """Connect to the bridge and set up the platforms."""
    hub = EldatHub(hass, entry.entry_id, entry.data, seen=_async_heard(hass, entry))
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


@callback
def _async_heard(hass: HomeAssistant, entry: EldatConfigEntry) -> dict:
    """The transmitters heard on this entry, kept across reloads.

    Adding a device reloads the entry, so a hub-owned list would be discarded
    the moment it is most useful -- when adding several devices in one sitting.
    """
    return (
        hass.data.setdefault(DOMAIN, {})
        .setdefault("heard", {})
        .setdefault(entry.entry_id, {})
    )


async def async_remove_entry(hass: HomeAssistant, entry: EldatConfigEntry) -> None:
    """Forget what this transceiver heard once it is gone for good."""
    hass.data.get(DOMAIN, {}).get("heard", {}).pop(entry.entry_id, None)


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

    async def handle_send_command(call: ServiceCall) -> ServiceResponse:
        command = str(call.data[ATTR_COMMAND]).strip()
        if command.lower().startswith(_REFUSED_COMMAND):
            raise HomeAssistantError(
                "refusing to start the bootloader: the transceiver would stop "
                "answering until it is physically unplugged"
            )
        hubs = _hubs(call.hass)
        if not hubs:
            raise HomeAssistantError("no Easywave transceiver is connected")
        try:
            response = await hubs[0].async_send_command(command)
        except EldatCommandError:
            # A bare ERROR is a legitimate answer when probing what an
            # undocumented firmware supports, not a failure of the call.
            return {"acknowledged": False, "response": "ERROR"}
        except EldatError as err:
            raise HomeAssistantError(f"cannot send '{command}': {err}") from err

        message = response.message
        return {
            "acknowledged": response.ack,
            "response": (
                None
                if message is None
                # Unknown payloads keep their raw text, which is the point here.
                else getattr(message, "payload", None) or str(message)
            ),
        }

    hass.services.async_register(
        DOMAIN, SERVICE_SET_LED, handle_set_led, schema=_SET_LED_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_COMMAND,
        handle_send_command,
        schema=_SEND_COMMAND_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
