#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Test MCU state migration with synthetic root directories and receipts."""

import errno
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("board_boot", REPO / "board/factory/boot.py")
boot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boot)


@unittest.skipUnless(os.geteuid() == 0, "MCU migration intentionally verifies root ownership")
class MigrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.data = self.base / "data"
        self.a, self.b = self.base / "slot-a", self.base / "slot-b"
        self.data.mkdir()
        for root in (self.a, self.b):
            directory = root / "var/lib/y4-board"
            directory.mkdir(mode=0o700, parents=True)
        self.shared = self.data / "y4-board/state"
        self.verified = []

    def record(self, root, job, content, *, sequence=7, phase=None, blocked=False):
        directory = root / "var/lib/y4-board"
        filename = f"firmware-{job * 32}.bin"
        boot.atomic(directory / filename, content)
        receipt = {"schema": 1, "job": job * 32, "package": filename,
                   "sha256": hashlib.sha256(content).hexdigest(), "sequence": sequence}
        if phase:
            receipt.update(phase=phase, status=-errno.EIO, blocked=blocked)
        boot.atomic_json(directory / ("update.json" if phase else "active.json"), receipt)
        return receipt

    def verify(self, path, root, receipt):
        self.assertEqual(path.name, "verify.bin")
        self.assertIn(root, (self.a, self.b))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), receipt["sha256"])
        self.verified.append(receipt["sequence"])

    def migrate(self):
        boot.migrate_mcu_state(self.data, (self.a, self.b), self.verify)

    def test_import_previous_slot_and_preserve_sources(self):
        active = self.record(self.b, "a", b"old root package")
        self.migrate()
        self.assertEqual(boot.read_json(self.shared / "active.json"), active)
        self.assertEqual((self.shared / active["package"]).read_bytes(), b"old root package")
        self.assertEqual(boot.read_json(self.b / "var/lib/y4-board/active.json"), active)
        self.assertEqual(self.verified, [7])
        self.assertEqual(stat.S_IMODE(self.shared.stat().st_mode), 0o700)
        for path in self.shared.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_two_slots_highest_signed_sequence(self):
        self.record(self.a, "a", b"lower", sequence=8)
        active = self.record(self.b, "b", b"higher", sequence=9)
        self.migrate()
        self.assertEqual(boot.read_json(self.shared / "active.json"), active)
        self.assertEqual(self.verified, [8, 9])

    def test_same_sequence_different_signed_packages_refused(self):
        self.record(self.a, "a", b"first")
        self.record(self.b, "b", b"second")
        with self.assertRaisesRegex(ValueError, "same signed sequence"):
            self.migrate()
        self.assertFalse(self.shared.exists())

    def test_same_sequence_same_signed_package_is_idempotent(self):
        first = self.record(self.a, "a", b"identical")
        self.record(self.b, "b", b"identical")
        self.migrate()
        self.assertEqual(boot.read_json(self.shared / "active.json"), first)

    def test_existing_shared_state_preserved(self):
        self.record(self.a, "a", b"keep shared", sequence=8)
        self.migrate()
        self.verified.clear()
        shared = boot.read_json(self.shared / "active.json")
        self.record(self.b, "b", b"old unshared slot", sequence=9)
        self.migrate()
        self.assertEqual(boot.read_json(self.shared / "active.json"), shared)
        self.assertEqual(self.verified, [])

    def test_corrupt_existing_state_is_not_overwritten(self):
        active = self.record(self.a, "a", b"package")
        self.migrate()
        corrupt = self.shared / active["package"]
        corrupt.write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.migrate()
        self.assertEqual(corrupt.read_bytes(), b"damaged")
        self.assertEqual(boot.read_json(self.shared / "active.json"), active)

    def test_missing_shared_marker_is_not_overwritten(self):
        boot.private_dir(self.shared.parent)
        boot.private_dir(self.shared)
        with self.assertRaises(FileNotFoundError):
            self.migrate()
        self.assertEqual(list(self.shared.iterdir()), [])

    def test_missing_committed_shared_directory_is_not_recreated(self):
        self.record(self.a, "a", b"first")
        self.migrate()
        preserved = self.shared.with_name("protected-copy")
        self.shared.rename(preserved)
        self.record(self.b, "b", b"second")
        with self.assertRaisesRegex(ValueError, "initialized shared MCU state is missing"):
            self.migrate()
        self.assertFalse(self.shared.exists())
        self.assertTrue((preserved / "active.json").exists())

    def test_bad_legacy_package_never_publishes(self):
        row = self.record(self.a, "a", b"bad")
        (self.a / "var/lib/y4-board" / row["package"]).write_bytes(b"different")
        self.record(self.b, "b", b"valid")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.migrate()
        self.assertFalse(self.shared.exists())

    def test_legacy_asset_cannot_override_signature_check(self):
        self.record(self.a, "a", b"unverified")
        with self.assertRaisesRegex(ValueError, "invalid signature"):
            boot.migrate_mcu_state(self.data, (self.a, self.b),
                                   verify=lambda *args: self.fail_signature())
        self.assertFalse(self.shared.exists())

    def fail_signature(self):
        raise ValueError("invalid signature")

    def test_unsettled_job_survives_alongside_active(self):
        active = self.record(self.a, "a", b"active version")
        pending = self.record(self.b, "b", b"unsettled package", phase="running")
        self.migrate()
        self.assertEqual(boot.read_json(self.shared / "active.json"), active)
        job = boot.read_json(self.shared / "update.json")
        self.assertEqual(job, dict(pending, phase="interrupted_unknown",
                                   blocked=True, status=-errno.EINPROGRESS))
        self.assertEqual((self.shared / pending["package"]).read_bytes(), b"unsettled package")
        self.assertTrue(boot.check_shared_mcu_state(self.shared))

    def test_distinct_unsettled_jobs_refused(self):
        self.record(self.a, "a", b"first", phase="cancelling")
        self.record(self.b, "b", b"second", phase="failed", blocked=True)
        with self.assertRaisesRegex(ValueError, "different unsettled"):
            self.migrate()
        self.assertFalse(self.shared.exists())

    def test_terminal_unblocked_history_not_migrated(self):
        self.record(self.a, "a", b"last failed package", phase="failed")
        self.migrate()
        self.assertFalse((self.shared / "update.json").exists())
        self.assertFalse(boot.check_shared_mcu_state(self.shared))

    def test_successfully_recovered_job_remains_terminal(self):
        self.record(self.a, "a", b"unsettled package", phase="running")
        self.migrate()
        receipt = boot.read_json(self.shared / "update.json")
        boot.atomic_json(self.shared / "update.json", dict(receipt, phase="recovery_verified",
                                                           blocked=False))
        self.assertFalse(boot.check_shared_mcu_state(self.shared))

    def test_interrupted_factory_writer_takes_priority_over_shared_mount(self):
        obj = boot.Initializer("01234567-89ab-cdef-0123-456789abcdef")
        with (patch.object(obj, "restore", return_value="interrupted"),
              patch.object(boot, "pending_barriers", return_value=[]),
              patch.object(obj, "interrupted", return_value=0) as interrupted,
              patch.object(boot, "shared_mcu_pending", side_effect=AssertionError("wrong order"))):
            self.assertEqual(obj.run(), 0)
            interrupted.assert_called_once_with()

    def test_boot_checks_real_bind_and_ready_marker(self):
        layout, ready = self.base / "layout", self.base / "ready"
        with patch.multiple(boot, STATE_LAYOUT=layout, STATE_READY=ready, DATA=self.data, STATE=self.shared):
            self.assertIsNone(boot.shared_mcu_pending())  # Standalone package on prior image.
            layout.write_bytes(b"y4-mcu-state v1\n")
            self.migrate()
            with self.assertRaises(FileNotFoundError):
                boot.shared_mcu_pending()
            ready.write_bytes(b"ready\n")
            with self.assertRaisesRegex(ValueError, "not mounted"):
                boot.shared_mcu_pending()
            with patch.object(Path, "is_mount", return_value=True):
                self.assertFalse(boot.shared_mcu_pending())
                with patch.object(boot, "STATE", self.a / "var/lib/y4-board"):
                    with self.assertRaisesRegex(ValueError, "not mounted"):
                        boot.shared_mcu_pending()

    def test_firmware_write_is_skipped_when_shared_storage_not_ready(self):
        obj = boot.Initializer("01234567-89ab-cdef-0123-456789abcdef")
        failure = OSError(errno.ENOENT, "missing shared MCU state")
        with (patch.object(obj, "restore", return_value="fresh"),
              patch.object(boot, "pending_barriers", return_value=[]),
              patch.object(boot, "shared_mcu_pending", side_effect=failure),
              patch.object(obj, "finish", return_value=0) as finish,
              patch.object(boot, "executable", side_effect=AssertionError("unexpected writer"))):
            self.assertEqual(obj.run(), 0)
            finish.assert_called_once_with(-errno.EOWNERDEAD)
        self.assertFalse(obj.state["mcu_state_ready"])
        self.assertTrue(obj.state["mcu_retry_blocked"])

    def test_firmware_write_is_skipped_for_current_or_unidentified_update(self):
        current_boot = "01234567-89ab-cdef-0123-456789abcdef"
        for job in ({"boot_id": current_boot}, {"boot_id": "invalid"}, {"job": "unknown"}):
            with self.subTest(job=job):
                obj = boot.Initializer(current_boot)
                with (patch.object(obj, "restore", return_value="fresh"),
                      patch.object(boot, "pending_barriers", return_value=[]),
                      patch.object(boot, "shared_mcu_pending", return_value=job),
                      patch.object(obj, "finish", return_value=0) as finish,
                      patch.object(boot, "executable", side_effect=AssertionError("unexpected writer"))):
                    self.assertEqual(obj.run(), 0)
                    finish.assert_called_once_with(-errno.EOWNERDEAD)
                self.assertTrue(obj.state["mcu_state_ready"])
                self.assertTrue(obj.state["mcu_retry_blocked"])

    def test_previous_boot_update_allows_normal_recovery_path(self):
        obj = boot.Initializer("01234567-89ab-cdef-0123-456789abcdef")
        pending = {"boot_id": "abcd4567-89ab-cdef-0123-456789abcdef"}
        with (patch.object(obj, "restore", return_value="fresh"),
              patch.object(boot, "pending_barriers", return_value=[]),
              patch.object(boot, "shared_mcu_pending", return_value=pending),
              patch.object(obj, "publish"),
              patch.object(boot, "executable", side_effect=RuntimeError("checked recovery validation"))):
            with self.assertRaisesRegex(RuntimeError, "checked recovery validation"):
                obj.run()
        self.assertTrue(obj.state["mcu_state_ready"])
        self.assertTrue(obj.state["mcu_recovery"])
        self.assertNotIn("mcu_retry_blocked", obj.state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
