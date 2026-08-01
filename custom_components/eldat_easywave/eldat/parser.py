"""Wire format of the ELDAT Easywave USB transceiver.

Pure functions, no I/O and no third-party dependencies, so the same module can be
used by the Home Assistant integration and by the bridge add-on.

The framing below was verified byte-exact against an ELDAT USB transceiver
(``155A:100E``, firmware ``0100``, ``INFO,RX09 EW+KEELOQ,www.fuhr.de``). It
deviates from ELDAT's published specification (SP_RTR09_DE_0809) in one
important way: a reply and its acknowledgement arrive in a *single*
CR-terminated frame, separated by a TAB -- not as two separate lines.

    b'OK\\r'                          plain acknowledgement
    b'ERROR\\r'                       plain rejection
    b'ID,155A,100E,0100\\tOK\\r'       reply + acknowledgement
    b'REC,1c14a3,A\\r'                unsolicited received telegram (no ack)

That gives a reliable way to tell solicited from unsolicited traffic: only
command responses carry an acknowledgement. Received telegrams never do, so they
can never be mistaken for the ack of an in-flight command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Easywave key codes. A four-key transmitter maps to A-D.
KEYS: Final = ("A", "B", "C", "D")

#: Frame terminator per specification (CR). LF is accepted on input as well.
TERMINATOR: Final = "\r"

#: Separates a reply payload from its trailing acknowledgement.
ACK_SEPARATOR: Final = "\t"

_FIELD_SEPARATOR: Final = ","
_ESCAPE: Final = "\\"


class EldatProtocolError(Exception):
    """Raised when a frame cannot be interpreted."""


# --------------------------------------------------------------------------
# Decoded messages
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Frame:
    """One CR-terminated frame from the device.

    ``ack`` is ``True`` for OK, ``False`` for ERROR and ``None`` when the frame
    carried no acknowledgement -- which marks it as unsolicited.
    """

    payload: str | None
    ack: bool | None

    @property
    def is_unsolicited(self) -> bool:
        return self.ack is None


@dataclass(frozen=True, slots=True)
class Identification:
    """Response to ``ID?``."""

    vendor_id: int
    product_id: int
    version: str

    @property
    def version_string(self) -> str:
        """``'0100'`` -> ``'1.00'``."""
        try:
            raw = int(self.version, 16)
        except ValueError:
            return self.version
        return f"{raw >> 8}.{raw & 0xFF:02d}"


@dataclass(frozen=True, slots=True)
class PositionCount:
    """Response to ``GETP?``.

    ``count`` is the number of transmit positions (serial numbers) burned into
    the stick -- 64 or 128 depending on the model. The observed firmware appends
    two further fields that are not documented; they are kept verbatim rather
    than discarded, so a future firmware can be inspected via diagnostics.
    """

    count: int
    extra: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Position:
    """Response to ``RDP?,<pos>`` -- absent on the FUHR OEM firmware."""

    position: int
    address: str


@dataclass(frozen=True, slots=True)
class Info:
    """Response to ``INFO?`` -- undocumented, reveals the firmware variant."""

    fields: tuple[str, ...]

    @property
    def text(self) -> str:
        return ", ".join(self.fields)


@dataclass(frozen=True, slots=True)
class Mode:
    """Response to ``MODE?`` -- undocumented. Only mode 0 is accepted."""

    value: int


@dataclass(frozen=True, slots=True)
class Received:
    """A received telegram: some transmitter sent on the air.

    Two wire formats exist. The published specification describes

        REC,<address>,<key>

    while the observed RX09 EW+KEELOQ firmware sends a richer form

        REC00,-47,1A2B3C4D,B

    with a channel suffix on the header, a signed-hex RSSI in dBm and an
    eight-digit address. Both decode into this one type; ``rssi`` and ``channel``
    are ``None`` when the short form is used.
    """

    address: str
    key: str
    rssi: int | None = None
    channel: int | None = None


@dataclass(frozen=True, slots=True)
class StatusText:
    """Human-readable status such as ``LED is OFF``."""

    subject: str
    value: str

    @property
    def is_on(self) -> bool:
        return self.value.upper() in ("ON", "PRESSED")


@dataclass(frozen=True, slots=True)
class Unknown:
    """A payload this parser does not model. Never raises -- forward compatible."""

    payload: str


Message = (
    Identification
    | PositionCount
    | Position
    | Info
    | Mode
    | Received
    | StatusText
    | Unknown
)


# --------------------------------------------------------------------------
# Field level: escaping
# --------------------------------------------------------------------------


def escape_field(value: str) -> str:
    """Escape ``,`` and ``\\`` as required by the specification."""
    out: list[str] = []
    for char in value:
        if char in (_FIELD_SEPARATOR, _ESCAPE):
            out.append(_ESCAPE)
        out.append(char)
    return "".join(out)


def split_fields(payload: str) -> list[str]:
    """Split on unescaped commas, removing the transport escapes."""
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in payload:
        if escaped:
            current.append(char)
            escaped = False
        elif char == _ESCAPE:
            escaped = True
        elif char == _FIELD_SEPARATOR:
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        # Trailing lone backslash -- keep it rather than losing data.
        current.append(_ESCAPE)
    fields.append("".join(current))
    return fields


def normalise_address(address: str) -> str:
    """Normalise an Easywave address to lower-case hex without padding noise.

    The specification describes a 22-bit serial as 4 hex bytes, right-aligned and
    zero-filled. Real devices disagree: some emit 6 characters, and the RX09
    EW+KEELOQ firmware emits 8 (``1A2B3C4D``, which does not even fit in 22 bits).
    Stripping leading zeros and lower-casing makes every spelling of the same
    address compare equal, so an entity keyed on one stays stable across
    firmwares, while addresses wider than 6 digits keep their full width.
    """
    cleaned = address.strip().lower()
    try:
        return f"{int(cleaned, 16):06x}"
    except ValueError:
        return cleaned


# --------------------------------------------------------------------------
# Frame level
# --------------------------------------------------------------------------


def iter_frames(buffer: str) -> tuple[list[str], str]:
    """Split a receive buffer into complete frames plus the unconsumed rest.

    Splits on CR or LF and tolerates CRLF, so partial USB reads and firmwares
    that terminate differently both work. Empty frames (from CRLF or keepalive
    newlines) are dropped.
    """
    frames: list[str] = []
    start = 0
    for index, char in enumerate(buffer):
        if char in ("\r", "\n"):
            chunk = buffer[start:index].strip()
            if chunk:
                frames.append(chunk)
            start = index + 1
    return frames, buffer[start:]


def parse_frame(frame: str) -> Frame:
    """Split a frame into its payload and acknowledgement.

    Handles ``payload\\tOK``, bare ``OK``/``ERROR`` and unsolicited payloads.
    """
    text = frame.strip()
    if not text:
        raise EldatProtocolError("empty frame")

    payload: str | None = text
    ack: bool | None = None

    if ACK_SEPARATOR in text:
        payload, _, tail = text.rpartition(ACK_SEPARATOR)
        ack = _parse_ack(tail.strip())
        if ack is None:
            # A TAB that was not an ack separator -- treat the whole thing as payload.
            payload, ack = text, None
    else:
        standalone = _parse_ack(text)
        if standalone is not None:
            payload, ack = None, standalone

    return Frame(payload=payload or None, ack=ack)


def parse_signed_hex(text: str) -> int | None:
    """Parse the firmware's signed-hex numbers, e.g. ``-4C`` -> ``-76``."""
    cleaned = text.strip()
    negative = cleaned.startswith("-")
    if negative or cleaned.startswith("+"):
        cleaned = cleaned[1:]
    if not cleaned:
        return None
    try:
        value = int(cleaned, 16)
    except ValueError:
        return None
    return -value if negative else value


