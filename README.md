# magic-trackpad-2-linux-userspace

The code and documentation in this repository were drafted by Claude Opus 5.5. They were extracted from a manual, back-and-forth debugging process across two computers: a NixOS desktop and a Steam Frame. The procedure as a whole has not been run from start to finish in its current form, so it is not guaranteed to work as written, or to work at all.

Tools to pair an Apple Magic Trackpad 2 with a Linux host over Bluetooth and to use it as a multitouch touchpad on kernels that lack `hid-magicmouse`. Both tools are single Python files that use only the standard library, so they run on a stock host Python such as SteamOS's `/usr/bin/python3`.

`mt2_pair.py` pairs the trackpad without Bluetooth discovery and writes the BlueZ state the trackpad needs. `mt2_driver.py` switches the trackpad to multitouch mode and republishes its touches as a `uinput` touchpad, so libinput provides clickfinger right-click, two-finger scrolling and gestures.

The tools were developed for and tested on a Steam Frame (SteamOS on ARM64, kernel 6.18, BlueZ 5.79) with a Lightning Magic Trackpad 2 (USB product `05ac:0265`, firmware version `0x0311`). A host with `hid-magicmouse` and a working pairing flow needs neither tool.

## Background

The Magic Trackpad 2 has three properties that make it difficult to use on such a host.

First, the trackpad does not advertise itself to a host that it is not bonded to, and some hosts do not discover it at all. On the Steam Frame used for development, BlueZ discovery returned no devices of any kind. Pairing by address works without discovery, provided that BlueZ has a device record for the address.

