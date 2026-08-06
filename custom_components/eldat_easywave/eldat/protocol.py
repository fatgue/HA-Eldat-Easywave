"""Asyncio client for the ELDAT Easywave transceiver.

Speaks the line protocol over any duplex byte stream. The hardware itself is
handled elsewhere -- the bridge add-on owns the USB/serial side and exposes a
plain TCP stream -- which keeps this module free of third-party dependencies.

Two properties of the device shape the design:

* **Commands must not overlap.** The specification requires waiting for a
  command's acknowledgement before sending the next one, so commands are
  serialised through a lock.
* **Received telegrams arrive unsolicited, at any time.** Because only command
  responses carry an acknowledgement (see :mod:`.parser`), a key press that
  lands in the middle of an in-flight command can never be mistaken for that
  command's ack.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Self

from .parser import (
    Frame,
    Identification,
    Info,
    Message,
    Mode,
    PositionCount,
    Received,
    StatusText,
    decode_payload,
    encode_command,
    iter_frames,
    parse_frame,
    transmit_command,
)
from .telegrams import BurstCollapser, TelegramEvent

_LOGGER = logging.getLogger(__name__)

#: The device answers immediately; this only guards against a wedged link.
DEFAULT_COMMAND_TIMEOUT: Final = 5.0

#: How often to poll the burst collapser for pending release events.
_TICK_INTERVAL: Final = 0.2

_READ_CHUNK: Final = 256


class EldatError(Exception):
    """Base class for client errors."""


class EldatCommandError(EldatError):
    """The device answered ERROR.

    Expected for unsupported commands: the RX09 EW+KEELOQ firmware rejects
    ``RDP?``, for instance.
    """

    def __init__(self, command: str) -> None:
        super().__init__(f"device rejected command: {command.strip()!r}")
        self.command = command.strip()


class EldatTimeoutError(EldatError):
    """No acknowledgement arrived in time."""


class EldatConnectionError(EldatError):
    """The stream is not usable."""


@dataclass(frozen=True, slots=True)
class Response:
    """A command's outcome."""

    ack: bool
    message: Message | None


TelegramCallback = Callable[[TelegramEvent], None]


