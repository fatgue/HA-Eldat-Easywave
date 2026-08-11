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
| `MODE?` / `MODE,<n>` | `MODE,00\tOK` | **undocumented**. All 256 values were tried: only `00` and `04` are accepted. Easywave reception is unchanged in mode 4, so what it selects is **still unknown** |
| `TXP,<pos>,<key>` | `OK` | transmit from a position; `<key>` is `A`–`D` |
| `LED?` / `LED,ON\|OFF` | `LED is OFF\tOK` | note the casing differs from the spec's "Led is ON" |
| `ECHO?` / `ECHO,ON\|OFF` | `ECHO is OFF\tOK` | default off |
| `BUTTON?` | `BUTTON is released\tOK` | the pushbutton on the stick |
| `RX?` / `RX,ON\|OFF` | `RX is ON\tOK` | **undocumented**, enables the receiver. Takes no other value: `KEELOQ`, `KL`, `EW`, `ALL`, `BOTH`, `1`, `2`, `00`, `01` are all refused |
| `RDP?,<pos>` | **`ERROR`** | **not implemented on this firmware** — see below |
| `Bootloader` | starts the bootloader | do not send; needs a reset to recover |

Unsupported commands return a bare `ERROR`, so probing is safe and cheap. About
sixty further candidates were tried against this firmware -- among them
`KEELOQ?`, `PROT?`, `LEARN?`, `CONFIG?`, `STATUS?`, `HELP?`, `FREQ?`, `ENC?` and
`RSSI?` -- and every one of them answered `ERROR`. The table above is therefore
close to the complete command set, not a selection from it.

Two entries here correct an earlier version of this document, which claimed that
`MODE,00` was the only accepted mode. That claim rested on a sample of two
values. Sweeping the range found `MODE,04`, and a systematic sweep also turned up
`RX?`, which no amount of reading the specification would have revealed.

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

