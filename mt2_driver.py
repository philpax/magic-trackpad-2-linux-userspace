#!/usr/bin/python3
"""Userspace multitouch driver for a Bluetooth Apple Magic Trackpad 2.

Kernels without hid-magicmouse leave the trackpad in its one-button mouse mode.
This driver switches it to multitouch mode through hidraw and republishes its
touch reports as a uinput touchpad. The decoding and device layout follow
hid-magicmouse's Magic Trackpad 2 handling, so libinput applies its usual
touchpad behaviour: clickfinger right-click, two-finger scrolling and gestures.

The kernel only passes reports declared in the HID descriptor to hidraw. The
trackpad's own Bluetooth descriptor does not declare its touch report (0x31),
so BlueZ must be given a patched descriptor; see `mt2_pair.py bluez-setup`.
"""

import errno
import fcntl
import glob
import os
import select
import signal
import struct
import sys
import time

HID_ID = "0005:0000004C:00000265"
REPORT_TOUCH = 0x31
ENABLE_MULTITOUCH = bytes([0xF1, 0x02, 0x01])

MIN_X, MAX_X, MIN_Y, MAX_Y = -3678, 3934, -2479, 2586
RES_X, RES_Y = 47, 44
SLOTS = 16

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
BTN_LEFT, BTN_TOOL_FINGER, BTN_TOOL_QUINTTAP, BTN_TOUCH = 0x110, 0x145, 0x148, 0x14A
BTN_TOOL_DOUBLETAP, BTN_TOOL_TRIPLETAP, BTN_TOOL_QUADTAP = 0x14D, 0x14E, 0x14F
TOOLS = [BTN_TOOL_FINGER, BTN_TOOL_DOUBLETAP, BTN_TOOL_TRIPLETAP, BTN_TOOL_QUADTAP, BTN_TOOL_QUINTTAP]
ABS_X, ABS_Y, ABS_PRESSURE = 0x00, 0x01, 0x18
ABS_MT_SLOT, ABS_MT_TOUCH_MAJOR, ABS_MT_TOUCH_MINOR = 0x2F, 0x30, 0x31
ABS_MT_ORIENTATION, ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x34, 0x35, 0x36
ABS_MT_TRACKING_ID, ABS_MT_PRESSURE = 0x39, 0x3A
INPUT_PROP_POINTER, INPUT_PROP_BUTTONPAD = 0x00, 0x02
BUS_BLUETOOTH = 0x05

ABS_AXES = [
    (ABS_X, MIN_X, MAX_X, RES_X),
    (ABS_Y, MIN_Y, MAX_Y, RES_Y),
    (ABS_PRESSURE, 0, 253, 0),
    (ABS_MT_SLOT, 0, SLOTS - 1, 0),
    (ABS_MT_TOUCH_MAJOR, 0, 255 << 2, 0),
    (ABS_MT_TOUCH_MINOR, 0, 255 << 2, 0),
    (ABS_MT_ORIENTATION, -3, 4, 0),
    (ABS_MT_POSITION_X, MIN_X, MAX_X, RES_X),
    (ABS_MT_POSITION_Y, MIN_Y, MAX_Y, RES_Y),
    (ABS_MT_TRACKING_ID, 0, 65535, 0),
    (ABS_MT_PRESSURE, 0, 253, 0),
]


def _iow(nr: int, size: int) -> int:
    return (1 << 30) | (size << 16) | (ord("U") << 8) | nr


def hid_set_feature(size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("H") << 8) | 0x06


UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
UI_DEV_SETUP, UI_ABS_SETUP = _iow(3, 92), _iow(4, 28)
UI_SET_EVBIT, UI_SET_KEYBIT = _iow(100, 4), _iow(101, 4)
UI_SET_ABSBIT, UI_SET_PROPBIT = _iow(103, 4), _iow(110, 4)


def _s32(value: int) -> int:
    return value - (1 << 32) if value & (1 << 31) else value


def decode_touch(t: bytes) -> dict:
    """Decode one 9-byte touch record."""
    return {
        "id": t[8] & 0xF,
        "x": _s32(((t[1] << 27) | (t[0] << 19)) & 0xFFFFFFFF) >> 19,
        "y": -(_s32(((t[3] << 30) | (t[2] << 22) | (t[1] << 14)) & 0xFFFFFFFF) >> 19),
        "down": t[3] & 0xC0 == 0x80,
        "major": t[4] << 2,
        "minor": t[5] << 2,
        "pressure": t[7],
        "orientation": -((t[8] >> 5) - 4),
    }


def decode_report(data: bytes) -> tuple[list[dict], bool] | None:
    """Return the touches and click state of a touch report, or None for other reports."""
    if len(data) < 4 or data[0] != REPORT_TOUCH or (len(data) - 4) % 9:
        return None
    touches = [decode_touch(data[i : i + 9]) for i in range(4, len(data), 9)]
    return touches, bool(data[1] & 1)