class EldatClient:
    """Frames the byte stream, serialises commands and dispatches telegrams."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        collapser: BurstCollapser | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._command_timeout = command_timeout
        self._collapser = collapser if collapser is not None else BurstCollapser()
        self._buffer = ""
        self._command_lock = asyncio.Lock()
        self._pending: asyncio.Future[Frame] | None = None
        self._listeners: list[TelegramCallback] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> Self:
        self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def start(self) -> None:
        """Begin reading. Safe to call once."""
        self._spawn(self._read_loop(), "eldat-reader")
        self._spawn(self._tick_loop(), "eldat-tick")

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._fail_pending(EldatConnectionError("connection closed"))
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (OSError, RuntimeError):  # already torn down
            pass

    @property
    def is_closed(self) -> bool:
        return self._closed

    # -- telegram listeners ------------------------------------------------

    def add_listener(self, callback: TelegramCallback) -> Callable[[], None]:
        """Register a telegram callback; returns an unsubscribe function."""
        self._listeners.append(callback)

        def remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return remove

    def _dispatch(self, events: list[TelegramEvent]) -> None:
        for event in events:
            for callback in list(self._listeners):
                try:
                    callback(event)
                except Exception:  # a bad listener must not kill the reader
                    _LOGGER.exception("error in telegram listener")

    # -- reading -----------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                chunk = await self._reader.read(_READ_CHUNK)
                if not chunk:
                    raise EldatConnectionError("stream closed by peer")
                self._feed(chunk.decode("ascii", errors="replace"))
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # Marking the client closed is the whole point: without it the reader
            # can die -- the stick unplugged, the bridge restarted -- while
            # is_closed still says False, so callers believe the connection is
            # healthy and nothing ever reconnects. Silence then looks exactly like
            # an idle radio.
            self._closed = True
            _LOGGER.warning("connection lost: %s", err)
            self._fail_pending(err)

    def _feed(self, text: str) -> None:
        """Frame incoming text and route each frame."""
        frames, self._buffer = iter_frames(self._buffer + text)
        if frames:
            # Logged because its absence is the only symptom when a transceiver
            # stops reporting: commands still work, and silence looks identical
            # to "nobody pressed anything".
            _LOGGER.debug("received %d frame(s): %s", len(frames), frames)
        for raw in frames:
            frame = parse_frame(raw)
            if frame.is_unsolicited:
                self._handle_unsolicited(frame)
            else:
                self._resolve(frame)

    def _handle_unsolicited(self, frame: Frame) -> None:
        if frame.payload is None:
            return
        message = decode_payload(frame.payload)
        if isinstance(message, Received):
            _LOGGER.debug(
                "telegram from %s key %s (%s dBm)",
                message.address,
                message.key,
                message.rssi,
            )
            self._dispatch(self._collapser.feed(message))
        else:
            _LOGGER.debug("unsolicited non-telegram frame: %r", frame.payload)

    def _resolve(self, frame: Frame) -> None:
        pending = self._pending
        if pending is None or pending.done():
            _LOGGER.debug("acknowledgement with no command in flight: %r", frame)
            return
        pending.set_result(frame)

    def _fail_pending(self, err: BaseException) -> None:
        pending = self._pending
        if pending is not None and not pending.done():
            pending.set_exception(err)

    async def _tick_loop(self) -> None:
        """Surface release events even while the air is quiet."""
        try:
            while not self._closed:
                await asyncio.sleep(_TICK_INTERVAL)
                self._dispatch(self._collapser.tick())
        except asyncio.CancelledError:
            raise

    # -- commands ----------------------------------------------------------

    async def execute(self, *fields: str) -> Response:
        """Send one command and await its acknowledgement.

        Raises :class:`EldatCommandError` when the device answers ERROR.
        """
        command = encode_command(*fields)
        return await self._send(command)

    async def send_raw(self, command: str) -> Response:
        """Send a pre-encoded command (already terminated)."""
        return await self._send(command)

    async def _send(self, command: str) -> Response:
        if self._closed:
            raise EldatConnectionError("client is closed")

        async with self._command_lock:
            loop = asyncio.get_running_loop()
            pending: asyncio.Future[Frame] = loop.create_future()
            self._pending = pending
            try:
                self._writer.write(command.encode("ascii"))
                await self._writer.drain()
                frame = await asyncio.wait_for(pending, self._command_timeout)
            except TimeoutError as err:
                raise EldatTimeoutError(
                    f"no acknowledgement for {command.strip()!r}"
                ) from err
            except (OSError, RuntimeError) as err:
                raise EldatConnectionError(str(err)) from err
            finally:
                self._pending = None

        if not frame.ack:
            raise EldatCommandError(command)

        message = decode_payload(frame.payload) if frame.payload else None
        return Response(ack=True, message=message)

    # -- high level operations --------------------------------------------

    async def identify(self) -> Identification:
        """``ID?`` -- vendor id, product id and firmware version."""
        message = (await self.execute("ID?")).message
        if not isinstance(message, Identification):
            raise EldatError(f"unexpected ID? response: {message!r}")
        return message

    async def position_count(self) -> int:
        """``GETP?`` -- number of transmit positions burned into the stick."""
        message = (await self.execute("GETP?")).message
        if not isinstance(message, PositionCount):
            raise EldatError(f"unexpected GETP? response: {message!r}")
        return message.count

    async def info(self) -> Info | None:
        """``INFO?`` -- undocumented; identifies OEM firmware variants.

        Returns ``None`` when the firmware does not implement it.
        """
        try:
            message = (await self.execute("INFO?")).message
        except EldatCommandError:
            return None
        return message if isinstance(message, Info) else None

    async def mode(self) -> int | None:
        """``MODE?`` -- undocumented. ``None`` when unsupported."""
        try:
            message = (await self.execute("MODE?")).message
        except EldatCommandError:
            return None
        return message.value if isinstance(message, Mode) else None

    async def read_position(self, position: int) -> str | None:
        """``RDP?,<pos>`` -- the serial stored at a transmit position.

        Returns ``None`` when the firmware lacks the command, which is the case
        on the RX09 EW+KEELOQ build. Callers must cope with not being able to
        enumerate the stick's addresses.
        """
        from .parser import Position, format_position

        try:
            message = (await self.execute("RDP?", format_position(position))).message
        except EldatCommandError:
            return None
        return message.address if isinstance(message, Position) else None

    async def transmit(self, position: int, key: str) -> None:
        """``TXP,<pos>,<key>`` -- emit an Easywave telegram. This is real RF."""
        await self.send_raw(transmit_command(position, key))

    async def set_led(self, on: bool) -> None:
        """``LED,ON|OFF`` -- the stick's red LED."""
        await self.execute("LED", "ON" if on else "OFF")

    async def led(self) -> bool | None:
        """``LED?``"""
        message = (await self.execute("LED?")).message
        return message.is_on if isinstance(message, StatusText) else None

    async def button(self) -> bool | None:
        """``BUTTON?`` -- the pushbutton on the stick itself."""
        message = (await self.execute("BUTTON?")).message
        return message.is_on if isinstance(message, StatusText) else None


async def connect_tcp(
    host: str,
    port: int,
    *,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    connect_timeout: float = 10.0,
) -> EldatClient:
    """Connect to a bridge exposing the stick as a raw TCP stream."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), connect_timeout
        )
    except TimeoutError as err:
        raise EldatConnectionError(f"timeout connecting to {host}:{port}") from err
    except OSError as err:
        raise EldatConnectionError(f"cannot connect to {host}:{port}: {err}") from err

    client = EldatClient(reader, writer, command_timeout=command_timeout)
    client.start()
    return client


async def connect_local(
    device,
    *,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> tuple[EldatClient, object]:
    """Open a stick attached to this machine, through usbfs.

    Returns the client and the underlying connection; close the connection after
    the client to release the USB device. Imported lazily so that a Home Assistant
    instance which only ever talks to a bridge does not pay for the USB layer.
    """
    from .usb_transport import open_local_device

    connection = await open_local_device(device)
    client = EldatClient(
        connection.reader, connection.writer, command_timeout=command_timeout
    )
    client.start()
    return client, connection


__all__ = [
    "EldatClient",
    "EldatCommandError",
    "EldatConnectionError",
    "EldatError",
    "EldatTimeoutError",
    "Response",
    "connect_local",
    "connect_tcp",
]
