#!/usr/bin/env python3
"""Offline APT manifest/source regression tests; no chroot or package install."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("apt_upgrade", Path(__file__).with_name("apt_upgrade.py"))
apt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apt)
KEY = b"test-only-pinned-key"
KEY_SHA = hashlib.sha256(KEY).hexdigest()


class Response(io.BytesIO):
    def geturl(self):
        return "https://example.test/gpg"


class AptManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write("usr/share/keyrings/debian-archive-keyring.gpg", b"test archive keyring")
        self.write("etc/apt/sources.list", "deb http://mirrors.ustc.edu.cn/debian bookworm main contrib non-free\n")

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mode & 0o777)
                for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()}

    def manifest(self, extras=True):
        return {
            "sources": [
                "https://deb.debian.org/debian bookworm main contrib non-free non-free-firmware",
                "https://security.debian.org/debian-security bookworm-security main contrib non-free non-free-firmware",
            ],
            "packages": [{"name": "chromium", "version": "156.0.8078.79-1~deb12u1"},
                         {"name": "docker-ce", "version": "5:27.3.1-1~debian.12~bookworm"},
                         {"name": "ffmpeg"}],
            "sources_extra": ([{
                "name": "docker-ce", "url": "https://example.test/docker",
                "suite": "bookworm", "components": ["stable"], "keyring_sha256": KEY_SHA
            }] if extras else [])
        }

    def configure(self, manifest=None):
        with patch.object(apt.urllib.request, "urlopen", side_effect=lambda *a, **k: Response(KEY)):
            return apt.install_manifest_packages(self.root, manifest or self.manifest())

    def test_primary_and_security_reach_final_sources(self):
        packages = self.configure()
        text = (self.root / "etc/apt/sources.list.d/y4-debian.list").read_text()
        self.assertIn("bookworm-security main contrib non-free non-free-firmware", text)
        self.assertIn("signed-by=/usr/share/keyrings/debian-archive-keyring.gpg", text)
        self.assertNotIn("mirrors.ustc", (self.root / "etc/apt/sources.list").read_text())
        self.assertEqual(packages, ["chromium=156.0.8078.79-1~deb12u1",
                                    "docker-ce=5:27.3.1-1~debian.12~bookworm", "ffmpeg"])

    def test_unknown_repositories_comments_and_source_entries_survive(self):
        original = "# local\n\ndeb https://other.test/debian bookworm main\n"
        self.write("etc/apt/sources.list.d/local.list", original)
        self.write("etc/apt/sources.list",
                   "deb-src https://deb.debian.org/debian bookworm main\n"
                   "deb http://mirrors.ustc.edu.cn/debian bookworm main\n")
        self.configure()
        self.assertEqual((self.root / "etc/apt/sources.list.d/local.list").read_text(), original)
        self.assertEqual((self.root / "etc/apt/sources.list").read_text(),
                         "deb-src https://deb.debian.org/debian bookworm main\n")

    def test_duplicate_extra_entry_removed_without_losing_other_sources(self):
        self.write("etc/apt/sources.list.d/old.list",
                   "deb [arch=arm64] https://example.test/docker/ bookworm stable\n"
                   "deb https://keep.test/repo stable main\n")
        self.configure()
        self.assertEqual((self.root / "etc/apt/sources.list.d/old.list").read_text(),
                         "deb https://keep.test/repo stable main\n")

    def test_deb822_preserves_unmanaged_stanzas(self):
        unmanaged = "Types: deb\nURIs: https://other.test/debian\nSuites: bookworm\nComponents: main"
        self.write("etc/apt/sources.list.d/base.sources",
                   "Types: deb\nURIs: https://deb.debian.org/debian\n"
                   "Suites: bookworm bookworm-updates\nComponents: main\n\n" + unmanaged + "\n")
        self.configure()
        actual = (self.root / "etc/apt/sources.list.d/base.sources").read_text()
        self.assertIn("Suites: bookworm-updates", actual)
        self.assertIn(unmanaged, actual)

    def test_unrelated_deb822_bytes_unchanged(self):
        text = "# keep whitespace\n\nTypes: deb\nURIs: https://other.test/archive\nSuites: stable\nComponents: main\n\n\n"
        path = self.write("etc/apt/sources.list.d/local.sources", text)
        self.configure()
        self.assertEqual(path.read_text(), text)

    def test_mixed_deb822_rejected_before_any_write(self):
        self.write("etc/apt/sources.list.d/base.sources",
                   "Types: deb deb-src\nURIs: https://deb.debian.org/debian\nSuites: bookworm\nComponents: main\n")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "mixed Deb822"):
            self.configure()
        self.assertEqual(self.snapshot(), before)

    def test_key_mismatch_leaves_all_sources_unchanged(self):
        before = self.snapshot()
        with patch.object(apt.urllib.request, "urlopen", return_value=Response(b"wrong key")):
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                apt.install_manifest_packages(self.root, self.manifest())
        self.assertEqual(self.snapshot(), before)

    def test_second_key_failure_does_not_publish_first_key(self):
        manifest = self.manifest()
        second = dict(manifest["sources_extra"][0], name="second", url="https://second.test/repo")
        manifest["sources_extra"].append(second)
        before = self.snapshot()
        with patch.object(apt.urllib.request, "urlopen", side_effect=[Response(KEY), Response(b"bad")]):
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                apt.install_manifest_packages(self.root, manifest)
        self.assertEqual(self.snapshot(), before)

    def test_cached_pinned_key_needs_no_network_and_repeat_is_idempotent(self):
        self.configure()
        before = self.snapshot()
        mtimes = {str(p): p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        with patch.object(apt.urllib.request, "urlopen", side_effect=AssertionError("network used")):
            apt.install_manifest_packages(self.root, self.manifest())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual({str(p): p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}, mtimes)

    def test_invalid_package_never_changes_sources(self):
        for package in ({"name": "--allow-unauthenticated"},
                        {"name": "ffmpeg;touch /tmp/unwanted"},
                        {"name": "chromium", "version": "1$(id)"},
                        {"name": "chromium", "version": "1\nbad"}):
            with self.subTest(package=package):
                manifest = self.manifest()
                manifest["packages"] = [package]
                before = self.snapshot()
                with patch.object(apt.urllib.request, "urlopen", side_effect=AssertionError("network used")):
                    with self.assertRaises(ValueError):
                        apt.install_manifest_packages(self.root, manifest)
                self.assertEqual(self.snapshot(), before)

    def test_unpinned_or_unsafe_extra_repository_rejected(self):
        for change in ({"keyring_sha256": None}, {"name": "../escape"},
                       {"keyring_url": "http://example.test/key"},
                       {"suite": "bookworm\ntrusted=yes"},
                       {"url": "https://user:password@example.test/repo"}):
            with self.subTest(change=change):
                manifest = self.manifest()
                manifest["sources_extra"][0].update(change)
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.configure(manifest)
                self.assertEqual(self.snapshot(), before)

    def test_duplicate_repo_rejected(self):
        manifest = self.manifest()
        manifest["sources"].append(manifest["sources"][0])
        with self.assertRaisesRegex(ValueError, "duplicate primary"):
            self.configure(manifest)
        manifest = self.manifest()
        manifest["sources_extra"].append(dict(manifest["sources_extra"][0], name="different-name"))
        with self.assertRaisesRegex(ValueError, "duplicate extra"):
            self.configure(manifest)

    def test_symlink_config_is_not_followed(self):
        outside = self.root / "outside"
        outside.write_text("untouched")
        target = self.root / "etc/apt/sources.list.d/y4-debian.list"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "unsafe staged"):
            self.configure()
        self.assertEqual(outside.read_text(), "untouched")
        self.assertTrue(target.is_symlink())

    def test_missing_archive_key_rejected_before_write(self):
        (self.root / "usr/share/keyrings/debian-archive-keyring.gpg").unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "archive keyring"):
            self.configure()
        self.assertEqual(self.snapshot(), before)

    def test_extra_key_and_sources_readable_by_apt(self):
        key = self.write("etc/apt/keyrings/docker-ce.asc", KEY)
        key.chmod(0o600)
        self.configure()
        for relative in ["etc/apt/keyrings/docker-ce.asc", "etc/apt/sources.list.d/docker-ce.list",
                         "etc/apt/sources.list.d/y4-debian.list"]:
            self.assertEqual((self.root / relative).stat().st_mode & 0o777, 0o644)

    def test_write_failure_restores_previous_files(self):
        before = self.snapshot()
        replace = os.replace
        count = 0

        def fail_second(src, dst):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated full disk")
            return replace(src, dst)

        with patch.object(apt.os, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "full disk"):
                self.configure()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.root.rglob(".y4-apt-*")))

    def test_legacy_manifest_without_sources_preserves_base(self):
        before = (self.root / "etc/apt/sources.list").read_bytes()
        self.configure({"packages": [{"name": "ffmpeg"}]})
        self.assertEqual((self.root / "etc/apt/sources.list").read_bytes(), before)


class AptCommandTests(unittest.TestCase):
    def exercise_main(self, audit_output=""):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in ("usr/bin", "etc", "proc", "sys", "dev"):
                (root / relative).mkdir(parents=True, exist_ok=True)
            calls = []

            def fake_run(argv, **kwargs):
                calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, stdout=audit_output if "--audit" in argv else "", stderr="")

            with patch.object(apt.sys, "argv", ["apt_upgrade.py", "--root", str(root)]), \
                    patch.object(apt.shutil, "copy2"), \
                    patch.object(apt, "run", side_effect=fake_run), \
                    patch.object(apt.subprocess, "run") as cleanup, \
                    contextlib.redirect_stdout(io.StringIO()):
                if audit_output:
                    with self.assertRaisesRegex(ValueError, "audit failed"):
                        apt.main()
                else:
                    apt.main()
                self.assertEqual(cleanup.call_count, 3)
            return calls

    def test_commands_pass_packages_as_argv_without_shell(self):
        calls = self.exercise_main()
        apt_calls = [(argv, kw) for argv, kw in calls if "/usr/bin/apt-get" in argv]
        self.assertEqual(len(apt_calls), 3)
        install = apt_calls[-1][0]
        self.assertEqual(install[-4:], ["install", "btop", "htop", "ffmpeg"])
        for argv, kw in apt_calls:
            self.assertNotIn("/bin/sh", argv)
            self.assertNotIn("shell", kw)
            self.assertEqual(kw["env"]["DEBIAN_FRONTEND"], "noninteractive")

    def test_nonempty_dpkg_audit_fails_and_cleans_mounts(self):
        self.exercise_main(" package is unpacked but not configured\n")

    def test_frozen_paths_replace_only_matching_package_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            fetch = Path(tmp) / "fetch"
            fetch.mkdir()
            for relative in ("usr/bin", "etc/apt", "proc", "sys", "dev"):
                (root / relative).mkdir(parents=True, exist_ok=True)
            original_sources = "deb https://keep.test/repo stable main\n"
            (root / "etc/apt/sources.list").write_text(original_sources)
            frozen_paths = {
                "chromium": "/var/cache/apt/archives/y4-frozen-browser.deb",
                "chromium-common": "/var/cache/apt/archives/y4-frozen-common.deb",
                "chromium-sandbox": "/var/cache/apt/archives/y4-frozen-sandbox.deb",
            }
            packages = [
                {"name": name, "version": "156.0.8078.79-1~deb12u1",
                 "deb": {"url": "https://security.debian.org/" + name + ".deb",
                         "sha256": "0" * 64, "bytes": 1}}
                for name in frozen_paths
            ] + [{"name": "ffmpeg"}]
            manifest = {"packages": packages}
            (fetch / "inputs-apt.json").write_text(json.dumps(manifest))
            events = []
            calls = []
            configure = apt.install_manifest_packages

            def prepare(actual_root, actual_manifest):
                self.assertEqual(actual_root, root)
                self.assertEqual(actual_manifest, manifest)
                self.assertEqual((root / "etc/apt/sources.list").read_text(), original_sources)
                events.append("prepare")
                return frozen_paths

            def configure_after_verify(actual_root, actual_manifest):
                self.assertEqual(events, ["prepare"])
                events.append("configure")
                return configure(actual_root, actual_manifest)

            def fake_run(argv, **kwargs):
                self.assertEqual(events[:2], ["prepare", "configure"])
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with patch.object(apt.sys, "argv", ["apt_upgrade.py", "--root", str(root),
                                               "--fetch", str(fetch)]), \
                    patch.object(apt, "prepare_frozen_packages", side_effect=prepare), \
                    patch.object(apt, "install_manifest_packages", side_effect=configure_after_verify), \
                    patch.object(apt.shutil, "copy2"), \
                    patch.object(apt, "run", side_effect=fake_run), \
                    patch.object(apt.subprocess, "run"), \
                    contextlib.redirect_stdout(io.StringIO()):
                apt.main()
            installs = [argv for argv in calls if "/usr/bin/apt-get" in argv and "install" in argv]
            self.assertEqual(len(installs), 2)
            self.assertEqual(installs[0][installs[0].index("install") + 1:], ["ffmpeg"])
            self.assertIn("--reinstall", installs[1])
            self.assertEqual(installs[1][installs[1].index("install") + 1:],
                             list(frozen_paths.values()))
            self.assertFalse(any(arg.startswith("chromium=") for argv in installs for arg in argv))
            upgrade_index = next(i for i, argv in enumerate(calls) if "upgrade" in argv)
            self.assertLess(upgrade_index, calls.index(installs[1]))

    def test_frozen_verification_failure_prevents_sources_mounts_and_apt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            fetch = Path(tmp) / "fetch"
            (root / "etc/apt").mkdir(parents=True)
            fetch.mkdir()
            sources = root / "etc/apt/sources.list"
            sources.write_text("original sources\n")
            (fetch / "inputs-apt.json").write_text(json.dumps({"packages": []}))
            with patch.object(apt.sys, "argv", ["apt_upgrade.py", "--root", str(root),
                                               "--fetch", str(fetch)]), \
                    patch.object(apt, "prepare_frozen_packages",
                                 side_effect=ValueError("frozen package SHA256 mismatch")), \
                    patch.object(apt, "install_manifest_packages") as configure, \
                    patch.object(apt.shutil, "copy2") as copy, \
                    patch.object(apt, "run") as run, \
                    patch.object(apt.subprocess, "run") as native:
                with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                    apt.main()
                configure.assert_not_called()
                copy.assert_not_called()
                run.assert_not_called()
                native.assert_not_called()
            self.assertEqual(sources.read_text(), "original sources\n")

    def test_temporary_resolver_preserves_guest_link_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            outer = Path(temp)
            root = outer / "root"
            etc = root / "etc"
            etc.mkdir(parents=True)
            target = outer / "outside"
            target.write_text("unchanged")
            guest = etc / "resolv.conf"
            guest.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "failed apt"):
                with apt._temporary_resolver(root):
                    self.assertEqual(guest.read_bytes(), Path("/etc/resolv.conf").read_bytes())
                    self.assertTrue(guest.resolve().is_relative_to(etc))
                    raise RuntimeError("failed apt")
            self.assertEqual(guest.readlink(), target)
            self.assertEqual(target.read_text(), "unchanged")
            self.assertEqual(list(etc.iterdir()), [guest])

    def test_host_root_refused_before_copy(self):
        with patch.object(apt.sys, "argv", ["apt_upgrade.py", "--root", "/"]), \
                patch.object(apt.shutil, "copy2") as copy:
            with self.assertRaisesRegex(ValueError, "host root"):
                apt.main()
            copy.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
