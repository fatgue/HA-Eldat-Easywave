"""Tests for driving a local stick.

The teardown tests exist because of a real and thoroughly misleading bug. Closing
the device used to send ``IFC_ENABLE = UART_DISABLE``, which reads like tidy
housekeeping. Measured on the hardware, it leaves the stick answering nothing at
all, and re-enabling it on the next open does not help -- only a USB reset does.

Home Assistant opens the device twice in normal use, once for the config flow and
once for the entry setup, so the first open worked and the second was met with
silence. That silence was then misattributed, in order, to QEMU USB passthrough,
to a cascaded hub, to control transfers, and to read-timeout polling.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.eldat_easywave.eldat import usb_transport
from custom_components.eldat_easywave.eldat.hardware import (
    IFC_ENABLE,
    LINE_CTL_8N1,
    MHS_DTR_RTS_ON,
    SET_BAUDRATE,
    SET_LINE_CTL,
    SET_MHS,
    UART_ENABLE,
)
from custom_components.eldat_easywave.eldat.usbfs import UsbDevice, UsbfsError

DEVICE = UsbDevice(
    vendor_id=0x155A,
    product_id=0x100E,
    bus=3,
    address=19,
    node=__import__("pathlib").Path("/dev/bus/usb/003/019"),
    sysfs=__import__("pathlib").Path("/sys/bus/usb/devices/3-1"),
    serial="00002858",
    interface=0,
    endpoint_in=0x81,
    endpoint_out=0x01,
    packet_size=64,
)


class FakeHandle:
    """Records every control transfer, so teardown behaviour is observable."""

    def __init__(self, *, fail_request: int | None = None) -> None:
        self.info = DEVICE
        self.controls: list[tuple[int, int]] = []
        self.closed = False
        self.reset_count = 0
        self.opened = False
        self._fail_request = fail_request

    def open(self) -> None:
        self.opened = True

    def control(self, request_type, request, value, index, data=b"") -> None:
        if request == self._fail_request:
            raise UsbfsError("simulated refusal")
        self.controls.append((request, value))

    def bulk_read(self, timeout_ms=200) -> bytes:
        return b""

    def bulk_write(self, data, timeout_ms=1000) -> None:
        return None

    def reset(self) -> None:
        self.reset_count += 1

    def close(self) -> None:
        self.closed = True


class TestUartInitialisation:
    def test_sends_the_four_registers_in_order(self):
        handle = FakeHandle()
        usb_transport.initialise_uart(handle)
        assert handle.controls == [
            (IFC_ENABLE, UART_ENABLE),
            (SET_BAUDRATE, 0),
            (SET_LINE_CTL, LINE_CTL_8N1),
            (SET_MHS, MHS_DTR_RTS_ON),
        ]

    @pytest.mark.parametrize(
        ("request_id", "label"),
        [
            (IFC_ENABLE, "IFC_ENABLE"),
            (SET_BAUDRATE, "SET_BAUDRATE"),
            (SET_LINE_CTL, "SET_LINE_CTL"),
            (SET_MHS, "SET_MHS"),
        ],
    )
    def test_names_the_register_that_was_refused(self, request_id, label):
        handle = FakeHandle(fail_request=request_id)
        with pytest.raises(UsbfsError, match=label):
            usb_transport.initialise_uart(handle)

    def test_does_not_blame_usb_passthrough(self):
        """It did, and that sent the investigation off for hours."""
        handle = FakeHandle(fail_request=IFC_ENABLE)
        with pytest.raises(UsbfsError) as excinfo:
            usb_transport.initialise_uart(handle)
        assert "passthrough" not in str(excinfo.value)


class TestTeardownLeavesTheDeviceUsable:
    """The regression that cost the most time in this project."""

    async def test_close_issues_no_control_transfer(self):
        handle = FakeHandle()
        connection = usb_transport.UsbConnection(handle)
        connection.start()
        await connection.close()

        assert handle.closed
        assert handle.controls == [], (
            "closing must not send any control request: IFC_ENABLE=UART_DISABLE "
            "leaves the stick mute until a USB reset"
        )

    async def test_close_specifically_never_disables_the_uart(self):
        handle = FakeHandle()
        connection = usb_transport.UsbConnection(handle)
        connection.start()
        await connection.close()
        assert not any(request == IFC_ENABLE for request, _ in handle.controls)

    def test_the_disable_constant_is_not_imported(self):
        """Guards against the teardown creeping back in.

        Checks the name is not bound in the module rather than absent from the
        text, which the explanatory comment mentions on purpose.
        """
        assert not hasattr(usb_transport, "UART_DISABLE")

    def test_the_reason_is_recorded_next_to_the_code(self):
        """Without the why, someone will helpfully add it back."""
        from pathlib import Path

        source = Path(usb_transport.__file__).read_text(encoding="utf-8")
        assert "USB reset" in source


class TestRecovery:
    def test_recover_opens_resets_and_closes(self):
        handle = FakeHandle()
        with patch.object(usb_transport, "UsbfsDevice", return_value=handle):
            usb_transport.recover_device(DEVICE)
        assert handle.opened
        assert handle.reset_count == 1
        assert handle.closed

    def test_recover_closes_even_when_the_reset_fails(self):
        handle = FakeHandle()

        def boom():
            raise UsbfsError("reset refused")

        handle.reset = boom
        with (
            patch.object(usb_transport, "UsbfsDevice", return_value=handle),
            pytest.raises(UsbfsError),
        ):
            usb_transport.recover_device(DEVICE)
        assert handle.closed

    async def test_reset_waits_for_re_enumeration(self):
        """A reset gives the device a new address; racing it finds nothing."""
        with (
            patch.object(usb_transport, "recover_device") as recover,
            patch.object(usb_transport, "_RESET_SETTLE_SECONDS", 0),
            patch.object(asyncio, "sleep", AsyncMock()) as sleep,
        ):
            await usb_transport.reset_local_device(DEVICE)
        recover.assert_called_once_with(DEVICE)
        sleep.assert_awaited_once()


class TestWriter:
    async def test_write_buffers_and_drain_transfers(self):
        handle = FakeHandle()
        sent: list[bytes] = []
        handle.bulk_write = lambda data, timeout_ms=1000: sent.append(data)

        writer = usb_transport._UsbWriter(handle, asyncio.get_running_loop())
        writer.write(b"ID")
        writer.write(b"?\r")
        assert sent == []
        await writer.drain()
        assert sent == [b"ID?\r"]

    async def test_drain_without_pending_data_is_a_no_op(self):
        handle = FakeHandle()
        calls: list[bytes] = []
        handle.bulk_write = lambda data, timeout_ms=1000: calls.append(data)
        writer = usb_transport._UsbWriter(handle, asyncio.get_running_loop())
        await writer.drain()
        assert calls == []

    async def test_usb_failure_surfaces_as_oserror(self):
        """So the client reports a connection problem rather than leaking usbfs."""
        handle = FakeHandle()

        def boom(data, timeout_ms=1000):
            raise UsbfsError("pipe error")

        handle.bulk_write = boom
        writer = usb_transport._UsbWriter(handle, asyncio.get_running_loop())
        writer.write(b"ID?\r")
        with pytest.raises(OSError, match="pipe error"):
            await writer.drain()


class TestReaderThread:
    async def test_data_reaches_the_stream(self):
        handle = FakeHandle()
        chunks = [b"ID,155A,100E,0100\tOK\r", b"", b""]

        def read(timeout_ms=200):
            return chunks.pop(0) if chunks else b""

        handle.bulk_read = read
        connection = usb_transport.UsbConnection(handle)
        connection.start()
        try:
            data = await asyncio.wait_for(connection.reader.read(64), 2)
            assert data == b"ID,155A,100E,0100\tOK\r"
        finally:
            await connection.close()

    async def test_read_failure_ends_the_stream(self):
        """The client then sees a closed connection instead of hanging forever."""
        handle = FakeHandle()

        def boom(timeout_ms=200):
            raise UsbfsError("device gone")

        handle.bulk_read = boom
        connection = usb_transport.UsbConnection(handle)
        connection.start()
        try:
            assert await asyncio.wait_for(connection.reader.read(64), 2) == b""
        finally:
            await connection.close()
