"""Client tests against an in-memory device.

Replies are the byte-exact frames captured from a real transceiver
(155A:100E, RX09 EW+KEELOQ).
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.eldat_easywave.eldat.parser import Identification, Info
from custom_components.eldat_easywave.eldat.protocol import (
    EldatClient,
    EldatCommandError,
    EldatConnectionError,
    EldatTimeoutError,
)
from custom_components.eldat_easywave.eldat.telegrams import Action

from .conftest import FakeWriter

pytestmark = pytest.mark.asyncio


class TestCommands:
    async def test_identify(self, harness):
        task = asyncio.create_task(harness.client.identify())
        await harness.respond_to("ID?\r", "ID,155A,100E,0100\tOK\r")
        result = await task
        assert result == Identification(0x155A, 0x100E, "0100")
        assert result.version_string == "1.00"

    async def test_position_count(self, harness):
        task = asyncio.create_task(harness.client.position_count())
        await harness.respond_to("GETP?\r", "GETP,40,00,00\tOK\r")
        assert await task == 64

    async def test_info_identifies_oem_firmware(self, harness):
        task = asyncio.create_task(harness.client.info())
        await harness.respond_to("INFO?\r", "INFO,RX09 EW+KEELOQ,www.fuhr.de\tOK\r")
        assert await task == Info(("RX09 EW+KEELOQ", "www.fuhr.de"))

    async def test_transmit_sends_expected_frame(self, harness):
        task = asyncio.create_task(harness.client.transmit(1, "A"))
        await harness.respond_to("TXP,01,A\r", "OK\r")
        await task
        assert harness.writer.text == "TXP,01,A\r"

    async def test_set_led(self, harness):
        task = asyncio.create_task(harness.client.set_led(True))
        await harness.respond_to("LED,ON\r", "OK\r")
        await task

    async def test_led_query(self, harness):
        task = asyncio.create_task(harness.client.led())
        await harness.respond_to("LED?\r", "LED is OFF\tOK\r")
        assert await task is False


class TestErrorHandling:
    async def test_error_reply_raises(self, harness):
        task = asyncio.create_task(harness.client.execute("NONSENSE"))
        await harness.respond_to("NONSENSE\r", "ERROR\r")
        with pytest.raises(EldatCommandError, match="NONSENSE"):
            await task

    async def test_unsupported_rdp_returns_none(self, harness):
        """The RX09 EW+KEELOQ firmware rejects RDP?; callers must cope."""
        task = asyncio.create_task(harness.client.read_position(1))
        await harness.respond_to("RDP?,01\r", "ERROR\r")
        assert await task is None

    async def test_unsupported_info_returns_none(self, harness):
        task = asyncio.create_task(harness.client.info())
        await harness.respond_to("INFO?\r", "ERROR\r")
        assert await task is None

    async def test_timeout_when_device_stays_silent(self):
        client = EldatClient(asyncio.StreamReader(), FakeWriter(), command_timeout=0.05)
        client.start()
        try:
            with pytest.raises(EldatTimeoutError):
                await client.execute("ID?")
        finally:
            await client.close()

    async def test_stream_close_fails_in_flight_command(self, harness):
        task = asyncio.create_task(harness.client.execute("ID?"))
        await harness.wait_for_write("ID?\r")
        harness.hang_up()
        with pytest.raises(EldatConnectionError):
            await task

    async def test_command_after_close_raises(self, harness):
        await harness.client.close()
        with pytest.raises(EldatConnectionError):
            await harness.client.execute("ID?")


class TestFraming:
    async def test_reply_split_across_reads(self, harness):
        task = asyncio.create_task(harness.client.identify())
        await harness.wait_for_write("ID?\r")
        harness.device_says("ID,155A,")
        await asyncio.sleep(0)
        harness.device_says("100E,0100\tOK\r")
        assert (await task).product_id == 0x100E

    async def test_telegram_between_command_and_ack(self, harness):
        """A key press mid-command must not be mistaken for the ack."""
        task = asyncio.create_task(harness.client.position_count())
        await harness.wait_for_write("GETP?\r")
        harness.device_says("REC00,-47,1A2B3C4D,B\r")
        await asyncio.sleep(0)
        harness.device_says("GETP,40,00,00\tOK\r")

        assert await task == 64
        await asyncio.sleep(0)
        assert [(e.address, e.key) for e in harness.events] == [("1a2b3c4d", "B")]

    async def test_commands_are_serialised(self, harness):
        """The device forbids overlapping commands."""
        first = asyncio.create_task(harness.client.execute("LED?"))
        second = asyncio.create_task(harness.client.execute("BUTTON?"))

        await harness.wait_for_write("LED?\r")
        assert "BUTTON?" not in harness.writer.text
        harness.device_says("LED is OFF\tOK\r")
        await first

        await harness.respond_to("BUTTON?\r", "BUTTON is released\tOK\r")
        await second

    async def test_stray_ack_is_ignored(self, harness):
        """An unexpected OK must not corrupt later commands."""
        harness.device_says("OK\r")
        await asyncio.sleep(0)
        task = asyncio.create_task(harness.client.identify())
        await harness.respond_to("ID?\r", "ID,155A,100E,0100\tOK\r")
        assert (await task).vendor_id == 0x155A


class TestTelegramDispatch:
    async def test_burst_becomes_single_event(self, harness):
        for _ in range(5):
            harness.device_says("REC00,-47,1A2B3C4D,B\r")
        await asyncio.sleep(0)
        assert len(harness.events) == 1
        assert harness.events[0].action is Action.PRESS

    async def test_window_open_and_close(self, harness):
        """Opening sends code A, closing code B on the RTS16."""
        harness.device_says("REC00,-3C,1A2B3C4D,A\r")
        await asyncio.sleep(0)
        harness.device_says("REC00,-43,1A2B3C4D,B\r")
        await asyncio.sleep(0)
        assert [event.key for event in harness.events] == ["A", "B"]

    async def test_listener_can_unsubscribe(self, harness):
        seen = []
        remove = harness.client.add_listener(seen.append)
        harness.device_says("REC00,-47,1A2B3C4D,A\r")
        await asyncio.sleep(0)
        assert len(seen) == 1

        remove()
        harness.device_says("REC00,-47,1A2B3C4D,B\r")
        await asyncio.sleep(0)
        assert len(seen) == 1

    async def test_failing_listener_does_not_stop_dispatch(self, harness):
        def boom(event):
            raise RuntimeError("listener is broken")

        harness.client.add_listener(boom)
        good: list = []
        harness.client.add_listener(good.append)

        harness.device_says("REC00,-47,1A2B3C4D,A\r")
        await asyncio.sleep(0)
        assert len(good) == 1
        # And the reader survives to handle the next telegram.
        harness.device_says("REC00,-47,1A2B3C4D,B\r")
        await asyncio.sleep(0)
        assert len(good) == 2

    async def test_unknown_unsolicited_frame_is_ignored(self, harness):
        harness.device_says("SOMETHINGNEW,42\r")
        await asyncio.sleep(0)
        assert harness.events == []
        # Connection still usable.
        task = asyncio.create_task(harness.client.identify())
        await harness.respond_to("ID?\r", "ID,155A,100E,0100\tOK\r")
        await task
