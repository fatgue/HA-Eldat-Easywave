"""Userspace CP210x driver for the ELDAT Easywave transceiver.

Why this exists: the ELDAT transceivers use Silicon Labs CP210x silicon behind
ELDAT's own USB IDs (vendor ``0x155A``), and the Linux ``cp210x`` driver only
lists ``0x1006``. A stick reporting any other product id -- ``0x100E`` on the
unit this was developed against -- never gets a ``/dev/ttyUSB*`` node, and on
Home Assistant OS there is no supported way to add a udev rule. Driving the chip
directly from userspace sidesteps the kernel entirely.

Register numbers and bit patterns are taken from the kernel driver
(``drivers/usb/serial/cp210x.c``) so the initialisation sequence matches what
the in-tree driver would do.

The API is deliberately blocking; :mod:`.server` pumps it from threads.
"""

from __future__ import annotations

import contextlib
import logging
import os
import struct
import threading
from typing import Final

import usb.core
import usb.util
from usb.backend import libusb1

_LOGGER = logging.getLogger(__name__)

#: ELDAT's USB vendor id. The device strings still say "Silicon Labs".
ELDAT_VENDOR_ID: Final = 0x155A

#: Product ids claimed by ELDAT's own Windows driver (utcvx-ew.inf). The kernel
#: knows only 0x1006, so every other id here needs this driver or a new_id bind.
ELDAT_PRODUCT_IDS: Final = tuple(range(0x1005, 0x1014))

#: Model names, straight from ELDAT's driver .inf, for nicer logs.
ELDAT_PRODUCT_NAMES: Final = {
    0x1005: "Easywave Transceiver",
    0x1006: "USB Transceiver Easywave (RX09)",
    0x1007: "USB Transceiver Tester 868MHz",
    0x1008: "USB Transceiver Tester 433MHz",
    0x1009: "USB Transceiver Easywave V2",
    0x100A: "USB Transceiver Easywave V3",
    0x100B: "USB Transceiver Easywave V4",
    0x100C: "USB Transceiver Easywave V5",
    0x100D: "USB Transceiver Easywave V6",
    0x100E: "ELDAT USB Device V1",
    0x100F: "ELDAT USB Device V2",
    0x1010: "ELDAT USB Device V3",
    0x1011: "ELDAT USB Device V4",
    0x1012: "ELDAT USB Device V5",
    0x1013: "ELDAT USB Device V6",
}

# --- CP210x vendor requests, from cp210x.c ---
_REQTYPE_HOST_TO_DEVICE: Final = 0x41
_IFC_ENABLE: Final = 0x00
_SET_LINE_CTL: Final = 0x03
_SET_MHS: Final = 0x07
_SET_BAUDRATE: Final = 0x1E

_UART_ENABLE: Final = 0x0001
_UART_DISABLE: Final = 0x0000

#: BITS_DATA_8 | BITS_PARITY_NONE | BITS_STOP_1 -- the 8N1 the stick requires.
_LINE_CTL_8N1: Final = 0x0800

#: CONTROL_DTR | CONTROL_RTS | CONTROL_WRITE_DTR | CONTROL_WRITE_RTS
_MHS_DTR_RTS_ON: Final = 0x0303

#: Fixed by the ELDAT specification: 57600 8N1, no flow control.
BAUDRATE: Final = 57600


class Cp210xError(Exception):
    """Raised when the device cannot be opened or used."""


#: Where libusb actually lives, when the loader cannot be asked. Alpine (musl)
#: has no ldconfig cache, so ``ctypes.util.find_library`` regularly comes back
#: empty even though the library is installed -- and Alpine is exactly what the
#: add-on runs on.
_LIBUSB_CANDIDATES: Final = (
    "/usr/lib/libusb-1.0.so.0",  # Alpine, and most Linux distributions
    "/usr/lib/x86_64-linux-gnu/libusb-1.0.so.0",  # Debian/Ubuntu amd64
    "/usr/lib/aarch64-linux-gnu/libusb-1.0.so.0",  # Debian/Ubuntu arm64
    "/usr/local/opt/libusb/lib/libusb-1.0.dylib",  # Homebrew on macOS
    "/opt/homebrew/opt/libusb/lib/libusb-1.0.dylib",
)


def _backend():
    """Resolve a libusb backend.

    Tries, in order: an explicitly configured path, pyusb's own library search,
    then a list of known locations.
    """
    found: str | None = None

    if path := os.environ.get("ELDAT_LIBUSB_PATH"):
        found = path if os.path.exists(path) else None
        if (backend := libusb1.get_backend(find_library=lambda _: path)) is not None:
            return backend
        raise Cp210xError(_diagnose_backend_failure(found))

    if (backend := libusb1.get_backend()) is not None:
        return backend

    for candidate in _LIBUSB_CANDIDATES:
        if not os.path.exists(candidate):
            continue
        found = candidate
        if (
            backend := libusb1.get_backend(find_library=lambda _, c=candidate: c)
        ) is not None:
            _LOGGER.debug("loaded libusb from %s", candidate)
            return backend

    raise Cp210xError(_diagnose_backend_failure(found))


