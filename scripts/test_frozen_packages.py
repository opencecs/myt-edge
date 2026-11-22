#!/usr/bin/env python3
"""Frozen-input regression tests: tiny real DEBs, no package installation."""
import copy
import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

sys.dont_write_bytecode = True
import frozen_packages as frozen

VERSION = "1:2.3-1~test1"
PRIMARY = "https://security.debian.org/debian-security/pool/test/"
MIRROR = "https://mirrors.ustc.edu.cn/debian-security/pool/test/"


def snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            value = ("link", os.readlink(path))
        elif path.is_file():
            value = ("file", stat.S_IMODE(path.stat().st_mode), path.read_bytes())
        else:
            continue
        result[str(path.relative_to(root))] = value
    return result


@unittest.skipUnless(shutil.which("dpkg-deb"), "native dpkg-deb required")
class FrozenPackagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = tempfile.TemporaryDirectory(prefix="y4-test-debs-")
        cls.addClassCleanup(cls.inputs.cleanup)
        cls.payloads = {}
        variants = {
            "browser": ("y4-test-browser", VERSION, "arm64"),
            "common": ("y4-test-common", VERSION, "arm64"),
            "all": ("y4-test-browser", VERSION, "all"),
            "wrong-name": ("y4-test-other", VERSION, "arm64"),
            "wrong-version": ("y4-test-browser", "9.0-1", "arm64"),
            "wrong-arch": ("y4-test-browser", VERSION, "amd64"),
        }
        for label, (name, version, arch) in variants.items():
            directory = Path(cls.inputs.name) / label
            control = directory / "DEBIAN/control"
            control.parent.mkdir(parents=True)
            control.write_text(
                "Package: " + name + "\nVersion: " + version + "\nArchitecture: " + arch
                + "\nMaintainer: Y4 Test <test@example.invalid>\n"
                + "Description: isolated frozen package input fixture\n")
            data = directory / "usr/share/y4-fixture/payload"
            data.parent.mkdir(parents=True)
            data.write_text(label)
            archive = directory.with_suffix(".deb")
            subprocess.run(["dpkg-deb", "--build", "--root-owner-group",
                            str(directory), str(archive)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            cls.payloads[label] = archive.read_bytes()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="y4-frozen-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "root"
        self.cache = self.root / "var/cache/apt/archives"
        self.cache.mkdir(parents=True)
        self.sources = self.root / "etc/apt/sources.list"
        self.sources.parent.mkdir(parents=True)
        self.sources.write_text("preserve original sources\n")
        (self.cache / "existing.deb").write_bytes(b"unrelated old cached bytes")
        self.requests = []
        self.native_run = subprocess.run

    def declaration(self, label="browser", name="y4-test-browser", version=VERSION, arch="arm64"):
        data = self.payloads[label]
        return {"name": name, "version": version, "arch": arch,
                "deb": {"url": PRIMARY + name + ".deb",
                        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}}

    def downloader(self, payloads):
        def download(url, destination, expected_bytes):
            self.requests.append(url)
            value = payloads[url]
            if isinstance(value, Exception):
                raise value
            destination.write_bytes(value)
        return download

    def forbid_installs(self, argv, **kwargs):
        self.assertEqual(argv[0:2], ["dpkg-deb", "-f"])
        return self.native_run(argv, **kwargs)

    def prepare(self, packages, download):
        with mock.patch.object(frozen.subprocess, "run", side_effect=self.forbid_installs):
            return frozen.prepare_frozen_packages(self.root, {"packages": packages}, download=download)

    def test_valid_download_returns_explicit_chroot_path_without_installing(self):
        entry = self.declaration()
        old = (self.cache / "existing.deb").read_bytes()
        result = self.prepare([entry], self.downloader({entry["deb"]["url"]: self.payloads["browser"]}))
        path = result[entry["name"]]
        self.assertTrue(path.startswith("/var/cache/apt/archives/y4-frozen-"))
        self.assertEqual((self.root / path.lstrip("/")).read_bytes(), self.payloads["browser"])
        self.assertEqual((self.cache / "existing.deb").read_bytes(), old)
        self.assertEqual(self.sources.read_text(), "preserve original sources\n")
        self.assertFalse(list(self.cache.glob(".y4-frozen-*")))

    def test_cache_reuse_uses_hash_not_filename_and_performs_no_download(self):
        entry = self.declaration()
        existing = self.cache / "arbitrary package cache name.deb"
        existing.write_bytes(self.payloads["browser"])
        before = snapshot(self.root)
        result = self.prepare([entry], mock.Mock(side_effect=AssertionError("network must not be used")))
        self.assertEqual(result[entry["name"]], "/var/cache/apt/archives/" + existing.name)
        self.assertEqual(snapshot(self.root), before)

    def test_bad_hash_and_size_preserve_cache_and_configuration(self):
        for field, value in (("sha256", "0" * 64), ("bytes", len(self.payloads["browser"]) + 1)):
            with self.subTest(field=field):
                entry = self.declaration()
                entry["deb"][field] = value
                before = snapshot(self.root)
                with self.assertRaises(ValueError):
                    self.prepare([entry], self.downloader({entry["deb"]["url"]: self.payloads["browser"]}))
                self.assertEqual(snapshot(self.root), before)
                self.assertFalse(list(self.cache.glob(".y4-frozen-*")))

    def test_real_package_name_version_and_architecture_must_match(self):
        for label in ("wrong-name", "wrong-version", "wrong-arch"):
            with self.subTest(label=label):
                entry = self.declaration(label=label)
                before = snapshot(self.root)
                with self.assertRaises(ValueError):
                    self.prepare([entry], self.downloader({entry["deb"]["url"]: self.payloads[label]}))
                self.assertEqual(snapshot(self.root), before)

    def test_cached_package_metadata_is_verified_even_with_matching_hash(self):
        entry = self.declaration(label="wrong-name")
        (self.cache / "matching-hash-wrong-name.deb").write_bytes(self.payloads["wrong-name"])
        before = snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "Package metadata"):
            self.prepare([entry], mock.Mock(side_effect=AssertionError("must not download")))
        self.assertEqual(snapshot(self.root), before)

    def test_architecture_all_is_supported(self):
        entry = self.declaration(label="all", arch="all")
        result = self.prepare([entry], self.downloader({entry["deb"]["url"]: self.payloads["all"]}))
        self.assertIn(entry["name"], result)

    def test_declared_arm64_does_not_silently_accept_all(self):
        entry = self.declaration(label="all", arch="arm64")
        with self.assertRaisesRegex(ValueError, "Architecture"):
            self.prepare([entry], self.downloader({entry["deb"]["url"]: self.payloads["all"]}))

    def test_non_debian_payload_is_rejected_even_when_digest_matches(self):
        entry = self.declaration()
        data = b"not a Debian archive"
        entry["deb"].update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        before = snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "invalid Debian archive"):
            self.prepare([entry], self.downloader({entry["deb"]["url"]: data}))
        self.assertEqual(snapshot(self.root), before)

    def test_partial_download_group_failure_publishes_nothing(self):
        first = self.declaration()
        second = self.declaration("common", "y4-test-common")
        before = snapshot(self.root)
        download = self.downloader({
            first["deb"]["url"]: self.payloads["browser"],
            second["deb"]["url"]: OSError("simulated unavailable mirror"),
        })
        with self.assertRaises(ValueError):
            self.prepare([first, second], download)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(list(self.cache.glob(".y4-frozen-*")))

    def test_fallback_mirror_must_provide_identical_frozen_bytes(self):
        entry = self.declaration()
        alternate = MIRROR + "browser.deb"
        entry["deb"]["urls"] = [alternate]
        result = self.prepare([entry], self.downloader({
            entry["deb"]["url"]: b"bad primary response", alternate: self.payloads["browser"]}))
        self.assertEqual(self.requests, [entry["deb"]["url"], alternate])
        self.assertEqual((self.root / result[entry["name"]].lstrip("/")).read_bytes(),
                         self.payloads["browser"])

    def test_invalid_primary_and_mirror_urls_fail_before_download(self):
        for url in ("http://example.test/pkg.deb", "file:///tmp/pkg.deb",
                    "https://user:password@example.test/pkg.deb", "https://example.test/pkg.deb#f"):
            for mirror in (False, True):
                with self.subTest(url=url, mirror=mirror):
                    entry = self.declaration()
                    if mirror:
                        entry["deb"]["urls"] = [url]
                    else:
                        entry["deb"]["url"] = url
                    before = snapshot(self.root)
                    download = mock.Mock(side_effect=AssertionError("must not download"))
                    with self.assertRaises(ValueError):
                        self.prepare([entry], download)
                    download.assert_not_called()
                    self.assertEqual(snapshot(self.root), before)

    def test_invalid_later_nonfrozen_entry_prevents_all_downloads(self):
        first = self.declaration()
        download = mock.Mock(side_effect=AssertionError("must not download"))
        before = snapshot(self.root)
        with self.assertRaises(ValueError):
            self.prepare([first, {"name": "--bad-option"}], download)
        download.assert_not_called()
        self.assertEqual(snapshot(self.root), before)

    def test_frozen_metadata_requires_fixed_version_size_digest_and_unique_name(self):
        variants = []
        for key, value in (("version", None), ("arch", "amd64")):
            entry = self.declaration()
            entry[key] = value
            variants.append([entry])
        for key, value in (("sha256", "not-a-hash"), ("bytes", True), ("bytes", 0)):
            entry = self.declaration()
            entry["deb"][key] = value
            variants.append([entry])
        entry = self.declaration()
        variants.append([entry, copy.deepcopy(entry)])
        for entries in variants:
            with self.subTest(entries=entries):
                download = mock.Mock(side_effect=AssertionError("must not download"))
                with self.assertRaises(ValueError):
                    self.prepare(entries, download)
                download.assert_not_called()

    def test_existing_conflicting_sha_filename_is_preserved(self):
        entry = self.declaration()
        collision = self.cache / ("y4-frozen-" + entry["deb"]["sha256"] + ".deb")
        collision.write_bytes(b"preserve even an invalid existing cache file")
        result = self.prepare([entry], self.downloader({entry["deb"]["url"]: self.payloads["browser"]}))
        self.assertEqual(collision.read_bytes(), b"preserve even an invalid existing cache file")
        self.assertNotEqual(result[entry["name"]], "/" + str(collision.relative_to(self.root)))

    def test_publication_failure_removes_only_this_calls_new_files(self):
        first = self.declaration()
        second = self.declaration("common", "y4-test-common")
        before = snapshot(self.root)
        real_link = os.link
        calls = []
        def fail_second(source, target):
            calls.append(str(target))
            if len(calls) == 2:
                raise OSError("simulated publication failure")
            return real_link(source, target)
        with mock.patch.object(frozen.os, "link", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "publication failure"):
                self.prepare([first, second], self.downloader({
                    first["deb"]["url"]: self.payloads["browser"],
                    second["deb"]["url"]: self.payloads["common"]}))
        self.assertEqual(snapshot(self.root), before)

    def test_cache_symlink_is_rejected_before_network_or_host_writes(self):
        original = self.cache.with_name("saved-archives")
        self.cache.rename(original)
        self.cache.symlink_to(original.name)
        download = mock.Mock(side_effect=AssertionError("must not download"))
        with self.assertRaises(ValueError):
            self.prepare([self.declaration()], download)
        download.assert_not_called()

    def test_no_frozen_entries_does_not_create_cache_or_download(self):
        absent = Path(self.temporary.name) / "unused-root"
        self.assertEqual(frozen.prepare_frozen_packages(absent, {"packages": [{"name": "ffmpeg"}]}), {})
        self.assertFalse(absent.exists())

    def test_https_redirect_cannot_downgrade_to_http(self):
        request = urllib.request.Request(PRIMARY + "browser.deb")
        with self.assertRaises(ValueError):
            frozen._HTTPSRedirect().redirect_request(
                request, None, 302, "Found", {}, "http://example.test/browser.deb")

    def test_downloader_rejects_oversize_response_without_installing(self):
        class Response(io.BytesIO):
            def geturl(self):
                return PRIMARY + "browser.deb"
        fake = mock.Mock()
        fake.open.return_value = Response(b"123456")
        destination = Path(self.temporary.name) / "too-large"
        with mock.patch.object(frozen.urllib.request, "build_opener", return_value=fake):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                frozen._download(PRIMARY + "browser.deb", destination, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
