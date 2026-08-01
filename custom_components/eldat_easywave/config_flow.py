"""Config and subentry flows.

The hub entry points at the bridge add-on. Each Easywave device is then a
subentry, because one stick fronts an arbitrary number of independent devices.

Two facts from the hardware shape these forms:

* **Transmitting is done by position, not by address.** The stick holds a fixed
  set of 64 or 128 burned-in serial numbers; you pick one and teach it to the
  receiver. So outgoing devices are configured with a position number.
* **Addresses cannot be enumerated.** ``RDP?`` is missing on some firmwares, so
  incoming devices are configured by picking from what has actually been heard on
  the air, with manual entry as a fallback.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ADDRESS,
    CONF_CONNECTION,
    CONF_DEVICE,
    CONF_DEVICE_CLASS,
    CONF_HOST,
    CONF_KEY,
    CONF_KEY_CLOSE,
    CONF_KEY_OFF,
    CONF_KEY_ON,
    CONF_KEY_OPEN,
    CONF_KEY_STATE_OFF,
    CONF_KEY_STATE_ON,
    CONF_KEY_STOP,
    CONF_NAME,
    CONF_PORT,
    CONF_POSITION,
    CONF_SERIAL,
    CONNECTION_LOCAL,
    CONNECTION_TCP,
    DEFAULT_HOST,
    DEFAULT_KEY_CLOSE,
    DEFAULT_KEY_OFF,
    DEFAULT_KEY_ON,
    DEFAULT_KEY_OPEN,
    DEFAULT_KEY_STOP,
    DEFAULT_PORT,
    DOMAIN,
    FALLBACK_POSITION_COUNT,
    FIRST_POSITION,
    KEYS,
    SUBENTRY_BUTTON,
    SUBENTRY_CONTACT,
    SUBENTRY_COVER,
    SUBENTRY_LIGHT,
    SUBENTRY_SWITCH,
    SUBENTRY_TRANSMITTER,
)
from .eldat.hardware import describe_product
from .eldat.parser import normalise_address
from .eldat.protocol import EldatError, connect_tcp

_LOGGER = logging.getLogger(__name__)

_MANUAL_ADDRESS = "__manual__"

_CONTACT_DEVICE_CLASSES = ("window", "door", "garage_door", "opening", "motion", "smoke")


def _key_selector() -> selector.Selector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(KEYS), mode=selector.SelectSelectorMode.DROPDOWN
        )
    )


def _position_selector(count: int) -> selector.Selector:
    """Offer exactly the positions the stick accepts.

    Positions are 0-based, so a stick reporting ``count`` positions accepts
    ``0`` to ``count - 1``.
    """
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=FIRST_POSITION,
            max=FIRST_POSITION + count - 1,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


class EldatConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the connection to the bridge."""

    VERSION = 1

    def __init__(self) -> None:
        self._local_devices: list = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prefer a stick attached to this machine, and only ask otherwise.

        Most installations have the transceiver plugged into the machine running
        Home Assistant, and the integration can drive it directly. Asking for a
        host and port first would make the common case look harder than it is.
        """
        self._local_devices = await _async_find_local(self.hass)
        if self._local_devices:
            return await self.async_step_pick_device()
        return await self.async_step_bridge()

    async def async_step_pick_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose among the attached sticks, or fall back to a bridge."""
        errors: dict[str, str] = {}
        if user_input is not None:
            choice = user_input[CONF_DEVICE]
            if choice == CONNECTION_TCP:
                return await self.async_step_bridge()

            device = next(
                (d for d in self._local_devices if _device_key(d) == choice), None
            )
            if device is None:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(
                    f"usb:{device.serial}" if device.serial else f"usb:{choice}"
                )
                self._abort_if_unique_id_configured()
                description = await _async_probe_local(device)
                if description is None:
                    errors["base"] = "cannot_connect_usb"
                else:
                    return self.async_create_entry(
                        title=description,
                        data={
                            CONF_CONNECTION: CONNECTION_LOCAL,
                            CONF_DEVICE: _device_key(device),
                            CONF_SERIAL: device.serial,
                        },
                    )

        options = [
            selector.SelectOptionDict(
                value=_device_key(device),
                label=(
                    f"{describe_product(device.product_id)} "
                    f"({device.usb_ids}, serial {device.serial or 'unreadable'})"
                ),
            )
            for device in self._local_devices
        ]
        options.append(
            selector.SelectOptionDict(
                value=CONNECTION_TCP, label="Connect to a bridge over the network"
            )
        )
        return self.async_show_form(
            step_id="pick_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE, default=options[0]["value"]): (
                        selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=options,
                                mode=selector.SelectSelectorMode.LIST,
                            )
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_bridge(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input[CONF_PORT])
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            description = await _async_probe(host, port)
            if description is None:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=description,
                    data={
                        CONF_CONNECTION: CONNECTION_TCP,
                        CONF_HOST: host,
                        CONF_PORT: port,
                    },
                )

        return self.async_show_form(
            step_id="bridge",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
                        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
                    }
                ),
                user_input,
            ),
            errors=errors,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Device types that can be added under a stick."""
        return {
            SUBENTRY_COVER: CoverSubentryFlow,
            SUBENTRY_SWITCH: SwitchSubentryFlow,
            SUBENTRY_LIGHT: LightSubentryFlow,
            SUBENTRY_BUTTON: ButtonSubentryFlow,
            SUBENTRY_CONTACT: ContactSubentryFlow,
            SUBENTRY_TRANSMITTER: TransmitterSubentryFlow,
        }


def _device_key(device) -> str:
    """Identify a device across re-enumeration: serial if it has one, else path."""
    return device.serial or f"{device.bus:03d}/{device.address:03d}"


async def _async_find_local(hass) -> list:
    """Sticks attached to this machine. Never raises -- absence is normal."""
    from .eldat.usb_transport import find_local_devices
    from .eldat.usbfs import UsbfsError

    try:
        return await hass.async_add_executor_job(find_local_devices)
    except UsbfsError as err:
        _LOGGER.debug("local USB is not usable here: %s", err)
        return []


async def _async_probe_local(device) -> str | None:
    """Open a local stick and read its identity, then let it go."""
    from .eldat.protocol import connect_local

    try:
        client, connection = await connect_local(device)
    except Exception as err:  # usbfs and protocol errors alike
        _LOGGER.debug("cannot open %s: %s", device.node, err)
        return None
    try:
        identification = await client.identify()
        info = await client.info()
    except EldatError as err:
        _LOGGER.debug("opened %s but got no identification: %s", device.node, err)
        return None
    finally:
        await client.close()
        await connection.close()

    if info and info.fields:
        return f"Easywave {info.fields[0]}"
    return f"Easywave {identification.vendor_id:04X}:{identification.product_id:04X}"


async def _async_probe(host: str, port: int) -> str | None:
    """Connect and identify the stick; returns a title or ``None`` on failure."""
    try:
        client = await connect_tcp(host, port)
    except EldatError as err:
        _LOGGER.debug("cannot connect to %s:%s: %s", host, port, err)
        return None
    try:
        identification = await client.identify()
        info = await client.info()
    except EldatError as err:
        _LOGGER.debug("connected to %s:%s but no identification: %s", host, port, err)
        return None
    finally:
        await client.close()

    if info and info.fields:
        # e.g. "RX09 EW+KEELOQ" -- much more informative than the USB ids.
        return f"Easywave {info.fields[0]}"
    return f"Easywave {identification.vendor_id:04X}:{identification.product_id:04X}"


class _EldatSubentryFlow(ConfigSubentryFlow):
    """Shared helpers for the device forms."""

    @property
    def _hub(self):
        return getattr(self._get_entry(), "runtime_data", None)

    @property
    def _position_count(self) -> int:
        hub = self._hub
        if hub is not None and hub.position_count:
            return hub.position_count
        return FALLBACK_POSITION_COUNT

    def _address_options(self) -> list[selector.SelectOptionDict]:
        """Transmitters heard so far, newest first, plus a manual option."""
        hub = self._hub
        options: list[selector.SelectOptionDict] = []
        if hub is not None:
            for address, event in reversed(list(hub.seen.items())):
                label = f"{address} (key {event.key}"
                if event.rssi is not None:
                    label += f", {event.rssi} dBm"
                label += ")"
                options.append(selector.SelectOptionDict(value=address, label=label))
        options.append(
            selector.SelectOptionDict(value=_MANUAL_ADDRESS, label="Enter manually")
        )
        return options

    def _receive_schema(self, extra: dict) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_ADDRESS): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=self._address_options(),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                    )
                ),
                **extra,
            }
        )

    def _resolve_address(self, user_input: dict[str, Any]) -> str | None:
        """Normalise the chosen address, rejecting the placeholder."""
        raw = str(user_input.get(CONF_ADDRESS, "")).strip()
        if not raw or raw == _MANUAL_ADDRESS:
            return None
        return normalise_address(raw)

    def _transmit_entry(
        self, user_input: dict[str, Any], keys: dict[str, str]
    ) -> SubentryFlowResult:
        data = {CONF_POSITION: int(user_input[CONF_POSITION]), **keys}
        return self.async_create_entry(title=user_input[CONF_NAME], data=data)


class CoverSubentryFlow(_EldatSubentryFlow):
    """A roller shutter or blind."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return self._transmit_entry(
                user_input,
                {
                    CONF_KEY_OPEN: user_input[CONF_KEY_OPEN],
                    CONF_KEY_CLOSE: user_input[CONF_KEY_CLOSE],
                    CONF_KEY_STOP: user_input[CONF_KEY_STOP],
                },
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): str,
                    vol.Required(
                        CONF_POSITION, default=FIRST_POSITION
                    ): _position_selector(self._position_count),
                    vol.Required(
                        CONF_KEY_OPEN, default=DEFAULT_KEY_OPEN
                    ): _key_selector(),
                    vol.Required(
                        CONF_KEY_CLOSE, default=DEFAULT_KEY_CLOSE
                    ): _key_selector(),
                    vol.Required(
                        CONF_KEY_STOP, default=DEFAULT_KEY_STOP
                    ): _key_selector(),
                }
            ),
        )


