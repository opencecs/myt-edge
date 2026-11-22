#!/usr/bin/env python3
"""Regression checks for the Y4 staged GPU stack and build cache identities.

Run with python3 -B scripts/test_graphics_config.py. No device access, external
program execution, or firmware build is performed.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
import build
from graphics_config import (
    EGL_JSON, GPU_LIB, LD_CONF, LEGACY_PROFILE, VULKAN_JSON,
    configure_graphics, tree_digest, verify_graphics,
)

LEGACY_TEXT = (
    "# RK1828 (Rockchip NPU) SDK environment for P1Pro Debian\n"
    '# Debian uses glibc natively; libs in /lib are registered via ldconfig.\n'
    'export LD_LIBRARY_PATH="/lib:${LD_LIBRARY_PATH}"\n'
    'export PATH="/bin:${PATH}"\n'
)
REQUIRED_LIBS = ("libmali.so.0", "libEGL_cix.so.1", "libgbm.so.1")


def put(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(mode)
    return path


class GraphicsFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="y4-graphics-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.lib = self.root / GPU_LIB
        self.lib.mkdir(parents=True)
        for index, name in enumerate(REQUIRED_LIBS):
            real = name + ".payload"
            put(self.lib / real, b"\x7fELF-vendor-fixture-" + bytes([index]), 0o700)
            (self.lib / name).symlink_to(real)
        self.lib.chmod(0o700)
        (self.root / "opt/cixgpu-pro").chmod(0o700)
        put(self.root / LD_CONF, "/opt/cixgpu-pro/lib/aarch64-linux-gnu\n", 0o700)
        put(self.root / EGL_JSON, json.dumps({
            "file_format_version": "1.0.0",
            "ICD": {"library_path": "/opt/cixgpu-pro/lib/libEGL_cix.so.1"},
        }), 0o700)
        put(self.root / VULKAN_JSON, json.dumps({
            "file_format_version": "1.0.0",
            "ICD": {"library_path": "/opt/cixgpu-pro/lib/libmali.so.0", "api_version": "1.4.305"},
        }))
        put(self.root / LEGACY_PROFILE, LEGACY_TEXT, 0o775)

    def test_repairs_existing_0700_and_flat_library_path(self):
        payloads = {p.name: p.read_bytes() for p in self.lib.iterdir() if not p.is_symlink()}
        links = {p.name: os.readlink(p) for p in self.lib.iterdir() if p.is_symlink()}
        configure_graphics(self.root)
        verify_graphics(self.root)
        self.assertEqual((self.root / LD_CONF).read_text(), "/opt/cixgpu-pro/lib\n")
        self.assertFalse((self.root / LEGACY_PROFILE).exists())
        self.assertEqual(stat.S_IMODE(self.lib.stat().st_mode), 0o755)
        for name, content in payloads.items():
            self.assertEqual((self.lib / name).read_bytes(), content)
            self.assertEqual(stat.S_IMODE((self.lib / name).stat().st_mode), 0o644)
        for name, target in links.items():
            self.assertTrue((self.lib / name).is_symlink())
            self.assertEqual(os.readlink(self.lib / name), target)

    def test_repeat_configuration_is_idempotent(self):
        configure_graphics(self.root)
        digest = tree_digest(self.root)
        configure_graphics(self.root)
        self.assertEqual(tree_digest(self.root), digest)

    def test_unknown_legacy_profile_is_preserved_without_partial_repair(self):
        put(self.root / LEGACY_PROFILE, LEGACY_TEXT + "export BUSINESS_RUNTIME=enabled\n")
        before = tree_digest(self.root)
        with self.assertRaises(ValueError):
            configure_graphics(self.root)
        self.assertEqual(tree_digest(self.root), before)

    def test_external_library_symlink_is_rejected_without_following_it(self):
        outside = put(Path(self.temp.name) / "external.so", "unchanged", 0o600)
        link = self.lib / "libmali.so.0"
        link.unlink()
        link.symlink_to(outside)
        before = tree_digest(self.root)
        with self.assertRaises(ValueError):
            configure_graphics(self.root)
        self.assertEqual(tree_digest(self.root), before)
        self.assertEqual(outside.read_text(), "unchanged")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o600)

    def test_broken_library_symlink_is_rejected(self):
        link = self.lib / "libmali.so.0"
        link.unlink()
        link.symlink_to("does-not-exist")
        with self.assertRaises((ValueError, FileNotFoundError)):
            configure_graphics(self.root)

    def test_library_directory_symlink_is_rejected(self):
        original = self.lib.with_name("actual-library")
        self.lib.rename(original)
        self.lib.symlink_to(original.name)
        with self.assertRaises(ValueError):
            configure_graphics(self.root)

    def test_missing_required_libraries_are_rejected_before_repair(self):
        for name in REQUIRED_LIBS:
            with self.subTest(name=name):
                link = self.lib / name
                target = os.readlink(link)
                link.unlink()
                before = tree_digest(self.root)
                with self.assertRaises(ValueError):
                    configure_graphics(self.root)
                self.assertEqual(tree_digest(self.root), before)
                link.symlink_to(target)

    def test_symlinked_loader_configuration_is_rejected(self):
        conf = self.root / LD_CONF
        conf.unlink()
        target = put(Path(self.temp.name) / "outside.conf", "preserve\n")
        conf.symlink_to(target)
        with self.assertRaises(ValueError):
            configure_graphics(self.root)
        self.assertEqual(target.read_text(), "preserve\n")

    def test_wrong_vendor_egl_path_is_rejected(self):
        put(self.root / EGL_JSON, json.dumps({"ICD": {"library_path": "/wrong/libEGL.so"}}))
        before = tree_digest(self.root)
        with self.assertRaises(ValueError):
            configure_graphics(self.root)
        self.assertEqual(tree_digest(self.root), before)

    def test_final_verifier_rejects_removed_required_library(self):
        configure_graphics(self.root)
        (self.lib / "libmali.so.0").unlink()
        with self.assertRaises(ValueError):
            verify_graphics(self.root)

    def test_final_verifier_rejects_changed_vendor_path(self):
        configure_graphics(self.root)
        put(self.root / EGL_JSON, json.dumps({"ICD": {"library_path": "/wrong/libEGL.so"}}))
        with self.assertRaises(ValueError):
            verify_graphics(self.root)

    def test_missing_vulkan_manifest_fails_build(self):
        (self.root / VULKAN_JSON).unlink()
        with self.assertRaisesRegex(ValueError, "Vulkan ICD manifest missing"):
            configure_graphics(self.root)

    def test_final_verifier_rejects_broken_vulkan_discovery(self):
        configure_graphics(self.root)
        path = self.root / VULKAN_JSON
        for field, value in (("library_path", "/wrong/libmali.so"), ("api_version", "1.0.0")):
            with self.subTest(field=field):
                spec = json.loads(path.read_text())
                original = path.read_text()
                spec["ICD"][field] = value
                put(path, json.dumps(spec))
                with self.assertRaisesRegex(ValueError, "Vulkan ICD library or API version"):
                    verify_graphics(self.root)
                put(path, original)
        path.parent.chmod(0o700)
        with self.assertRaisesRegex(ValueError, "graphics permissions"):
            verify_graphics(self.root)

    def test_final_verifier_rejects_permission_and_loader_regressions(self):
        configure_graphics(self.root)
        payload = self.lib / "libmali.so.0.payload"
        payload.chmod(0o700)
        with self.assertRaises(ValueError):
            verify_graphics(self.root)
        payload.chmod(0o644)
        put(self.root / LD_CONF, "/opt/cixgpu-pro/lib/aarch64-linux-gnu\n")
        with self.assertRaises(ValueError):
            verify_graphics(self.root)


class CacheIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="y4-graphics-cache-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.manifest = put(self.source / "rootfs/fetch/inputs-apt.json", '{"packages":["chromium=1"]}')
        put(self.source / "rootfs/overlay/etc/product.conf", "unchanged\n")
        self.vendor = put(self.source / "rootfs/vendor/cix/lib/libmali.so.1", "payload one")
        put(self.source / "scripts/apt_upgrade.py", "# fixture\n")
        self.helper = put(self.source / "scripts/graphics_config.py", "# fixture\n")
        put(self.source / "board/install.py", "# fixture\n")
        for name in ("rootfs_bootstrap.py", "rootfs_bootstrap_chroot.sh", "rootfs_download.py", "rootfs_host_policy.py"):
            put(self.source / "scripts" / name, "# fixture\n")
        self.board = self.root / "board"
        put(self.board / "y4-board.deb", "board package")
        self.builder = object.__new__(build.Builder)
        self.builder.report = {"inputs": {"debian_base": {"sha256": "fixture"}}}
        self.builder.source = self.source
        self.builder.board_output = self.board
        self.builder.fs = self.root / "filesystem"
        self.builder.out = self.root / "output"
        self.calls = []
        self.builder.run = lambda *args, **kwargs: self.calls.append((args, kwargs))
        self.reference = self.root / "reference"
        self.builder.args = type("Args", (), {"reuse_rootfs": str(self.reference)})()

    def save_rootfs_marker(self):
        put(self.reference / "ROOTFS-FINGERPRINT.json",
            json.dumps({"fingerprint": self.builder.rootfs_fingerprint()}))

    def assert_rootfs_rejected(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.builder.try_rootfs_reuse())
        self.assertEqual(self.calls, [])

    def test_manifest_content_change_rejects_rootfs_reuse(self):
        self.save_rootfs_marker()
        self.manifest.write_text('{"packages":["chromium=2"]}')
        self.assert_rootfs_rejected()

    def test_manifest_symlink_target_change_rejects_rootfs_even_for_equal_bytes(self):
        parent = self.manifest.parent
        put(parent / "manifest-a.json", "same bytes")
        put(parent / "manifest-b.json", "same bytes")
        self.manifest.unlink()
        self.manifest.symlink_to("manifest-a.json")
        self.save_rootfs_marker()
        self.manifest.unlink()
        self.manifest.symlink_to("manifest-b.json")
        self.assert_rootfs_rejected()

    def test_overlay_executable_mode_change_rejects_rootfs_reuse(self):
        script = put(self.source / "rootfs/overlay/usr/local/bin/launch", "#!/bin/sh", 0o644)
        self.save_rootfs_marker()
        script.chmod(0o755)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.builder.try_rootfs_reuse())
        self.assertEqual(self.calls, [])

    def test_overlay_absolute_and_dangling_link_changes_reject_rootfs_reuse(self):
        link = self.source / "rootfs/overlay/etc/current.conf"
        first = put(self.root / "outside-a.conf", "same bytes")
        second = put(self.root / "outside-b.conf", "same bytes")
        for original, changed in ((str(first), str(second)), ("missing-a", "missing-b")):
            with self.subTest(original=original):
                if link.is_symlink():
                    link.unlink()
                link.symlink_to(original)
                self.save_rootfs_marker()
                link.unlink()
                link.symlink_to(changed)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertFalse(self.builder.try_rootfs_reuse())
                self.assertEqual(self.calls, [])

    def test_frozen_package_helper_change_rejects_rootfs_reuse(self):
        helper = put(self.source / "scripts/frozen_packages.py", "# first fixture")
        self.save_rootfs_marker()
        helper.write_text("# changed fixture")
        self.assert_rootfs_rejected()

    def test_kernel_policy_helper_change_rejects_rootfs_reuse(self):
        helper = put(self.source / "scripts/kernel_requirements.py", "# first fixture")
        self.save_rootfs_marker()
        helper.write_text("# changed fixture")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.builder.try_rootfs_reuse())
        self.assertEqual(self.calls, [])

    def test_running_kernel_policy_helper_change_rejects_rootfs_reuse(self):
        runtime = put(self.root / "runtime-policy.py", "def verify(): pass")
        namespace = {}
        exec(compile(runtime.read_text(), str(runtime), "exec"), namespace)
        with mock.patch.object(build, "verify_kernel_compatibility", namespace["verify"]):
            self.save_rootfs_marker()
            runtime.write_text("def verify(): return None")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(self.builder.try_rootfs_reuse())
        self.assertEqual(self.calls, [])

    def test_vendor_payload_change_rejects_rootfs_identity(self):
        self.save_rootfs_marker()
        self.vendor.write_text("payload two")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.builder.try_rootfs_reuse())
        self.assertEqual(self.calls, [])

    def test_vendor_symlink_target_change_rejects_rootfs_reuse(self):
        sibling = put(self.vendor.with_name("libmali.so.2"), self.vendor.read_bytes())
        link = self.vendor.with_name("libmali.so")
        link.symlink_to(self.vendor.name)
        self.save_rootfs_marker()
        link.unlink()
        link.symlink_to(sibling.name)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.builder.try_rootfs_reuse())
        self.assertEqual(self.calls, [])

    def test_legacy_vendor_change_and_graphics_helper_change_affect_rootfs(self):
        before = self.builder.rootfs_fingerprint()
        legacy = put(self.source / "vendor/cix/extra.so", "legacy")
        after_legacy = self.builder.rootfs_fingerprint()
        self.assertNotEqual(before, after_legacy)
        self.helper.write_text("# changed fixture\n")
        after_helper = self.builder.rootfs_fingerprint()
        self.assertNotEqual(after_legacy, after_helper)
        legacy.chmod(0o700)
        self.assertNotEqual(after_helper, self.builder.rootfs_fingerprint())

    def test_broken_input_root_symlinks_have_distinct_identities(self):
        link = self.root / "broken"
        link.symlink_to("missing-a")
        first = tree_digest(link)
        self.assertIsInstance(first, str)
        link.unlink()
        link.symlink_to("missing-b")
        self.assertNotEqual(first, tree_digest(link))

    def test_input_digest_does_not_follow_external_symlink(self):
        external = put(self.root / "external.payload", "one")
        (self.vendor.parent / "external.so").symlink_to(external)
        before = tree_digest(self.source / "rootfs/vendor")
        external.write_text("two")
        self.assertEqual(tree_digest(self.source / "rootfs/vendor"), before)

    def test_input_digest_accounts_for_modes_and_empty_missing_trees(self):
        self.assertIsNone(tree_digest(self.root / "missing"))
        empty = self.root / "empty"
        empty.mkdir()
        self.assertIsInstance(tree_digest(empty), str)
        before = tree_digest(self.source / "rootfs/vendor")
        self.vendor.chmod(0o700)
        self.assertNotEqual(tree_digest(self.source / "rootfs/vendor"), before)

    def test_bootstrap_script_change_rejects_rootfs_reuse(self):
        self.save_rootfs_marker()
        (self.source / "scripts/rootfs_bootstrap.py").write_text("# new product defaults")
        self.assert_rootfs_rejected()

    def test_only_verified_debs_can_be_reused(self):
        from rootfs_download import digest, fetch, good
        payload = put(self.root / "input.deb", b"verified fixture")
        entry = {"sha256": digest(payload), "bytes": payload.stat().st_size, "url": "https://example.invalid/input.deb"}
        cache = self.root / "cache"
        cached = put(cache / (entry["sha256"] + ".deb"), payload.read_bytes())
        with mock.patch("rootfs_download.subprocess.run") as download:
            self.assertEqual(fetch(entry,cache), cached)
            download.assert_not_called()
        cached.write_bytes(b"corrupt! fixture")
        self.assertFalse(good(cached,entry))


if __name__ == "__main__":
    unittest.main(verbosity=2)
