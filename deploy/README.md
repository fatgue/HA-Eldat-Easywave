# Running the bridge outside Home Assistant

The integration drives a locally attached stick by itself, so this is only needed
when the transceiver cannot be attached to the machine running Home Assistant --
for instance when it has to sit elsewhere for radio range.

> **Not needed for virtual machines.** An earlier version of this project
> recommended it for them, on the strength of a failure that turned out to be a bug
> of its own: closing the device disabled its UART, which left it silent on the next
> open. Passing the stick through to a guest works. See [PROTOCOL.md](../PROTOCOL.md).

## An LXC container on Proxmox

A container is preferable to the hypervisor host itself: no packages on the host,
and LXC bind-mounts the real device node instead of emulating one.

**1. Take the stick away from the VM**, so it is free:

```bash
qm set <vmid> --delete usb2     # whichever usb entry holds it
```

**2. Create a privileged container.** Privileged is required for raw USB access:

```bash
pct create 135 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst \
  --hostname eldat-bridge --cores 1 --memory 512 --swap 256 \
  --rootfs local-lvm:4 --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 0 --onboot 1
```

**3. Give it the USB bus.** Append to `/etc/pve/lxc/135.conf`:

```
lxc.cgroup2.devices.allow: c 189:* rwm
lxc.mount.entry: /dev/bus/usb dev/bus/usb none bind,optional,create=dir
```

Major 189 is the USB character device. Bind the whole `/dev/bus/usb` tree rather
than one node, because replugging renumbers the node.

**4. Install and run:**

```bash
pct start 135
pct exec 135 -- apt-get update
pct exec 135 -- apt-get install -y python3 python3-usb libusb-1.0-0 git
pct exec 135 -- git clone --depth 1 \
  https://github.com/fatgue/HA-Eldat-Easywave-Addon.git /opt/eldat
pct exec 135 -- install -m644 \
  /opt/eldat/deploy/eldat-bridge.service /etc/systemd/system/
pct exec 135 -- systemctl enable --now eldat-bridge
```

**5. Check it opened the stick:**

```
found 155A:100E ELDAT USB Device V1 (serial 00002858)
opened ... at 57600 baud 8N1
bridging ... on ('0.0.0.0', 5000)
```

A readable serial number is the quick signal that control transfers work; in a
QEMU guest it reads `unreadable`.

**6. Point the integration** at the container's address and port 5000, instead of
the add-on's `172.30.32.1`.

## Directly on a Linux host

Same as steps 4-6, without the container. The bridge needs `python3`, `pyusb` and
`libusb`, and must run as a user that may open `/dev/bus/usb` -- root, or a user
in the group a udev rule grants.

## Updating

```bash
cd /opt/eldat && git pull && systemctl restart eldat-bridge
```