**ELDAT SH01 humidity sensor** (`SH01E5002-01`, archived). Per its
[manual](https://www.eldat.de/archiv/easywave/_ba/sh01e_ba_de.pdf) it is a humidity
sensor *and* a push-button, and the datasheet's `Codierung: 2x Easywave A/B` says
what that means in practice: **two addresses, each with keys A and B**.

| Function | Codes | Sent when |
|---|---|---|
| Button | `A1` on, `B1` off | key "I" and key "0" pressed |
| Humidity | `A2` on | above 74 % rH, or a 4 % rise within 2 minutes while above 40 % |
| Humidity | `B2` off | below 72 % rH, or back near the starting value -- and unconditionally after 4 hours |

Below 40 % rH the sensor does not react at all. It measures every 2 minutes, over
1-99 % rH at ±5 % accuracy, on a CR2032 or 12-24 V.

Two things follow for an integration. There is **no measured value on the air** --
Easywave carries a switching telegram and nothing else, so this is a binary sensor
(`moisture`), not a humidity reading. And because the two functions are two
addresses, the device is added twice: once for the button, once for the threshold.

The same shape applies across ELDAT's sensor range -- ST01/ST02 temperature, SL01
light, RTS40 and SM01 motion all report by switching telegram -- which is why the
contact device type offers those classes rather than only openings.

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

### A transmitter this stick cannot hear

Not every ELDAT transmitter on 868.30 MHz is an Easywave transmitter. An ELDAT
`RTS21-5003K-02` key fob was measured against this stick and produced **not one
byte** -- no telegram, and no unparsable frame either. What rules out the ordinary
explanations:

* It transmits: its LED lights on every press, which per the RT21 manual is the
  send indicator, and a blinking LED (its low-battery warning) was not seen.
* It is on the right frequency: 868,30 MHz is printed on the housing.
* The receiver works: a window contact, a humidity sensor and a neck-strap
  transmitter were all heard reliably during the same session, from unlearned
  addresses, so the stick reports Easywave from anything within range.
* There is no mode that changes it: all 256 `MODE` values were tried and only `00`
  and `04` exist; Easywave reception is identical in both, and the fob is heard in
  neither. No `KEELOQ`, `PROT`, `LEARN` or similar command exists to switch.

The firmware calls itself `RX09 EW+KEELOQ`, so the hardware can demodulate KeeLoq,
but nothing in the serial protocol exposes it. KeeLoq is a rolling-code scheme: a
receiver that has not been paired with a transmitter cannot validate its telegram
and drops it silently -- which is exactly the observed silence.

**This is a negative result worth recording: such a transmitter cannot be
integrated through this stick, and no amount of software changes that.** It
belongs to the door lock it was supplied with, not to the Easywave system.

#### The part number says which system a device speaks

A second transmitter, an `RT21-5003K-02`, behaved identically: it drives a FUHR
receiver reliably, and the stick never hears a byte of it -- measured against a
window contact heard at −73 dBm seconds earlier, so the receiver was demonstrably
working at the time.

Lining the devices up by part number shows what separates them:

| Part number | Device | Heard by the stick |
|---|---|---|
| `RTS16E5001B01` | window contact | yes |
| `SH01E5002-01` | humidity sensor | yes |
| `RTS21-5003K-02` | key fob | **never** |
| `RT21-5003K-02` | hand transmitter | **never** |

Every Easywave part number in ELDAT's own documentation carries an **`E`** after
the model group -- `RT21E5001-01`, `RT21E5002-01`, `RTS03E5004-04-27P`,
`RS16E5001-01`, `SH01E5002-01`. Both silent transmitters lack it.

FUHR's own manuals describe their system as a **proprietary rolling code on
868,30 MHz with FSK** -- the same frequency and the same modulation as Easywave --
and state that only original FUHR transmitters can be paired. So two mutually deaf
systems share the band, and nothing observable on the air separates them.

**The `E` is a reliable rule of thumb, not a documented one.** No ELDAT type-code
legend could be found stating what the letter means; the rule rests on a perfect
correlation across nine part numbers. Do not over-read the other letters either:
the trailing group is a colour code -- `-23K` anthracite, `-00K` white, `-27P` --
so a `K` in a part number does not imply KeeLoq. When buying, the dependable check
is the printed **Easywave logo**, which the genuine transmitters carry.

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
| **An actual receiver acting on `TXP`** | **still not verified** — one attempt failed against a rolling-code receiver, see below |
| **`hold` / `release` detection** | **not verified** — a contact cannot be held down |
| **Battery-low telegram** | **not verified** — needs a depleted battery |
| **Kernel `new_id` bind path** | **not verified** — needs a Linux host |

The unverified transmit case is a real gap: the command path is proven up to the
stick's acknowledgement, but nothing has yet confirmed a receiver responds.

### The receiver that would not learn — and what it does *not* prove

A **FUHR NZ80088 socket receiver** was put into learning mode and offered
telegrams from the stick. Per its
[manual](https://www.fuhr.de/fileadmin/documents/Anleitungen/Zubehoer/DE/anleitung-steckdosen-funkempfaenger-VNZ80088-MB40-de.pdf)
a stored code makes its LED light for about four seconds. It never did:

* Position 0, key A, ~60 telegrams/s for 6 s — no reaction.
* Position 0, key A, paced at ~10/s for 5 s, matching a held transmitter's
  repetition — no reaction.
* **All 64 positions**, key A, a short held press each — no reaction. The LED kept
  blinking throughout, so learning mode never ended and never accepted anything.

Its variant line reads `NZ80088 — 868,30 MHz (Rolling Code)`, and it was afterwards
paired successfully with an `RT21-5003K-02` -- a transmitter with no `E` in its
part number, which this stick cannot hear either. So the receiver works, and works
on rolling code.

**That makes this attempt no test of transmit at all.** A receiver expecting
rolling code would refuse Easywave telegrams however well the stick sent them, so
the failure says nothing either way. `TXP` remains unproven: the stick
acknowledges the command, it does not report its own transmissions, and no
self-check exists. Settling it needs a **receiver with an `E` in its part
number** -- an ELDAT RCP-series socket, for instance. Until one is available,
transmit stays a gap, and picking the wrong receiver to test it with wastes the
attempt.

Worth noting for anyone modelling this hardware: the NZ80088's mains outlet is
explicitly `nicht schaltbar` -- it only passes power through. What it switches is a
dry relay contact giving a **1-second impulse**, for a garage door drive. That is a
`button`, not a `switch`.

The
`hold`/`release` thresholds are reasoned from the measured 38 ms cadence rather
than observed, so treat their timing as provisional.

### The kernel path never gets a chance

On Home Assistant OS the `cp210x` module is not loaded, so
`/sys/bus/usb-serial/drivers/cp210x/` does not exist and there is nothing to write
`new_id` into. Confirmed from an add-on log:

```text
cp210x driver not loaded (/sys/bus/usb-serial/drivers/cp210x missing)
kernel path unavailable, driving the CP210x from userspace
```

The same held in a Debian LXC container. So the userspace driver is not a
fallback in practice -- it is the path that runs.

### A silent stick was my own teardown, not the passthrough

Every control transfer failing in a guest looked like an emulation limit. It was
not. Closing the device used to send ``IFC_ENABLE = UART_DISABLE``, which reads
like tidy housekeeping and is what the in-tree ``cp210x`` driver does. Measured on
this hardware, the stick then answers **nothing at all**, and re-enabling it on the
next open does not help -- only a USB reset does.

Home Assistant opens the device twice in normal use, once for the config flow and
once for the entry setup. So the first open worked, the teardown quietly disabled
the stick, and the second open met silence.

Measured in a Debian guest with the stick passed through by QEMU, at the same port:

```text
close without disabling, reopen  x3   all three answer
close with IFC_ENABLE=DISABLE, reopen  silence
USBDEVFS_RESET, then reopen           answers again
```

Everything else this was blamed on works: control transfers, bulk transfers, QEMU
passthrough, cascaded hubs, and read-timeout polling all behave. The whole
investigation is worth recording because four plausible causes were named before
the real one, each on evidence that looked sufficient at the time:

| Suspected | Ruled out by |
|---|---|
| QEMU cannot carry control transfers | a HmIP-RFUSB works on the same guest |
| A cascaded second USB hub | the same stick works at the same port from a container |
| Control transfers specifically | the log shows all four vendor requests accepted |
| Read-timeout polling | every read strategy works in a Debian guest |

The lesson that generalises: the failure was on the *second* open, and every early
test opened the device once.

## One speaker only

The stick tolerates a single connection, and the protocol is strictly
request/acknowledge with no command nesting. The bridge therefore accepts one TCP
client and rejects further ones, and the client serialises commands behind a lock.