def _diagnose_backend_failure(found: str | None) -> str:
    """Explain *why* no backend could be built.

    pyusb collapses two very different problems into a ``None`` backend: the
    library being absent, and the library loading fine but ``libusb_init()``
    failing afterwards. The second happens whenever the process cannot reach the
    USB subsystem at all -- for an add-on, that means it was not granted USB
    access. Reporting that as "libusb not available" sends people off to install
    a library that is already there.
    """
    if found is None:
        return (
            "libusb is not installed, or sits somewhere unexpected -- "
            "point ELDAT_LIBUSB_PATH at it"
        )
    if not os.path.isdir("/dev/bus/usb"):
        return (
            f"libusb loaded from {found} but cannot reach the USB subsystem: "
            "/dev/bus/usb is missing. A Home Assistant add-on needs 'usb: true' "
            "and 'full_access: true' for that, and the transceiver has to be "
            "plugged into the host."
        )
    return (
        f"libusb loaded from {found} but libusb_init() failed. Check that this "
        "process is allowed to access /dev/bus/usb."
    )


def find_devices(
    product_ids: tuple[int, ...] = ELDAT_PRODUCT_IDS,
) -> list[usb.core.Device]:
    """All attached ELDAT transceivers, in bus order."""
    found = usb.core.find(find_all=True, idVendor=ELDAT_VENDOR_ID, backend=_backend())
    return [device for device in found if device.idProduct in product_ids]


def describe(device: usb.core.Device) -> str:
    name = ELDAT_PRODUCT_NAMES.get(device.idProduct, "unknown ELDAT device")
    serial = device.serial_number or "?"
    return f"{device.idVendor:04X}:{device.idProduct:04X} {name} (serial {serial})"


class Cp210xDevice:
    """A CP210x-based ELDAT stick, opened without kernel involvement.

    Blocking, and safe to use from two threads at once (one reading, one
    writing) -- reads and writes go to separate endpoints, and control transfers
    only happen during open/close.
    """

    def __init__(self, device: usb.core.Device) -> None:
        self._device = device
        self._interface_number = 0
        self._ep_in = None
        self._ep_out = None
        self._open = False
        self._write_lock = threading.Lock()

    @property
    def description(self) -> str:
        return describe(self._device)

    @property
    def serial_number(self) -> str | None:
        return self._device.serial_number

    def open(self) -> None:
        if self._open:
            return
        try:
            configuration = self._device.get_active_configuration()
        except usb.core.USBError as err:
            raise Cp210xError(f"cannot read configuration: {err}") from err

        interface = configuration[(0, 0)]
        self._interface_number = interface.bInterfaceNumber
        self._ep_out = usb.util.find_descriptor(
            interface,
            custom_match=lambda ep: (
                usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT
            ),
        )
        self._ep_in = usb.util.find_descriptor(
            interface,
            custom_match=lambda ep: (
                usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN
            ),
        )
        if self._ep_in is None or self._ep_out is None:
            raise Cp210xError("device does not expose the expected bulk endpoints")

        try:
            usb.util.claim_interface(self._device, self._interface_number)
            self._control(_IFC_ENABLE, _UART_ENABLE)
            self._control(_SET_BAUDRATE, 0, struct.pack("<I", BAUDRATE))
            self._control(_SET_LINE_CTL, _LINE_CTL_8N1)
            self._control(_SET_MHS, _MHS_DTR_RTS_ON)
        except usb.core.USBError as err:
            raise Cp210xError(f"cannot initialise UART: {err}") from err

        self._open = True
        _LOGGER.info("opened %s at %d baud 8N1", self.description, BAUDRATE)

    def _control(self, request: int, value: int, data: bytes | int = 0) -> None:
        self._device.ctrl_transfer(
            _REQTYPE_HOST_TO_DEVICE, request, value, self._interface_number, data
        )

    def read(self, timeout_ms: int = 200) -> bytes:
        """Read whatever is available. Returns ``b''`` on timeout."""
        if not self._open:
            raise Cp210xError("device is not open")
        try:
            data = self._device.read(
                self._ep_in.bEndpointAddress,
                self._ep_in.wMaxPacketSize,
                timeout=timeout_ms,
            )
        except usb.core.USBTimeoutError:
            return b""
        except usb.core.USBError as err:
            # pyusb maps timeouts inconsistently across backends.
            if "timeout" in str(err).lower():
                return b""
            raise Cp210xError(f"read failed: {err}") from err
        return bytes(data)

    def write(self, data: bytes, timeout_ms: int = 1000) -> None:
        if not self._open:
            raise Cp210xError("device is not open")
        with self._write_lock:
            try:
                self._device.write(
                    self._ep_out.bEndpointAddress, data, timeout=timeout_ms
                )
            except usb.core.USBError as err:
                raise Cp210xError(f"write failed: {err}") from err

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        with contextlib.suppress(usb.core.USBError):
            self._control(_IFC_ENABLE, _UART_DISABLE)
        with contextlib.suppress(usb.core.USBError):
            usb.util.release_interface(self._device, self._interface_number)
        usb.util.dispose_resources(self._device)
        _LOGGER.info("closed %s", self.description)

    def __enter__(self) -> Cp210xDevice:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
