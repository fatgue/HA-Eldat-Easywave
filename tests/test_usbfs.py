"""Tests for the pure-Python usbfs driver.

The ioctl request numbers and structure layouts have to be byte-exact or the
kernel rejects the calls, and there is no forgiving middle ground -- so they are
checked against the values every usbfs consumer uses, and against the sizes the
kernel headers imply.

Device discovery is checked against a fabricated sysfs tree. That it reads sysfs
at all is the point: it yields endpoints and the serial number without a single
control transfer, and control transfers are what fail on a device passed through
to a virtual machine.
"""

from __future__ import annotations

import ctypes

import pytest

from custom_components.eldat_easywave.eldat import usbfs
from custom_components.eldat_easywave.eldat.hardware import (
    ELDAT_PRODUCT_IDS,
    ELDAT_VENDOR_ID,
)


class TestIoctlEncoding:
    """Values as used by libusb and every other usbfs consumer on 64-bit Linux."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("USBDEVFS_CONTROL", 0xC0185500),
            ("USBDEVFS_BULK", 0xC0185502),
            ("USBDEVFS_SETCONFIGURATION", 0x80045505),
            ("USBDEVFS_CLAIMINTERFACE", 0x8004550F),
            ("USBDEVFS_RELEASEINTERFACE", 0x80045510),
            ("USBDEVFS_RESET", 0x00005514),
            ("USBDEVFS_CLEAR_HALT", 0x80045515),
        ],
    )
    def test_request_numbers(self, name, expected):
        assert getattr(usbfs, name) == expected, f"{name} would be rejected"

    def test_ctrltransfer_layout(self):
        """struct usbdevfs_ctrltransfer: 8 bytes of fields, u32, then a pointer."""
        assert ctypes.sizeof(usbfs._CtrlTransfer) == 24

    def test_bulktransfer_layout(self):
        """struct usbdevfs_bulktransfer: three uints, then a pointer."""
        assert ctypes.sizeof(usbfs._BulkTransfer) == 24

    def test_direction_bits_are_not_swapped(self):
        """_IOR and _IOW are easy to transpose, and the kernel would say EINVAL."""
        assert usbfs._ior(ord("U"), 1, 4) != usbfs._iow(ord("U"), 1, 4)
        assert usbfs._ior(ord("U"), 1, 4) >> 30 == 2
        assert usbfs._iow(ord("U"), 1, 4) >> 30 == 1
        assert usbfs._iowr(ord("U"), 1, 4) >> 30 == 3


def build_sysfs(root, *, serial="00002858", product=0x100E, endpoints=True):
    """Fabricate the parts of sysfs the driver reads."""
    devices = root / "sys" / "bus" / "usb" / "devices"
    device = devices / "3-2.4.1"
    device.mkdir(parents=True)
    (device / "idVendor").write_text("155a\n")
    (device / "idProduct").write_text(f"{product:04x}\n")
    (device / "busnum").write_text("3\n")
    (device / "devnum").write_text("19\n")
    if serial is not None:
        (device / "serial").write_text(f"{serial}\n")

    interface = device / "3-2.4.1:1.0"
    interface.mkdir()
    (interface / "bInterfaceNumber").write_text("00\n")
    if endpoints:
        for address, direction in (("81", "in"), ("01", "out")):
            endpoint = interface / f"ep_{address}"
            endpoint.mkdir()
            (endpoint / "bEndpointAddress").write_text(f"{address}\n")
            (endpoint / "type").write_text("Bulk\n")
            (endpoint / "wMaxPacketSize").write_text("0040\n")
            (endpoint / "direction").write_text(f"{direction}\n")

    nodes = root / "dev" / "bus" / "usb" / "003"
    nodes.mkdir(parents=True)
    (nodes / "019").write_text("")
    return devices, root / "dev" / "bus" / "usb"


@pytest.fixture
def fake_sysfs(tmp_path, monkeypatch):
    devices, nodes = build_sysfs(tmp_path)
    monkeypatch.setattr(usbfs, "SYSFS_USB_DEVICES", devices)
    monkeypatch.setattr(usbfs, "DEV_BUS_USB", nodes)
    return tmp_path


class TestDiscovery:
    def test_finds_the_stick(self, fake_sysfs):
        found = usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)
        assert len(found) == 1
        device = found[0]
        assert device.vendor_id == 0x155A
        assert device.product_id == 0x100E
        assert device.usb_ids == "155A:100E"

    def test_serial_comes_from_sysfs_not_the_device(self, fake_sysfs):
        """The string-descriptor request is what fails behind QEMU."""
        assert usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)[0].serial == (
            "00002858"
        )

    def test_endpoints_match_the_hardware(self, fake_sysfs):
        device = usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)[0]
        assert device.interface == 0
        assert device.endpoint_in == 0x81
        assert device.endpoint_out == 0x01
        assert device.packet_size == 64

    def test_node_path_is_derived_from_bus_and_address(self, fake_sysfs):
        device = usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)[0]
        assert device.node.name == "019"
        assert device.node.parent.name == "003"

    def test_missing_serial_is_tolerated(self, tmp_path, monkeypatch):
        devices, nodes = build_sysfs(tmp_path, serial=None)
        monkeypatch.setattr(usbfs, "SYSFS_USB_DEVICES", devices)
        monkeypatch.setattr(usbfs, "DEV_BUS_USB", nodes)
        assert (
            usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)[0].serial is None
        )

    def test_device_without_bulk_endpoints_is_skipped(self, tmp_path, monkeypatch):
        devices, nodes = build_sysfs(tmp_path, endpoints=False)
        monkeypatch.setattr(usbfs, "SYSFS_USB_DEVICES", devices)
        monkeypatch.setattr(usbfs, "DEV_BUS_USB", nodes)
        assert usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS) == []

    def test_other_vendors_are_ignored(self, fake_sysfs):
        assert usbfs.enumerate_devices(0x10C4, ELDAT_PRODUCT_IDS) == []

    def test_unlisted_product_is_ignored(self, fake_sysfs):
        assert usbfs.enumerate_devices(ELDAT_VENDOR_ID, (0x1006,)) == []

    def test_absent_sysfs_is_distinguishable_from_no_stick(self, tmp_path, monkeypatch):
        """A caller must be able to tell "wrong machine" from "nothing plugged in"."""
        monkeypatch.setattr(usbfs, "SYSFS_USB_DEVICES", tmp_path / "nowhere")
        with pytest.raises(usbfs.UsbfsUnavailableError, match="needs Linux"):
            usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)

    def test_missing_device_nodes_names_the_container_problem(
        self, tmp_path, monkeypatch
    ):
        devices, _ = build_sysfs(tmp_path)
        monkeypatch.setattr(usbfs, "SYSFS_USB_DEVICES", devices)
        monkeypatch.setattr(usbfs, "DEV_BUS_USB", tmp_path / "no-dev")
        with pytest.raises(usbfs.UsbfsUnavailableError, match="/dev has to be mapped"):
            usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)


class TestDescription:
    def test_includes_ids_and_serial(self, fake_sysfs):
        device = usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)[0]
        text = device.describe({0x100E: "ELDAT USB Device V1"})
        assert "155A:100E" in text
        assert "ELDAT USB Device V1" in text
        assert "00002858" in text

    def test_unreadable_serial_is_said_so(self, tmp_path, monkeypatch):
        devices, nodes = build_sysfs(tmp_path, serial=None)
        monkeypatch.setattr(usbfs, "SYSFS_USB_DEVICES", devices)
        monkeypatch.setattr(usbfs, "DEV_BUS_USB", nodes)
        device = usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)[0]
        assert "unreadable" in device.describe(None)


class TestOpening:
    def test_not_open_is_reported(self, fake_sysfs):
        device = usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)[0]
        handle = usbfs.UsbfsDevice(device)
        with pytest.raises(usbfs.UsbfsError, match="not open"):
            handle.bulk_read()

    def test_vanished_node_is_reported(self, fake_sysfs):
        device = usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)[0]
        device.node.unlink()
        with pytest.raises(usbfs.UsbfsError, match="disappeared"):
            usbfs.UsbfsDevice(device).open()

    def test_close_without_open_is_harmless(self, fake_sysfs):
        device = usbfs.enumerate_devices(ELDAT_VENDOR_ID, ELDAT_PRODUCT_IDS)[0]
        usbfs.UsbfsDevice(device).close()


class TestConstantsStayInStep:
    """The add-on cannot import from the integration, so both carry a copy.

    The Supervisor builds an add-on with the add-on's directory as the Docker
    context, so it cannot reach the rest of the repository. This test is what
    makes that duplication safe.
    """

    @pytest.fixture(scope="class")
    def addon(self):
        import sys
        from pathlib import Path

        root = Path(__file__).parent.parent / "addon" / "eldat_easywave_bridge"
        sys.path.insert(0, str(root))
        pytest.importorskip("usb.core", reason="pyusb is required for the add-on")
        from bridge import cp210x

        return cp210x

    def test_vendor_and_product_ids(self, addon):
        from custom_components.eldat_easywave.eldat import hardware

        assert addon.ELDAT_VENDOR_ID == hardware.ELDAT_VENDOR_ID
        assert addon.ELDAT_PRODUCT_IDS == hardware.ELDAT_PRODUCT_IDS

    def test_product_names(self, addon):
        from custom_components.eldat_easywave.eldat import hardware

        assert addon.ELDAT_PRODUCT_NAMES == hardware.ELDAT_PRODUCT_NAMES

    def test_cp210x_registers(self, addon):
        from custom_components.eldat_easywave.eldat import hardware

        assert addon._REQTYPE_HOST_TO_DEVICE == hardware.REQTYPE_HOST_TO_DEVICE
        assert addon._IFC_ENABLE == hardware.IFC_ENABLE
        assert addon._SET_LINE_CTL == hardware.SET_LINE_CTL
        assert addon._SET_MHS == hardware.SET_MHS
        assert addon._SET_BAUDRATE == hardware.SET_BAUDRATE
        assert addon._UART_ENABLE == hardware.UART_ENABLE
        assert addon._UART_DISABLE == hardware.UART_DISABLE
        assert addon._LINE_CTL_8N1 == hardware.LINE_CTL_8N1
        assert addon._MHS_DTR_RTS_ON == hardware.MHS_DTR_RTS_ON
        assert addon.BAUDRATE == hardware.BAUDRATE
