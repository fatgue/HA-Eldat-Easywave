"""Shared test fixtures."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from custom_components.eldat_easywave.eldat.protocol import EldatClient
from custom_components.eldat_easywave.eldat.telegrams import BurstCollapser, TelegramEvent


class FakeWriter:
    """Minimal StreamWriter stand-in that records what was sent."""

    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    @property
    def text(self) -> str:
        return self.written.decode("ascii")


@dataclass
class Harness:
    """Drives an :class:`EldatClient` against an in-memory stream."""

    client: EldatClient
    reader: asyncio.StreamReader
    writer: FakeWriter
    events: list[TelegramEvent] = field(default_factory=list)

    async def wait_for_write(self, expected: str, timeout: float = 1.0) -> None:
        """Block until the client has written ``expected``."""
        async with asyncio.timeout(timeout):
            while expected not in self.writer.text:
                await asyncio.sleep(0)

    def device_says(self, text: str) -> None:
        """Push bytes from the device towards the client."""
        self.reader.feed_data(text.encode("ascii"))

    def hang_up(self) -> None:
        self.reader.feed_eof()

    async def respond_to(self, command: str, reply: str, timeout: float = 1.0) -> None:
        await self.wait_for_write(command, timeout)
        self.device_says(reply)


@pytest.fixture
async def harness():
    """A started client wired to a fake device, with telegrams collected.

    Async so that the stream objects and reader task are created inside the
    running loop.
    """
    reader = asyncio.StreamReader()
    writer = FakeWriter()
    # A generous timeout keeps unrelated tests from flaking; timeout behaviour
    # is exercised explicitly with its own client.
    client = EldatClient(reader, writer, command_timeout=5.0)
    harness = Harness(client=client, reader=reader, writer=writer)
    client.add_listener(harness.events.append)
    client.start()
    try:
        yield harness
    finally:
        await client.close()


@pytest.fixture
def collapser_clock():
    """A settable clock for burst-timing tests."""

    class Clock:
        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds

    return Clock()


@pytest.fixture
def instant_collapser(collapser_clock) -> BurstCollapser:
    return BurstCollapser(time_source=collapser_clock)
