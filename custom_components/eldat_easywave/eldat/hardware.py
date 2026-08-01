"""Hardware identity and CP210x registers.

Duplicated in ``addon/eldat_easywave_bridge/bridge/cp210x.py``, and unavoidably
so: the Supervisor builds an add-on with the add-on's own directory as the Docker
context, so it cannot reach files elsewhere in the repository. ``tests/`` asserts
the two copies agree, which is what keeps the duplication safe.
"""

from __future__ import annotations

from typing import Final

#: ELDAT's USB vendor id. The device strings still say "Silicon Labs".
ELDAT_VENDOR_ID: Final = 0x155A

#: Product ids claimed by ELDAT's own Windows driver (utcvx-ew.inf). The Linux
#: cp210x driver knows only 0x1006, which is why the others need a userspace
#: driver or a new_id bind.
ELDAT_PRODUCT_IDS: Final = tuple(range(0x1005, 0x1014))

#: Model names, straight from ELDAT's driver .inf, for nicer logs and UI.
ELDAT_PRODUCT_NAMES: Final = {
    0x1005: "Easywave Transceiver",
    0x1006: "USB Transceiver Easywave (RX09)",
    0x1007: "USB Transceiver Tester 868MHz",
    0x1008: "USB Transceiver Tester 433MHz",
    0x1009: "USB Transceiver Easywave V2",
    0x100A: "USB Transceiver Easywave V3",
    0x100B: "USB Transceiver Easywave V4",
    0x100C: "USB Transceiver Easywave V5",
    0x100D: "USB Transceiver Easywave V6",
    0x100E: "ELDAT USB Device V1",
    0x100F: "ELDAT USB Device V2",
    0x1010: "ELDAT USB Device V3",
    0x1011: "ELDAT USB Device V4",
    0x1012: "ELDAT USB Device V5",
    0x1013: "ELDAT USB Device V6",
}

# --- CP210x vendor requests, from drivers/usb/serial/cp210x.c ---

#: Host to device, vendor request, interface recipient.
REQTYPE_HOST_TO_DEVICE: Final = 0x41

IFC_ENABLE: Final = 0x00
SET_LINE_CTL: Final = 0x03
SET_MHS: Final = 0x07
SET_BAUDRATE: Final = 0x1E

UART_ENABLE: Final = 0x0001
UART_DISABLE: Final = 0x0000

#: BITS_DATA_8 | BITS_PARITY_NONE | BITS_STOP_1 -- the 8N1 the stick requires.
LINE_CTL_8N1: Final = 0x0800

#: CONTROL_DTR | CONTROL_RTS | CONTROL_WRITE_DTR | CONTROL_WRITE_RTS
MHS_DTR_RTS_ON: Final = 0x0303

#: Fixed by the ELDAT specification: 57600 8N1, no flow control.
BAUDRATE: Final = 57600


def describe_product(product_id: int) -> str:
    return ELDAT_PRODUCT_NAMES.get(product_id, "unknown ELDAT device")
