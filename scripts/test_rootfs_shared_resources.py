#!/usr/bin/env python3
"""Check the metadata and path boundaries of production rootfs deduplication."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build import share_packaged_resources


@unittest.skipUnless(os.geteuid() == 0, "staged package files must be owned by root")
class SharedResourcesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.time_ns = 1660000000123456789
        self.contents = b"a" * 8192

    def file(self, relative, data=None, mode=0o644, delta_ns=0):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.contents if data is None else data)
        path.chmod(mode)
        stamp = self.time_ns + delta_ns
        os.utime(path, ns=(stamp, stamp))
        return path

    def test_identical_resources_keep_paths_metadata_and_independent_replacement(self):
        a = self.file("usr/share/doc/a/changelog.gz")
        b = self.file("usr/share/doc/b/changelog.gz")
        locales = "usr/lib/chromium/locales/"
        en = self.file(locales + "en-US.pak.info")
        zh = self.file(locales + "zh-CN.pak.info")
        actual_locale = self.file(locales + "zh-CN.pak")
        old_time = b.parent.stat().st_mtime_ns
        result = share_packaged_resources(self.root)
        self.assertEqual(result, {"linked_paths": 2, "shared_file_bytes": 2 * len(self.contents)})
        self.assertTrue(a.samefile(b))
        self.assertTrue(en.samefile(zh))
        self.assertFalse(en.samefile(actual_locale))
        self.assertEqual(b.parent.stat().st_mtime_ns, old_time)
        self.assertEqual(b.stat().st_mtime_ns, self.time_ns)
        self.assertEqual(share_packaged_resources(self.root)["linked_paths"], 0)
        replacement = self.file("new-changelog.gz", b"updated")
        replacement.replace(a)
        self.assertEqual(b.read_bytes(), self.contents)

    def test_unmatched_files_and_links_are_untouched(self):
        a = self.file("usr/share/doc/a/NEWS.gz")
        variants = [
            self.file("usr/share/doc/b/NEWS.gz", delta_ns=1),
            self.file("usr/share/doc/c/NEWS.gz", mode=0o444),
            self.file("usr/share/doc/d/NEWS.gz", data=b"b" * len(self.contents)),
            self.file("usr/share/doc/e/NEWS.gz"),
            self.file("usr/share/doc/f/NEWS.gz"),
            self.file("usr/share/doc/h/NEWS.gz"),
            self.file("usr/share/doc/i/NEWS.gz", data=b"small"),
            self.file("usr/share/doc/j/NEWS.gz", data=b"small"),
            self.file("usr/share/doc/k/NEWS.Debian.gz"),
        ]
        os.setxattr(variants[3], "user.test", b"keep")
        os.chown(variants[4], 1, 1)
        os.link(variants[5], variants[5].with_suffix(".other"))
        outside = self.file("../outside/NEWS.gz")
        (a.parent.parent / "outside").symlink_to(outside.parent, target_is_directory=True)
        symlink = a.parent.parent / "symbolic"
        symlink.mkdir()
        (symlink / "NEWS.gz").symlink_to(outside)
        inodes = [p.stat().st_ino for p in [a, *variants, outside]]
        self.assertEqual(share_packaged_resources(self.root)["linked_paths"], 0)
        self.assertEqual(inodes, [p.stat().st_ino for p in [a, *variants, outside]])

    def test_failed_replace_preserves_both_files(self):
        a = self.file("usr/share/doc/a/NEWS.gz")
        b = self.file("usr/share/doc/b/NEWS.gz")
        with patch("build.os.replace", side_effect=OSError("test replacement failure")):
            with self.assertRaises(OSError):
                share_packaged_resources(self.root)
        self.assertFalse(a.samefile(b))
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(list(b.parent.iterdir()), [b])


if __name__ == "__main__":
    unittest.main()