class _OnOffSubentryFlow(_EldatSubentryFlow):
    """Shared form for switch and light."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return self._transmit_entry(
                user_input,
                {
                    CONF_KEY_ON: user_input[CONF_KEY_ON],
                    CONF_KEY_OFF: user_input[CONF_KEY_OFF],
                },
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): str,
                    vol.Required(
                        CONF_POSITION, default=FIRST_POSITION
                    ): _position_selector(self._position_count),
                    vol.Required(CONF_KEY_ON, default=DEFAULT_KEY_ON): _key_selector(),
                    vol.Required(CONF_KEY_OFF, default=DEFAULT_KEY_OFF): _key_selector(),
                }
            ),
        )


class SwitchSubentryFlow(_OnOffSubentryFlow):
    """A switching actuator."""


class LightSubentryFlow(_OnOffSubentryFlow):
    """A light."""


class ButtonSubentryFlow(_EldatSubentryFlow):
    """A single key press."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return self._transmit_entry(user_input, {CONF_KEY: user_input[CONF_KEY]})
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): str,
                    vol.Required(
                        CONF_POSITION, default=FIRST_POSITION
                    ): _position_selector(self._position_count),
                    vol.Required(CONF_KEY, default=DEFAULT_KEY_ON): _key_selector(),
                }
            ),
        )


