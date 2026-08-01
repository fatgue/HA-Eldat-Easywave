"""Parser tests.

The frames below are byte-exact captures from a real ELDAT USB transceiver
(155A:100E, firmware 0100, "RX09 EW+KEELOQ", OEM firmware for fuhr.de), taken
over a userspace CP210x connection. Testing against captured bytes rather than
against the published specification matters here, because this firmware deviates
from the spec in ways that would otherwise go unnoticed.
"""

from __future__ import annotations

import pytest

from custom_components.eldat_easywave.eldat.parser import (
    Frame,
    Identification,
    Info,
    Mode,
    PositionCount,
    Received,
    StatusText,
    Unknown,
    decode_payload,
    encode_command,
    escape_field,
    format_position,
    iter_frames,
    normalise_address,
    parse_frame,
    split_fields,
    transmit_command,
)

# Captured verbatim from the device.
CAP_ID = "ID,155A,100E,0100\tOK\r"
CAP_GETP = "GETP,40,00,00\tOK\r"
CAP_INFO = "INFO,RX09 EW+KEELOQ,www.fuhr.de\tOK\r"
CAP_MODE = "MODE,00\tOK\r"
CAP_LED = "LED is OFF\tOK\r"
CAP_BUTTON = "BUTTON is released\tOK\r"
CAP_ECHO = "ECHO is OFF\tOK\r"
CAP_ERROR = "ERROR\r"
CAP_OK = "OK\r"


def only_frame(buffer: str) -> Frame:
    frames, rest = iter_frames(buffer)
    assert rest == ""
    assert len(frames) == 1
    return parse_frame(frames[0])


class TestFraming:
    def test_reply_and_ack_share_one_frame(self):
        """The spec implies two lines; the device sends one frame with a TAB."""
        frame = only_frame(CAP_ID)
        assert frame == Frame(payload="ID,155A,100E,0100", ack=True)
        assert not frame.is_unsolicited

    def test_bare_ok(self):
        assert only_frame(CAP_OK) == Frame(payload=None, ack=True)

    def test_bare_error(self):
        assert only_frame(CAP_ERROR) == Frame(payload=None, ack=False)

    def test_received_telegram_has_no_ack(self):
        """This is what lets us tell unsolicited traffic from command responses."""
        frame = only_frame("REC,1c14a3,A\r")
        assert frame.ack is None
        assert frame.is_unsolicited

    def test_fragmented_read_is_buffered(self):
        """A USB read can split a frame anywhere."""
        frames, rest = iter_frames("ID,155A")
        assert frames == [] and rest == "ID,155A"
        frames, rest = iter_frames(rest + ",100E,0100\tOK\r")
        assert frames == ["ID,155A,100E,0100\tOK"] and rest == ""

    def test_multiple_frames_in_one_read(self):
        frames, rest = iter_frames("REC,1c14a3,A\rREC,1c14a3,A\rOK\r")
        assert frames == ["REC,1c14a3,A", "REC,1c14a3,A", "OK"]
        assert rest == ""

    def test_crlf_and_blank_frames_tolerated(self):
        frames, rest = iter_frames("OK\r\n\r\nERROR\r\n")
        assert frames == ["OK", "ERROR"] and rest == ""

    def test_unsolicited_rec_between_command_and_ack(self):
        """A key press during an in-flight command must not be read as its ack."""
        frames, rest = iter_frames("REC,1c14a3,B\r" + CAP_GETP)
        assert rest == ""
        first, second = (parse_frame(f) for f in frames)
        assert first.is_unsolicited
        assert decode_payload(first.payload) == Received("1c14a3", "B")
        assert second.ack is True
        assert decode_payload(second.payload) == PositionCount(64, ("00", "00"))


