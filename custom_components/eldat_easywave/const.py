"""Constants for the ELDAT Easywave integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "eldat_easywave"

MANUFACTURER: Final = "ELDAT"

# --- hub configuration ---
CONF_CONNECTION: Final = "connection"
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_DEVICE: Final = "device"
CONF_SERIAL: Final = "serial"

#: The stick is attached to the machine running Home Assistant, and the
#: integration drives it itself through usbfs.
CONNECTION_LOCAL: Final = "local"

#: The stick is somewhere else, reachable as a TCP stream served by the bridge.
#: Required when Home Assistant runs in a virtual machine, because QEMU USB
#: passthrough does not carry the CP210x control transfers.
CONNECTION_TCP: Final = "tcp"

DEFAULT_PORT: Final = 5000

#: Gateway address of the Supervisor network. The bridge add-on publishes its
#: port on the host, so this is where the integration reaches it on Home
#: Assistant OS. Add-ons from custom repositories get a repository-derived
#: hostname rather than a predictable one, so an address is the better default.
DEFAULT_HOST: Final = "172.30.32.1"

# --- subentry types ---
SUBENTRY_COVER: Final = "cover"
SUBENTRY_SWITCH: Final = "switch"
SUBENTRY_LIGHT: Final = "light"
SUBENTRY_BUTTON: Final = "button"
SUBENTRY_CONTACT: Final = "contact"
SUBENTRY_TRANSMITTER: Final = "transmitter"

# --- device configuration keys ---
CONF_NAME: Final = "name"
CONF_POSITION: Final = "position"
CONF_ADDRESS: Final = "address"
CONF_DEVICE_CLASS: Final = "device_class"

CONF_KEY_OPEN: Final = "key_open"
CONF_KEY_CLOSE: Final = "key_close"
CONF_KEY_STOP: Final = "key_stop"
CONF_KEY_ON: Final = "key_on"
CONF_KEY_OFF: Final = "key_off"
CONF_KEY: Final = "key"
CONF_KEYS: Final = "keys"
CONF_KEY_STATE_ON: Final = "key_state_on"
CONF_KEY_STATE_OFF: Final = "key_state_off"

#: Easywave key codes a transmitter can send.
KEYS: Final = ("A", "B", "C", "D")

# Defaults follow ELDAT's own conventions: a two-key channel uses A/B, and the
# RTS16 window contact sends A when the contact opens and B when it closes.
DEFAULT_KEY_OPEN: Final = "A"
DEFAULT_KEY_CLOSE: Final = "B"
DEFAULT_KEY_STOP: Final = "C"
DEFAULT_KEY_ON: Final = "A"
DEFAULT_KEY_OFF: Final = "B"

#: Number of transmit positions if the stick will not say (GETP? failed).
FALLBACK_POSITION_COUNT: Final = 64

#: Transmit positions are addressed **0-based**, which the specification does not
#: state. ``GETP?`` reports a count, not a highest number: a stick answering
#: ``GETP,40`` (64) accepts ``TXP,00`` through ``TXP,3F`` and rejects ``TXP,40``.
#: Measured against the hardware -- offering 1..count would expose one position
#: the stick refuses and hide one that works.
FIRST_POSITION: Final = 0

# --- events and services ---
EVENT_TELEGRAM: Final = f"{DOMAIN}_telegram"

SERVICE_SEND_TELEGRAM: Final = "send_telegram"
SERVICE_SET_LED: Final = "set_led"
SERVICE_SEND_COMMAND: Final = "send_command"

ATTR_POSITION: Final = "position"
ATTR_KEY: Final = "key"
ATTR_ADDRESS: Final = "address"
ATTR_ACTION: Final = "action"
ATTR_RSSI: Final = "rssi"
ATTR_REPEATS: Final = "repeats"
ATTR_ON: Final = "on"
ATTR_COMMAND: Final = "command"