def _decode_received(head: str, fields: list[str]) -> Received | None:
    """Decode both the documented and the observed ``REC`` layouts."""
    suffix = head[3:]
    channel: int | None = None
    if suffix:
        channel = parse_signed_hex(suffix)
        if channel is None:
            return None

    if len(fields) >= 4:
        # REC00,<rssi>,<address>,<key>
        return Received(
            address=normalise_address(fields[2]),
            key=fields[3].strip().upper(),
            rssi=parse_signed_hex(fields[1]),
            channel=channel,
        )
    if len(fields) == 3:
        # REC,<address>,<key> as published
        return Received(
            address=normalise_address(fields[1]),
            key=fields[2].strip().upper(),
            channel=channel,
        )
    return None


def _parse_ack(text: str) -> bool | None:
    upper = text.upper()
    if upper == "OK":
        return True
    if upper == "ERROR":
        return False
    return None


def decode_payload(payload: str) -> Message:
    """Interpret a frame payload as a typed message.

    Never raises on unrecognised input -- unknown payloads become ``Unknown`` so
    that a firmware variant cannot take the connection down.
    """
    fields = split_fields(payload)
    head = fields[0].strip().upper()

    if head.startswith("REC"):
        received = _decode_received(head, fields)
        if received is not None:
            return received

    if head == "ID" and len(fields) >= 4:
        try:
            return Identification(
                vendor_id=int(fields[1], 16),
                product_id=int(fields[2], 16),
                version=fields[3].strip(),
            )
        except ValueError:
            return Unknown(payload)

    if head == "GETP" and len(fields) >= 2:
        try:
            return PositionCount(
                count=int(fields[1], 16),
                extra=tuple(field.strip() for field in fields[2:]),
            )
        except ValueError:
            return Unknown(payload)

    if head == "RDP" and len(fields) >= 3:
        try:
            return Position(
                position=int(fields[1], 16), address=normalise_address(fields[2])
            )
        except ValueError:
            return Unknown(payload)

    if head == "MODE" and len(fields) >= 2:
        try:
            return Mode(value=int(fields[1], 16))
        except ValueError:
            return Unknown(payload)

    if head == "INFO" and len(fields) >= 2:
        return Info(fields=tuple(field.strip() for field in fields[1:]))

    # "LED is OFF", "ECHO is OFF", "BUTTON is released"
    parts = payload.split()
    if len(parts) == 3 and parts[1].lower() == "is":
        return StatusText(subject=parts[0].upper(), value=parts[2])

    return Unknown(payload)


# --------------------------------------------------------------------------
# Command encoding
# --------------------------------------------------------------------------


def encode_command(*fields: str) -> str:
    """Build a command frame, escaping the fields and appending the terminator."""
    return _FIELD_SEPARATOR.join(escape_field(field) for field in fields) + TERMINATOR


def format_position(position: int) -> str:
    """Positions travel as one hex byte in upper case, e.g. ``1`` -> ``01``."""
    if not 0 <= position <= 0xFF:
        raise ValueError(f"position out of range: {position}")
    return f"{position:02X}"


def transmit_command(position: int, key: str) -> str:
    """``TXP,<pos>,<key>`` -- send an Easywave telegram from a stick position."""
    normalised = key.strip().upper()
    if normalised not in KEYS:
        raise ValueError(f"key must be one of {KEYS}, got {key!r}")
    return encode_command("TXP", format_position(position), normalised)