class TestDecoding:
    def test_identification(self):
        message = decode_payload(only_frame(CAP_ID).payload)
        assert message == Identification(0x155A, 0x100E, "0100")
        assert message.version_string == "1.00"

    def test_position_count_keeps_undocumented_extra_fields(self):
        """0x40 = 64 transmit positions; the two trailing fields are undocumented."""
        message = decode_payload(only_frame(CAP_GETP).payload)
        assert message == PositionCount(count=64, extra=("00", "00"))

    def test_info_reveals_oem_firmware(self):
        message = decode_payload(only_frame(CAP_INFO).payload)
        assert message == Info(("RX09 EW+KEELOQ", "www.fuhr.de"))
        assert message.text == "RX09 EW+KEELOQ, www.fuhr.de"

    def test_mode(self):
        assert decode_payload(only_frame(CAP_MODE).payload) == Mode(0)

    @pytest.mark.parametrize(
        ("captured", "expected", "is_on"),
        [
            (CAP_LED, StatusText("LED", "OFF"), False),
            (CAP_ECHO, StatusText("ECHO", "OFF"), False),
            (CAP_BUTTON, StatusText("BUTTON", "released"), False),
            ("LED is ON\tOK\r", StatusText("LED", "ON"), True),
            ("BUTTON is pressed\tOK\r", StatusText("BUTTON", "pressed"), True),
        ],
    )
    def test_status_text(self, captured, expected, is_on):
        message = decode_payload(only_frame(captured).payload)
        assert message == expected
        assert message.is_on is is_on

    def test_received_documented_short_form(self):
        message = decode_payload("REC,1c14a3,A")
        assert message == Received("1c14a3", "A", rssi=None, channel=None)

    def test_received_observed_long_form(self):
        """Captured from the real device: channel suffix, signed-hex RSSI, 8-digit id."""
        message = decode_payload("REC00,-47,1A2B3C4D,B")
        assert message == Received("1a2b3c4d", "B", rssi=-0x47, channel=0)
        assert message.rssi == -71

    @pytest.mark.parametrize(
        ("raw", "dbm"),
        [("-47", -71), ("-46", -70), ("-4C", -76), ("-3B", -59), ("-53", -83)],
    )
    def test_rssi_is_signed_hex_not_decimal(self, raw, dbm):
        """Values such as -4C prove the field is hex; decimal parsing would fail."""
        assert decode_payload(f"REC00,{raw},1A2B3C4D,A").rssi == dbm

    @pytest.mark.parametrize("key", ["A", "B", "C", "D"])
    def test_all_keys_decode(self, key):
        assert decode_payload(f"REC,229ad6,{key}").key == key
        assert decode_payload(f"REC00,-40,1A2B3C4D,{key}").key == key

    def test_received_is_unsolicited_in_long_form_too(self):
        frame = only_frame("REC00,-47,1A2B3C4D,B\r")
        assert frame.is_unsolicited
        assert isinstance(decode_payload(frame.payload), Received)

    def test_malformed_rec_degrades_to_unknown(self):
        assert isinstance(decode_payload("REC00,-47"), Unknown)
        assert isinstance(decode_payload("RECXY,-47,1A2B3C4D,B"), Unknown)

    def test_unknown_payload_never_raises(self):
        """A firmware variant must not be able to break the connection."""
        assert decode_payload("SOMETHINGNEW,42") == Unknown("SOMETHINGNEW,42")

    def test_malformed_hex_degrades_to_unknown(self):
        assert isinstance(decode_payload("ID,ZZZZ,100E,0100"), Unknown)
        assert isinstance(decode_payload("GETP,XY"), Unknown)


class TestAddressNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1c14a3", "1c14a3"),
            ("1C14A3", "1c14a3"),
            ("001c14a3", "1c14a3"),  # spec spells it as 4 bytes
            ("0000001C14A3", "1c14a3"),
            ("A3", "0000a3"),
        ],
    )
    def test_spellings_converge(self, raw, expected):
        assert normalise_address(raw) == expected

    def test_non_hex_is_passed_through(self):
        assert normalise_address("not-hex") == "not-hex"


class TestEscaping:
    @pytest.mark.parametrize(
        ("raw", "escaped"),
        [
            ("plain", "plain"),
            ("a,b", "a\\,b"),
            ("back\\slash", "back\\\\slash"),
            ("both,\\", "both\\,\\\\"),
        ],
    )
    def test_roundtrip(self, raw, escaped):
        assert escape_field(raw) == escaped
        assert split_fields(escaped) == [raw]

    def test_split_respects_escaped_separator(self):
        assert split_fields("one,two\\,still-two,three") == [
            "one",
            "two,still-two",
            "three",
        ]

    def test_info_commas_are_plain_fields(self):
        """The device does not escape the comma in its INFO string."""
        assert split_fields("INFO,RX09 EW+KEELOQ,www.fuhr.de") == [
            "INFO",
            "RX09 EW+KEELOQ",
            "www.fuhr.de",
        ]


class TestCommandEncoding:
    def test_terminator_is_cr_only(self):
        assert encode_command("ID?") == "ID?\r"

    def test_transmit(self):
        assert transmit_command(1, "A") == "TXP,01,A\r"
        assert transmit_command(64, "d") == "TXP,40,D\r"

    def test_transmit_rejects_bad_key(self):
        with pytest.raises(ValueError, match="key must be one of"):
            transmit_command(1, "E")

    def test_position_formatting(self):
        assert format_position(1) == "01"
        assert format_position(128) == "80"
        with pytest.raises(ValueError, match="out of range"):
            format_position(256)