class ContactSubentryFlow(_EldatSubentryFlow):
    """A contact whose two key codes carry a state.

    Defaults match the ELDAT RTS16 window contact in EIN/AUS mode: code A when
    the contact opens, code B when it closes.
    """

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            address = self._resolve_address(user_input)
            if address is None:
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        CONF_ADDRESS: address,
                        CONF_KEY_STATE_ON: user_input[CONF_KEY_STATE_ON],
                        CONF_KEY_STATE_OFF: user_input[CONF_KEY_STATE_OFF],
                        CONF_DEVICE_CLASS: user_input[CONF_DEVICE_CLASS],
                    },
                    unique_id=f"contact_{address}",
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self._receive_schema(
                {
                    vol.Required(
                        CONF_KEY_STATE_ON, default=DEFAULT_KEY_OPEN
                    ): _key_selector(),
                    vol.Required(
                        CONF_KEY_STATE_OFF, default=DEFAULT_KEY_CLOSE
                    ): _key_selector(),
                    vol.Required(CONF_DEVICE_CLASS, default="window"): (
                        selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=list(_CONTACT_DEVICE_CLASSES),
                                mode=selector.SelectSelectorMode.DROPDOWN,
                                translation_key="contact_device_class",
                            )
                        )
                    ),
                }
            ),
            errors=errors,
        )


class TransmitterSubentryFlow(_EldatSubentryFlow):
    """A hand-held or wall transmitter, exposed as event entities."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            address = self._resolve_address(user_input)
            if address is None:
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={CONF_ADDRESS: address},
                    unique_id=f"transmitter_{address}",
                )

        return self.async_show_form(
            step_id="user", data_schema=self._receive_schema({}), errors=errors
        )
