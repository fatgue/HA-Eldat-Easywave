"""Tests for the bridge add-on.

The add-on cannot be exercised end to end without a USB device, but its
decision-making is plain logic and worth pinning down -- especially the failure
reporting, since a misleading message here sends people looking for the wrong
problem entirely.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ADDON = Path(__file__).parent.parent / "addon" / "eldat_easywave_bridge"
sys.path.insert(0, str(ADDON))

pytest.importorskip("usb.core", reason="pyusb is required for the add-on tests")

from bridge import kernel  # noqa: E402
from bridge.__main__ import _parse_product_ids, build_config  # noqa: E402
from bridge.cp210x import (  # noqa: E402
    BAUDRATE,
    ELDAT_PRODUCT_IDS,
    ELDAT_PRODUCT_NAMES,
    ELDAT_VENDOR_ID,
    _diagnose_backend_failure,
)


class TestDeviceIdentification:
    def test_vendor_id(self):
        assert ELDAT_VENDOR_ID == 0x155A

    def test_product_ids_match_eldats_own_driver(self):
        """utcvx-ew.inf claims 0x1005-0x1013."""
        assert ELDAT_PRODUCT_IDS[0] == 0x1005
        assert ELDAT_PRODUCT_IDS[-1] == 0x1013
        assert len(ELDAT_PRODUCT_IDS) == 15

    def test_every_product_id_has_a_name(self):
        assert set(ELDAT_PRODUCT_NAMES) == set(ELDAT_PRODUCT_IDS)

    def test_the_developed_against_stick_is_covered(self):
        """0x100E is the one the kernel driver does not know."""
        assert 0x100E in ELDAT_PRODUCT_IDS
        assert ELDAT_PRODUCT_NAMES[0x100E] == "ELDAT USB Device V1"

    def test_the_kernel_known_rx09_is_covered_too(self):
        assert ELDAT_PRODUCT_NAMES[0x1006].startswith("USB Transceiver Easywave")

    def test_baudrate_is_the_documented_one(self):
        assert BAUDRATE == 57600


class TestBackendDiagnosis:
    """A ``None`` backend from pyusb has two very different causes."""

    def test_missing_library_says_so(self):
        message = _diagnose_backend_failure(None)
        assert "not installed" in message
        assert "ELDAT_LIBUSB_PATH" in message

    def test_library_present_but_no_usb_access_points_at_the_add_on_options(
        self, monkeypatch
    ):
        """The case a container without USB access hits."""
        monkeypatch.setattr("os.path.isdir", lambda path: False)
        message = _diagnose_backend_failure("/usr/lib/libusb-1.0.so.0")
        assert "/dev/bus/usb is missing" in message
        assert "full_access: true" in message
        # Virtualised installs need the device passed through to the guest too,
        # which is exactly the setup this was found on.
        assert "passed through" in message

    def test_library_present_and_usb_present_reports_init_failure(self, monkeypatch):
        monkeypatch.setattr("os.path.isdir", lambda path: True)
        message = _diagnose_backend_failure("/usr/lib/libusb-1.0.so.0")
        assert "libusb_init() failed" in message

    def test_never_claims_a_present_library_is_absent(self, monkeypatch):
        """The bug this function exists to prevent."""
        monkeypatch.setattr("os.path.isdir", lambda path: False)
        message = _diagnose_backend_failure("/usr/lib/libusb-1.0.so.0")
        assert "not installed" not in message


class TestOptionParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("100E", (0x100E,)),
            ("100E,1006", (0x100E, 0x1006)),
            ("100e, 1006", (0x100E, 0x1006)),
            (["100E", "1006"], (0x100E, 0x1006)),
        ],
    )
    def test_explicit_ids(self, raw, expected):
        assert _parse_product_ids(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", [], "not-hex", 42])
    def test_unusable_input_falls_back_to_every_known_id(self, raw):
        """Better to scan all ELDAT ids than to find nothing at all."""
        assert _parse_product_ids(raw) == ELDAT_PRODUCT_IDS

    def test_partially_bad_input_keeps_the_good_entries(self):
        assert _parse_product_ids("100E,nonsense") == (0x100E,)

    def test_defaults(self, monkeypatch):
        for name in (
            "ELDAT_HOST",
            "ELDAT_PORT",
            "ELDAT_PRODUCT_IDS",
            "ELDAT_PREFER_KERNEL",
        ):
            monkeypatch.delenv(name, raising=False)
        config = build_config()
        assert config.host == "0.0.0.0"  # noqa: S104 - a bridge must be reachable
        assert config.port == 5000
        assert config.prefer_kernel is True
        assert config.product_ids == ELDAT_PRODUCT_IDS

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("false", False), ("0", False), ("no", False), ("true", True)],
    )
    def test_prefer_kernel_from_environment(self, monkeypatch, value, expected):
        monkeypatch.setenv("ELDAT_PREFER_KERNEL", value)
        assert build_config().prefer_kernel is expected

    def test_port_from_environment(self, monkeypatch):
        monkeypatch.setenv("ELDAT_PORT", "5111")
        assert build_config().port == 5111


class TestKernelBinding:
    def test_absent_driver_is_reported_not_raised(self, monkeypatch, tmp_path):
        """An unwritable or absent sysfs is an expected outcome, not an error."""
        monkeypatch.setattr(kernel, "_NEW_ID", tmp_path / "nope" / "new_id")
        assert kernel.driver_present() is False
        assert kernel.bind(0x155A, 0x100E) is False

    def test_bind_writes_the_id_in_the_kernel_format(self, monkeypatch, tmp_path):
        driver = tmp_path / "cp210x"
        driver.mkdir()
        new_id = driver / "new_id"
        monkeypatch.setattr(kernel, "_NEW_ID", new_id)

        assert kernel.bind(0x155A, 0x100E) is True
        assert new_id.read_text() == "155a 100e\n"

    def test_already_known_id_counts_as_success(self, monkeypatch, tmp_path):
        """The driver answers EEXIST when it already has the pair."""
        import errno

        driver = tmp_path / "cp210x"
        driver.mkdir()
        new_id = driver / "new_id"
        new_id.touch()
        monkeypatch.setattr(kernel, "_NEW_ID", new_id)

        def refuse(*args, **kwargs):
            raise OSError(errno.EEXIST, "File exists")

        monkeypatch.setattr(type(new_id), "write_text", refuse)
        assert kernel.bind(0x155A, 0x100E) is True

    def test_find_tty_gives_up_quietly(self, monkeypatch):
        monkeypatch.setattr(kernel, "_by_id_path", lambda serial: None)
        monkeypatch.setattr(kernel, "_bound_tty", lambda: None)
        assert kernel.find_tty("00002858", timeout=0) is None

    def test_find_tty_prefers_a_stable_by_id_path(self, monkeypatch):
        monkeypatch.setattr(
            kernel, "_by_id_path", lambda serial: "/dev/serial/by-id/eldat"
        )
        monkeypatch.setattr(kernel, "_bound_tty", lambda: "/dev/ttyUSB0")
        assert kernel.find_tty("00002858", timeout=0) == "/dev/serial/by-id/eldat"


class TestSerialNumberIsCosmetic:
    """Reading the serial must never be able to take the bridge down.

    A USB device passed through to a virtual machine may refuse the language-id
    request, which pyusb surfaces as ValueError("The device has no langid").
    That happened on a real Home Assistant OS guest, and because the serial was
    read for a log line, it crashed the add-on on startup.
    """

    class _Refusing:
        idVendor = 0x155A
        idProduct = 0x100E

        @property
        def serial_number(self):
            raise ValueError(
                "The device has no langid (permission issue, no string "
                "descriptors supported or device error)"
            )

    class _Erroring:
        idVendor = 0x155A
        idProduct = 0x100E

        @property
        def serial_number(self):
            import usb.core

            raise usb.core.USBError("pipe error")

    class _Working:
        idVendor = 0x155A
        idProduct = 0x100E
        serial_number = "00002858"

    def test_langid_failure_yields_none(self):
        from bridge.cp210x import read_serial_number

        assert read_serial_number(self._Refusing()) is None

    def test_usb_error_yields_none(self):
        from bridge.cp210x import read_serial_number

        assert read_serial_number(self._Erroring()) is None

    def test_readable_serial_is_returned(self):
        from bridge.cp210x import read_serial_number

        assert read_serial_number(self._Working()) == "00002858"

    def test_describe_survives_an_unreadable_serial(self):
        from bridge.cp210x import describe

        text = describe(self._Refusing())
        assert "155A:100E" in text
        assert "ELDAT USB Device V1" in text
        assert "unreadable" in text

    def test_describe_still_reports_a_readable_serial(self):
        from bridge.cp210x import describe

        assert "00002858" in describe(self._Working())


class TestLogLevel:
    """run.sh is plain sh, so the level has to be resolved in Python.

    Mapping it in run.sh would mean bashio, and bashio talks to the Supervisor
    API -- which without the hassio_api permission fails on every start with
    "Unable to access the API, forbidden".
    """

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch):
        monkeypatch.delenv("ELDAT_LOG_LEVEL", raising=False)

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ("trace", "DEBUG"),
            ("debug", "DEBUG"),
            ("info", "INFO"),
            ("notice", "INFO"),
            ("warning", "WARNING"),
            ("error", "ERROR"),
            ("fatal", "CRITICAL"),
        ],
    )
    def test_supervisor_levels_map_onto_python(self, monkeypatch, configured, expected):
        from bridge import __main__ as entry

        monkeypatch.setattr(entry, "_load_options", lambda: {"log_level": configured})
        assert entry._log_level() == expected

    def test_default_without_options_or_environment(self, monkeypatch):
        from bridge import __main__ as entry

        monkeypatch.setattr(entry, "_load_options", lambda: {})
        assert entry._log_level() == "INFO"

    def test_environment_is_the_fallback(self, monkeypatch):
        from bridge import __main__ as entry

        monkeypatch.setattr(entry, "_load_options", lambda: {})
        monkeypatch.setenv("ELDAT_LOG_LEVEL", "debug")
        assert entry._log_level() == "DEBUG"

    def test_options_win_over_the_environment(self, monkeypatch):
        from bridge import __main__ as entry

        monkeypatch.setattr(entry, "_load_options", lambda: {"log_level": "error"})
        monkeypatch.setenv("ELDAT_LOG_LEVEL", "debug")
        assert entry._log_level() == "ERROR"

    def test_unknown_level_is_passed_through_uppercased(self, monkeypatch):
        from bridge import __main__ as entry

        monkeypatch.setattr(entry, "_load_options", lambda: {"log_level": "critical"})
        assert entry._log_level() == "CRITICAL"


class TestRunScript:
    def test_does_not_invoke_bashio(self):
        """It would need Supervisor API access the add-on does not request.

        Checks for actual use -- the shebang and ``bashio::`` calls -- rather than
        the mere word, which the script mentions to explain its own absence.
        """
        text = (ADDON / "run.sh").read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh")
        assert "bashio::" not in text
        assert "env bashio" not in text

    def test_starts_the_bridge_module(self):
        text = (ADDON / "run.sh").read_text(encoding="utf-8")
        assert "python3 -m bridge" in text
