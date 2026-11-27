#!/usr/bin/env python3
"""Exercise data partition preparation without opening any block devices."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SOURCE = (REPO / "rootfs/overlay/usr/libexec/y4-data-provision").read_text()
PREPARE = SOURCE[SOURCE.index('if type=$(blkid -p -o value -s TYPE "$data_device"'):]
MOCKS = r"""
set -eu
state=$TEST_STATE
data_device=/dev/mock-data
fail() { echo "$*" >&2; exit 1; }
record() { echo "$*" >> "$state/events"; }
blkid() {
    [ "$6" = "$data_device" ] || exit 90
    case "$5" in
        TYPE) [ -n "$TEST_TYPE" ] || return 2; echo "$TEST_TYPE" ;;
        LABEL) echo Y4DEB_DATA ;;
        *) exit 91 ;;
    esac
}
e2fsck() {
    [ ! -e "$state/mounted" ] || exit 92
    record "check $*"
    case "$*" in
        "-p $data_device") return "$TEST_CHECK_STATUS" ;;
        "-p -f $data_device") return "$TEST_GROW_STATUS" ;;
        *) exit 93 ;;
    esac
}
mount() { test ! -e "$state/mounted"; touch "$state/mounted"; record mount; }
umount() { rm "$state/mounted"; record unmount; }
wipefs() { record wipe; }
blockdev() { echo "$TEST_PARTITION_BYTES"; }
dumpe2fs() { printf 'Block count: 16\nBlock size: 4096\n'; }
resize2fs() { test ! -e "$state/mounted"; record grow; }
cp() { :; }
sync() { :; }
desktop_state_prepare() { record desktop; }
mcu_state_prepare() { record mcu; }
"""


class DataProvisionTests(unittest.TestCase):
    def prepare(self, *, existing=True, stamped=True, check=0, grow=0, expandable=False):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "events").touch()
            mkfs = state / "mkfs.ext4"
            mkfs.write_text('#!/bin/sh\necho format >> "$TEST_STATE/events"\n')
            mkfs.chmod(0o700)
            if existing and stamped:
                (state / "mnt").mkdir()
                (state / "mnt/.y4-data-stamp").write_text("y4-data v1\n")
            env = dict(os.environ, TEST_STATE=directory,
                       PATH=directory + os.pathsep + os.environ["PATH"],
                       TEST_TYPE="ext4" if existing else "",
                       TEST_CHECK_STATUS=str(check), TEST_GROW_STATUS=str(grow),
                       TEST_PARTITION_BYTES=str(131072 if expandable else 65536))
            result = subprocess.run(
                ["dash", "-c", MOCKS + PREPARE], env=env, text=True,
                capture_output=True, timeout=10, check=False,
            )
            events = (state / "events").read_text().splitlines()
            ready = (state / "result").is_file()
        return result, events, ready

    def test_blank_partition_formats_without_fsck(self):
        result, events, ready = self.prepare(existing=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(ready)
        self.assertEqual(events[:3], ["format", "mount", "unmount"])
        self.assertFalse(any(event.startswith("check") for event in events))

    def test_existing_partition_checks_before_mount_and_keeps_filesystem(self):
        for status in (0, 1):
            with self.subTest(status=status):
                result, events, ready = self.prepare(check=status)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(ready)
                self.assertEqual(events, [
                    "check -p /dev/mock-data", "mount", "unmount",
                    "mount", "desktop", "mcu", "unmount",
                ])

    def test_existing_partition_check_failure_stops_before_any_mount(self):
        for status in (2, 4, 8):
            with self.subTest(status=status):
                result, events, ready = self.prepare(check=status)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"e2fsck status {status}", result.stderr)
                self.assertEqual(events, ["check -p /dev/mock-data"])
                self.assertFalse(ready)

    def test_existing_data_label_without_stamp_remains_protected(self):
        result, events, ready = self.prepare(stamped=False)
        self.assertIn("refusing to format it", result.stderr)
        self.assertNotIn("format", events)
        self.assertNotIn("wipe", events)
        self.assertFalse(ready)

    def test_forced_check_finishes_before_growing_existing_partition(self):
        result, events, ready = self.prepare(expandable=True, grow=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(ready)
        self.assertEqual(events[:5], [
            "check -p /dev/mock-data", "mount", "unmount",
            "check -p -f /dev/mock-data", "grow",
        ])
        self.assertNotIn("format", events)


if __name__ == "__main__":
    unittest.main()