def frame_events(tracking: dict[int, int], next_id: int, touches: list[dict], click: bool):
    """Build the evdev events for one report. Returns (events, next tracking id)."""
    events = []
    seen = set()
    for touch in touches:
        slot = touch["id"]
        seen.add(slot)
        if touch["down"]:
            if slot not in tracking:
                tracking[slot] = next_id
                next_id = (next_id + 1) & 0xFFFF
            events += [
                (EV_ABS, ABS_MT_SLOT, slot),
                (EV_ABS, ABS_MT_TRACKING_ID, tracking[slot]),
                (EV_ABS, ABS_MT_POSITION_X, touch["x"]),
                (EV_ABS, ABS_MT_POSITION_Y, touch["y"]),
                (EV_ABS, ABS_MT_TOUCH_MAJOR, touch["major"]),
                (EV_ABS, ABS_MT_TOUCH_MINOR, touch["minor"]),
                (EV_ABS, ABS_MT_PRESSURE, touch["pressure"]),
                (EV_ABS, ABS_MT_ORIENTATION, touch["orientation"]),
            ]
        elif slot in tracking:
            del tracking[slot]
            events += [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, -1)]
    for slot in [s for s in tracking if s not in seen]:
        del tracking[slot]
        events += [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, -1)]

    active = [t for t in touches if t["down"] and t["id"] in tracking]
    count = len(active)
    events.append((EV_KEY, BTN_TOUCH, int(count > 0)))
    for index, tool in enumerate(TOOLS):
        selected = count == index + 1 or (tool == BTN_TOOL_QUINTTAP and count > 5)
        events.append((EV_KEY, tool, int(selected)))
    if active:
        first = min(active, key=lambda t: (tracking[t["id"]] - next_id) & 0xFFFF)
        events += [
            (EV_ABS, ABS_X, first["x"]),
            (EV_ABS, ABS_Y, first["y"]),
            (EV_ABS, ABS_PRESSURE, first["pressure"]),
        ]
    else:
        events.append((EV_ABS, ABS_PRESSURE, 0))
    events += [(EV_KEY, BTN_LEFT, int(click)), (EV_SYN, SYN_REPORT, 0)]
    return events, next_id


class Touchpad:
    """A uinput touchpad laid out like hid-magicmouse's Magic Trackpad 2 device."""

    def __init__(self, path: str = "/dev/uinput"):
        self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        for bit in (EV_SYN, EV_KEY, EV_ABS):
            fcntl.ioctl(self.fd, UI_SET_EVBIT, bit)
        for key in (BTN_LEFT, BTN_TOUCH, *TOOLS):
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, key)
        for prop in (INPUT_PROP_POINTER, INPUT_PROP_BUTTONPAD):
            fcntl.ioctl(self.fd, UI_SET_PROPBIT, prop)
        for code, minimum, maximum, resolution in ABS_AXES:
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
            setup = struct.pack("H2x6i", code, 0, minimum, maximum, 0, 0, resolution)
            fcntl.ioctl(self.fd, UI_ABS_SETUP, setup)
        name = b"Apple Inc. Magic Trackpad"
        setup = struct.pack("HHHH80sI", BUS_BLUETOOTH, 0x004C, 0x0265, 0x0311, name, 0)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.tracking: dict[int, int] = {}
        self.next_id = 0

    def frame(self, touches: list[dict], click: bool) -> None:
        events, self.next_id = frame_events(self.tracking, self.next_id, touches, click)
        os.write(self.fd, b"".join(struct.pack("qqHHi", 0, 0, *e) for e in events))

    def release(self) -> None:
        self.frame([], False)

    def close(self) -> None:
        fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        os.close(self.fd)


def find_hidraw() -> str | None:
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(node + "/device/uevent") as uevent:
                if f"HID_ID={HID_ID}" in uevent.read().split():
                    return "/dev/" + os.path.basename(node)
        except OSError:
            continue
    return None


def declares_touch_report(descriptor: bytes) -> bool:
    """Whether a HID report descriptor contains a Report ID 0x31 item."""
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:  # long item
            i += 3 + (descriptor[i + 1] if i + 1 < len(descriptor) else 0)
            continue
        size = (0, 1, 2, 4)[prefix & 3]
        if prefix & 0xFC == 0x84 and size == 1 and descriptor[i + 1 : i + 2] == b"\x31":
            return True
        i += 1 + size
    return False


def serve(touchpad: Touchpad, path: str, debug: bool) -> None:
    sysfs = f"/sys/class/hidraw/{os.path.basename(path)}/device/report_descriptor"
    try:
        with open(sysfs, "rb") as f:
            if not declares_touch_report(f.read()):
                print(
                    f"{path}: HID descriptor does not declare report 0x31; touches will be "
                    "dropped by the kernel. Run `mt2_pair.py bluez-setup` and reconnect.",
                    file=sys.stderr,
                    flush=True,
                )
    except OSError:
        pass
    fd = os.open(path, os.O_RDWR)
    try:
        buffer = bytearray(ENABLE_MULTITOUCH)
        fcntl.ioctl(fd, hid_set_feature(len(buffer)), buffer, True)
        print(f"{path}: multitouch enabled", flush=True)
        while True:
            select.select([fd], [], [])
            decoded = decode_report(os.read(fd, 4096))
            if decoded is None:
                continue
            touches, click = decoded
            if debug:
                summary = " ".join(
                    f"{t['id']}:{'D' if t['down'] else 'u'}({t['x']},{t['y']})" for t in touches
                )
                print(f"click={int(click)} {summary}", flush=True)
            touchpad.frame(touches, click)
    finally:
        os.close(fd)
        touchpad.release()


def main() -> int:
    debug = "--debug" in sys.argv[1:]
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    touchpad = Touchpad()
    waiting = False
    try:
        while True:
            path = find_hidraw()
            if path is None:
                if not waiting:
                    print("waiting for the trackpad to connect", flush=True)
                    waiting = True
                time.sleep(2)
                continue
            waiting = False
            try:
                serve(touchpad, path, debug)
            except OSError as error:
                if error.errno not in (errno.ENODEV, errno.EIO, errno.ENOENT, errno.EPIPE):
                    raise
                print(f"{path}: disconnected ({error.strerror})", flush=True)
                time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        touchpad.close()


if __name__ == "__main__":
    sys.exit(main())
