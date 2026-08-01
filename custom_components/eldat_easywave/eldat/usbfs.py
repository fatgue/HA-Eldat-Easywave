"""Talk to the ELDAT transceiver through Linux usbfs, using only the standard library.

This exists so the integration can open the stick by itself, with no add-on and no
third-party packages. Neither of the obvious routes is available inside Home
Assistant:

* **A serial port.** The in-tree ``cp210x`` driver lists only ELDAT product id
  ``0x1006``, so most ELDAT sticks never get a ``/dev/ttyUSB*`` node.
* **libusb.** The Home Assistant container does not ship it, and an integration
  cannot install system libraries.

What the container *does* have is the raw device: Supervisor bind-mounts ``/dev``,
runs the container privileged, and grants it the USB device cgroup rules. Those
device nodes are driven by ioctls on ``/dev/bus/usb/<bus>/<address>`` -- which is
all libusb does underneath -- and ``fcntl.ioctl`` plus ``ctypes`` is enough to
issue them.

Definitions follow ``include/uapi/linux/usbdevice_fs.h``. Request numbers are
derived from the structure sizes rather than written out as constants, so they
cannot drift from the layouts above them.

Device discovery reads sysfs only. That matters: it yields the endpoints without a
single control transfer, and control transfers are exactly what fails when a stick
is passed through to a virtual machine.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_LOGGER = logging.getLogger(__name__)

SYSFS_USB_DEVICES: Final = Path("/sys/bus/usb/devices")
DEV_BUS_USB: Final = Path("/dev/bus/usb")


class UsbfsError(Exception):
    """Raised when the device cannot be opened or driven."""


class UsbfsUnavailableError(UsbfsError):
    """usbfs itself is not reachable, so local USB is not an option at all."""


# --------------------------------------------------------------------------
# ioctl encoding (asm-generic, which every architecture Home Assistant runs on
# uses)
# --------------------------------------------------------------------------

_IOC_NRBITS: Final = 8
_IOC_TYPEBITS: Final = 8
_IOC_SIZEBITS: Final = 14

_IOC_NRSHIFT: Final = 0
_IOC_TYPESHIFT: Final = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT: Final = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT: Final = _IOC_SIZESHIFT + _IOC_SIZEBITS

_IOC_NONE: Final = 0
_IOC_WRITE: Final = 1
_IOC_READ: Final = 2


def _ioc(direction: int, request_type: int, number: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (request_type << _IOC_TYPESHIFT)
        | (number << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def _ior(request_type: int, number: int, size: int) -> int:
    """``_IOR``: the kernel reads the argument from userspace."""
    return _ioc(_IOC_READ, request_type, number, size)


def _iow(request_type: int, number: int, size: int) -> int:
    return _ioc(_IOC_WRITE, request_type, number, size)


def _iowr(request_type: int, number: int, size: int) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, request_type, number, size)


def _io(request_type: int, number: int) -> int:
    return _ioc(_IOC_NONE, request_type, number, 0)


_USBDEVFS: Final = ord("U")


class _CtrlTransfer(ctypes.Structure):
    """``struct usbdevfs_ctrltransfer``."""

    _fields_ = (
        ("bRequestType", ctypes.c_uint8),
        ("bRequest", ctypes.c_uint8),
        ("wValue", ctypes.c_uint16),
        ("wIndex", ctypes.c_uint16),
        ("wLength", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    )


class _BulkTransfer(ctypes.Structure):
    """``struct usbdevfs_bulktransfer``."""

    _fields_ = (
        ("ep", ctypes.c_uint),
        ("len", ctypes.c_uint),
        ("timeout", ctypes.c_uint),
        ("data", ctypes.c_void_p),
    )


USBDEVFS_CONTROL: Final = _iowr(_USBDEVFS, 0, ctypes.sizeof(_CtrlTransfer))
USBDEVFS_BULK: Final = _iowr(_USBDEVFS, 2, ctypes.sizeof(_BulkTransfer))
USBDEVFS_SETCONFIGURATION: Final = _ior(_USBDEVFS, 5, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_CLAIMINTERFACE: Final = _ior(_USBDEVFS, 15, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_RELEASEINTERFACE: Final = _ior(_USBDEVFS, 16, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_RESET: Final = _io(_USBDEVFS, 20)
USBDEVFS_CLEAR_HALT: Final = _ior(_USBDEVFS, 21, ctypes.sizeof(ctypes.c_uint))


# --------------------------------------------------------------------------
# Discovery, from sysfs only
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UsbDevice:
    """An ELDAT stick found in sysfs, with everything needed to drive it."""

    vendor_id: int
    product_id: int
    bus: int
    address: int
    node: Path
    sysfs: Path
    serial: str | None
    interface: int
    endpoint_in: int
    endpoint_out: int
    packet_size: int

    @property
    def usb_ids(self) -> str:
        return f"{self.vendor_id:04X}:{self.product_id:04X}"

    def describe(self, names: dict[int, str] | None = None) -> str:
        name = (names or {}).get(self.product_id, "ELDAT device")
        serial = self.serial or "unreadable"
        return f"{self.usb_ids} {name} (serial {serial}) at {self.node}"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def _read_hex(path: Path) -> int | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


def _read_int(path: Path) -> int | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw, 10)
    except ValueError:
        return None


def _find_bulk_endpoints(interface_dir: Path) -> tuple[int | None, int | None, int]:
    """Bulk in/out addresses and packet size, straight out of sysfs."""
    endpoint_in: int | None = None
    endpoint_out: int | None = None
    packet_size = 64
    for endpoint in sorted(interface_dir.glob("ep_*")):
        if _read_text(endpoint / "type") != "Bulk":
            continue
        address = _read_hex(endpoint / "bEndpointAddress")
        if address is None:
            continue
        if address & 0x80:
            endpoint_in = address
            if size := _read_hex(endpoint / "wMaxPacketSize"):
                packet_size = size
        else:
            endpoint_out = address
    return endpoint_in, endpoint_out, packet_size


def enumerate_devices(vendor_id: int, product_ids: tuple[int, ...]) -> list[UsbDevice]:
    """Every matching stick that sysfs knows about.

    Raises :class:`UsbfsUnavailableError` when there is no usbfs to look at, so a
    caller can tell "not this machine's job" apart from "no stick plugged in".
    """
    if not SYSFS_USB_DEVICES.is_dir():
        raise UsbfsUnavailableError(f"{SYSFS_USB_DEVICES} is missing; this needs Linux")
    if not DEV_BUS_USB.is_dir():
        raise UsbfsUnavailableError(
            f"{DEV_BUS_USB} is missing. Home Assistant needs the USB device nodes "
            "for this; in a container that means /dev has to be mapped in."
        )

    found: list[UsbDevice] = []
    for entry in sorted(SYSFS_USB_DEVICES.iterdir()):
        if _read_hex(entry / "idVendor") != vendor_id:
            continue
        product = _read_hex(entry / "idProduct")
        if product not in product_ids:
            continue

        bus, address = _read_int(entry / "busnum"), _read_int(entry / "devnum")
        if bus is None or address is None:
            continue

        interface_dir = entry / f"{entry.name}:1.0"
        endpoint_in, endpoint_out, packet_size = _find_bulk_endpoints(interface_dir)
        if endpoint_in is None or endpoint_out is None:
            _LOGGER.debug("%s has no pair of bulk endpoints; skipping", entry.name)
            continue

        found.append(
            UsbDevice(
                vendor_id=vendor_id,
                product_id=product,
                bus=bus,
                address=address,
                node=DEV_BUS_USB / f"{bus:03d}" / f"{address:03d}",
                sysfs=entry,
                # From sysfs, so no string-descriptor request is needed -- the very
                # transfer that fails on a device passed through to a VM.
                serial=_read_text(entry / "serial"),
                interface=_read_int(interface_dir / "bInterfaceNumber") or 0,
                endpoint_in=endpoint_in,
                endpoint_out=endpoint_out,
                packet_size=packet_size,
            )
        )
    return found


# --------------------------------------------------------------------------
# The device itself
# --------------------------------------------------------------------------


class UsbfsDevice:
    """A USB device driven through usbfs ioctls.

    Blocking by design; callers pump it from a thread. Reads and writes go to
    separate endpoints, so one thread may read while another writes.
    """

    def __init__(self, device: UsbDevice) -> None:
        self._device = device
        self._fd: int | None = None
        self._claimed = False

    @property
    def info(self) -> UsbDevice:
        return self._device

    def open(self) -> None:
        if self._fd is not None:
            return
        try:
            self._fd = os.open(self._device.node, os.O_RDWR | os.O_CLOEXEC)
        except FileNotFoundError as err:
            raise UsbfsError(f"{self._device.node} disappeared") from err
        except PermissionError as err:
            raise UsbfsError(
                f"not allowed to open {self._device.node}. Home Assistant needs "
                "read and write access to the USB device nodes."
            ) from err
        except OSError as err:
            raise UsbfsError(f"cannot open {self._device.node}: {err}") from err

        try:
            self._claim()
        except UsbfsError:
            self.close()
            raise

    def _claim(self) -> None:
        interface = ctypes.c_uint(self._device.interface)
        try:
            fcntl.ioctl(self._fd, USBDEVFS_CLAIMINTERFACE, interface)
        except OSError as err:
            if err.errno == errno.EBUSY:
                raise UsbfsError(
                    f"interface {self._device.interface} is already claimed -- "
                    "another program or a kernel driver has the stick"
                ) from err
            raise UsbfsError(f"cannot claim the interface: {err}") from err
        self._claimed = True

    def control(
        self,
        request_type: int,
        request: int,
        value: int,
        index: int,
        data: bytes = b"",
        timeout_ms: int = 1000,
    ) -> None:
        """Issue a control transfer. Host-to-device only, which is all we need."""
        buffer = ctypes.create_string_buffer(data, len(data)) if data else None
        transfer = _CtrlTransfer(
            bRequestType=request_type,
            bRequest=request,
            wValue=value,
            wIndex=index,
            wLength=len(data),
            timeout=timeout_ms,
            data=ctypes.cast(buffer, ctypes.c_void_p) if buffer else None,
        )
        try:
            fcntl.ioctl(self._require_fd(), USBDEVFS_CONTROL, transfer)
        except OSError as err:
            raise UsbfsError(
                f"control request 0x{request:02X} failed: {os.strerror(err.errno or 0)}"
            ) from err

    def bulk_read(self, timeout_ms: int = 200) -> bytes:
        """Read up to one packet. Returns ``b''`` on timeout."""
        size = self._device.packet_size
        buffer = ctypes.create_string_buffer(size)
        transfer = _BulkTransfer(
            ep=self._device.endpoint_in,
            len=size,
            timeout=timeout_ms,
            data=ctypes.cast(buffer, ctypes.c_void_p),
        )
        try:
            transferred = fcntl.ioctl(self._require_fd(), USBDEVFS_BULK, transfer)
        except OSError as err:
            if err.errno in (errno.ETIMEDOUT, errno.EAGAIN):
                return b""
            raise UsbfsError(f"bulk read failed: {os.strerror(err.errno or 0)}") from err
        return buffer.raw[:transferred]

    def bulk_write(self, data: bytes, timeout_ms: int = 1000) -> None:
        buffer = ctypes.create_string_buffer(data, len(data))
        transfer = _BulkTransfer(
            ep=self._device.endpoint_out,
            len=len(data),
            timeout=timeout_ms,
            data=ctypes.cast(buffer, ctypes.c_void_p),
        )
        try:
            written = fcntl.ioctl(self._require_fd(), USBDEVFS_BULK, transfer)
        except OSError as err:
            raise UsbfsError(f"bulk write failed: {os.strerror(err.errno or 0)}") from err
        if written != len(data):
            raise UsbfsError(f"short write: {written} of {len(data)} bytes")

    def reset(self) -> None:
        """Reset the device, which re-enumerates it on the bus.

        The recovery path for a stick that has stopped answering. Note the device
        gets a new address afterwards, so anything holding the old node path has
        to look it up again.
        """
        try:
            fcntl.ioctl(self._require_fd(), USBDEVFS_RESET)
        except OSError as err:
            raise UsbfsError(f"reset failed: {os.strerror(err.errno or 0)}") from err
        _LOGGER.debug("reset %s", self._device.node)

    def _require_fd(self) -> int:
        if self._fd is None:
            raise UsbfsError("device is not open")
        return self._fd

    def close(self) -> None:
        if self._fd is None:
            return
        if self._claimed:
            try:
                fcntl.ioctl(
                    self._fd,
                    USBDEVFS_RELEASEINTERFACE,
                    ctypes.c_uint(self._device.interface),
                )
            except OSError as err:
                _LOGGER.debug("releasing the interface failed: %s", err)
            self._claimed = False
        try:
            os.close(self._fd)
        except OSError as err:
            _LOGGER.debug("closing %s failed: %s", self._device.node, err)
        self._fd = None

    def __enter__(self) -> UsbfsDevice:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
