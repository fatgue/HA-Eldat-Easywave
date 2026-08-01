# ELDAT Easywave Bridge

Makes an ELDAT Easywave USB transceiver usable by Home Assistant and serves it as
a raw TCP stream for the ELDAT Easywave integration.

## Why this add-on exists

ELDAT's transceivers are Silicon Labs CP210x chips behind ELDAT's own USB vendor
id `0x155A`. The Linux `cp210x` driver knows only product id `0x1006`, so most
ELDAT sticks never get a `/dev/ttyUSB*` device. Home Assistant OS offers no way to
add a udev rule, and the Home Assistant container has no libusb — so this add-on
takes over the hardware and hands the integration something it can use.

It tries two paths, in order:

1. **Kernel bind.** Registers the stick's USB id with the running `cp210x` driver
   via `/sys/bus/usb-serial/drivers/cp210x/new_id` and uses the resulting serial
   port. Preferred when available.
2. **Userspace CP210x.** Drives the chip directly over libusb. Used when the driver
   is not loaded or sysfs is not writable.

The log line after startup tells you which path was taken.

## Options

| Option | Default | Meaning |
|---|---|---|
| `port` | `5000` | TCP port the protocol stream is served on. |
| `prefer_kernel` | `true` | Try the kernel bind first. Set `false` to always use libusb. |
| `product_ids` | *(empty)* | Comma-separated hex USB product ids to accept, e.g. `100E,1006`. Empty means every id ELDAT's own Windows driver claims (`1005`–`1013`). |
| `log_level` | `info` | `trace`, `debug`, `info`, `warning` or `error`. |

## Notes

- **One client only.** The stick tolerates a single speaker, and the protocol is
  strictly request/acknowledge. A second connection is refused rather than
  silently interleaved.
- **Enable "Start on boot"**, so the integration finds the bridge after a restart.
- Telegrams arriving while no client is connected are discarded. Easywave is
  stateless one-way radio, so there is nothing meaningful to replay.
