"""Drive a locally attached stick and present it as an asyncio stream pair.

Lets :class:`~.protocol.EldatClient` talk to USB hardware without knowing about
USB: it still sees a reader and a writer, exactly as it does over TCP. The usbfs
ioctls block, so a thread does the reading and writes are handed to an executor.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
from typing import Final

from .hardware import (
    BAUDRATE,
    ELDAT_PRODUCT_IDS,
    ELDAT_VENDOR_ID,
    IFC_ENABLE,
    LINE_CTL_8N1,
    MHS_DTR_RTS_ON,
    REQTYPE_HOST_TO_DEVICE,
    SET_BAUDRATE,
    SET_LINE_CTL,
    SET_MHS,
    UART_DISABLE,
    UART_ENABLE,
    describe_product,
)
from .usbfs import UsbDevice, UsbfsDevice, UsbfsError, enumerate_devices

_LOGGER = logging.getLogger(__name__)

_READ_TIMEOUT_MS: Final = 200


def find_local_devices() -> list[UsbDevice]:
    """Every ELDAT stick attached to this machine.

    Raises :class:`~.usbfs.UsbfsUnavailableError` when usbfs is not reachable at
    all, which a caller should treat as "local USB is not possible here" rather
    than "no stick".
    """
    return enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)


def describe(device: UsbDevice) -> str:
    return (
        f"{device.usb_ids} {describe_product(device.product_id)} "
        f"(serial {device.serial or 'unreadable'})"
    )


def initialise_uart(device: UsbfsDevice) -> None:
    """Put the CP210x into the 8N1 mode the ELDAT protocol needs.

    Each register is sent separately so a failure says which one the device
    rejected -- on an unreliable USB path, vendor requests are the first thing to
    go.
    """
    interface = device.info.interface
    for label, request, value, data in (
        ("IFC_ENABLE", IFC_ENABLE, UART_ENABLE, b""),
        ("SET_BAUDRATE", SET_BAUDRATE, 0, struct.pack("<I", BAUDRATE)),
        ("SET_LINE_CTL", SET_LINE_CTL, LINE_CTL_8N1, b""),
        ("SET_MHS", SET_MHS, MHS_DTR_RTS_ON, b""),
    ):
        try:
            device.control(REQTYPE_HOST_TO_DEVICE, request, value, interface, data)
        except UsbfsError as err:
            raise UsbfsError(
                f"the {label} vendor request (0x{request:02X}) failed: {err}. "
                "The device is reachable but will not accept vendor control "
                "transfers. On a virtualised Home Assistant this is the USB "
                "passthrough: run the bridge on the host the stick is attached "
                "to and connect over the network instead."
            ) from err
        _LOGGER.debug("%s accepted", label)
    _LOGGER.debug("UART set to %d baud 8N1", BAUDRATE)


def shutdown_uart(device: UsbfsDevice) -> None:
    try:
        device.control(
            REQTYPE_HOST_TO_DEVICE, IFC_ENABLE, UART_DISABLE, device.info.interface
        )
    except UsbfsError as err:
        _LOGGER.debug("disabling the UART failed: %s", err)


class _UsbWriter:
    """The slice of ``asyncio.StreamWriter`` that :class:`.EldatClient` uses.

    ``write`` buffers and ``drain`` performs the transfer, which matches how the
    client already writes a whole command and then awaits it.
    """

    def __init__(self, device: UsbfsDevice, loop: asyncio.AbstractEventLoop) -> None:
        self._device = device
        self._loop = loop
        self._pending = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        self._pending.extend(data)

    async def drain(self) -> None:
        if not self._pending or self._closed:
            return
        payload, self._pending = bytes(self._pending), bytearray()
        try:
            await self._loop.run_in_executor(None, self._device.bulk_write, payload)
        except UsbfsError as err:
            raise OSError(str(err)) from err

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, name: str, default: object = None) -> object:
        return default


class UsbConnection:
    """Owns the device, the reader thread, and the stream pair built on them."""

    def __init__(self, device: UsbfsDevice) -> None:
        self._device = device
        self._loop = asyncio.get_running_loop()
        self.reader = asyncio.StreamReader()
        self.writer = _UsbWriter(device, self._loop)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def description(self) -> str:
        return describe(self._device.info)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._read_forever, name="eldat-usb-reader", daemon=True
        )
        self._thread.start()

    def _read_forever(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._device.bulk_read(_READ_TIMEOUT_MS)
            except UsbfsError as err:
                _LOGGER.warning("USB read failed, closing the connection: %s", err)
                self._loop.call_soon_threadsafe(self.reader.feed_eof)
                return
            if data:
                self._loop.call_soon_threadsafe(self.reader.feed_data, data)

    async def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            await self._loop.run_in_executor(None, self._thread.join, 2.0)
            self._thread = None
        await self._loop.run_in_executor(None, self._teardown)

    def _teardown(self) -> None:
        shutdown_uart(self._device)
        self._device.close()


async def open_local_device(device: UsbDevice) -> UsbConnection:
    """Open a stick, configure its UART, and start pumping it."""
    loop = asyncio.get_running_loop()
    handle = UsbfsDevice(device)
    await loop.run_in_executor(None, handle.open)
    try:
        await loop.run_in_executor(None, initialise_uart, handle)
    except UsbfsError:
        await loop.run_in_executor(None, handle.close)
        raise

    connection = UsbConnection(handle)
    connection.start()
    _LOGGER.info("opened %s at %d baud 8N1", connection.description, BAUDRATE)
    return connection
