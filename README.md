# ELDAT Easywave for Home Assistant

Control ELDAT Easywave devices — roller shutters, switching actuators, lights —
and react to Easywave transmitters such as window contacts and wall switches,
using an ELDAT Easywave USB transceiver.

**Plug the stick into the machine running Home Assistant, install the integration
from HACS, and add it.** The integration finds the transceiver and drives it
itself, so there is nothing else to install.

Two situations need more than that:

| Situation | What to do |
|---|---|
| Stick plugged into the Home Assistant machine | Just the HACS integration |
| Home Assistant in a virtual machine | Just the integration, with the stick passed through to the guest |
| Stick attached to a different machine | Same: run the bridge there |

## How it drives the stick

ELDAT's transceivers are Silicon Labs CP210x chips behind ELDAT's own USB vendor
id, and the Linux `cp210x` driver recognises exactly one of ELDAT's fifteen
product ids (`0x155A:0x1006`, the plain RX09). Every other stick — including
`0x100E`, which this was developed against — gets **no `/dev/ttyUSB*` node**, so
there is no serial port to open. The Home Assistant container has no libusb
either, and an integration cannot install system libraries.

What it does have is the raw device: Supervisor bind-mounts `/dev`, runs the
container privileged, and grants it the USB device cgroup rules. So the
integration drives the chip through Linux usbfs ioctls directly, using nothing but
the standard library. No add-on, no dependencies.

> **In a virtual machine**, pass the stick through to the guest and the
> integration drives it there like anywhere else. Verified on Proxmox. If it opens
> and then answers nothing, you are on a version older than 0.2.1 -- see
> [PROTOCOL.md](PROTOCOL.md).

See [PROTOCOL.md](PROTOCOL.md) for the measured protocol, including several places
where the hardware disagrees with ELDAT's published specification.

## Installation

1. Plug the transceiver into the machine running Home Assistant.
2. In HACS, add this repository as a custom repository of type **Integration**,
   install **ELDAT Easywave**, and restart Home Assistant.
3. **Settings → Devices & services → Add integration → ELDAT Easywave**.

The stick should be listed for you to pick, labelled with its model, USB ids and
serial number. The entry title then shows the firmware it reports, for example
`Easywave RX09 EW+KEELOQ`.

If no stick is attached to this machine, the same dialog asks for a bridge address
instead. The add-on in this repository serves one on `172.30.32.1:5000`; a bridge
you run yourself serves one wherever you put it.

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

**Contact or sensor** becomes a binary sensor. Defaults match the ELDAT RTS16 in
EIN/AUS mode: key A means open, key B means closed. Its state survives restarts,
because that variant sends no periodic status telegram.

Easywave sensors that measure something -- humidity, temperature, light, motion --
belong here too. They transmit a switching telegram when a threshold is crossed and
never a measured value, so there is nothing numeric to expose; pick the device class
that matches what they report. Combination devices use one address per function, so
they get added once per function: the SH01 humidity sensor sends A/B on one address
for its button and A/B on another for the humidity threshold. See
[PROTOCOL.md](PROTOCOL.md) for its exact codes.

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
