#!/usr/bin/python3
"""Pair an Apple Magic Trackpad 2 with a BlueZ host, without Bluetooth discovery.

Subcommands:
  check                     report what this host needs
  probe                     show the trackpad's address and stored host (USB)
  redirect [ADAPTER]        point the trackpad at a host address (USB)
  bluez-setup [ADAPTER]     write BlueZ's device record and patched SDP cache (root)
  pair DEVICE               pair, trust and connect by address
  capture-sdp DEVICE        print the trackpad's HID and PnP SDP records

ADAPTER defaults to the host's default Bluetooth controller. See README.md for
the procedure.
"""

import argparse
import configparser
import fcntl
import glob
import io
import os
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
USB_HID_ID = "0003:000005AC:00000265"
BT_HID_ID = "0005:0000004C:00000265"
REPORT_DEVICE, REPORT_HOST = 0x34, 0x35
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
PNP_UUID = "00001200-0000-1000-8000-00805f9b34fb"
HID_HANDLE, PNP_HANDLE = 0x00010000, 0x00010001
# Vendor collection declaring input report 0x31: a 3-byte header and up to 16
# nine-byte touches after the report ID.
TOUCH_COLLECTION = bytes.fromhex("0600ff0931a1018531150026ff0075089693000931" "8102c0")
ADDRESS = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def fail(message: str) -> None:
    sys.exit(f"mt2_pair: {message}")


def address(value: str) -> str:
    value = value.upper()
    if not ADDRESS.match(value):
        raise argparse.ArgumentTypeError(f"not a Bluetooth address: {value}")
    return value


# --- USB feature reports ------------------------------------------------------


