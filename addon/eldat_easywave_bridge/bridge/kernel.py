"""Persuading the Linux kernel to bind an unlisted ELDAT stick.

The in-tree ``cp210x`` driver only lists ELDAT product id ``0x1006``, so a stick
reporting anything else gets no tty. When the module is loaded, its
``new_id`` attribute accepts extra vendor/product pairs at runtime, which is far
preferable to a userspace driver: everything downstream then sees an ordinary
serial port.

This needs a writable ``/sys``, i.e. a privileged container. It is expected to
fail on Home Assistant OS in some setups, and the caller falls back to
:mod:`.cp210x`.
"""

from __future__ import annotations

import errno
import glob
import logging
import os
import time
from pathlib import Path
from typing import Final

_LOGGER = logging.getLogger(__name__)

_NEW_ID: Final = Path("/sys/bus/usb-serial/drivers/cp210x/new_id")
_USB_SERIAL_DEVICES: Final = Path("/sys/bus/usb-serial/devices")


def driver_present() -> bool:
    """Whether the cp210x driver is loaded and accepts new ids."""
    return _NEW_ID.parent.is_dir()


def bind(vendor_id: int, product_id: int) -> bool:
    """Register a vendor/product pair with the running cp210x driver.

    Returns ``True`` when the id was accepted (or was already known). Never
    raises -- an unwritable sysfs is an expected outcome, not an error.
    """
    if not driver_present():
        _LOGGER.info(
            "cp210x driver not loaded (%s missing); cannot bind %04X:%04X",
            _NEW_ID.parent,
            vendor_id,
            product_id,
        )
        return False
    try:
        _NEW_ID.write_text(f"{vendor_id:04x} {product_id:04x}\n")
    except OSError as err:
        # EEXIST/EINVAL simply mean the driver already knows this id.
        if err.errno in (errno.EEXIST, errno.EINVAL):
            _LOGGER.info("cp210x already knows %04X:%04X", vendor_id, product_id)
            return True
        _LOGGER.info("cannot write %s: %s", _NEW_ID, err)
        return False
    _LOGGER.info("registered %04X:%04X with the cp210x driver", vendor_id, product_id)
    return True


def find_tty(serial_number: str | None = None, timeout: float = 5.0) -> str | None:
    """Wait for a tty belonging to an ELDAT stick to appear.

    Prefers ``/dev/serial/by-id`` because those names survive re-enumeration;
    falls back to whatever the cp210x driver has bound.
    """
    deadline = time.monotonic() + timeout
    while True:
        if path := _by_id_path(serial_number) or _bound_tty():
            return path
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def _by_id_path(serial_number: str | None) -> str | None:
    candidates = sorted(glob.glob("/dev/serial/by-id/*"))
    for path in candidates:
        lowered = os.path.basename(path).lower()
        if "eldat" not in lowered and "silicon_labs" not in lowered:
            continue
        if serial_number and serial_number.lower() not in lowered:
            continue
        return path
    return None


def _bound_tty() -> str | None:
    if not _USB_SERIAL_DEVICES.is_dir():
        return None
    for entry in sorted(_USB_SERIAL_DEVICES.iterdir()):
        device = Path("/dev") / entry.name
        if device.exists():
            return str(device)
    return None
