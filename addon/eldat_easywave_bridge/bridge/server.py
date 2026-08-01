"""Exposes the ELDAT stick as a plain TCP stream.

The Home Assistant integration only needs a duplex byte stream, so all the
hardware awkwardness lives here: the missing kernel USB id, libusb, and the fact
that the stick tolerates exactly one speaker.

Two ways in, tried in order:

1. **Kernel bind** -- register the stick's USB id with the running ``cp210x``
   driver and use the resulting tty. Cheapest and most robust when it works.
2. **Userspace CP210x** -- drive the chip over libusb. Needed on Home Assistant
   OS, where sysfs may not be writable and the module may not even be loaded.

Only one client may be connected at a time; a second connection is closed
immediately rather than silently interleaving commands and corrupting the
strictly request/acknowledge protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from dataclasses import dataclass
from typing import Protocol

from . import kernel
from .cp210x import (
    BAUDRATE,
    ELDAT_PRODUCT_IDS,
    ELDAT_VENDOR_ID,
    Cp210xDevice,
    Cp210xError,
    describe,
    find_devices,
)

_LOGGER = logging.getLogger(__name__)

_READ_TIMEOUT_MS = 200


class ByteDevice(Protocol):
    """The blocking byte-stream interface the pump drives."""

    @property
    def description(self) -> str: ...

    def read(self, timeout_ms: int = ...) -> bytes: ...

    def write(self, data: bytes, timeout_ms: int = ...) -> None: ...

    def close(self) -> None: ...


class TtyDevice:
    """A kernel-provided serial port, opened with the settings ELDAT requires."""

    def __init__(self, path: str) -> None:
        import serial  # imported lazily: only needed on the kernel path

        self._path = path
        self._port = serial.Serial(
            port=path,
            baudrate=BAUDRATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            rtscts=False,
            dsrdtr=False,
            timeout=_READ_TIMEOUT_MS / 1000,
        )

    @property
    def description(self) -> str:
        return f"{self._path} at {BAUDRATE} baud 8N1"

    def read(self, timeout_ms: int = _READ_TIMEOUT_MS) -> bytes:
        self._port.timeout = timeout_ms / 1000
        # read(1) blocks up to the timeout, then drain whatever else arrived.
        first = self._port.read(1)
        if not first:
            return b""
        return first + self._port.read(self._port.in_waiting or 0)

    def write(self, data: bytes, timeout_ms: int = 1000) -> None:
        self._port.write(data)
        self._port.flush()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._port.close()


@dataclass(frozen=True)
class BridgeConfig:
    host: str = "0.0.0.0"
    port: int = 5000
    product_ids: tuple[int, ...] = ELDAT_PRODUCT_IDS
    prefer_kernel: bool = True


def open_device(config: BridgeConfig) -> ByteDevice:
    """Open the stick, preferring a kernel tty and falling back to libusb."""
    devices = find_devices(config.product_ids)
    if not devices:
        raise Cp210xError(
            "no ELDAT transceiver found "
            f"(vendor {ELDAT_VENDOR_ID:04X}, products "
            f"{', '.join(f'{pid:04X}' for pid in config.product_ids)})"
        )
    if len(devices) > 1:
        _LOGGER.warning(
            "%d ELDAT transceivers found, using the first: %s",
            len(devices),
            ", ".join(describe(device) for device in devices),
        )
    device = devices[0]
    _LOGGER.info("found %s", describe(device))

    if config.prefer_kernel:
        if tty := _try_kernel(device):
            return tty
        _LOGGER.info("kernel path unavailable, driving the CP210x from userspace")

    stick = Cp210xDevice(device)
    stick.open()
    return stick


def _try_kernel(device) -> TtyDevice | None:
    """Bind the id and open the resulting tty, or give up quietly."""
    if not kernel.bind(device.idVendor, device.idProduct):
        return None
    path = kernel.find_tty(device.serial_number)
    if path is None:
        _LOGGER.info("cp210x accepted the id but no tty appeared")
        return None
    try:
        tty = TtyDevice(path)
    except Exception as err:  # pyserial raises a variety of types
        _LOGGER.info("cannot open %s: %s", path, err)
        return None
    _LOGGER.info("using kernel serial port %s", tty.description)
    return tty


class Bridge:
    """Pumps bytes between one TCP client and the stick."""

    def __init__(self, device: ByteDevice) -> None:
        self._device = device
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: asyncio.StreamWriter | None = None
        self._client_lock = asyncio.Lock()
        self._stop = threading.Event()
        #: Set when the device is gone, so serve() can wait rather than poll.
        self._dead = asyncio.Event()
        self._reader_thread: threading.Thread | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._reader_thread = threading.Thread(
            target=self._read_forever, name="eldat-usb-reader", daemon=True
        )
        self._reader_thread.start()

    def _read_forever(self) -> None:
        """Device -> asyncio. Runs in its own thread; the device API blocks."""
        while not self._stop.is_set():
            try:
                data = self._device.read(_READ_TIMEOUT_MS)
            except Exception as err:
                _LOGGER.error("device read failed, stopping bridge: %s", err)
                self._stop.set()
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._dead.set)
                return
            if data and self._loop is not None:
                self._loop.call_soon_threadsafe(self._forward_to_client, data)

    def _forward_to_client(self, data: bytes) -> None:
        client = self._client
        if client is None:
            # Nobody listening. Telegrams while unconnected are simply lost;
            # the radio protocol is stateless so there is nothing to replay.
            _LOGGER.debug("dropping %d bytes, no client connected", len(data))
            return
        try:
            client.write(data)
        except Exception as err:
            _LOGGER.debug("cannot write to client: %s", err)

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        async with self._client_lock:
            if self._client is not None:
                _LOGGER.warning("rejecting %s, another client is connected", peer)
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                return
            self._client = writer

        _LOGGER.info("client connected: %s", peer)
        loop = asyncio.get_running_loop()
        try:
            while True:
                data = await reader.read(256)
                if not data:
                    break
                # Keep the blocking write off the event loop.
                await loop.run_in_executor(None, self._device.write, data)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception as err:
            _LOGGER.error("client %s failed: %s", peer, err)
        finally:
            _LOGGER.info("client disconnected: %s", peer)
            async with self._client_lock:
                if self._client is writer:
                    self._client = None
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def stop(self) -> None:
        self._stop.set()
        if self._reader_thread is not None:
            await asyncio.get_running_loop().run_in_executor(
                None, self._reader_thread.join, 2.0
            )
        self._device.close()

    async def wait_until_dead(self) -> None:
        """Block until the device stops responding."""
        await self._dead.wait()


async def serve(config: BridgeConfig) -> None:
    """Open the stick and serve it until cancelled or the device dies."""
    device = open_device(config)
    bridge = Bridge(device)
    await bridge.start()

    server = await asyncio.start_server(bridge.handle_client, config.host, config.port)
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets)
    _LOGGER.info("bridging %s on %s", device.description, addresses)

    try:
        async with server:
            # Exit if the reader thread reports the device is gone, so the
            # Supervisor can restart us and re-open the stick.
            await bridge.wait_until_dead()
            _LOGGER.error("device is gone, shutting down")
    finally:
        await bridge.stop()
