"""Connection manager shared by all entities of one config entry.

Owns the single TCP connection to the bridge, keeps it alive, and fans received
telegrams out to entities. Everything is funnelled through one object because
the stick is strictly single-speaker: commands must not overlap, and only one
process may hold it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    ATTR_ACTION,
    ATTR_ADDRESS,
    ATTR_KEY,
    ATTR_REPEATS,
    ATTR_RSSI,
    DOMAIN,
    EVENT_TELEGRAM,
)
from .eldat.parser import Identification, Info
from .eldat.protocol import (
    EldatClient,
    EldatConnectionError,
    EldatError,
    connect_tcp,
)
from .eldat.telegrams import TelegramEvent

_LOGGER = logging.getLogger(__name__)

_RECONNECT_DELAYS = (1, 2, 5, 10, 30, 60)

#: How often the supervisor checks whether the connection is still alive.
_POLL_INTERVAL = 1


class EldatHub:
    """Holds the connection and distributes telegrams."""

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str, port: int) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.host = host
        self.port = port

        self.identification: Identification | None = None
        self.info: Info | None = None
        self.position_count: int | None = None
        self.mode: int | None = None

        #: Transmitters heard since startup, newest first. Because the firmware
        #: may lack ``RDP?`` there is no way to enumerate addresses, so the
        #: config flow offers what has actually been heard on the air instead.
        self.seen: dict[str, TelegramEvent] = {}

        self._client: EldatClient | None = None
        self._availability_listeners: list[Callable[[], None]] = []
        self._supervisor: asyncio.Task[None] | None = None
        self._shutdown = False
        self._remove_client_listener: Callable[[], None] | None = None

    # -- properties --------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._client is not None and not self._client.is_closed

    @property
    def signal_telegram(self) -> str:
        return f"{DOMAIN}_{self.entry_id}_telegram"

    @property
    def unique_id(self) -> str:
        """Identifies the bridge endpoint.

        The stick's USB serial is not reachable over the protocol -- ``ID?``
        returns only vendor, product and firmware version -- so the bridge
        endpoint is what identifies this hub.
        """
        return f"{self.host}:{self.port}"

    # -- lifecycle ---------------------------------------------------------

    async def async_setup(self) -> None:
        """Connect once, failing loudly so setup can be retried by HA."""
        await self._connect()
        self._supervisor = self.hass.async_create_background_task(
            self._supervise(), f"{DOMAIN}-supervisor-{self.entry_id}"
        )

    async def _connect(self) -> None:
        client = await connect_tcp(self.host, self.port)
        try:
            self.identification = await client.identify()
            self.info = await client.info()
            self.mode = await client.mode()
            try:
                self.position_count = await client.position_count()
            except EldatError as err:
                _LOGGER.warning("cannot read the number of transmit positions: %s", err)
        except EldatError:
            await client.close()
            raise

        self._client = client
        self._remove_client_listener = client.add_listener(self._handle_telegram)

        _LOGGER.info(
            "connected to %s: %s firmware %s, %s positions%s",
            self.unique_id,
            f"{self.identification.vendor_id:04X}:{self.identification.product_id:04X}",
            self.identification.version_string,
            self.position_count if self.position_count is not None else "unknown",
            f", {self.info.text}" if self.info else "",
        )
        self._notify_availability()

    async def async_shutdown(self) -> None:
        self._shutdown = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
        await self._close_client()

    async def _close_client(self) -> None:
        if self._remove_client_listener is not None:
            self._remove_client_listener()
            self._remove_client_listener = None
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._notify_availability()

    async def _supervise(self) -> None:
        """Reconnect when the bridge goes away, e.g. while it is updated."""
        attempt = 0
        while not self._shutdown:
            await asyncio.sleep(_POLL_INTERVAL)
            if self.available:
                attempt = 0
                continue

            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            attempt += 1
            _LOGGER.debug("reconnecting to %s in %ss", self.unique_id, delay)
            await asyncio.sleep(delay)
            if self._shutdown:
                return
            await self._reconnect_once()

    async def _reconnect_once(self) -> bool:
        """Drop the old connection and try once more. Never raises.

        Swallowing everything is deliberate: an exception escaping here would
        kill the supervisor task, leaving every entity unavailable with no way
        back short of reloading the integration by hand.
        """
        await self._close_client()
        try:
            await self._connect()
        except EldatError as err:
            _LOGGER.debug("reconnect to %s failed: %s", self.unique_id, err)
            return False
        except Exception:
            _LOGGER.exception("unexpected error reconnecting to %s", self.unique_id)
            return False
        return True

    # -- telegram distribution --------------------------------------------

    @callback
    def _handle_telegram(self, event: TelegramEvent) -> None:
        """Fan a telegram out to entities and to the HA event bus."""
        # Re-insert so the mapping stays ordered newest-last, which the config
        # flow reverses to put the most recently heard transmitter on top.
        self.seen.pop(event.address, None)
        self.seen[event.address] = event

        async_dispatcher_send(self.hass, self.signal_telegram, event)
        self.hass.bus.async_fire(
            EVENT_TELEGRAM,
            {
                ATTR_ADDRESS: event.address,
                ATTR_KEY: event.key,
                ATTR_ACTION: str(event.action),
                ATTR_RSSI: event.rssi,
                ATTR_REPEATS: event.repeats,
            },
        )

    @callback
    def add_availability_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        self._availability_listeners.append(listener)

        def remove() -> None:
            if listener in self._availability_listeners:
                self._availability_listeners.remove(listener)

        return remove

    @callback
    def _notify_availability(self) -> None:
        for listener in list(self._availability_listeners):
            listener()

    # -- commands ----------------------------------------------------------

    async def async_transmit(self, position: int, key: str) -> None:
        """Send an Easywave telegram. Raises if the bridge is unreachable."""
        client = self._client
        if client is None or client.is_closed:
            raise EldatConnectionError(f"not connected to {self.unique_id}")
        await client.transmit(position, key)

    async def async_set_led(self, on: bool) -> None:
        client = self._client
        if client is None or client.is_closed:
            raise EldatConnectionError(f"not connected to {self.unique_id}")
        await client.set_led(on)