Second, the trackpad stores the address of its host in a USB HID feature report. macOS pairs the trackpad by writing its own address and a link key to this report over the cable. On the tested trackpad, the address write takes effect and the link key write does not. The cause is not confirmed. A possible cause is the firmware update that Apple started to release on 2024-01-09, after [hi_my_name_is_keyboard](https://github.com/marcnewlin/hi_my_name_is_keyboard) showed that the link key could be read over the Lightning port. That update may also stop the trackpad from accepting a link key over USB. After an address write, the trackpad accepts a new Bluetooth bond with that address. Without a recent address write, a bonded trackpad accepts a pairing request but does not keep the new link key, and the next connection fails with a missing-key error (`br-connection-key-missing`, or `Invalid exchange` in the bluetoothd log).

Third, over Bluetooth without `hid-magicmouse`, the trackpad stays in a mode that reports one button and relative motion. Right-click on the trackpad is a two-finger click, which needs the multitouch data. The trackpad sends multitouch reports (report ID `0x31`) after the host sends feature report `F1 02 01`. The trackpad's HID descriptor does not declare report `0x31`, and the kernel passes only declared reports to `hidraw`. `hid-magicmouse` registers the report in the kernel. Without that driver, BlueZ must give the kernel a descriptor that declares the report.

## Host requirements

`mt2_pair.py check` reports the state of each requirement.

BlueZ must be able to create HID devices. If the kernel has no `hidp` module, `UserspaceHID=true` must be set in the `[General]` section of `/etc/bluetooth/input.conf`, so that BlueZ uses `uhid` instead. Changing the file needs root and a restart of `bluetooth.service`. On SteamOS, `/etc` is a writable overlay, so the change does not require unlocking the root filesystem.

The user that runs `mt2_driver.py` needs write access to `/dev/uinput` and to the trackpad's Bluetooth `hidraw` node. On SteamOS, both are writable by the `input` group and by the logged-in user.

`mt2_pair.py probe` and `redirect` need write access to the trackpad's USB `hidraw` node. On SteamOS this is the same `input` group access. On other hosts these subcommands may need root.

`mt2_pair.py bluez-setup` writes to `/var/lib/bluetooth` and needs root. On SteamOS, `/var` is writable without unlocking the root filesystem.

## Pairing procedure

The commands run on the host that the trackpad pairs with. `ADAPTER` is the host's Bluetooth controller address, as shown by `bluetoothctl show`. Subcommands that take `ADAPTER` default to the default controller. `DEVICE` is the trackpad's Bluetooth address.

1. Connect the trackpad to the host with a USB cable and read its address:

   ```
   python3 mt2_pair.py probe
   ```

   The output includes `trackpad address`, which is `DEVICE` in the following steps.

2. Create BlueZ's device record and SDP cache, then restart BlueZ:

   ```
   sudo python3 mt2_pair.py bluez-setup DEVICE
   sudo systemctl restart bluetooth
   ```

   The device record has no link key, so the pairing in step 4 creates one. If BlueZ already has a bond with the trackpad, `bluez-setup` keeps that bond and updates only the SDP cache. `--reset` replaces the existing record with one that has no link key. A stale key from an earlier pairing prevents a new pairing, so a repeat of this procedure uses `--reset`.

3. With the cable still connected, point the trackpad at the host:

   ```
   python3 mt2_pair.py redirect
   ```

4. Start the pairing loop, then disconnect the cable and switch the trackpad off and on:

   ```
   python3 mt2_pair.py pair DEVICE --wait-unplug
   ```

   The loop retries for two minutes. The first attempt after power-on sometimes fails with `AuthenticationCanceled`, and a later attempt succeeds. After pairing, the loop trusts and connects the trackpad.

The redirect must come directly before the pairing. If the trackpad connects to another bonded host between steps 3 and 4, the redirect no longer applies and steps 3 and 4 must be repeated. Removing the bond on the other host, or switching off its Bluetooth, prevents this.

Afterwards, the trackpad reconnects to the host when it is switched on or clicked.

## SDP records

BlueZ builds the HID device from the trackpad's SDP records. `bluez-setup` writes two records into BlueZ's cache (`/var/lib/bluetooth/ADAPTER/cache/DEVICE`): the HID record at handle `0x00010000` and the PnP Information record at handle `0x00010001`. The device record lists both services in `Services=`, so BlueZ uses the cached records and does not query the trackpad.

The records in `data/` were captured from the tested trackpad. They contain no addresses or serial numbers. Before writing the HID record, `bluez-setup` appends a vendor collection to its report descriptor. The collection declares input report `0x31` with 147 bytes of data, which covers the 3-byte header and 16 touches of 9 bytes each. `tests/patched-hid-record.hex` holds the resulting record, which is the record in use on the tested host.

A trackpad with different firmware can have different records. `mt2_pair.py capture-sdp DEVICE` prints the records of a connected trackpad, and `bluez-setup --hid-record FILE` uses a captured HID record in place of the bundled one. The SDP query needs a working Bluetooth link to the trackpad, which in practice means a host that is already paired with it.

If BlueZ queries the trackpad's SDP records itself, for example after a pairing through the desktop's Bluetooth settings, BlueZ replaces the cached HID record with the unpatched one. `mt2_driver.py` then prints a warning on connection, and `bluez-setup` followed by a restart of BlueZ restores the patched record.

## Driver

```
python3 mt2_driver.py [--debug]
```

The driver creates a `uinput` touchpad named `Apple Inc. Magic Trackpad` with the Bluetooth vendor and product IDs of the trackpad, so libinput applies its Apple touchpad defaults. It waits for the trackpad's Bluetooth `hidraw` node, sends the multitouch feature report, and translates each touch report into evdev multitouch events. When the trackpad disconnects, the driver releases all touches and waits for the next connection, so the `uinput` device stays in place across reconnects. `--debug` prints each decoded report.

The decoding follows the Magic Trackpad 2 handling in the kernel's `hid-magicmouse` driver. Each touch report has a 4-byte header and 9 bytes per touch. Bit 0 of header byte 1 is the click state. A touch is down when bits 6 and 7 of its byte 3 equal `0b10`. Coordinates range from -3678 to 3934 on the X axis and from -2479 to 2586 on the Y axis.

In multitouch mode, the trackpad stops sending its one-button mouse reports. The kernel's mouse device for the trackpad remains but receives no events, so input is not duplicated.

`systemd/mt2-driver.service` is a `systemd --user` unit for the driver. The path in `ExecStart` must match the checkout location. To install it:

```
cp systemd/mt2-driver.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mt2-driver.service
```

## Steam Frame notes

The SteamOS kernel on the Steam Frame includes neither `hidp` nor `hid_magicmouse`, so the setup needs `UserspaceHID=true` and the driver.

In Desktop Mode, KWin draws the cursor on a hardware plane that the headset view does not show. Setting `KWIN_FORCE_SW_CURSOR=1` in the session environment, for example in `~/.config/environment.d/`, makes KWin draw the cursor into the frame. gamescope sets `XCURSOR_SIZE=256`, and KWin applies that size at each start until it rereads `kcminputrc`. Writing `cursorSize` to the `[Mouse]` group of `kcminputrc` and sending the `org.kde.KGlobalSettings.notifyChange` signal with arguments `5` and `0` on the session bus applies the configured size. Applications that gamescope shows outside the desktop session show no cursor for a mouse or trackpad.

The Frame's USB port sometimes leaves host mode. `lsusb` then lists no devices. Reconnecting the cable restores host mode.

## Tests

```
python3 -m unittest discover -s tests
```

The tests use captured reports and records and need no hardware.

## Credits

The USB feature report layout (`0x34` and `0x35`) comes from Marc Newlin's reverse engineering in [hi_my_name_is_keyboard](https://github.com/marcnewlin/hi_my_name_is_keyboard) and from Jake Hillion's [magicbluetoothd](https://gitea.hillion.co.uk/JakeHillion/magicbluetoothd). The multitouch protocol handling follows the Linux kernel's [hid-magicmouse](https://github.com/torvalds/linux/blob/master/drivers/hid/hid-magicmouse.c) driver. The Desktop Mode cursor setting comes from [steam-frame-utils](https://github.com/curiousjtuber/steam-frame-utils).

## License

MIT. See `LICENSE`.
