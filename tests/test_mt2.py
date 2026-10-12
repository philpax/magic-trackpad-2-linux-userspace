import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mt2_driver as driver  # noqa: E402
import mt2_pair as pair  # noqa: E402

# Reports captured from a Magic Trackpad 2 over Bluetooth in multitouch mode.
ONE_FINGER_CLICK = bytes.fromhex("31a962016bfd9793674b17c125")
ONE_FINGER_LIFTING = bytes.fromhex("31482101ed3dd223474d141022")
TWO_FINGER_CLICK = bytes.fromhex("31b98c024de4f8935138191721ab3eba8f444518262a")


def key_state(events, code):
    values = [value for kind, c, value in events if kind == driver.EV_KEY and c == code]
    return values[-1] if values else None


class DecodeTests(unittest.TestCase):
    def test_one_finger_click(self):
        touches, click = driver.decode_report(ONE_FINGER_CLICK)
        self.assertTrue(click)
        self.assertEqual(len(touches), 1)
        touch = touches[0]
        self.assertEqual((touch["id"], touch["x"], touch["y"]), (5, -661, 833))
        self.assertTrue(touch["down"])
        self.assertEqual(touch["pressure"], 193)

    def test_lifting_touch_is_not_down(self):
        touches, click = driver.decode_report(ONE_FINGER_LIFTING)
        self.assertFalse(click)
        self.assertFalse(touches[0]["down"])

    def test_two_fingers(self):
        touches, click = driver.decode_report(TWO_FINGER_CLICK)
        self.assertTrue(click)
        self.assertEqual([t["id"] for t in touches], [1, 10])
        for touch in touches:
            self.assertTrue(driver.MIN_X <= touch["x"] <= driver.MAX_X)
            self.assertTrue(driver.MIN_Y <= touch["y"] <= driver.MAX_Y)

    def test_other_reports_ignored(self):
        self.assertIsNone(driver.decode_report(bytes.fromhex("9001020304")))
        self.assertIsNone(driver.decode_report(ONE_FINGER_CLICK[:-1]))


class FrameTests(unittest.TestCase):
    def test_two_finger_click_reports_doubletap_and_button(self):
        tracking = {}
        touches, click = driver.decode_report(TWO_FINGER_CLICK)
        events, next_id = driver.frame_events(tracking, 0, touches, click)
        self.assertEqual(key_state(events, driver.BTN_TOOL_DOUBLETAP), 1)
        self.assertEqual(key_state(events, driver.BTN_TOOL_FINGER), 0)
        self.assertEqual(key_state(events, driver.BTN_LEFT), 1)
        self.assertEqual(key_state(events, driver.BTN_TOUCH), 1)
        self.assertEqual(tracking, {1: 0, 10: 1})
        self.assertEqual(next_id, 2)
        self.assertEqual(events[-1], (driver.EV_SYN, driver.SYN_REPORT, 0))

    def test_missing_touches_are_released(self):
        tracking = {}
        touches, click = driver.decode_report(TWO_FINGER_CLICK)
        _, next_id = driver.frame_events(tracking, 0, touches, click)
        events, _ = driver.frame_events(tracking, next_id, [], False)
        released = [
            value
            for kind, code, value in events
            if kind == driver.EV_ABS and code == driver.ABS_MT_TRACKING_ID
        ]
        self.assertEqual(released, [-1, -1])
        self.assertEqual(tracking, {})
        self.assertEqual(key_state(events, driver.BTN_TOUCH), 0)
        self.assertEqual(key_state(events, driver.BTN_LEFT), 0)


class DescriptorTests(unittest.TestCase):
    def setUp(self):
        self.hid, self.pnp = pair.bundled_records()

    def descriptor(self, record):
        at = record.find(b"\x09\x02\x06") + 3
        return record[at + 8 : at + 8 + record[at + 7]]

    def test_patched_record_matches_known_good(self):
        expected = (ROOT / "tests/patched-hid-record.hex").read_text().strip()
        self.assertEqual(pair.patch_hid_record(self.hid).hex().upper(), expected)

    def test_patch_declares_touch_report(self):
        self.assertFalse(driver.declares_touch_report(self.descriptor(self.hid)))
        patched = pair.patch_hid_record(self.hid)
        self.assertTrue(driver.declares_touch_report(self.descriptor(patched)))
        pair.validate_sdp(patched)

    def test_patch_is_idempotent(self):
        patched = pair.patch_hid_record(self.hid)
        self.assertEqual(pair.patch_hid_record(patched), patched)

    def test_pnp_ids(self):
        self.assertEqual(pair.pnp_ids(self.pnp), (0x004C, 0x0265, 0x0311))


class HostReportTests(unittest.TestCase):
    def test_redirect_layout(self):
        report = bytes([0x35]) + bytes(24)
        key = bytes(range(16))
        data = pair.redirected_host_report(report, "90:82:C3:6F:45:2C", key)
        self.assertEqual(data[:3], b"\x35\x00\x00")
        self.assertEqual(pair.format_address(data[3:9]), "90:82:C3:6F:45:2C")
        self.assertEqual(data[9:25], key[::-1])
        self.assertEqual(len(data), len(report))


class BluezSetupTests(unittest.TestCase):
    ADAPTER, DEVICE = "90:82:C3:6F:45:2C", "34:B1:EB:ED:39:CA"

    def run_setup(self, storage, *extra):
        pair.main(["bluez-setup", self.DEVICE, self.ADAPTER, "--storage", str(storage), *extra])
        base = Path(storage) / self.ADAPTER
        return base / self.DEVICE / "info", base / "cache" / self.DEVICE

    def test_fresh_setup(self):
        with tempfile.TemporaryDirectory() as storage:
            info, cache = self.run_setup(storage)
            parsed = pair.keyfile(info.read_text())
            self.assertFalse(parsed.has_section("LinkKey"))
            self.assertIn(pair.HID_UUID, parsed["General"]["Services"])
            self.assertEqual(parsed["DeviceID"]["Product"], "613")
            records = pair.keyfile(cache.read_text())["ServiceRecords"]
            expected = (ROOT / "tests/patched-hid-record.hex").read_text().strip()
            self.assertEqual(records["0x00010000"], expected)
            self.assertNotIn(" = ", cache.read_text())

    def test_existing_pairing_kept_unless_reset(self):
        with tempfile.TemporaryDirectory() as storage:
            info, cache = self.run_setup(storage)
            bonded = info.read_text() + "\n[LinkKey]\nKey=00112233445566778899AABBCCDDEEFF\nType=4\n"
            info.write_text(bonded)
            cache.write_text("[General]\nName=Magic Trackpad\n\n[ServiceRecords]\n0x00010002=35\n")
            self.run_setup(storage)
            self.assertEqual(info.read_text(), bonded)
            records = pair.keyfile(cache.read_text())["ServiceRecords"]
            self.assertEqual(records["0x00010002"], "35")
            self.assertIn("0x00010000", records)
            self.run_setup(storage, "--reset")
            self.assertFalse(pair.has_link_key(info.read_text()))


if __name__ == "__main__":
    unittest.main()
