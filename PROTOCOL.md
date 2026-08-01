# The ELDAT Easywave transceiver protocol, as actually observed

ELDAT publishes a specification for the RX09/RTR09 USB transceiver
([SP_RTR09_DE_0809](https://www.eldat.de/produkte/_div/rx09e_sp_de.pdf)). Useful,
but not sufficient: the unit this integration was developed against deviates from
it in ways that silently break naive implementations. This file records what was
measured on the wire, so the next person does not have to rediscover it.

Everything below was captured from real hardware. One substitution: the window
contact's transmitter address is shown throughout as `1A2B3C4D` rather than its
real value, to keep a private device identifier out of a public repository. Every
other byte, timing and value is as measured.

Everything below was captured from:

```text
USB          155A:100E  "ELDAT USB Device V1"   serial 00002858
ID?       -> ID,155A,100E,0100                  (firmware 1.00)
INFO?     -> INFO,RX09 EW+KEELOQ,www.fuhr.de
```

That `INFO?` line is the key to the whole puzzle: this is an **RX09 running OEM
firmware for Carl FUHR** (door hardware), supporting Easywave *and* KeeLoq. ELDAT
sells the same silicon under many product ids, and the firmware differs between
them.

## Hardware and driver

The transceivers are Silicon Labs **CP210x** USB-UART bridges behind ELDAT's own
USB vendor id `0x155A`. ELDAT's Windows driver (`utcvx-ew.inf`, shipping
`silabser.sys`) claims product ids `0x1005`–`0x1013`:

| PID | Name | PID | Name |
|---|---|---|---|
| 1005 | Easywave Transceiver | 100C | USB Transceiver Easywave V5 |
| 1006 | USB Transceiver Easywave (RX09) | 100D | USB Transceiver Easywave V6 |
| 1007 | USB Transceiver Tester 868MHz | 100E | ELDAT USB Device V1 |
| 1008 | USB Transceiver Tester 433MHz | 100F | ELDAT USB Device V2 |
| 1009 | USB Transceiver Easywave V2 | 1010 | ELDAT USB Device V3 |
| 100A | USB Transceiver Easywave V3 | 1011 | ELDAT USB Device V4 |
| 100B | USB Transceiver Easywave V4 | 1012–1013 | ELDAT USB Device V5–V6 |

**The Linux `cp210x` driver lists only `0x155A:0x1006`.** See
[`drivers/usb/serial/cp210x.c`](https://github.com/torvalds/linux/blob/master/drivers/usb/serial/cp210x.c);
the entry arrived in 4.16 via the patch *"USB: serial: cp210x: add ELDAT Easywave
RX09 id"*. Any other ELDAT stick therefore gets **no `/dev/ttyUSB*` node at all**,
which is the entire reason this project ships a bridge add-on rather than talking
to a serial port directly.

Two ways out, both implemented in `addon/eldat_easywave_bridge`:

1. Register the id with the loaded driver:
   `echo "155a 100e" > /sys/bus/usb-serial/drivers/cp210x/new_id`
2. Drive the chip from userspace over libusb. Initialisation, with register
   numbers from `cp210x.c`:

   | Step | `bRequest` | Value / data |
   |---|---|---|
   | `IFC_ENABLE` | `0x00` | `0x0001` (`UART_ENABLE`) |
   | `SET_BAUDRATE` | `0x1E` | `57600` as 4-byte little-endian data |
   | `SET_LINE_CTL` | `0x03` | `0x0800` (8 data bits, no parity, 1 stop) |
   | `SET_MHS` | `0x07` | `0x0303` (DTR+RTS on, write both) |

   `bmRequestType` is `0x41`; data then flows over the two bulk endpoints.

## Serial parameters

57600 baud, 8 data bits, no parity, 1 stop bit, **no flow control**. As
documented.

## Framing — differs from the specification

The specification presents replies and acknowledgements as separate items, which
reads as separate lines. On the wire they are **one CR-terminated frame with a TAB
between reply and acknowledgement**:

```text
b'OK\r'                            plain acknowledgement
b'ERROR\r'                         plain rejection
b'ID,155A,100E,0100\tOK\r'         reply + acknowledgement, TAB-separated (0x09)
b'REC00,-47,1A2B3C4D,B\r'          unsolicited telegram, no acknowledgement
```

This yields a robust way to separate solicited from unsolicited traffic: **only
command responses carry an acknowledgement.** A key press arriving in the middle
of an in-flight command therefore cannot be mistaken for that command's ack —
much safer than matching on payload text.

Fields are comma-separated. A literal `,` or `\` inside a field is escaped with a
preceding `\`. Note the device does *not* escape the comma in its own `INFO?`
string; that is simply a multi-field reply.

## Command set on this firmware

| Command | Response | Notes |
|---|---|---|
| `ID?` | `ID,155A,100E,0100\tOK` | vendor, product, firmware version |
| `GETP?` | `GETP,40,00,00\tOK` | **64** transmit positions. Two extra undocumented fields |
| `INFO?` | `INFO,RX09 EW+KEELOQ,www.fuhr.de\tOK` | **undocumented**, identifies the OEM build |
| `MODE?` | `MODE,00\tOK` | **undocumented**. Only `MODE,00` is accepted; `01`/`02` give `ERROR` |
| `TXP,<pos>,<key>` | `OK` | transmit from a position; `<key>` is `A`–`D` |
| `LED?` / `LED,ON\|OFF` | `LED is OFF\tOK` | note the casing differs from the spec's "Led is ON" |
| `ECHO?` / `ECHO,ON\|OFF` | `ECHO is OFF\tOK` | default off |
| `BUTTON?` | `BUTTON is released\tOK` | the pushbutton on the stick |
| `RDP?,<pos>` | **`ERROR`** | **not implemented on this firmware** — see below |
| `Bootloader` | starts the bootloader | do not send; needs a reset to recover |

Unsupported commands return a bare `ERROR`, so probing is safe and cheap.

### Positions are 0-based

`GETP?` reports a **count, not a highest number**, and the specification never says
where counting starts. Measured on a stick answering `GETP,40` (64):

```text
TXP,00,A -> OK          TXP,3F,A -> OK
TXP,01,A -> OK          TXP,40,A -> ERROR
...                     TXP,FF,A -> ERROR
```

So the valid range is `0x00`–`0x3F`. Assuming `1..count` gets it wrong twice over:
it offers `0x40`, which the stick refuses, and hides `0x00`, which works. All 64
positions in `0..63` were exercised against the hardware and accepted, and `64`
was confirmed to be refused.

Every key code `A`–`D` is accepted on every valid position.

The stick does **not** report its own transmissions back as `REC` telegrams, so
there is no self-reception loop to filter out.

### The missing `RDP?` matters

`RDP?` is meant to report the serial number stored at a transmit position. Without
it there is **no way to enumerate the stick's 64 addresses**, so a user interface
cannot show them, and pairing cannot be verified from software. The only workable
procedure is:

1. Choose a position (say `01`).
2. Put the Easywave receiver into learning mode.
3. Send `TXP,01,A` and let the receiver learn whatever address it hears.

That is why the integration exposes a `send_telegram` service and plain `button`
entities — they exist so a receiver can be taught.

## Received telegrams — substantially different

The specification describes `REC,<22-bit serial>,<key>`. What the device actually
sends is:

```text
REC00,-47,1A2B3C4D,B
│     │   │        └─ key code A-D
│     │   └────────── transmitter address, 8 hex digits
│     └────────────── RSSI in dBm, as *signed hexadecimal*
└──────────────────── "REC" plus a channel suffix
```

Two traps here:

* **The RSSI is hex, not decimal.** Observed values include `-4C`, `-3B`, `-4F`,
  which decimal parsing rejects outright. Range seen: `-3B` to `-55`, i.e. −59 to
  −85 dBm.
* **The address is 8 hex digits, not 22 bits.** `1A2B3C4D` does not fit in 22
  bits, so any code validating against the documented width will discard valid
  telegrams. The parser normalises both spellings to the same canonical form.

### Repetition — one press is five frames

The specification says repeats arrive "at least every 100 ms". Measured, a single
contact change produces exactly **five frames about 38 ms apart**:

```text
+51.827s  REC00,-47,1A2B3C4D,B
+51.865s  REC00,-46,1A2B3C4D,B
+51.903s  REC00,-47,1A2B3C4D,B
+51.941s  REC00,-46,1A2B3C4D,B
+51.978s  REC00,-47,1A2B3C4D,B
```

Sizing a de-duplication window from the document rather than from the wire would
split every press into five events. `eldat/telegrams.py` collapses bursts with a
400 ms silence window, comfortably above the 38 ms cadence and below any plausible
double press. RSSI varies between frames of the same burst, so it is informational
only — never an identity.

The ratio is exact and is asserted in CI: `tests/capture_rts16.py` holds a real
65-frame recording of the window contact being opened and closed, and replaying it
through the collapser yields **13 events from 65 frames — precisely 5:1**, all
`press`, with keys strictly alternating. Verified again live through the full
bridge → TCP → library path: 23 contact changes produced 23 events, never 115.

## Devices verified against this

**ELDAT RTS16E5001B01 window contact.** Per its
[manual](https://www.eldat.de/produkte/_ba/rts16e-B01-B02_ba_de.pdf), the EIN/AUS
variant sends **Easywave code A when the contact opens and code B when it
closes** (the B02 variant is inverted). Captures match exactly. Worth knowing:

* This variant has **no periodic status telegram** — only the `-01`/`-02` STATUS
  models re-send every 24 hours. A restart would leave the state unknown until the
  window next moves, so the integration restores the last state.
* A low battery adds a separate `BATTERIE-LOW` telegram. The manual does not say
  which key code carries it, and it could not be provoked on a healthy battery, so
  it remains unverified.

## Verification status

What has actually been exercised against hardware, and what has not:

| Area | Status |
|---|---|
| Userspace CP210x open at 57600 8N1 | verified |
| `ID?`, `GETP?`, `INFO?`, `MODE?`, `BUTTON?`, `LED?` | verified |
| `LED,ON` / `LED,OFF` round trip | verified — a real write to the device |
| `RDP?` rejection | verified (answers `ERROR`) |
| `TXP` accepted for all keys, all 64 positions | verified at protocol level |
| Position range `0`–`63`, `64` refused | verified exhaustively |
| Receive: framing, RSSI, address, 5:1 burst collapse | verified live and by capture replay |
| Contact semantics A = open, B = closed | per manufacturer manual, consistent with captures |
| **An actual receiver acting on `TXP`** | **not verified** — needs an actuator in learning mode |
| **`hold` / `release` detection** | **not verified** — a contact cannot be held down |
| **Battery-low telegram** | **not verified** — needs a depleted battery |
| **Kernel `new_id` bind path** | **not verified** — needs a Linux host |

The unverified transmit case is a real gap: the command path is proven up to the
stick's acknowledgement, but nothing has yet confirmed a receiver responds. The
`hold`/`release` thresholds are reasoned from the measured 38 ms cadence rather
than observed, so treat their timing as provisional.

## One speaker only

The stick tolerates a single connection, and the protocol is strictly
request/acknowledge with no command nesting. The bridge therefore accepts one TCP
client and rejects further ones, and the client serialises commands behind a lock.