def _hid_feature(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr


def find_usb_hidraw() -> str:
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        uevent = Path(node, "device/uevent").read_text()
        if f"HID_ID={USB_HID_ID}" in uevent.split() and "/input0\n" in uevent:
            return "/dev/" + os.path.basename(node)
    fail("no Magic Trackpad 2 connected over USB")


def get_feature(fd: int, report: int) -> bytes:
    buffer = bytearray(256)
    buffer[0] = report
    length = fcntl.ioctl(fd, _hid_feature(0x07, len(buffer)), buffer, True)
    return bytes(buffer[:length])


def set_feature(fd: int, data: bytes) -> None:
    fcntl.ioctl(fd, _hid_feature(0x06, len(data)), bytearray(data), True)


def format_address(raw: bytes) -> str:
    return ":".join(f"{b:02X}" for b in raw)


def read_reports(fd: int) -> dict:
    device = get_feature(fd, REPORT_DEVICE)
    host = get_feature(fd, REPORT_HOST)
    return {
        "address": format_address(device[4:10]),
        "class": int.from_bytes(device[10:13], "big"),
        "model": device[13:].rstrip(b"\0").decode(errors="replace"),
        "host": format_address(host[3:9]),
        "host_report": host,
    }


def redirected_host_report(report: bytes, host: str, key: bytes) -> bytes:
    """Report 0x35 with the host address and link key replaced (key stored little-endian)."""
    data = bytearray(report)
    data[3:9] = bytes.fromhex(host.replace(":", ""))
    data[9:25] = key[::-1]
    return bytes(data)


# --- BlueZ host state ---------------------------------------------------------


def default_adapter() -> str:
    try:
        output = subprocess.run(
            ["bluetoothctl", "show"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        fail(f"cannot query bluetoothctl: {error}")
    match = re.search(r"Controller ([0-9A-F:]{17})", output)
    if not match:
        fail("no Bluetooth controller found; pass the adapter address explicitly")
    return match.group(1)


def sdp_elements(record: bytes, offset: int = 0):
    """Return (type, payload_start, payload_end) for the data element at offset."""
    kind, size = record[offset] >> 3, record[offset] & 7
    offset += 1
    if kind == 0:
        return kind, offset, offset
    if size < 5:
        length = (1, 2, 4, 8, 16)[size]
    else:
        width = (1, 2, 4)[size - 5]
        length = int.from_bytes(record[offset : offset + width], "big")
        offset += width
    return kind, offset, offset + length


def validate_sdp(record: bytes) -> None:
    """Raise ValueError unless every nested sequence length is consistent."""

    def walk(offset: int) -> int:
        kind, start, end = sdp_elements(record, offset)
        if end > len(record):
            raise ValueError("element exceeds record")
        if kind in (6, 7):
            inner = start
            while inner < end:
                inner = walk(inner)
            if inner != end:
                raise ValueError("sequence length mismatch")
        return end

    if walk(0) != len(record):
        raise ValueError("trailing data after record")


def patch_hid_record(record: bytes) -> bytes:
    """Append the touch report collection to the HIDDescriptorList descriptor.

    The record is the trackpad's HID service record. The descriptor sits in
    attribute 0x0206 as 35 L1 35 L2 08 22 25 L3 <descriptor>; each length and
    the record's own sequence length grow by the inserted bytes.
    """
    if TOUCH_COLLECTION in record:
        return record
    data = bytearray(record)
    at = data.find(b"\x09\x02\x06")
    p = at + 3
    if at < 0 or data[p] != 0x35 or data[p + 2] != 0x35 or data[p + 4 : p + 7] != b"\x08\x22\x25":
        raise ValueError("unexpected HIDDescriptorList layout")
    start, length = p + 8, data[p + 7]
    if data[start + length - 1] != 0xC0:
        raise ValueError("descriptor does not end with End Collection")
    delta = len(TOUCH_COLLECTION)
    if max(data[p + 1], data[p + 3], length) + delta > 0xFF:
        raise ValueError("descriptor too long for one-byte lengths")
    data[start + length : start + length] = TOUCH_COLLECTION
    for index in (p + 1, p + 3, p + 7):
        data[index] += delta
    if data[0] != 0x36:
        raise ValueError("record is not a 16-bit-length sequence")
    data[1:3] = (len(data) - 3).to_bytes(2, "big")
    validate_sdp(bytes(data))
    return bytes(data)


def bundled_records() -> tuple[bytes, bytes]:
    hid = bytes.fromhex((DATA / "hid-record.hex").read_text().strip())
    pnp = bytes.fromhex((DATA / "pnp-record.hex").read_text().strip())
    return hid, pnp


def keyfile(text: str = "") -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(text)
    return parser


def render(parser: configparser.ConfigParser) -> str:
    out = io.StringIO()
    parser.write(out, space_around_delimiters=False)
    return out.getvalue()


def device_info(pnp: bytes) -> str:
    """BlueZ's device record without a link key, so pairing can create one."""
    vendor, product, version = pnp_ids(pnp)
    parser = keyfile()
    parser["General"] = {
        "Name": "Magic Trackpad",
        "Class": "0x002594",
        "SupportedTechnologies": "BR/EDR;",
        "Trusted": "true",
        "Blocked": "false",
        "Services": f"{HID_UUID};{PNP_UUID};",
    }
    parser["DeviceID"] = {
        "Source": "1",
        "Vendor": str(vendor),
        "Product": str(product),
        "Version": str(version),
    }
    return render(parser)


def pnp_ids(pnp: bytes) -> tuple[int, int, int]:
    """Vendor (0x0201), product (0x0202) and version (0x0203) from the PnP record."""
    values = {}
    _, offset, end = sdp_elements(pnp)
    while offset < end:
        _, id_start, id_end = sdp_elements(pnp, offset)
        attribute = int.from_bytes(pnp[id_start:id_end], "big")
        _, value_start, value_end = sdp_elements(pnp, id_end)
        values[attribute] = int.from_bytes(pnp[value_start:value_end], "big")
        offset = value_end
    return values[0x0201], values[0x0202], values[0x0203]


def device_cache(existing: str, hid: bytes, pnp: bytes) -> str:
    """BlueZ's SDP cache for the device with the patched HID record."""
    parser = keyfile(existing)
    if not parser.has_section("General"):
        parser["General"] = {"Name": "Magic Trackpad"}
    if not parser.has_section("ServiceRecords"):
        parser["ServiceRecords"] = {}
    parser["ServiceRecords"][f"0x{HID_HANDLE:08X}"] = patch_hid_record(hid).hex().upper()
    parser["ServiceRecords"][f"0x{PNP_HANDLE:08X}"] = pnp.hex().upper()
    return render(parser)


def has_link_key(info: str) -> bool:
    return keyfile(info).has_section("LinkKey")


# --- bluetoothctl -------------------------------------------------------------


def bluetoothctl(commands: list[tuple[str, float]], timeout: float = 60) -> str:
    process = subprocess.Popen(
        ["bluetoothctl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for command, delay in commands:
            process.stdin.write(command + "\n")
            process.stdin.flush()
            time.sleep(delay)
        process.stdin.close()
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output)


def device_property(device: str, name: str) -> str | None:
    output = subprocess.run(
        ["bluetoothctl", "info", device], capture_output=True, text=True
    ).stdout
    match = re.search(rf"^\s*{name}: (.*)$", output, re.MULTILINE)
    return match.group(1).strip() if match else None


# --- SDP capture --------------------------------------------------------------


def sdp_query(device: str, uuid16: int) -> bytes:
    """ServiceSearchAttributeRequest for one service, returning its first record."""
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    sock.settimeout(15)
    try:
        sock.connect((device, 1))
        pattern = bytes([0x35, 0x03, 0x19]) + uuid16.to_bytes(2, "big")
        attributes = bytes([0x35, 0x05, 0x0A, 0x00, 0x00, 0xFF, 0xFF])
        continuation, data, transaction = b"\x00", b"", 1
        while True:
            params = pattern + struct.pack(">H", 0xFFFF) + attributes + continuation
            sock.send(struct.pack(">BHH", 0x06, transaction, len(params)) + params)
            reply = sock.recv(4096)
            if reply[0] != 0x07:
                raise OSError(f"SDP error response {reply.hex()}")
            count = struct.unpack(">H", reply[5:7])[0]
            data += reply[7 : 7 + count]
            continuation = reply[7 + count :]
            transaction += 1
            if continuation[0] == 0:
                break
    finally:
        sock.close()
    _, start, end = sdp_elements(data)
    _, _, record_end = sdp_elements(data, start)
    if start == end:
        raise OSError("service not found")
    return data[start:record_end]


# --- subcommands --------------------------------------------------------------


def cmd_check(args) -> int:
    hidp = Path("/sys/module/hidp").exists() or subprocess.run(
        ["modinfo", "hidp"], capture_output=True
    ).returncode == 0
    print(f"kernel hidp: {'available' if hidp else 'missing (BlueZ needs UserspaceHID=true)'}")
    try:
        conf = keyfile(Path("/etc/bluetooth/input.conf").read_text())
        userspace = conf.get("General", "UserspaceHID", fallback="unset")
    except (OSError, configparser.Error):
        userspace = "unreadable"
    print(f"input.conf UserspaceHID: {userspace}")
    magic = Path("/sys/module/hid_magicmouse").exists() or subprocess.run(
        ["modinfo", "hid_magicmouse"], capture_output=True
    ).returncode == 0
    driver = "available (mt2_driver.py not needed)" if magic else "missing (use mt2_driver.py)"
    print(f"kernel hid_magicmouse: {driver}")
    print(f"/dev/uinput writable: {os.access('/dev/uinput', os.W_OK)}")
    bt = [
        "/dev/" + os.path.basename(n)
        for n in glob.glob("/sys/class/hidraw/hidraw*")
        if f"HID_ID={BT_HID_ID}" in Path(n, "device/uevent").read_text().split()
    ]
    for node in bt:
        print(f"Bluetooth hidraw {node} writable: {os.access(node, os.W_OK)}")
    if not bt:
        print("Bluetooth hidraw: trackpad not connected")
    return 0


def cmd_probe(args) -> int:
    path = find_usb_hidraw()
    fd = os.open(path, os.O_RDWR)
    try:
        info = read_reports(fd)
    finally:
        os.close(fd)
    print(f"USB node:         {path}")
    print(f"trackpad address: {info['address']}")
    print(f"model:            {info['model']} (class 0x{info['class']:06x})")
    print(f"USB-paired host:  {info['host']}")
    return 0


def cmd_redirect(args) -> int:
    host = args.adapter or default_adapter()
    path = find_usb_hidraw()
    fd = os.open(path, os.O_RDWR)
    try:
        report = get_feature(fd, REPORT_HOST)
        key = os.urandom(16)
        set_feature(fd, redirected_host_report(report, host, key))
        info = read_reports(fd)
    finally:
        os.close(fd)
    if info["host"] != host:
        fail(f"trackpad reports host {info['host']} after writing {host}")
    print(f"trackpad {info['address']} now points at {host}")
    print("Unplug it, switch it off and on, then run: mt2_pair.py pair " + info["address"])
    return 0


def cmd_bluez_setup(args) -> int:
    adapter = args.adapter or default_adapter()
    device = args.device
    hid, pnp = bundled_records()
    if args.hid_record:
        hid = bytes.fromhex(Path(args.hid_record).read_text().strip())
    base = Path(args.storage) / adapter
    info_path, cache_path = base / device / "info", base / "cache" / device
    info = info_path.read_text() if info_path.exists() else None
    if info is not None and has_link_key(info) and not args.reset:
        print(f"{info_path}: keeping existing pairing (use --reset to replace it)")
    else:
        info_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info_path.write_text(device_info(pnp))
        info_path.chmod(0o600)
        print(f"{info_path}: wrote device record without a link key")
    existing = cache_path.read_text() if cache_path.exists() else ""
    cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache_path.write_text(device_cache(existing, hid, pnp))
    cache_path.chmod(0o600)
    print(f"{cache_path}: wrote SDP records with the touch report declared")
    print("Restart BlueZ to load them: systemctl restart bluetooth")
    return 0


def cmd_pair(args) -> int:
    device = args.device
    if device_property(device, "Name") is None:
        fail(f"BlueZ does not know {device}; run bluez-setup first")
    if args.wait_unplug:
        print("waiting for the USB cable to be unplugged")
        while any(
            f"HID_ID={USB_HID_ID}" in Path(n, "device/uevent").read_text().split()
            for n in glob.glob("/sys/class/hidraw/hidraw*")
        ):
            time.sleep(1)
    deadline = time.monotonic() + args.timeout
    while device_property(device, "Paired") != "yes":
        if time.monotonic() > deadline:
            fail("pairing timed out; switch the trackpad off and on and retry")
        output = bluetoothctl(
            [("agent NoInputNoOutput", 1), ("default-agent", 1), (f"pair {device}", 14)]
        )
        result = re.search(r"Pairing successful|Failed to pair: \S+", output)
        print(time.strftime("%H:%M:%S"), result.group(0) if result else "no response")
    subprocess.run(["bluetoothctl", "trust", device], capture_output=True)
    output = bluetoothctl([(f"connect {device}", 15)])
    result = re.search(r"Connection successful|Failed to connect: \S+", output)
    print("connect:", result.group(0) if result else "no response")
    return 0 if device_property(device, "Connected") == "yes" else 1


def cmd_capture_sdp(args) -> int:
    for name, uuid in (("hid", 0x1124), ("pnp", 0x1200)):
        record = sdp_query(args.device, uuid)
        validate_sdp(record)
        print(f"{name}: {record.hex().upper()}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="report what this host needs").set_defaults(func=cmd_check)
    commands.add_parser("probe", help="read the trackpad over USB").set_defaults(func=cmd_probe)
    redirect = commands.add_parser("redirect", help="point the trackpad at a host (USB)")
    redirect.add_argument("adapter", nargs="?", type=address)
    redirect.set_defaults(func=cmd_redirect)
    setup = commands.add_parser("bluez-setup", help="write BlueZ records (root)")
    setup.add_argument("device", type=address)
    setup.add_argument("adapter", nargs="?", type=address)
    setup.add_argument("--reset", action="store_true", help="replace an existing pairing")
    setup.add_argument("--hid-record", help="file with a captured HID record in hex")
    setup.add_argument("--storage", default="/var/lib/bluetooth", help=argparse.SUPPRESS)
    setup.set_defaults(func=cmd_bluez_setup)
    pair = commands.add_parser("pair", help="pair, trust and connect by address")
    pair.add_argument("device", type=address)
    pair.add_argument("--wait-unplug", action="store_true", help="start after the USB unplug")
    pair.add_argument("--timeout", type=float, default=120)
    pair.set_defaults(func=cmd_pair)
    capture = commands.add_parser("capture-sdp", help="print the trackpad's SDP records")
    capture.add_argument("device", type=address)
    capture.set_defaults(func=cmd_capture_sdp)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
