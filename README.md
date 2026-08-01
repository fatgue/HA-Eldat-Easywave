# ELDAT Easywave for Home Assistant

Control ELDAT Easywave devices — roller shutters, switching actuators, lights —
and react to Easywave transmitters such as window contacts and wall switches,
using an ELDAT Easywave USB transceiver.

Two pieces, because the hardware makes it necessary:

| | What it is | How to install |
|---|---|---|
| **ELDAT Easywave** | Home Assistant integration: config flow, devices, entities | HACS |
| **ELDAT Easywave Bridge** | Owns the USB stick and serves it over TCP | Add-on repository, or [on a Linux host](deploy/README.md) |

> **Home Assistant in a virtual machine?** The add-on cannot open the stick there.
> QEMU USB passthrough does not carry the CP210x control transfers -- verified on
> Proxmox, where the hypervisor host handles every transfer the guest fails. Run
> the bridge outside the VM instead: see [deploy/README.md](deploy/README.md).

## Why an add-on is required

ELDAT's transceivers are Silicon Labs CP210x chips behind ELDAT's own USB vendor
id, and the Linux `cp210x` driver recognises exactly one of ELDAT's fifteen
product ids (`0x155A:0x1006`, the plain RX09). Every other stick — including
`0x100E`, which this was developed against — gets **no `/dev/ttyUSB*` node**.

On Home Assistant OS there is no supported way to add a udev rule, and the Home
Assistant container ships no libusb, so the integration cannot solve this itself.
The add-on can: it runs privileged, registers the USB id with the kernel driver
when possible, and otherwise drives the CP210x chip directly over libusb. Either
way it exposes the stick as a plain TCP stream, which keeps the integration free
of third-party dependencies.

See [PROTOCOL.md](PROTOCOL.md) for the measured protocol, including several places
where the hardware disagrees with ELDAT's published specification.

## Installation

### 1. The bridge add-on

1. **Settings → Add-ons → Add-on store**, then ⋮ → **Repositories**, and add this
   repository's URL.
2. Install **ELDAT Easywave Bridge**, start it, and check the log. It should report
   the stick it found and which access path it used:
   ```
   found 155A:100E ELDAT USB Device V1 (serial 00002858)
   opened ... at 57600 baud 8N1
   bridging ... on ('0.0.0.0', 5000)
   ```
3. Enable **Start on boot**.

### 2. The integration

1. In HACS, add this repository as a custom repository of type **Integration**,
   then install **ELDAT Easywave** and restart Home Assistant.
2. **Settings → Devices & services → Add integration → ELDAT Easywave**.
3. Accept the suggested address (`172.30.32.1`, port `5000`) unless you changed the
   add-on's port. The title will show the firmware the stick reports.

## Adding devices

Each Easywave device is added under the transceiver via **Add device** on the
integration page.

### Devices you control (transmitting)

Easywave transmits from **positions**, not from arbitrary addresses. The stick
holds 64 or 128 burned-in serial numbers; you pick one per device and teach it to
the receiver. Positions are numbered **from 0** — a 64-position stick uses 0 to 63:

1. Add the device in Home Assistant, choosing a position not yet in use.
2. Put the receiver into learning mode (see its manual).
3. Trigger the entity once — open the cover, turn the switch on, press the button.
   The receiver learns the address it hears.

> Firmwares that lack the `RDP?` command cannot report which serial belongs to
> which position, so keep your own note of what you assigned where. The
> `eldat_easywave.send_telegram` service sends a single telegram from any position
> if you need to pair without creating an entity first.

Available types: **roller shutter** (up/down/stop), **switch**, **light**,
**button**.

Easywave receivers never report back, so these entities are marked
`assumed_state`: Home Assistant shows what it last commanded, not what the device
actually did. Dimmers are not offered for the same reason — a brightness level
could not be tracked honestly.

### Devices that report to you (receiving)

1. Trigger the transmitter once — open the window, press the key.
2. Add a **contact** or **transmitter** device; the transmitter now appears in the
   dropdown, labelled with its address, last key and signal strength.

**Contact** becomes a binary sensor. Defaults match the ELDAT RTS16 in EIN/AUS
mode: key A means open, key B means closed. Its state survives restarts, because
that variant sends no periodic status telegram.

**Transmitter** becomes four `event` entities, one per key code, each reporting
`press`, `hold` or `release` — ready to use as automation triggers.

Every telegram is also fired on the event bus as `eldat_easywave_telegram` with
`address`, `key`, `action`, `rssi` and `repeats`, which is handy while setting
things up.

## Services

| Service | Purpose |
|---|---|
| `eldat_easywave.send_telegram` | Send one telegram from a given position and key. The pairing tool. |
| `eldat_easywave.set_led` | Turn the transceiver's red LED on or off, to identify a stick. |

## Troubleshooting

**The integration cannot connect.** Check the add-on is running. The stick accepts
only one connection at a time, so nothing else may be using it.

**The add-on finds no stick.** Confirm it is plugged in and that the add-on still
has `full_access` (set by default) -- that is what grants both `/dev/bus/usb` and a
writable `/sys`. On a virtualised Home Assistant the stick also has to be passed
through to the VM; binding it by USB id rather than by port survives replugging. Product ids outside
`0x1005`–`0x1013` are not recognised; set the add-on's `product_ids` option to add
yours.

**A receiver ignores commands.** The position was probably never taught. Repeat the
learning procedure, and check the actuator's expected key codes — stop in
particular varies between models.

**Nothing is received.** Range is roughly 150 m outdoors but only about 30 m
indoors, and much less through metal. Trigger the transmitter and watch the add-on
log with the log level at `debug`.

## Development

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest -q
```

Tests run against a real Home Assistant release via
`pytest-homeassistant-custom-component`. The protocol tests assert against
byte-exact captures from real hardware rather than against the specification —
deliberately, since the two disagree.

## Credits

Protocol groundwork from ELDAT's own RX09/RTR09 specification, plus prior
reverse-engineering in
[Easywave2MQTT](https://github.com/marcselis/Easywave2MQTT) and
[python-easywave](https://github.com/ferensw/python-easywave).

## Licence

MIT — see [LICENSE](LICENSE).
