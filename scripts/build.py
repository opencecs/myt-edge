#!/usr/bin/env python3
"""Build and package Debian 12 for MYT Edge Y4 or Y12."""
import argparse
from contextlib import ExitStack
import datetime
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from board_config import board_config
import gpt
from release_package import create_full
from root_image_workspace import RootImageWorkspace
from spi_bundle import MAX_BUNDLE_SIZE, read_spi_bundle
import rootfs_host_policy
from graphics_config import configure_graphics, tree_digest, verify_graphics
from kernel_requirements import verify_kernel_compatibility

ROOT = Path(__file__).resolve().parents[1]
PATHS = ("scripts", "boot", "kernel", "mcu", "rootfs", "board", "build.sh", "LICENSE", "NOTICE")


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def item(path):
    return {"bytes": path.stat().st_size, "sha256": sha(path)}


def share_packaged_resources(root):
    """Hardlink identical static changelogs and Chromium index files in a private staging tree."""
    root = root.resolve(strict=True)
    patterns = ("usr/share/doc/*/changelog.gz", "usr/share/doc/*/changelog.Debian.gz",
                "usr/share/doc/*/NEWS.gz", "usr/share/doc/*/NEWS.Debian.gz",
                "usr/lib/chromium/locales/*.pak.info")
    linked = saved = 0
    for pattern in patterns:
        originals = {}
        for path in sorted(root.glob(pattern)):
            if path.parent.resolve() != path.parent:
                continue
            info = path.lstat()
            if (info.st_mode not in (stat.S_IFREG | 0o644, stat.S_IFREG | 0o444)
                    or info.st_uid or info.st_gid or info.st_nlink != 1
                    or info.st_size < 4096 or os.listxattr(path, follow_symlinks=False)):
                continue
            key = (info.st_dev, info.st_size, info.st_mode, info.st_mtime_ns, sha(path))
            if key not in originals:
                originals[key] = path
                continue
            # Keep the dpkg-owned path in place even if linking fails. Package
            # upgrades replace a pathname and therefore detach it from its peers.
            parent_info = path.parent.stat()
            pending = path.with_name(".myt-link-" + uuid.uuid4().hex)
            os.link(originals[key], pending)
            try:
                os.replace(pending, path)
            finally:
                pending.unlink(missing_ok=True)
            os.utime(path.parent, ns=(parent_info.st_atime_ns, parent_info.st_mtime_ns))
            linked += 1
            saved += info.st_size
    return {"linked_paths": linked, "shared_file_bytes": saved}


def verify_panel_binary(path, manifest):
    if manifest.get("schema") != 1 or manifest.get("arch") != "arm64":
        raise ValueError("Expected an ARM64 desktop panel input manifest")
    if not re.fullmatch(r"[0-9a-f]{40}", manifest.get("commit", "")):
        raise ValueError("Desktop panel manifest must name the full source commit")
    with path.open("rb") as stream:
        header = stream.read(20)
    if (len(header) != 20 or header[:6] != b"\x7fELF\x02\x01"
            or int.from_bytes(header[18:20], "little") != 183):
        raise ValueError("Desktop panel input is not an AArch64 ELF")
    actual = item(path)
    if any(actual[k] != manifest.get(k) for k in actual):
        raise ValueError("Desktop panel input checksum or size mismatch: " + str(path))
    return actual


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def assert_hardware_sources(repo, previous, current):
    """Reusing hardware outputs requires identical source/config Git objects."""
    if not re.fullmatch(r"[0-9a-f]{40}", previous):
        raise ValueError("Invalid previous hardware source commit")
    objects = {}
    def existing(ref, candidates):
        for path in candidates:
            value = subprocess.run(["git", "rev-parse", "--verify", ref + ":" + path], cwd=repo, capture_output=True, text=True)
            if not value.returncode:
                return path, value.stdout.strip()
        raise ValueError("Missing hardware source/config: " + str(candidates))
    groups = [[path] for path in ["mcu/firmware", "mcu/host", "mcu/include", "mcu/system", "kernel/linux"]]
    groups.append(["kernel/debian.config", "debian/kernel.config"])
    for candidates in groups:
        _, before = existing(previous, candidates)
        path, after = existing(current, candidates)
        if before != after:
            raise ValueError("Hardware reuse refused: source/config changed: " + path)
        objects[path] = after
    def patches(ref):
        def read(path):
            return subprocess.run(["git", "show", ref + ":" + path], cwd=repo, capture_output=True)
        value = read("kernel/patches.json")
        if not value.returncode:
            records = json.loads(value.stdout)["patches"]
            paths = [("kernel/" + row["path"], row["sha256"]) for row in records]
        else:
            base = json.loads(read("kernel/modules/inputs.json").stdout)
            network = json.loads(read("kernel/modules/network-inputs.json").stdout)
            extra = json.loads(read("kernel/modules/ix-hotplug-inputs.json").stdout)
            records = base["patches"] + [network["extra_patch"]] + extra["additional_patches"]
            paths = [(row["path"].removeprefix("../"), row["sha256"]) for row in records]
        result = []
        for path, expected in paths:
            data = read(path)
            if data.returncode or hashlib.sha256(data.stdout).hexdigest() != expected:
                raise ValueError("Hardware patch content mismatch: " + path)
            result.append(expected)
        return result
    if patches(previous) != patches(current):
        raise ValueError("Hardware reuse refused: ordered kernel patches changed")
    return objects


def grub_cfg_text(board_label, kernel_dir, dtb_name, slot_a_uuid, slot_b_uuid):
    """Zero-conditional dual-slot configuration.

    This board's grub 2.11 build evaluates if/test conditions unreliably,
    so slot selection is driven entirely by grubenv: load_env supplies
    default=<0|1> (numeric; a title string is rejected) plus the per-slot
    kernel directory boot_kernel_a/boot_kernel_b, each naming a delivery
    version under /kernels/ (kernel-ota-design 4.1). grub.cfg is
    version-free and is never rewritten by updates. The set lines before
    load_env are only the fallback for a grubenv that is missing,
    unreadable, or rewritten by an older agent without the per-slot keys:
    grub then boots the first entry, slot A, from the shipped directory.
    grubenv files carry the exact grub-editenv block format and must never
    be edited in place."""
    def entry(title, kernel_var, root_uuid):
        return ("menuentry '" + title + "' {\n"
                "    devicetree /kernels/${" + kernel_var + "}/" + dtb_name + "\n"
                "    linux /kernels/${" + kernel_var + "}/Image console=tty0 console=ttyAMA2,57600 "
                "earlycon=pl011,0x040d0000 loglevel=5 acpi=off efi=noruntime cpuidle.off=1 "
                "nvme_core.default_ps_max_latency_us=0 pcie_aspm.policy=performance pcie_port_pm=off "
                "root=PARTUUID=" + str(root_uuid) + " rootwait rw\n}\n")
    return ("set timeout=3\n"
            "set boot_kernel_a=" + kernel_dir + "\n"
            "set boot_kernel_b=" + kernel_dir + "\n"
            "load_env -f ($root)/grub/grubenv\n"
            + entry("A", "boot_kernel_a", slot_a_uuid)
            + entry("B", "boot_kernel_b", slot_b_uuid))


def bundle_kernel_release(bundle, sums):
    """Derive the kernel release from a bundle's SHA256SUMS file set.

    The kernel release comes from the bundle, never from this script: a
    bundle carries kernel.release (listed in its SHA256SUMS) and every
    artifact name repeats it, so the module tree, /boot staging and grub
    boot_kernel cannot diverge from the bundle (kernel-ota-design 4.4
    build gate). Bundles produced before the file existed fall back to the
    single modules-<release>.tar.zst name in SHA256SUMS. Returns the
    release string or raises ValueError."""
    release_file = bundle / "kernel.release"
    if "kernel.release" in sums:
        version = release_file.read_text().strip()
        source = "kernel.release"
    elif release_file.exists():
        raise ValueError("bundle kernel.release is not covered by SHA256SUMS")
    else:
        derived = sorted(name[len("modules-"):-len(".tar.zst")] for name in sums
                         if name.startswith("modules-") and name.endswith(".tar.zst"))
        if len(derived) != 1:
            raise ValueError("bundle lacks kernel.release and a single modules-<release>.tar.zst")
        version = derived[0]
        source = "SHA256SUMS"
    if not version or "/" in version or version in (".", ".."):
        raise ValueError("Bundle kernel.release is not a safe directory name")
    return version, source


BOARD_PROFILES = {
    "Y4": ("Y4-6187", "Y4-6187-R1P", "sky1-y4.dtb"),
    "Y12": ("Y12-6187", "Y12-6187", "sky1-y12.dtb"),
}


def resolve_board(config, requested=None):
    if config.get("schema") != 1:
        raise ValueError("Expected build configuration schema 1")
    configured = config.get("hardware")
    label = requested or next(
        (name for name, profile in BOARD_PROFILES.items() if profile[0] == configured),
        None,
    )
    if label not in BOARD_PROFILES:
        raise ValueError("Select Y4 or Y12 with --board or the legacy hardware field")
    hardware, uefi_board, dtb_name = BOARD_PROFILES[label]
    if configured is not None and configured != hardware:
        raise ValueError(f"{label} conflicts with configured hardware {configured!r}")
    if "uefi_board" in config and config["uefi_board"] != uefi_board:
        raise ValueError(f"{label} requires uefi_board={uefi_board}")
    return label, hardware, uefi_board, dtb_name


def board_selection(label, hardware, uefi_board, dtb_name):
    return {
        "board": label,
        "hardware": hardware,
        "uefi_project": uefi_board.removeprefix("Y4-"),
        "dtb": dtb_name,
        "mcu_binary": f"{label.lower()}-mcu.bin",
        "boardd_device": label,
        "network_interfaces": [label.lower() + role for role in ("lan", "sfp1", "sfp2")],
    }


def verify_dtb_board(path, board):
    result = subprocess.run(["fdtget", "-t", "s", str(path), "/", "compatible"],
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"Cannot read {board} DTB compatible from {path}: {result.stderr.strip()}")
    compatible = result.stdout.split()
    expected = f"myt,{board.lower()}"
    if not compatible or compatible[0] != expected:
        raise ValueError(f"DTB primary compatible does not match {board}: {path}")

    if board == "Y12":
        # The Sky1 host driver rejects the fixed fabric if max-payload
        # is missing or differs from sky1,fixed-mps.
        switch_root = "/soc/pcie@0a010000"
        for prop in ("sky1,fixed-mps", "max-payload"):
            result = subprocess.run(["fdtget", "-t", "u", str(path), switch_root, prop],
                                    capture_output=True, text=True)
            if result.returncode or result.stdout.strip() != "256":
                raise ValueError(f"Y12 IX8024 DTB requires {switch_root} {prop} = 256: {path}")
        ports = subprocess.run(
            ["fdtget", "-t", "u", str(path), switch_root, "myt,slot-downstream-ports"],
            capture_output=True, text=True,
        )
        if ports.returncode or sorted(int(port) for port in ports.stdout.split()) != list(range(12)):
            raise ValueError(f"Y12 IX8024 DTB requires twelve distinct downstream ports: {path}")


class Builder:
    def __init__(self, args):
        self.args = args
        self.config = board_config(json.loads(args.config.read_text()), args.board)
        self.board_label, hardware, self.uefi_board, self.dtb_name = resolve_board(
            self.config, getattr(args, "board", None))
        self.config["hardware"] = hardware
        inputs = self.config.get("inputs", {})
        self.mcu_assets = args.mcu_assets or (Path(inputs["mcu_assets"]) if "mcu_assets" in inputs else None)
        self.pg_enabled = bool(inputs.get("realtek_pg"))
        if self.mcu_assets and (args.reuse_components or args.repackage_from):
            raise ValueError("Use a fresh component build with a prebuilt MCU bundle")
        self.kernel_bundle = args.kernel_bundle.absolute() if args.kernel_bundle else None
        self.pg_from = args.pg_from.absolute() if args.pg_from else None
        # The 6.6 prebuilt kernel is the default product kernel: the config
        # names the canonical bundle directory and the previous 6.1 build
        # whose objects compile the out-of-tree PG module. A source 6.1
        # build remains available explicitly via --kernel-source-build.
        if not self.kernel_bundle and not args.kernel_source_build:
            configured = self.config.get("inputs", {}).get("kernel_bundle")
            if configured:
                self.kernel_bundle = Path(configured).expanduser().resolve(strict=True)
        if self.pg_enabled and not self.pg_from:
            configured = self.config.get("inputs", {}).get("kernel_pg_reference")
            if configured:
                self.pg_from = Path(configured).expanduser().resolve(strict=True)
        self.repackage_from = args.repackage_from.absolute() if args.repackage_from else None
        if self.repackage_from:
            if self.board_label != "Y4":
                raise ValueError("--repackage-from currently requires a Y4 reference image")
            if not self.kernel_bundle:
                raise ValueError("--repackage-from requires --kernel-bundle")
            if self.args.reuse_components or self.args.reuse_rootfs:
                raise ValueError("--repackage-from is mutually exclusive with reuse flags")
            prev = self.repackage_from
            if not (prev / "BUILD.json").is_file() or not (prev / "filesystem").is_dir() \
                    or not (prev / "source/mcu/firmware/build/y4-mcu.bin").is_file():
                raise ValueError("--repackage-from lacks a complete previous build layout: " + str(prev))
            old = json.loads((prev / "BUILD.json").read_text())
            if old.get("hardware") != hardware:
                raise ValueError("--repackage-from target hardware mismatch")
            if not old.get("complete"):
                raise ValueError("--repackage-from previous build is not complete")
        if self.kernel_bundle:
            if self.args.reuse_components:
                raise ValueError("--kernel-bundle and --reuse-components are mutually exclusive")
            if self.pg_enabled and not self.repackage_from and not self.pg_from:
                raise ValueError("--kernel-bundle requires --pg-from: the previous build whose 6.1 kernel "
                                 "objects compile the out-of-tree PG module (pgdrv stays on the 6.1 vermagic; "
                                 "the 6.6 kernel carries r8125/r8127 in-tree)")
            if self.pg_from:
                pg_objects = self.pg_from / "kernel/objects"
                pg_source = self.pg_from / "source/kernel/linux"
                if not (pg_objects / "include/config/kernel.release").is_file() \
                        or not (pg_objects / "Module.symvers").is_file() \
                        or not (pg_source / "Makefile").is_file():
                    raise ValueError("--pg-from lacks a complete 6.1 kernel objects/source layout: " + str(self.pg_from))
        self.out = args.output.absolute()
        self.commit = subprocess.check_output(["git", "rev-parse", args.ref + "^{commit}"], cwd=ROOT, text=True).strip()
        # The delivery version is the board-facing version axis: it is stamped
        # into /etc/y4-release.json, used as the boot FAT kernel directory name
        # and carried by OTA manifests (kernel-ota-design section 3). It is
        # computed exactly once per build so a midnight rollover cannot split
        # one delivery into two versions.
        commit_count = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                                      cwd=ROOT, capture_output=True, text=True,
                                      check=True).stdout.strip()
        self.delivery_version = args.version or datetime.datetime.now().strftime("%Y%m%d") + "-r" + commit_count
        if not re.fullmatch(r"[0-9]{8}-r[0-9]+", self.delivery_version):
            raise ValueError("Expected a YYYYMMDD-rNN build version")
        self.report = {"schema": 1, "profile": "debian", "source_commit": self.commit,
                       "board": self.board_label, "hardware": hardware,
                       "config_sha256": sha(args.config), "jobs": args.jobs,
                       "started_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
                       "device_access": False, "board_verified": False, "complete": False,
                       "commands": [], "components": {}, "inputs": {}}
        self.panel_input = None
        self.report_lock = threading.Lock()
        self.env = dict(os.environ, Y4_BUILD_JOBS=str(args.jobs))

    def input(self, name):
        path = Path(self.config["inputs"][name]).expanduser().resolve(strict=True)
        return path

    def record(self):
        with self.report_lock:
            write_json(self.out / "BUILD.json", self.report)

    def run(self, name, argv, cwd=None, env=None, accepted=(0,)):
        print(name, flush=True)
        start = time.monotonic()
        with (self.out / "logs" / (name + ".log")).open("wb") as log:
            proc = subprocess.run(list(map(str, argv)), cwd=cwd or self.source,
                                  env=env or self.env, stdout=log, stderr=subprocess.STDOUT)
        with self.report_lock:
            self.report["commands"].append({"stage": name, "argv": list(map(str, argv)),
                                             "exit": proc.returncode, "seconds": time.monotonic() - start})
        self.record()
        if proc.returncode not in accepted:
            raise RuntimeError("Failed " + name + "; original log retained")

    def preflight(self):
        if self.args.spi_bin:
            path = self.args.spi_bin
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BUNDLE_SIZE:
                raise ValueError("Expected one regular, board-specific SPI BIN")
            read_spi_bundle(path.read_bytes(), self.board_label)
            self.report["inputs"]["spi_bin"] = item(path)
        required = ["board/build.py"]
        if self.board_label == "Y12":
            required.append("mcu/include/y12_power_wire.h")
        for path in required:
            present = subprocess.run(
                ["git", "cat-file", "-e", self.commit + ":" + path],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if present.returncode:
                raise ValueError(f"{self.board_label} source ref lacks {path}: {self.commit}")
        if self.board_label == "Y12" and self.args.component == "all":
            # Check the selected commit's release policy before starting any build.
            policy = "rootfs/fetch/inputs-kernel-y12.json"
            present = subprocess.run(
                ["git", "cat-file", "-e", self.commit + ":" + policy],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if present.returncode:
                raise ValueError("Y12 full build requires a validated " + policy)
        if not 1 <= self.args.jobs <= 32:
            raise ValueError("jobs must be 1..32")
        if self.kernel_bundle:
            # Reject a wrong-board bundle before reading the large Debian base
            # archive or starting any component build.
            verify_dtb_board(self.kernel_bundle / self.dtb_name, self.board_label)
        elif self.board_label == "Y12" and not self.args.reuse_components:
            raise ValueError("Y12 requires the 6.6 kernel bundle; the legacy source build is 6.1")
        if "root_size_mib" in self.config:
            self.report["legacy_root_size_mib_ignored"] = self.config["root_size_mib"]
        self.report["root_image_size_policy"] = "adaptive-content-preserving"
        for name in ["tar", "zstd", "make", "gcc", "patch", "depmod", "mke2fs", "e2fsck",
                     "img2simg", "simg2img", "mkfs.vfat", "fsck.fat", "mcopy", "mmd", "grub-file",
                     "grub-script-check", "aarch64-linux-gnu-gcc",
                      "qemu-aarch64-static", "debootstrap", "unshare", "dpkg-deb", "pigz", "mount", "umount", "fdtget"]:
            if not shutil.which(name):
                raise ValueError("Missing host tool: " + name)
        if self.args.panel_manifest:
            manifest = json.loads(self.args.panel_manifest.read_text())
        else:
            committed_panel = subprocess.run(["git", "show", self.commit + ":rootfs/fetch/panel.json"],
                                             cwd=ROOT, capture_output=True, text=True)
            manifest = json.loads(committed_panel.stdout) if committed_panel.returncode == 0 else None
        if self.args.panel_binary and not manifest:
            raise ValueError("An explicit panel binary requires a manifest")
        if manifest:
            self.panel_input = self.args.panel_binary or self.input("panel_binary")
            verify_panel_binary(self.panel_input, manifest)
            self.report["inputs"]["panel_binary"] = dict(manifest, path=str(self.panel_input))
            if self.repackage_from:
                verify_panel_binary(self.repackage_from / "filesystem/usr/bin/myt_desktop", manifest)
        def committed(path):
            return subprocess.check_output(["git", "show", self.commit + ":" + path], cwd=ROOT)
        recipe = json.loads(committed("rootfs/base.json"))
        lock_path = "rootfs/fetch/rootfs-packages.json"
        if recipe.get("kind") != "debootstrap" or recipe.get("manifest") != "fetch/rootfs-packages.json":
            raise ValueError("The selected source must declare the shared Debian bootstrap manifest")
        raw = committed(lock_path)
        lock = json.loads(raw)
        checksum = hashlib.sha256(raw).hexdigest()
        if recipe["sha256"] != checksum or lock["architecture"] != "arm64" or lock["suite"] != "bookworm":
            raise ValueError("Unexpected Debian bootstrap manifest")
        self.report["inputs"]["debian_base"] = dict(kind="debootstrap", manifest=lock_path, sha256=checksum,
                                                    packages=len(lock["common_packages"]))
        if self.repackage_from:
            previous = json.loads((self.repackage_from / "BUILD.json").read_text())
            if previous.get("inputs", {}).get("debian_base", {}).get("sha256") != checksum:
                raise ValueError("Previous filesystem was not built from the selected Debian manifest")
        for name in ("dependencies", "kernel_config", "windows_tools"):
            self.input(name)
        if self.pg_enabled:
            self.input("realtek_pg")
        if self.mcu_assets:
            manifest_path = self.mcu_assets / "manifest.json"
            expected = self.args.mcu_manifest_sha256 or self.config["inputs"].get("mcu_manifest_sha256")
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("Pin the MCU manifest with --mcu-manifest-sha256")
            if manifest_path.is_symlink() or not manifest_path.is_file() or sha(manifest_path) != expected:
                raise ValueError("MCU asset manifest differs from its pinned SHA256")
            self.report["inputs"]["mcu_assets"] = item(manifest_path)
        else:
            self.input("mcu_vendor")
            if not shutil.which("arm-none-eabi-gcc"):
                raise ValueError("Missing host tool: arm-none-eabi-gcc")
            signing = self.config.get("board", {}).get("signing", {})
            for field in ["private_key", "public_key", "sequence"]:
                if field not in signing:
                    raise ValueError("Configure board.signing." + field + " for the signed Debian MCU package")
            key = Path(signing["private_key"])
            public = Path(signing["public_key"])
            if key.is_symlink() or not key.is_file() or key.stat().st_mode & 0o077:
                raise ValueError("MCU signing key must be a protected regular external file")
            if public.is_symlink() or not public.is_file() or public.stat().st_size != 32:
                raise ValueError("MCU trust key must be a raw32-byte regular public key")
            if isinstance(signing["sequence"], bool) or not isinstance(signing["sequence"], int) or not 1 <= signing["sequence"] < 2**64:
                raise ValueError("MCU package sequence must be a positive uint64")
        deps = self.input("dependencies")
        for name, path in {"kernel_compiler": deps / "kernel-toolchain-12.3/bin/aarch64-none-linux-gnu-gcc",
                           "kernel_config": self.input("kernel_config")}.items():
            self.report["inputs"][name] = dict(path=str(path), **item(path))
        if ROOT == self.out or ROOT in self.out.resolve().parents or self.out.resolve() in ROOT.parents:
            raise ValueError("Build output must be outside the source checkout")
        return self.report

    def snapshot(self):
        if self.out.exists() or self.out.is_symlink():
            raise ValueError("A fresh output directory is required")
        self.out.mkdir(parents=True)
        self.started = True
        (self.out / "logs").mkdir()
        self.source = self.out / "source"
        self.source.mkdir()
        self.record()
        paths = list(PATHS)
        if self.args.reuse_components:
            previous = self.args.reuse_components.resolve(strict=True)
            old = json.loads((previous / "BUILD.json").read_text())
            proof = assert_hardware_sources(ROOT, old["source_commit"], self.commit)
            prepared = (previous / "source/kernel/linux").resolve(strict=True)
            children = subprocess.check_output(["git", "ls-tree", "--name-only", self.commit + ":kernel"],
                                                cwd=ROOT, text=True).splitlines()
            paths.remove("kernel")
            paths.extend("kernel/" + name for name in children if name != "linux")
            self.report["source_snapshot"] = dict(kernel_source="verified-build-reference",
                    kernel_source_directory=str(prepared), kernel_git_tree=proof["kernel/linux"],
                    original_build=str(previous), archived_paths=paths)
        else:
            self.report["source_snapshot"] = dict(kernel_source="full-source-export", archived_paths=paths)
        with (self.out / "logs/source.log").open("wb") as log:
            proc = subprocess.Popen(["git", "-c", "tar.umask=0022", "archive", self.commit, *paths], cwd=ROOT, stdout=subprocess.PIPE, stderr=log)
            extract = subprocess.run(["tar", "-x", "-C", self.source], stdin=proc.stdout, stderr=log)
            proc.stdout.close()
            if proc.wait() or extract.returncode:
                raise RuntimeError("Source snapshot failed")
        common = subprocess.check_output(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ROOT, text=True).strip()
        subprocess.run(["git", "init", "-q", self.source], check=True)
        (self.source / ".git/objects/info/alternates").write_text(str(Path(common) / "objects") + "\n")
        subprocess.run(["git", "update-ref", "--no-deref", "HEAD", self.commit], cwd=self.source, check=True)

    def verify_mcu_source(self):
        """Check the selected snapshot against the MCU source used for releases."""
        repository = self.input("mcu_vendor").parent
        command = ["git", "-C", str(repository)]
        top = Path(subprocess.check_output(command + ["rev-parse", "--show-toplevel"], text=True).strip())
        if top != repository:
            raise ValueError("MCU vendor input must belong to the MCU source checkout")
        revision = subprocess.check_output(command + ["rev-parse", "refs/heads/main"], text=True).strip()
        for folder in ("firmware", "include"):
            expected = set(subprocess.check_output(
                command + ["ls-tree", "-r", revision + ":" + folder]).splitlines())
            actual = set(subprocess.check_output(
                ["git", "-C", str(ROOT), "ls-tree", "-r", self.commit + ":mcu/" + folder]).splitlines())
            if not expected or not expected <= actual:
                raise ValueError(f"Selected build has stale MCU {folder} sources versus {revision}; "
                                 "sync the MCU sources before building")
        return revision

    def mcu(self):
        if self.mcu_assets:
            spec = importlib.util.spec_from_file_location("mcu_assets", self.source / "board/mcu_assets.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            digest = self.report["inputs"]["mcu_assets"]["sha256"]
            files = module.load_assets(self.mcu_assets, self.board_label, digest)
            base = self.source / "mcu"
            for name, data in files.items():
                target = base / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(data)
                if name.startswith("host/"):
                    target.chmod(0o755)
            manifest = (self.mcu_assets / "manifest.json").read_bytes()
            if hashlib.sha256(manifest).hexdigest() != digest:
                raise ValueError("MCU manifest changed during staging")
            (base / "manifest.json").write_bytes(manifest)
            meta = json.loads(files["mcu-builtin.json"])
            firmware = base / "firmware/build" / (self.board_label.lower() + "-mcu.bin")
            self.report["components"]["mcu"] = dict(board=self.board_label, version=meta["version"],
                                                    mode="signed-assets", **item(firmware))
            self.record()
            return
        source_revision = self.verify_mcu_source()
        mcu = self.source / "mcu"
        (mcu / "vendor").symlink_to(self.input("mcu_vendor"), target_is_directory=True)
        if self.board_label == "Y12":
            header = (mcu / "include/y12_power_wire.h").read_text()
            version = ".".join(re.search(r"^#define Y12_FW_" + part + r" ([0-9]+)$", header, re.M).group(1)
                               for part in ("MAJOR", "MINOR", "PATCH"))
            self.run("mcu-y12-slot-power-test", ["make", "test-y12-slot-power"], mcu / "firmware")
            self.run("mcu", ["make", "y12-image"], mcu / "firmware")
            self.report["components"]["mcu"] = dict(
                board="Y12", version=version, mode="source-build", source_revision=source_revision,
                **item(mcu / "firmware/build/y12-mcu.bin"))
            self.record()
            return
        header = (mcu / "include/protocol.h").read_text()
        version = ".".join(re.search(r"^#define Y4_FW_" + name + r" ([0-9]+)$", header, re.M).group(1)
                           for name in ["MAJOR", "MINOR", "PATCH"])
        self.run("mcu", ["make", "-j" + str(self.args.jobs), "all", "test-lcd-spi-dma",
                         "test-fan-backend", "test-slot-state",
                         "EXTRA_CFLAGS=-DY4_LCD_HARDWARE_SPI=1 -DY4_FAN_HARDWARE_PWM=0"], mcu / "firmware")
        sysroot = self.input("dependencies") / "cix-sysroot"
        flags = "-Os -std=c11 -Wall -Wextra -Werror -Wno-misleading-indentation -Wno-format-truncation --sysroot=" + str(sysroot)
        flags += " -isystem " + str(sysroot / "usr/include/aarch64-linux-gnu") + " -isystem " + str(sysroot / "usr/include")
        self.run("mcu-host", ["make", "-j" + str(self.args.jobs), "all", "CC=aarch64-linux-gnu-gcc", "CFLAGS=" + flags], mcu / "host")
        for name in ["mcud", "mcuctl", "hexverify", "y4-mcu-reset", "y4-mcu-flash"]:
            data = (mcu / "host" / name).read_bytes()
            if data[:6] != b"\x7fELF\x02\x01" or int.from_bytes(data[18:20], "little") != 183:
                raise ValueError("Invalid ARM64 MCU host program: " + name)
            versions = re.findall(rb"GLIBC_([0-9]+)\.([0-9]+)", data)
            if any(tuple(map(int, pair)) > (2, 36) for pair in versions):
                raise ValueError("MCU host ABI exceeds Debian 12")
        self.report["components"]["mcu"] = dict(version=version, mode="source-build", source_revision=source_revision,
                                                  **item(mcu / "firmware/build/y4-mcu.bin"))
        self.record()

    def verify_kernel_rootfs(self, image=None, staged_root=None):
        # A kernel-only repackage retains the previous rootfs, so use that
        # rootfs's source policy, not the current packaging checkout's pin.
        if self.repackage_from:
            rootfs_source = self.repackage_from / "source/rootfs"
            if staged_root is None:
                staged_root = self.repackage_from / "filesystem"
        else:
            rootfs_source = self.source / "rootfs"
        bundle = getattr(self, "kernel_bundle", None)
        dtb = modules_archive = None
        if image is not None:
            image = Path(image)
            # Check the DTB actually paired with this Image, including staged
            # objects and reused components; never substitute the bundle DTB.
            dtb_dir = image.parent if bundle and image.parent == bundle else image.parent / "dts/cix"
            dtb = dtb_dir / self.dtb_name
        if bundle:
            archives = list(bundle.glob("modules-*.tar.zst"))
            if len(archives) == 1:
                modules_archive = archives[0]
        result = verify_kernel_compatibility(rootfs_source, image, staged_root,
                                             dtb=dtb, modules_archive=modules_archive,
                                             board=self.board_label)
        if result is not None:
            result["validator_sha256"] = sha(Path(verify_kernel_compatibility.__code__.co_filename))
            self.report["inputs"]["kernel_rootfs_compatibility"] = result
        return result

    def kernel(self):
        if self.kernel_bundle:
            return self.kernel_from_bundle()
        kernel = self.source / "kernel/linux"
        self.kernel_source = kernel
        manifest = json.loads((self.source / "kernel/patches.json").read_text())
        applied = []
        for index, patch in enumerate(manifest["patches"]):
            path = (self.source / "kernel" / patch["path"]).resolve()
            if not path.is_relative_to(self.source / "kernel/patches") or sha(path) != patch["sha256"]:
                raise ValueError("Kernel patch input mismatch")
            data = path.read_bytes()
            probe = subprocess.run(["patch", "--batch", "--forward", "--fuzz=0", "-p1", "--dry-run"], input=data, cwd=kernel, capture_output=True)
            if probe.returncode:
                reverse = subprocess.run(["patch", "--batch", "--reverse", "--fuzz=0", "-p1", "--dry-run"], input=data, cwd=kernel, capture_output=True)
                if reverse.returncode:
                    (self.out / "logs" / ("patch-%d.log" % index)).write_bytes(probe.stdout + probe.stderr + reverse.stdout + reverse.stderr)
                    raise ValueError("Patch neither applies exactly nor is already present: " + str(path))
                applied.append(dict(patch, state="already-present"))
            else:
                self.run("patch-%d" % index, ["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i", path], kernel)
                applied.append(dict(patch, state="applied"))
        self.kernel_objects = self.out / "kernel/objects"
        self.kernel_objects.mkdir(parents=True)
        (kernel / ".scmversion").write_text("")
        self.run("kernel-config-merge", ["bash", kernel / "scripts/kconfig/merge_config.sh", "-m", "-O", self.kernel_objects,
                                        self.input("kernel_config"), kernel / "arch/arm64/configs/cix.config",
                                        self.source / "kernel/debian.config"], kernel)
        prefix = self.input("dependencies") / "kernel-toolchain-12.3/bin/aarch64-none-linux-gnu-"
        self.kernel_command = ["make", "-C", kernel, "O=" + str(self.kernel_objects), "ARCH=arm64",
                               "CROSS_COMPILE=" + str(prefix), "LOCALVERSION=-generic"]
        self.env.update(KBUILD_BUILD_USER="y4-debian", KBUILD_BUILD_HOST="y4-build", KBUILD_BUILD_VERSION="1")
        if shutil.which("ccache"):
            self.env.update(CCACHE_DIR=str(ROOT.parent / "inputs/ccache"), CCACHE_MAXSIZE="40G", CCACHE_BASEDIR=str(self.out))
            self.kernel_command += ["CC=ccache " + str(prefix) + "gcc", "HOSTCC=ccache gcc"]
        self.run("kernel-olddefconfig", self.kernel_command + ["olddefconfig"])
        config = (self.kernel_objects / ".config").read_text().splitlines()
        for line in (self.source / "kernel/debian.config").read_text().splitlines():
            if not line.startswith("CONFIG_"):
                continue
            if line.endswith("=n"):
                # kconfig records a disabled option as "# CONFIG_X is not
                # set"; failure means the option is still y or m.
                name = line[len("CONFIG_"):-len("=n")]
                if "CONFIG_" + name + "=y" in config or "CONFIG_" + name + "=m" in config:
                    raise ValueError("Required Debian kernel option missing: " + line)
            elif line not in config:
                raise ValueError("Required Debian kernel option missing: " + line)
        self.run("kernel", self.kernel_command + ["-j" + str(self.args.jobs), "Image", "modules", "dtbs"])
        image = self.kernel_objects / "arch/arm64/boot/Image"
        dtb = self.kernel_objects / ("arch/arm64/boot/dts/cix/" + self.dtb_name)
        if image.read_bytes()[56:60] != b"ARMd" or dtb.read_bytes()[:4] != b"\xd0\x0d\xfe\xed":
            raise ValueError("Invalid ARM64 kernel/DTB")
        verify_dtb_board(dtb, self.board_label)
        self.kernel_version = (self.kernel_objects / "include/config/kernel.release").read_text().strip()
        self.report["components"]["kernel"] = dict(mode="source-build", version=self.kernel_version,
                image=item(image), dtb=item(dtb), config=item(self.kernel_objects / ".config"), patches=applied,
                source_tree=subprocess.check_output(["git", "rev-parse", self.commit + ":kernel/linux"], cwd=ROOT, text=True).strip())
        self.record()

    def kernel_from_bundle(self):
        """Adopt a verified prebuilt kernel bundle (the 6.6.89 handoff).

        The bundle artifacts are staged into the canonical kernel objects
        layout so every later stage (board inputs, /boot staging, ESP
        assembly, the grub boot_kernel default) keeps working unchanged.
        Module installation and the proprietary GPU/VPU out-of-tree builds
        are handled by their bundle branches in filesystem(); the 6.6
        kernel carries Mali KMD, MGM/PMA, amvx and r8127 built in, and the
        remaining modules arrive from the bundle tarball."""
        bundle = self.kernel_bundle
        sums = {}
        for line in (bundle / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split(None, 1)
            name = name.strip()
            if name in sums or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"Invalid or duplicate kernel bundle checksum: {name}")
            sums[name] = digest
        self.kernel_version, release_source = bundle_kernel_release(bundle, sums)
        self.report["inputs"]["kernel_bundle_release_source"] = release_source
        expected = {"Image", self.dtb_name,
                    "modules-" + self.kernel_version + ".tar.zst",
                    "System.map-" + self.kernel_version,
                    "config-" + self.kernel_version}
        if "kernel.release" in sums:
            expected.add("kernel.release")
        if set(sums) != expected:
            raise ValueError("Kernel bundle SHA256SUMS does not cover the expected artifacts: " + str(sorted(sums)))
        for name in sorted(sums):
            path = bundle / name
            if not path.is_file():
                raise ValueError("Kernel bundle artifact missing: " + name)
            actual = sha(path)
            if actual != sums[name]:
                raise ValueError("Kernel bundle artifact checksum mismatch: " + name)
            self.report["inputs"]["kernel_bundle_" + name] = dict(
                path=str(path), bytes=path.stat().st_size, sha256=actual)
        verify_dtb_board(bundle / self.dtb_name, self.board_label)
        if self.args.component != "components":
            self.verify_kernel_rootfs(bundle / "Image")
        self.kernel_objects = self.out / "kernel/objects"
        image_dir = self.kernel_objects / "arch/arm64/boot"
        dtb_dir = image_dir / "dts/cix"
        release_dir = self.kernel_objects / "include/config"
        for directory in (image_dir, dtb_dir, release_dir):
            directory.mkdir(parents=True)
        shutil.copy2(bundle / "Image", image_dir / "Image")
        shutil.copy2(bundle / self.dtb_name, dtb_dir / self.dtb_name)
        shutil.copy2(bundle / ("config-" + self.kernel_version), self.kernel_objects / ".config")
        shutil.copy2(bundle / ("System.map-" + self.kernel_version), self.kernel_objects / "System.map")
        image = image_dir / "Image"
        dtb = dtb_dir / self.dtb_name
        if image.read_bytes()[56:60] != b"ARMd" or dtb.read_bytes()[:4] != b"\xd0\x0d\xfe\xed":
            raise ValueError("Invalid ARM64 kernel/DTB in bundle")
        release_dir.joinpath("kernel.release").write_text(self.kernel_version + "\n")
        self.kernel_source = None
        self.kernel_command = None
        self.report["components"]["kernel"] = dict(
            mode="prebuilt-bundle", version=self.kernel_version, bundle=str(bundle),
            handoff_doc=item(bundle / "RELEASE.md"), image=item(image), dtb=item(dtb),
            config=item(self.kernel_objects / ".config"))
        self.record()

    def splice_bundle_kernel(self):
        """Replace a staged tree's kernel payload with this build's bundle.

        Kernel-only re-deliveries (unchanged rootfs inputs) splice the new
        bundle into a reused tree: modules tarball, /boot staging and
        depmod. The proprietary GPU/VPU userland is bundle-versioned with
        the kernel; the pinned firmware guard below refuses to splice when
        the tree's userland does not match the current handoff, forcing a
        fresh build that reinstalls it."""
        csffw = self.fs / "lib/firmware/mali_csffw.bin"
        if sha(csffw) != "4f3043499f3a48fe2ea15cad559f26af77f5276f3348982a6a1c6f35dfe2572d":
            raise ValueError("staged tree GPU userland differs from the pinned 6.6 handoff; rebuild fresh")
        modules = self.fs / "lib/modules"
        if modules.exists():
            for old in list(modules.iterdir()):
                if old.is_symlink():
                    old.unlink()
                elif old.is_dir():
                    shutil.rmtree(old)
        modules.mkdir(parents=True, exist_ok=True)
        exclusions = [] if self.pg_enabled else ["--exclude=*/pgdrv.ko*"]
        self.run("kernel-splice-modules", ["tar", *exclusions, "--zstd", "-xf",
                 str(self.kernel_bundle / ("modules-" + self.kernel_version + ".tar.zst")),
                 "-C", str(modules)])
        # Two-way assertion (kernel-ota-design 4.4): the tarball root
        # directory name must equal the bundle's kernel.release.
        if not (modules / self.kernel_version).is_dir():
            raise ValueError("modules tarball root does not match kernel.release " + self.kernel_version)
        for name in ["build", "source"]:
            path = self.fs / "lib/modules" / self.kernel_version / name
            if path.is_symlink():
                path.unlink()
        boot = self.fs / "boot"
        shutil.copy2(self.kernel_objects / "arch/arm64/boot/Image", boot / ("vmlinuz-" + self.kernel_version))
        shutil.copy2(self.kernel_objects / ".config", boot / ("config-" + self.kernel_version))
        shutil.copy2(self.kernel_objects / "System.map", boot / ("System.map-" + self.kernel_version))
        shutil.copy2(self.kernel_objects / ("arch/arm64/boot/dts/cix/" + self.dtb_name), boot / self.dtb_name)
        self.run("kernel-splice-depmod", ["depmod", "-b", self.fs, self.kernel_version])
        if self.pg_enabled:
            pgdrv = self.fs / "lib/modules" / self.kernel_version / "extra/y4/pgdrv.ko"
            if not pgdrv.is_file():
                raise ValueError("PG-enabled kernel bundle is missing extra/y4/pgdrv.ko")
            self.report["factory_pgdrv"] = item(pgdrv)
        self.report["rootfs_kernel_spliced"] = True

    def repackage(self):
        """Kernel-only re-delivery: splice a new bundle into a previous
        build's staged tree and repackage, skipping every component build.

        The previous tree is copied (never mutated in place); the ESP,
        grub.cfg/grubenv and MANIFEST are regenerated fresh by package()."""
        prev = self.repackage_from
        self.verify_kernel_rootfs(self.kernel_bundle / "Image")
        old = json.loads((prev / "BUILD.json").read_text())
        for name in ("mcu", "board", "debian"):
            self.report["components"][name] = dict(old["components"][name], reused_from=str(prev))
        # package() annotates the image plan with the staging-root
        # normalization record that filesystem() would normally produce;
        # the reused tree was normalized by the reference build.
        if old.get("staging_root_directory"):
            self.report["staging_root_directory"] = old["staging_root_directory"]
        mcu_build = self.source / "mcu/firmware/build"
        mcu_build.mkdir(parents=True)
        for suffix in ("bin", "hex"):
            shutil.copy2(prev / ("source/mcu/firmware/build/y4-mcu." + suffix), mcu_build / ("y4-mcu." + suffix))
        self.kernel_from_bundle()
        self.fs = self.out / "filesystem"
        self.run("repackage-fs-copy", ["cp", "-a", str(prev / "filesystem"), str(self.fs)])
        self.splice_bundle_kernel()
        self.package()

    def reuse_components(self, previous):
        previous = previous.resolve(strict=True)
        if self.args.component != "components":
            self.verify_kernel_rootfs(previous / "kernel/objects/arch/arm64/boot/Image")
        old = json.loads((previous / "BUILD.json").read_text())
        if old.get("hardware") != self.config["hardware"]:
            raise ValueError("Hardware reuse refused: reference board differs from the target")
        if old.get("profile") != "debian" or not all(name in old.get("components", {}) for name in ["mcu", "kernel"]):
            raise ValueError("Previous build lacks successfully compiled Debian hardware components")
        proof = assert_hardware_sources(ROOT, old["source_commit"], self.commit)
        if self.input("mcu_vendor") != (previous / "source/mcu/vendor").resolve(strict=True):
            raise ValueError("Hardware reuse refused: MCU vendor input directory changed")
        for name in ["kernel_compiler", "kernel_config"]:
            if old["inputs"][name]["sha256"] != self.report["inputs"][name]["sha256"]:
                raise ValueError("Hardware reuse refused: input changed: " + name)
        self.kernel_objects = previous / "kernel/objects"
        checks = {
            self.kernel_objects / "arch/arm64/boot/Image": old["components"]["kernel"]["image"]["sha256"],
            self.kernel_objects / ("arch/arm64/boot/dts/cix/" + self.dtb_name): old["components"]["kernel"]["dtb"]["sha256"],
            self.kernel_objects / ".config": old["components"]["kernel"]["config"]["sha256"],
            previous / f"source/mcu/firmware/build/{self.board_label.lower()}-mcu.bin": old["components"]["mcu"]["sha256"]}
        for path, expected in checks.items():
            if sha(path) != expected:
                raise ValueError("Hardware reuse refused: artifact hash mismatch: " + str(path))
        verify_dtb_board(self.kernel_objects / "arch/arm64/boot/dts/cix" / self.dtb_name,
                         self.board_label)
        self.kernel_version = (self.kernel_objects / "include/config/kernel.release").read_text().strip()
        if self.kernel_version != old["components"]["kernel"]["version"]:
            raise ValueError("Reused kernel version mismatch")
        modules = {}
        for module in sorted(self.kernel_objects.rglob("*.ko")):
            data = module.read_bytes()
            versions = re.findall(rb"(?:^|\x00)vermagic=([^\x00]*)", data)
            if data[:6] != b"\x7fELF\x02\x01" or int.from_bytes(data[18:20], "little") != 183 or not versions:
                raise ValueError("Invalid reused ARM64 module: " + str(module))
            if any(not version.startswith(self.kernel_version.encode() + b" ") for version in versions):
                raise ValueError("Reused module/kernel version mismatch")
            modules[str(module.relative_to(previous))] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if not modules:
            raise ValueError("No compiled kernel modules available for reuse")
        mcu = self.source / "mcu"
        (mcu / "firmware/build").mkdir(parents=True)
        files = [f"firmware/build/{self.board_label.lower()}-mcu.bin"]
        if self.board_label == "Y4":
            files.append("firmware/build/y4-mcu.hex")
            files += ["host/" + name for name in ["mcud", "mcuctl", "hexverify", "y4-mcu-reset", "y4-mcu-flash"]]
        reused = {}
        for name in files:
            src = previous / "source/mcu" / name
            shutil.copy2(src, mcu / name)
            reused[name] = item(src)
        prefix = self.input("dependencies") / "kernel-toolchain-12.3/bin/aarch64-none-linux-gnu-"
        self.kernel_source = previous / "source/kernel/linux"
        self.kernel_command = ["make", "-C", previous / "source/kernel/linux", "O=" + str(self.kernel_objects),
                               "ARCH=arm64", "CROSS_COMPILE=" + str(prefix), "LOCALVERSION=-generic"]
        for name in ["mcu", "kernel"]:
            self.report["components"][name] = dict(old["components"][name], mode="reused-source-build",
                    build_source_commit=old["source_commit"], build_directory=str(previous))
        write_json(self.out / "REUSED-HARDWARE.json", {"source_commit": old["source_commit"],
                    "matching_git_objects": proof, "modules": modules, "mcu_files": reused,
                    "module_symvers": item(self.kernel_objects / "Module.symvers")})
        self.report["hardware_reuse"] = {"previous": str(previous), "modules_verified": len(modules),
                                         "receipt": item(self.out / "REUSED-HARDWARE.json")}
        self.record()

    def board(self):
        # Native Debian board services, signed MCU assets and a PG module tied
        # to this exact kernel. No OpenCecs PID1/State/ZFS userspace is imported.
        # With a kernel bundle the out-of-tree PG module still compiles
        # against the referenced 6.1 kernel (--pg-from): pgdrv is a
        # factory-test module and its 6.1 vermagic is inert on 6.6, while
        # the runtime NIC drivers (r8125/r8127) ship with the 6.6 kernel.
        self.board_output = self.out / "board"
        command = [sys.executable, self.source / "board/build.py",
            "--config", self.args.config.resolve(), "--board", self.board_label,
            "--output", self.board_output, "--dependencies", self.input("dependencies"),
            "--jobs", str(self.args.jobs), "--architectures", "arm64", "--require-signed", "--make-deb"]
        if self.mcu_assets:
            command += ["--mcu-assets", self.source / "mcu", "--mcu-assets-manifest-sha256",
                        self.report["inputs"]["mcu_assets"]["sha256"]]
        else:
            command += ["--mcu-vendor", self.input("mcu_vendor")]
            if self.board_label == "Y4":
                command += ["--mcu-bin", self.source / "mcu/firmware/build/y4-mcu.bin"]
        if self.pg_enabled:
            pg_objects = self.pg_from / "kernel/objects" if self.kernel_bundle else self.kernel_objects
            pg_source = self.pg_from / "source/kernel/linux" if self.kernel_bundle else self.kernel_source
            command += ["--kernel-objects", pg_objects, "--kernel-source", pg_source,
                        "--realtek-pg", self.input("realtek_pg")]
            if self.kernel_bundle:
                command.append("--pg-userspace-only")
        self.run("board-components", command)
        result = json.loads((self.board_output / "BUILD.json").read_text())
        if result.get("board") != self.board_label:
            raise ValueError("Board component build reported a different product")
        if not result.get("complete") or not result.get("signed_mcu", {}).get("signed"):
            raise ValueError("Incomplete Debian board package")
        if self.board_label == "Y12":
            expected = self.report["components"]["mcu"]
            signed = result["signed_mcu"]
            if (signed.get("board"), signed.get("version"), signed.get("image_sha256")) != (
                    self.board_label, expected["version"], expected["sha256"]):
                raise ValueError("Signed Y12 MCU differs from the selected source build")
        self.report["components"]["board"] = {
            "mode": "source-build", "package": item(self.board_output / "y4-board.deb"),
            "mcu_version": result["mcu_version"],
            "mcu_sequence": result["signed_mcu"]["sequence"],
            "kernel_release": self.kernel_version, "factory_pg": self.pg_enabled,
            "tests_executed": False, "hardware_access": False
        }
        self.record()

    def rootfs_fingerprint(self):
        def sha(path):
            return item(Path(path))["sha256"]
        overlay = self.source / "rootfs/overlay"
        return {
            "debian_base": self.report["inputs"]["debian_base"]["sha256"],
            "bootstrap_scripts": {name: sha(self.source / "scripts" / name) for name in
                                  ("rootfs_bootstrap.py", "rootfs_bootstrap_chroot.sh", "rootfs_download.py", "rootfs_host_policy.py")},
            "overlay_tree": tree_digest(overlay),
            "panel_binary": self.report["inputs"].get("panel_binary", {}).get("sha256"),
            "board_package": sha(self.board_output / "y4-board.deb"),
            "build_script": sha(Path(__file__).resolve()),
            "apt_upgrade_script": sha(self.source / "scripts/apt_upgrade.py"),
            "apt_inputs": tree_digest(self.source / "rootfs/fetch"),
            "frozen_package_helper": tree_digest(self.source / "scripts/frozen_packages.py"),
            "vendor_inputs": tree_digest(self.source / "rootfs/vendor"),
            "legacy_vendor_inputs": tree_digest(self.source / "vendor"),
            "graphics_script": sha(self.source / "scripts/graphics_config.py"),
            "graphics_runtime_script": sha(Path(configure_graphics.__code__.co_filename)),
            "kernel_requirements_script": tree_digest(self.source / "scripts/kernel_requirements.py"),
            "kernel_requirements_runtime_script": sha(Path(verify_kernel_compatibility.__code__.co_filename)),
            "board_install_script": sha(self.source / "board/install.py"),
        }

    def try_rootfs_reuse(self):
        ref = Path(self.args.reuse_rootfs).resolve()
        saved = ref / "ROOTFS-FINGERPRINT.json"
        if not saved.is_file():
            print("rootfs-reuse: reference " + ref.name + " has no fingerprint; building fresh", flush=True)
            return False
        if json.loads(saved.read_text()).get("fingerprint") != self.rootfs_fingerprint():
            print("rootfs-reuse: fingerprint mismatch; building fresh", flush=True)
            return False
        self.run("rootfs-reuse-copy", ["cp", "-a", str(ref / "filesystem"), str(self.out / "filesystem")])
        self.fs = self.out / "filesystem"
        # The ESP carries the kernel: refuse the reuse when the copied tree
        # does not hold the exact kernel object selected by this build.
        image = self.kernel_objects / "arch/arm64/boot/Image"
        staged = self.fs / ("boot/vmlinuz-" + self.kernel_version)
        if item(image)["sha256"] != item(staged)["sha256"]:
            raise ValueError("rootfs-reuse: copied tree kernel differs from the selected kernel objects")
        # The reused tree carries the delivery version: the boot FAT kernel
        # directory and the grubenv variables must name the version the
        # shipped rootfs reports, so a reused pair keeps one identity end to
        # end (kernel-ota-design 3/4.4).
        release_path = self.fs / "etc/y4-release.json"
        if not release_path.is_file():
            raise ValueError("rootfs-reuse: reference tree lacks etc/y4-release.json")
        reused = json.loads(release_path.read_text())
        if reused.get("board", "Y4") != self.board_label:
            raise ValueError("rootfs-reuse: reference board differs from the build target")
        reused_version = reused.get("rootfs")
        if not isinstance(reused_version, str) or not reused_version:
            raise ValueError("rootfs-reuse: reference y4-release.json has no rootfs version")
        if reused.get("kernel") != self.kernel_version:
            raise ValueError("rootfs-reuse: reference y4-release.json kernel differs from the selected bundle")
        self.delivery_version = reused_version
        kept = ("boot.img", "boot-4096.img", "boot_sparse.img", "boot_sparse-4096.img",
                "partition-table.img", "partition-table-4096.img",
                "partition-table.img.json", "partition-table-4096.img.json",
                "grub.cfg", "grubenv", "ROOTFS-IMAGE.json", "ROOTFS-INVENTORY.json",
                "rootfs-content-verification.json", "rootfs-timestamps.json",
                "root-sparse.json")
        for name in kept:
            source = ref / name
            if source.is_file():
                shutil.copy2(source, self.out / name)
        ref_report = json.loads((ref / "BUILD.json").read_text())
        self.report["components"]["debian"] = ref_report["components"]["debian"]
        self.report["staging_root_directory"] = ref_report.get("staging_root_directory")
        self.report["rootfs_reused_from"] = str(ref)
        self.rootfs_reused = True
        self.record()
        print("rootfs-reuse: content identical to " + ref.name + "; reusing staged image artifacts", flush=True)
        return True

    def trim_production_rootfs(self, chroot):
        # The 6.6 boot assets already live in the staged root and ESP inputs.
        # Purge the exact SDK 6.1 packages so dpkg also removes their metadata.
        legacy = "6.1.44-cix-build-generic"
        if self.kernel_bundle and self.kernel_version != legacy:
            self.run("rootfs-purge-legacy-kernel", chroot + ["/usr/bin/dpkg", "--purge",
                     "linux-image-" + legacy + "-dbg",
                     "linux-headers-" + legacy, "linux-image-" + legacy])
            # Factory media/model samples are kept in the frozen SDK base.
            testdata = self.fs / "usr/share/cix/testdata"
            if testdata.exists():
                shutil.rmtree(testdata)
        # Use dpkg with an explicit list: decline new reverse dependencies
        # instead of allowing a solver to remove more desktop or GPU packages.
        self.run("rootfs-purge-benchmarks", chroot + ["/usr/bin/dpkg", "--purge",
                 "mlucas", "glmark2-drm", "glmark2-es2-drm", "glmark2-es2-wayland",
                 "glmark2-es2-x11", "glmark2-wayland", "glmark2-x11", "glmark2-data"])
        self.run("rootfs-clean-apt-archives", chroot + ["/usr/bin/apt-get", "clean"])
        # Remove indexes and generated cache binaries from the shipped copy.
        # Verified package downloads are cached outside the staged filesystem.
        for path in (self.fs / "var/lib/apt/lists").glob("*"):
            if path.is_file() and path.name != "lock":
                path.unlink()
        for name in ("pkgcache.bin", "srcpkgcache.bin"):
            (self.fs / "var/cache/apt" / name).unlink(missing_ok=True)
        self.run("rootfs-dpkg-audit", chroot + ["/usr/bin/dpkg", "--audit"])
        if (self.out / "logs/rootfs-dpkg-audit.log").read_text().strip():
            raise ValueError("Trimmed rootfs has incomplete Debian package state")
        self.report["rootfs_shared_resources"] = share_packaged_resources(self.fs)

    def filesystem(self):
        self.verify_kernel_rootfs(self.kernel_objects / "arch/arm64/boot/Image")
        if self.args.reuse_rootfs and self.try_rootfs_reuse():
            return
        self.fs = self.out / "filesystem"
        self.fs.mkdir()
        lock_path = self.source / self.report["inputs"]["debian_base"]["manifest"]
        cache = (self.out / "apt-downloads" if self.args.fresh_apt else
                 Path(os.environ.get("MYT_ROOTFS_DEB_CACHE", ROOT.parent / "cache/rootfs-pinned-debs")))
        self.run("debian-bootstrap", [sys.executable, "-B", self.source / "scripts/rootfs_bootstrap.py",
                                      self.fs, lock_path, cache])
        # Install the product login settings after Debian account-maintainer scripts.
        shadow = self.fs / "etc/shadow"
        lines = shadow.read_text().splitlines()
        for i, line in enumerate(lines):
            if line.startswith("root:"):
                lines[i] = ":".join(["root", "$6$1Sn6zHZuYjgawejQ$dp5gpBPlG8V/eJsY3QLIYktOdMkXMTxcsqp8IMculcT5agm.YHBxdwA1b1ZHu6bpkYyR45Z.E4p7cQ2InUt9V0", "19969", "0", "99999", "7", "", "", ""])
                break
        else:
            raise ValueError("staged /etc/shadow has no root entry")
        shadow.write_text("\n".join(lines) + "\n")
        passwd = self.fs / "etc/passwd"
        plines = passwd.read_text().splitlines()
        for i, line in enumerate(plines):
            if line.startswith("root:"):
                fields = line.split(":")
                if fields[1] != "x":
                    fields[1] = "x"
                    plines[i] = ":".join(fields)
                break
        else:
            raise ValueError("staged /etc/passwd has no root entry")
        passwd.write_text("\n".join(plines) + "\n")
        # The SDK archive's outer staging directory is 1000:1000/0775. Preserve
        # the existing delivered ext4 root policy, rather than importing it.
        archive_root = self.fs.stat()
        os.chown(self.fs, 0, 0)
        self.fs.chmod(0o755)
        normalized_root = self.fs.stat()
        self.report["staging_root_directory"] = dict(
            archive=dict(uid=archive_root.st_uid, gid=archive_root.st_gid,
                         mode=archive_root.st_mode & 0o7777, mtime_ns=archive_root.st_mtime_ns),
            normalized=dict(uid=normalized_root.st_uid, gid=normalized_root.st_gid,
                            mode=normalized_root.st_mode & 0o7777, mtime_ns=normalized_root.st_mtime_ns),
            recursive=False, reason="match the 0:0/0755 root inode of earlier delivered ext4 images")
        osrelease = (self.fs / "etc/os-release").read_text()
        if 'VERSION_ID="12"' not in osrelease or 'ID=debian' not in osrelease:
            raise ValueError("Expected Debian 12 base")
        init = (self.fs / "sbin/init").readlink()
        if "systemd" not in str(init):
            raise ValueError("Debian systemd init required")
        overlay = self.source / "rootfs/overlay"
        for source in overlay.rglob("*"):
            # Symlinks are applied too (e.g. multi-user.target.wants
            # entries); a symlink to a regular file also answers is_file(),
            # so the symlink test must come first.
            if source.is_symlink():
                target = self.fs / source.relative_to(overlay)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.exists():
                    target.unlink()
                target.symlink_to(os.readlink(source))
            elif source.is_file():
                target = self.fs / source.relative_to(overlay)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink():
                    target.unlink()
                shutil.copy2(source, target)
        if self.panel_input is not None:
            target = self.fs / "usr/bin/myt_desktop"
            if target.is_symlink():
                target.unlink()
            shutil.copyfile(self.panel_input, target)
            target.chmod(0o755)
            verify_panel_binary(target, self.report["inputs"]["panel_binary"])
        # The SDK archive was staged by the vendor account and tar
        # --numeric-owner preserves it: /etc, /usr and /opt arrived as
        # cix:cix 0775, so the unprivileged cix account could write system
        # configuration, unit files and PATH entries. Normalize the imported
        # system trees to root, as Debian ships them.
        vendor_ids = set()
        for line in (self.fs / "etc/passwd").read_text().splitlines():
            fields = line.split(":")
            if len(fields) > 3 and fields[0] == "cix":
                vendor_ids.update((int(fields[2]), int(fields[3])))
        for name in ("etc", "usr", "opt"):
            top = self.fs / name
            for path in [top, *top.rglob("*")]:
                st = path.lstat()
                if st.st_uid in vendor_ids or st.st_gid in vendor_ids:
                    os.chown(path, 0, 0, follow_symlinks=False)
            top.chmod(0o755)
        # The slot-power control marker and map are trust anchors: git does
        # not preserve the required modes, so pin them here (the runner
        # demands 0600 on the marker; the worker accepts any non-group/
        # world-writable map).
        marker = self.fs / "etc/y4-ix-slot-lab.conf"
        if marker.is_file():
            marker.chmod(0o600)
        slot_map = self.fs / "etc/y4-ix-slot-lab.map"
        if slot_map.is_file():
            slot_map.chmod(0o644)
        grow_script = self.fs / "usr/libexec/y4-root-grow"
        grow_script.chmod(0o755)
        grow_dependency = self.fs / "etc/systemd/system/sysinit.target.requires/y4-root-grow.service"
        grow_dependency.parent.mkdir(parents=True, exist_ok=True)
        if grow_dependency.is_symlink():
            grow_dependency.unlink()
        elif grow_dependency.exists():
            raise ValueError("Unexpected regular root-growth dependency")
        grow_dependency.symlink_to("/usr/lib/systemd/system/y4-root-grow.service")
        for name in ["usr/sbin/resize2fs", "usr/sbin/dumpe2fs", "usr/sbin/blockdev", "usr/bin/findmnt"]:
            if not (self.fs / name).is_file():
                raise ValueError("Debian standard root-growth tool missing: " + name)
        stale_script = self.fs / "usr/libexec/y4-root-stale-clean"
        stale_script.chmod(0o755)
        stale_dependency = self.fs / "etc/systemd/system/sysinit.target.requires/y4-root-stale-clean.service"
        stale_dependency.parent.mkdir(parents=True, exist_ok=True)
        if stale_dependency.is_symlink():
            stale_dependency.unlink()
        elif stale_dependency.exists():
            raise ValueError("Unexpected regular stale-clean dependency")
        stale_dependency.symlink_to("/usr/lib/systemd/system/y4-root-stale-clean.service")
        for name in ["usr/bin/dd", "usr/bin/udevadm", "usr/sbin/blkid"]:
            if not (self.fs / name).is_file():
                raise ValueError("Debian standard stale-signature cleanup tool missing: " + name)
        provision_script = self.fs / "usr/libexec/y4-data-provision"
        provision_script.chmod(0o755)
        provision_dependency = self.fs / "etc/systemd/system/sysinit.target.requires/y4-data-provision.service"
        provision_dependency.parent.mkdir(parents=True, exist_ok=True)
        if provision_dependency.is_symlink():
            provision_dependency.unlink()
        elif provision_dependency.exists():
            raise ValueError("Unexpected regular data-provision dependency")
        provision_dependency.symlink_to("/usr/lib/systemd/system/y4-data-provision.service")
        for name in ["usr/bin/mount", "usr/bin/umount", "usr/sbin/wipefs", "usr/sbin/mkfs.ext4",
                     "usr/sbin/resize2fs", "usr/sbin/blkid", "usr/bin/cp"]:
            if not (self.fs / name).is_file():
                raise ValueError("Debian standard data-provision tool missing: " + name)
        modules = self.fs / "lib/modules"
        for old in list(modules.iterdir()) if modules.exists() else []:
            if old.is_symlink():
                old.unlink()
            elif old.is_dir():
                # Only disposable staging content, never the input filesystem.
                assert old.resolve().is_relative_to(self.fs.resolve())
                shutil.rmtree(old)
        if self.kernel_bundle:
            # The bundle tarball carries the complete 6.6 module set (680
            # modules including r8125/nfsd) rooted at the release directory.
            modules.mkdir(parents=True, exist_ok=True)
            exclusions = [] if self.pg_enabled else ["--exclude=*/pgdrv.ko*"]
            self.run("kernel-modules-install", ["tar", *exclusions, "--zstd", "-xf",
                     str(self.kernel_bundle / ("modules-" + self.kernel_version + ".tar.zst")),
                     "-C", str(modules)])
        else:
            self.run("kernel-modules-install", self.kernel_command + ["-j" + str(self.args.jobs), "modules_install", "INSTALL_MOD_PATH=" + str(self.fs),
                                                                       "INSTALL_MOD_STRIP=1", "DEPMOD=true"])
        for name in ["build", "source"]:
            path = self.fs / "lib/modules" / self.kernel_version / name
            if path.is_symlink():
                path.unlink()
        self.run("depmod", ["depmod", "-b", self.fs, self.kernel_version])
        # Keep native Debian init/network stack. Retire historical MCU owners.
        disabled = ["mcu-daemon.service", "mcu-daemon-y4.service", "mcu-flash.service", "mcu-boot.service",
                    "y12-fan-default.service", "y12-sysdisk-expand.service", "myt-desktop.service", "y4-app.service", "y4-netd.service",
                    "rknn3.service", "myt-hostname.service"]
        for directory in (self.fs / "etc/systemd/system").glob("*.wants"):
            for name in disabled:
                p = directory / name
                if p.is_symlink():
                    p.unlink()
        # Product service set: units the Y4 image never runs. A /dev/null
        # symlink in /etc/systemd/system masks the unit, so D-Bus activation
        # and dependency pulls cannot start it either, not just the boot-time
        # enable link. The Houmo M50 firmware-update stack is removed
        # outright below; nothing houmo ships on this image.
        # NAS services (nfs-server, rpcbind, smbd, nmbd, tgt) are NOT masked:
        # the NAS management panel needs them available on demand (disabled but
        # startable). They are still not enabled at boot - the panel controls
        # when they run.
        for name in ["cix-audio-switch.service", "dhcpcd.service", "ModemManager.service",
                     "bluetooth.service", "bluetooth-autoconnect.service", "wpa_supplicant.service",
                     "vsftpd.service"]:
            for directory in (self.fs / "etc/systemd/system").glob("*.wants"):
                p = directory / name
                if p.is_symlink():
                    p.unlink()
            p = self.fs / "etc/systemd/system" / name
            if p.is_symlink() or p.exists():
                p.unlink()
            p.symlink_to("/dev/null")
        # NAS services: remove any enablement links so they do not auto-start,
        # but leave the unit files alone (the panel starts them on demand).
        for name in ["nfs-server.service", "nfs-blkmap.service", "rpcbind.service",
                     "rpcbind.socket", "rpc-statd-notify.service", "smbd.service",
                     "nmbd.service", "samba-ad-dc.service", "tgt.service"]:
            for directory in (self.fs / "etc/systemd/system").glob("*.wants"):
                p = directory / name
                if p.is_symlink():
                    p.unlink()
        # systemd unit files installed with the product overlay are data files.
        for name in ["bluetooth-autoconnect.service", "cix-audio-switch.service",
                     "cix-docker-env.service", "cix_resume.service",
                     "cix_resume_prepare.service", "debian_docker_monitor.service",
                     "systemd-pstore.service"]:
            unit = self.fs / "usr/lib/systemd/system" / name
            if unit.is_file():
                unit.chmod(0o644)
        rule = self.fs / "usr/lib/udev/rules.d/60-libfprint-2.rules"
        if rule.is_file():
            rule.chmod(0o644)
        # Retired-owner binaries whose units are already gone from boot:
        # the old Y12 desktop, the legacy MCU daemon/ctl pair and the Y12
        # disk resize helpers. y4-boardd and the Debian init stack replaced
        # all of them.
        dead_files = [
            "usr/local/bin/myt-desktop",
            "usr/lib/systemd/system/myt-desktop.service",
            "etc/systemd/system/myt-desktop.service",
            "usr/local/bin/mcu_daemon_y4",
            "usr/local/bin/mcu_ctl_y4",
            "usr/local/bin/mcu_daemon",
            "usr/local/bin/mcu_ctl",
            "usr/local/bin/y12-part-resize",
            "usr/local/bin/y12-sysdisk-expand.sh",
            "usr/local/bin/mcu-flash-check.sh",
            "usr/local/bin/mcu-boot.sh",
            "usr/local/bin/mcu-boot.sh.orig-before-gpio-fix",
            "usr/local/bin/mcu_flash.sh",
            "usr/local/bin/mcu_flash.sh.orig-before-gpio-fix",
            "usr/local/bin/y12-fan",
            "usr/local/bin/slot-pcie",
            "usr/local/bin/board-detect.sh",
            "usr/local/bin/myt-hostname.sh",
            "opt/cix",
            "usr/lib/Multiarch",
        ]
        for name in dead_files:
            path = self.fs / name
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        # The NPU demo python stack (onnx and its dependency closure) was
        # staged by the vendor account and nothing on the image imports it.
        python_npu = [
            "usr/local/lib/python3.11/dist-packages/coloredlogs",
            "usr/local/lib/python3.11/dist-packages/coloredlogs-15.0.1.dist-info",
            "usr/local/lib/python3.11/dist-packages/coloredlogs.pth",
            "usr/local/lib/python3.11/dist-packages/flatbuffers",
            "usr/local/lib/python3.11/dist-packages/flatbuffers-24.3.25.dist-info",
            "usr/local/lib/python3.11/dist-packages/google",
            "usr/local/lib/python3.11/dist-packages/humanfriendly",
            "usr/local/lib/python3.11/dist-packages/humanfriendly-10.0.dist-info",
            "usr/local/lib/python3.11/dist-packages/isympy.py",
            "usr/local/lib/python3.11/dist-packages/mpmath",
            "usr/local/lib/python3.11/dist-packages/mpmath-1.3.0.dist-info",
            "usr/local/lib/python3.11/dist-packages/numpy",
            "usr/local/lib/python3.11/dist-packages/numpy-1.26.4.dist-info",
            "usr/local/lib/python3.11/dist-packages/numpy.libs",
            "usr/local/lib/python3.11/dist-packages/onnx",
            "usr/local/lib/python3.11/dist-packages/onnx-1.17.0.dist-info",
            "usr/local/lib/python3.11/dist-packages/protobuf-5.29.1.dist-info",
            "usr/local/lib/python3.11/dist-packages/sympy",
            "usr/local/lib/python3.11/dist-packages/sympy-1.13.3.dist-info",
            "usr/local/bin/check-model",
            "usr/local/bin/check-node",
            "usr/local/bin/backend-test-tools",
            "usr/local/bin/f2py",
            "usr/local/bin/isympy",
            "usr/local/bin/coloredlogs",
            "usr/local/bin/humanfriendly",
        ]
        for name in python_npu:
            path = self.fs / name
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        # The Houmo M50 NPU firmware-update stack goes entirely: service,
        # update/monitoring tools, SDK, card firmware image, LLM runtime,
        # profile environment, config and logs. A future firmware
        # deployment ships its own current toolchain.
        houmo_files = [
            "etc/systemd/system/houmo-fw.service",
            "etc/houmo",
            "etc/profile.d/houmo-m50.sh",
            "usr/local/bin/houmo-fw-start.sh",
            "usr/local/bin/hm_smi",
            "usr/local/bin/hm_upgrade_cli",
            "usr/share/houmo",
            "usr/local/houmo",
            "usr/local/houmo-sdk",
            "opt/houmo",
            "root/.local/share/hm_smi",
            "var/log/houmo-fw.log",
        ]
        for name in houmo_files:
            path = self.fs / name
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)

        # Drop enablement links whose unit no longer resolves anywhere:
        # the removals above leave exactly these behind. A masked unit
        # still counts as present (the base image pairs some masks with
        # their enable link on purpose), and a template-instance link is
        # resolved against its template.
        for directory in (self.fs / "etc/systemd/system").glob("*.wants"):
            for link in directory.iterdir():
                if not link.is_symlink() or os.readlink(link) == "/dev/null":
                    continue
                name = link.name
                if "@" in name:
                    unit, _, argument = name.partition("@")
                    name = unit + "@." + argument.rsplit(".", 1)[-1]
                if not any((self.fs / base / name).exists()
                           for base in ("etc/systemd/system",
                                        "usr/lib/systemd/system",
                                        "lib/systemd/system")):
                    link.unlink()
        # CIX GPU/VPU product stack. With a kernel bundle the 6.6 kernel
        # carries Mali KMD (r54p1), MGM/PMA, amvx and r8127 built in, so
        # only the proprietary userland is installed, from the 6.6 BSP
        # handoff tree. Otherwise the vendor kernel modules are built
        # against this build's 6.1 kernel first. The vendor BSP lives
        # outside this repository; its location is fixed on the build host.
        if self.kernel_bundle:
            # The 6.6-paired GPU/VPU userland is vendored in the rootfs
            # repository (debian-y4.git vendor/); the cix-bsp working tree
            # must not override the exported, fingerprinted source snapshot.
            vendor_bases = [self.source / "rootfs/vendor", self.source / "vendor"]
            csffw = None
            for base in vendor_bases:
                candidate = base / "cix-gpu-umd/usr/lib/firmware/mali_csffw.bin"
                if candidate.is_file():
                    csffw = candidate
                    break
            if csffw is None:
                raise ValueError("Missing frozen vendor input: cix-gpu-umd/usr/lib/firmware/mali_csffw.bin")
            # Pinned to the handoff-verified firmware (the cix_module_fw
            # copy is a different build and must not be picked up).
            if sha(csffw) != "4f3043499f3a48fe2ea15cad559f26af77f5276f3348982a6a1c6f35dfe2572d":
                raise ValueError("6.6 mali_csffw.bin does not match the handoff-verified firmware")
            shutil.copy2(csffw, self.fs / "lib/firmware/mali_csffw.bin")
            vpu_firmware = None
            for base in vendor_bases:
                candidate = base / "cix-vpu-umd/usr/lib/firmware"
                if candidate.is_dir():
                    vpu_firmware = candidate
                    break
            if vpu_firmware is None:
                raise ValueError("Missing frozen vendor input: cix-vpu-umd/usr/lib/firmware")
            for fwb in vpu_firmware.glob("*.fwb"):
                shutil.copy2(fwb, self.fs / "lib/firmware" / fwb.name)
            # Y4 r194: the QtScrcpy mirroring payload ships prebuilt in the
            # rootfs vendor tree (vendor/qtscrcpy); install it flat under
            # /opt/y4-qtscrcpy - the app resolves adb and scrcpy-server
            # relative to the binary directory.
            qtscrcpy = None
            for base in vendor_bases:
                candidate = base / "qtscrcpy/opt/y4-qtscrcpy"
                if candidate.is_dir():
                    qtscrcpy = candidate
                    break
            if qtscrcpy is None:
                raise ValueError("QtScrcpy vendor payload missing: qtscrcpy/opt/y4-qtscrcpy")
            qtscrcpy_target = self.fs / "opt/y4-qtscrcpy"
            qtscrcpy_target.mkdir(parents=True)
            for name in ("QtScrcpy", "scrcpy-server"):
                shutil.copy2(qtscrcpy / name, qtscrcpy_target / name)
            (qtscrcpy_target / "QtScrcpy").chmod(0o755)
            adb = self.fs / "usr/bin/adb"
            if not adb.is_file() or not os.access(adb, os.X_OK):
                raise ValueError("QtScrcpy requires Debian ARM64 adb from inputs-apt.json")
            adb_link = qtscrcpy_target / "adb"
            adb_link.unlink(missing_ok=True)
            adb_link.symlink_to("../../usr/bin/adb")
            pro_lib = None
            for base in vendor_bases:
                candidate = base / "cix-gpu-umd/opt/cixgpu-pro/lib/aarch64-linux-gnu"
                if candidate.is_dir():
                    pro_lib = candidate
                    break
            if pro_lib is None:
                raise ValueError("Missing frozen vendor input: cix-gpu-umd/opt/cixgpu-pro/lib/aarch64-linux-gnu")
            shutil.copytree(pro_lib, self.fs / "opt/cixgpu-pro/lib", symlinks=True, dirs_exist_ok=True)
            glvnd_dir = self.fs / "usr/share/glvnd/egl_vendor.d"
            glvnd_dir.mkdir(parents=True, exist_ok=True)
            # 6.6 ships the glvend vendor JSON only under cixgpu-compat.
            glvend = None
            for base in vendor_bases:
                candidate = base / "cix-gpu-umd/opt/cixgpu-compat/share/glvnd/egl_vendor.d/40_cix.json"
                if candidate.is_file():
                    glvend = candidate
                    break
            if glvend is None:
                raise ValueError("Missing frozen vendor input: cix-gpu-umd/opt/cixgpu-compat/share/glvnd/egl_vendor.d/40_cix.json")
            shutil.copy2(glvend, glvnd_dir / "40_cix.json")
            ldsoconf = None
            for base in vendor_bases:
                candidate = base / "cix-gpu-umd/etc/ld.so.conf.d/00-cixgpu-pro.conf"
                if candidate.is_file():
                    ldsoconf = candidate
                    break
            if ldsoconf is None:
                raise ValueError("Missing frozen vendor input: cix-gpu-umd/etc/ld.so.conf.d/00-cixgpu-pro.conf")
            shutil.copy2(ldsoconf, self.fs / "etc/ld.so.conf.d/00-cixgpu-pro.conf")
            (self.fs / "usr/lib/udev/rules.d").mkdir(parents=True, exist_ok=True)
            cma_rules = None
            for base in vendor_bases:
                candidate = base / "cix-gpu-umd/lib/udev/rules.d/99-cma-device.rules"
                if candidate.is_file():
                    cma_rules = candidate
                    break
            if cma_rules is None:
                raise ValueError("Missing frozen vendor input: cix-gpu-umd/lib/udev/rules.d/99-cma-device.rules")
            cma_rules_dest = self.fs / "usr/lib/udev/rules.d/99-cma-device.rules"
            shutil.copyfile(cma_rules, cma_rules_dest)
            cma_rules_dest.chmod(0o644)
            configure_graphics(self.fs)
            # Accelerator userland vendored in the rootfs repository
            # (debian-y4.git vendor/): the audio DSP firmware/codec stack
            # and the SoC NPU (AIPU/ZhouYi) NOE runtime.
            audio = None
            for base in vendor_bases:
                if (base / "cix-audio-dsp").is_dir():
                    audio = base / "cix-audio-dsp"
                    break
            if audio is None:
                audio = self.source / "vendor/cix-audio-dsp"
            if audio.is_dir():
                shutil.copytree(audio / "usr/share/cix/lib", self.fs / "usr/share/cix/lib", dirs_exist_ok=True)
                shutil.copytree(audio / "usr/share/cix/include", self.fs / "usr/share/cix/include", dirs_exist_ok=True)
                shutil.copy2(audio / "usr/lib/firmware/dsp_fw.bin", self.fs / "lib/firmware/dsp_fw.bin")
            noe = None
            for base in vendor_bases:
                if (base / "cix-noe-umd").is_dir():
                    noe = base / "cix-noe-umd"
                    break
            if noe is None:
                noe = self.source / "vendor/cix-noe-umd"
            if noe.is_dir():
                shutil.copytree(noe / "usr/share/cix/lib", self.fs / "usr/share/cix/lib", dirs_exist_ok=True)
                shutil.copytree(noe / "usr/share/cix/include", self.fs / "usr/share/cix/include", dirs_exist_ok=True)
                pypi = self.fs / "usr/share/cix/pypi"
                pypi.mkdir(parents=True, exist_ok=True)
                for wheel in (noe / "usr/share/cix/pypi").glob("*.whl"):
                    shutil.copy2(wheel, pypi / wheel.name)
            pkgconfig = noe / "usr/lib/aarch64-linux-gnu/pkgconfig"
            if pkgconfig.is_dir():
                (self.fs / "usr/lib/aarch64-linux-gnu/pkgconfig").mkdir(parents=True, exist_ok=True)
                shutil.copytree(pkgconfig, self.fs / "usr/lib/aarch64-linux-gnu/pkgconfig", dirs_exist_ok=True)
        else:
            vendor = self.input("legacy_graphics_sdk")
            gpu_src = vendor / "component/cix_opensource/gpu/gpu_kernel"
            vpu_src = vendor / "component/cix_opensource/vpu/vpu_driver/driver"
            proprietary = vendor / "component/cix_proprietary"
            toolchain = self.input("dependencies") / "kernel-toolchain-12.3/bin/aarch64-none-linux-gnu-"
            mali_configs = ["CONFIG_MALI_PLATFORM_NAME=sky1", "CONFIG_MALI_CSF_SUPPORT=y",
                            "CONFIG_MALI_MEMORY_GROUP_MANAGER=y",
                            "CONFIG_MALI_PROTECTED_MEMORY_ALLOCATOR=y"]
            self.run("gpu-memory-group-manager",
                     ["make", "-C", str(gpu_src / "drivers/base/arm"),
                      "KDIR=" + str(self.kernel_objects), "ARCH=arm64",
                      "CROSS_COMPILE=" + str(toolchain), *mali_configs],
                     gpu_src / "drivers/base/arm")
            self.run("gpu-mali-kbase",
                     ["make", "-C", str(gpu_src / "drivers/gpu/arm"),
                      "KDIR=" + str(self.kernel_objects), "ARCH=arm64",
                      "CROSS_COMPILE=" + str(toolchain), *mali_configs],
                     gpu_src / "drivers/gpu/arm")
            # The kernel source tree sits next to the kernel objects in both
            # the fresh-snapshot and the component-reuse layouts.
            kernel_src = self.kernel_objects.parent.parent / "source/kernel/linux"
            self.run("vpu-amvx",
                     ["make", "-C", str(vpu_src), "KDIR=" + str(kernel_src),
                      "O=" + str(self.kernel_objects), "ARCH=arm64",
                      "CROSS_COMPILE=" + str(toolchain)], vpu_src)
            extra = self.fs / "lib/modules" / self.kernel_version / "extra"
            extra.mkdir(parents=True, exist_ok=True)
            for module in (gpu_src / "drivers/base/arm/memory_group_manager/memory_group_manager.ko",
                           gpu_src / "drivers/base/arm/protected_memory_allocator/protected_memory_allocator.ko",
                           gpu_src / "drivers/gpu/arm/midgard/mali_kbase.ko",
                           vpu_src / "amvx.ko"):
                shutil.copy2(module, extra / module.name)
            self.run("depmod-gpu", ["depmod", "-b", self.fs, self.kernel_version])
            shutil.copy2(proprietary / "cix_proprietary-debs/cix-gpu-umd/usr/lib/firmware/mali_csffw.bin",
                         self.fs / "lib/firmware/mali_csffw.bin")
            for fwb in (proprietary / "cix_proprietary-debs/cix-vpu-umd/usr/lib/firmware").glob("*.fwb"):
                shutil.copy2(fwb, self.fs / "lib/firmware" / fwb.name)
            pro_lib = proprietary / "cix_proprietary-debs/cix-gpu-umd/opt/cixgpu-pro/lib/aarch64-linux-gnu"
            shutil.copytree(pro_lib, self.fs / "opt/cixgpu-pro/lib", dirs_exist_ok=True)
            shutil.copytree(proprietary / "linux_rootfs/usr/share/cix/lib",
                            self.fs / "usr/share/cix/lib", dirs_exist_ok=True)
            glvnd_dir = self.fs / "usr/share/glvnd/egl_vendor.d"
            glvnd_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(proprietary / "cix_proprietary-debs/cix-gpu-umd/usr/share/glvnd/egl_vendor.d/40_cix.json",
                         glvnd_dir / "40_cix.json")
            include_dir = self.fs / "usr/share/cix/include"
            include_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(vendor / "component/cix_opensource/vpu/vpu_driver/include/aarch64-none-linux-gnu/mvx-v4l2-controls.h",
                         include_dir / "mvx-v4l2-controls.h")
        # Desktop account and session policy: root-only system, root
        # autologin, hardware renderer preference for mutter.
        for name in ("etc/passwd", "etc/group", "etc/shadow", "etc/shadow-"):
            conf = self.fs / name
            if conf.is_file():
                kept = [l for l in conf.read_text().splitlines() if not l.startswith("cix:")]
                conf.write_text("\n".join(kept) + "\n")
        # The root password line is owned by the post-apt bake in
        # filesystem(); a legacy rewrite here used to clobber it with
        # the base tarball hash and reset the production password.

        for dead in ("home/cix", "userdata", "var/mail/cix"):
            path = self.fs / dead
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        gdm = self.fs / "etc/gdm3/daemon.conf"
        gdm.write_text(gdm.read_text().replace("AutomaticLogin = cix",
                                               "AutomaticLogin = root"))
        pam = self.fs / "etc/pam.d/gdm-autologin"
        pam.write_text(pam.read_text().replace(
            "auth\trequired\tpam_succeed_if.so user != root quiet_success",
            "#auth\trequired\tpam_succeed_if.so user != root quiet_success"))
        sshd_dir = self.fs / "etc/ssh/sshd_config.d"
        sshd_dir.mkdir(parents=True, exist_ok=True)
        (sshd_dir / "99-y4-root-login.conf").write_text("PermitRootLogin yes\n")
        udev_dir = self.fs / "etc/udev/rules.d"
        udev_dir.mkdir(parents=True, exist_ok=True)
        (udev_dir / "61-mutter-primary-gpu.rules").write_text(
            'SUBSYSTEM=="drm", KERNEL=="card2", TAG+="mutter-device-preferred-primary"\n')
        modules_load = self.fs / "etc/modules-load.d"
        modules_load.mkdir(parents=True, exist_ok=True)
        if not self.kernel_bundle:
            (modules_load / "mali.conf").write_text("mali_kbase\n")
            (modules_load / "amvx.conf").write_text("amvx\n")
        else:
            # The Mali KMD and the VPU driver are built in; the frozen base
            # still ships modules-load entries for them (the 6.1 flow
            # overwrote these files, so the stale vendor copies never
            # showed). Remove them or systemd-modules-load fails at boot.
            for name in ("mali.conf", "amvx.conf"):
                stale = modules_load / name
                if stale.is_symlink() or stale.exists():
                    stale.unlink()
        user_drop = self.fs / "etc/systemd/user/org.gnome.Shell@wayland.service.d"
        user_drop.mkdir(parents=True, exist_ok=True)
        # Default session renderer: the vendor GBM-capable libmali stack
        # (verified on hardware 2026-11-09: session registers, libmali
        # mapped, shell CPU halved vs swrast). The software renderer
        # stays next to it as a manually renameable fallback.
        (user_drop / "gpu.conf").write_text(
            "[Service]\nEnvironment=LD_LIBRARY_PATH=/opt/cixgpu-pro/lib\n")
        (user_drop / "swrast.conf.disabled").write_text(
            "[Service]\nEnvironment=MESA_LOADER_DRIVER_OVERRIDE=kms_swrast\n")
        # Desktop default resolution: the virtual head enumerates 640x480
        # as preferred; pin 1080p for the product.
        monitors = self.fs / "root/.config/monitors.xml"
        monitors.parent.mkdir(parents=True, exist_ok=True)
        monitors.write_text(
            "<monitors version=\"2\">\n"
            "  <configuration>\n"
            "    <logicalmonitor>\n"
            "      <x>0</x>\n      <y>0</y>\n      <scale>1</scale>\n"
            "      <primary>yes</primary>\n"
            "      <monitor>\n"
            "        <monitorspec>\n"
            "          <connector>Virtual-1</connector>\n"
            "          <vendor>unknown</vendor>\n"
            "          <product>unknown</product>\n"
            "          <serial>unknown</serial>\n"
            "        </monitorspec>\n"
            "        <mode>\n"
            "          <width>1920</width>\n          <height>1080</height>\n"
            "          <rate>60.000000</rate>\n"
            "        </mode>\n"
            "      </monitor>\n"
            "    </logicalmonitor>\n"
            "  </configuration>\n"
            "</monitors>\n")
        ssh_dir = self.fs / "root/.ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)
        keys = ssh_dir / "authorized_keys"
        keys.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIP7NTzBMdFAxHg0otTDPdwu8B"
                        "MQfqSvf/AAYVkRdh7jF zcg-mac\n")
        keys.chmod(0o600)
        ssh_dir.chmod(0o700)
        # Release manifest: every component version the management API
        # reports. Rootfs version = the delivery version computed once in
        # __init__; the boot FAT kernel directory carries the same value
        # (kernel-ota-design 3). The kernel Image digest prefix lets the
        # board self-check its running kernel against the stamped release.
        fw_gpu_sha = hashlib.sha256(
            (self.fs / "lib/firmware/mali_csffw.bin").read_bytes()).hexdigest()[:16]
        vpu_files = sorted(f.name for f in (self.fs / "lib/firmware").glob("*.fwb"))
        release = {
            "rootfs": self.delivery_version,
            "board": self.board_label,
            "build_commit": self.commit[:12],
            "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "kernel": self.kernel_version,
            "kernel_image_sha256_16": hashlib.sha256(
                (self.kernel_objects / "arch/arm64/boot/Image").read_bytes()).hexdigest()[:16],
            "fw_gpu": {"file": "mali_csffw.bin", "sha256_16": fw_gpu_sha},
            "fw_vpu": {"fwb_count": len(vpu_files)},
            # The agent on this rootfs applies schema 2 packages: the
            # management API can rely on rootfs+kernel atomic updates.
            "ota_package_schema_max": 2,
        }
        (self.fs / "etc/y4-release.json").write_text(json.dumps(release))
        if self.board_label == "Y4":
            mcu = self.source / "mcu"
            install = {"mcud": "usr/local/sbin/y4-mcud", "mcuctl": "usr/local/bin/mcuctl",
                       "y4-mcu-reset": "usr/local/libexec/y4-mcu/reset", "y4-mcu-flash": "usr/local/libexec/y4-mcu/flash",
                       "hexverify": "usr/local/libexec/y4-mcu/hexverify"}
            for name, target in install.items():
                path = self.fs / target
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.is_symlink():
                    path.unlink()
                shutil.copy2(mcu / "host" / name, path)
            for name in ["y4-mcud.service", "y4-mcu-boot.service"]:
                shutil.copy2(mcu / "system" / name, self.fs / "usr/lib/systemd/system" / name)
            (self.fs / "etc/y4-mcu").mkdir(exist_ok=True)
            wants = self.fs / "etc/systemd/system/multi-user.target.wants/y4-mcud.service"
            if wants.is_symlink():
                wants.unlink()
            wants.symlink_to("/usr/lib/systemd/system/y4-mcud.service")
            firmware = self.fs / "usr/local/share/mcu/images"
            firmware.mkdir(parents=True, exist_ok=True)
            for suffix in ["bin", "hex"]:
                shutil.copy2(mcu / ("firmware/build/y4-mcu." + suffix), firmware / ("y4-mcu." + suffix))
        # OTA trust anchor: the daemon verifies update packages against
        # this raw Ed25519 public key (private key never enters the tree).
        ota_key = self.source / "board/trust/y4-ota-production.pub"
        if not ota_key.is_file() or ota_key.stat().st_size != 32:
            raise ValueError("OTA trust anchor missing or malformed")
        (self.fs / "usr/share/y4-board").mkdir(parents=True, exist_ok=True)
        shutil.copy2(ota_key, self.fs / "usr/share/y4-board/ota-public-key.bin")
        (self.fs / "etc/machine-id").write_text("")
        dbus_id = self.fs / "var/lib/dbus/machine-id"
        if dbus_id.is_file() or dbus_id.is_symlink():
            dbus_id.unlink()
        dbus_id.symlink_to("/etc/machine-id")
        hostname = f"MYT-{self.board_label}"
        (self.fs / "etc/hostname").write_text(f"{hostname}\n")
        hosts = self.fs / "etc/hosts"
        hosts.write_text(hosts.read_text().replace("MYT-Y4 localhost", f"{hostname} localhost", 1))
        # Root comes from the grub command line, which picks the PARTUUID of
        # the active slot; fstab therefore has no / entry and is identical in
        # both slots. The data partition mounts nofail and its subtrees are
        # bind-mounted over the machine-state directories so user data
        # survives every slot switch. Sources are the fixed partition GUIDs,
        # resolved from the GPT itself, never probed by blkid. The data
        # provisioner checks existing ext4 before mounting. A separate fsck
        # job would race it while initializing or growing the partition.
        (self.fs / "etc/fstab").write_text(
              "PARTUUID=" + str(self.part_ids[0]) + " /boot/efi vfat defaults,umask=0077,nofail 0 2\n"
              + "PARTUUID=" + str(self.part_ids[3]) + " /data ext4 defaults,nofail 0 0\n"
              + "/data/home /home none bind,nofail 0 0\n"
              + "/data/root /root none bind,nofail 0 0\n"
              + "/data/docker /var/lib/docker none bind,nofail 0 0\n"
              + "/data/journal /var/log/journal none bind,nofail 0 0\n"
              + "/data/containerd /var/lib/containerd none bind,nofail 0 0\n"
              + "/data/samba /var/lib/samba none bind,nofail 0 0\n"
              + "/data/nfs /var/lib/nfs none bind,nofail 0 0\n"
              + "/data/myt_desktop /var/lib/myt_desktop none bind,nofail 0 0\n"
              + "/data/y4-board/state /var/lib/y4-board none bind,nofail,x-systemd.requires=data.mount 0 0\n")
        for mount_point in ["data", "data/home", "data/root", "data/docker", "data/journal",
                            "home", "root", "var/lib/docker", "var/log/journal",
                            "var/lib/containerd", "var/lib/samba", "var/lib/nfs", "var/lib/myt_desktop"]:
            (self.fs / mount_point).mkdir(parents=True, exist_ok=True)
        # The /data mount must wait for provisioning; if provisioning fails,
        # the data partition stays unmounted (every entry is nofail) and the
        # system still boots to a reachable state - provisioning failure is
        # never allowed to isolate emergency.target.
        # A failed bind can leave services using the underlying empty directory.
        mcu_state_mount = self.fs / "var/lib/y4-board"
        mcu_state_mount.mkdir(mode=0o700, parents=True, exist_ok=True)
        mcu_state_mount.chmod(0o700)
        shared_mcu = self.fs / "etc/y4-board/mcu-state-shared"
        shared_mcu.parent.mkdir(parents=True, exist_ok=True)
        shared_mcu.write_text("y4-mcu-state v1\n")
        # Wait for the bind attempt without requiring it. The daemon and the
        # initializer create their own private directory if the data is missing.
        # Reset StateDirectory's implicit hard mount requirement in this image.
        for service in ("y4-board-init.service", "y4-boardd.service"):
            drop_in = self.fs / "etc/systemd/system" / (service + ".d")
            drop_in.mkdir(parents=True, exist_ok=True)
            mounts = r"var-lib-y4\x2dboard.mount"
            if service == "y4-boardd.service":
                mounts += " boot-efi.mount"
            (drop_in / "30-shared-mcu-state.conf").write_text(
                f"[Unit]\nWants={mounts}\nAfter={mounts}\n[Service]\nStateDirectory=\n")
        provision_drop = self.fs / "etc/systemd/system/data.mount.d"
        provision_drop.mkdir(parents=True, exist_ok=True)
        (provision_drop / "y4-order.conf").write_text(
            "[Unit]\nAfter=y4-data-provision.service\nRequires=y4-data-provision.service\n")
        # Maintenance (emergency/rescue) must keep remote access alive: the
        # board is headless, so a networkless maintenance mode is unreachable.
        for target in ["emergency.target", "rescue.target"]:
            drop = self.fs / ("etc/systemd/system/" + target + ".d")
            drop.mkdir(parents=True, exist_ok=True)
            (drop / "y4-remote-access.conf").write_text(
                "[Unit]\nWants=dbus.socket dbus.service NetworkManager.service sshd.service\n")
        (self.fs / "boot/efi").mkdir(parents=True, exist_ok=True)
        default_target = self.fs / "etc/systemd/system/default.target"
        if default_target.is_symlink():
            default_target.unlink()
        elif default_target.exists():
            raise ValueError("Unexpected regular systemd default.target")
        # Boot to the desktop: the product ships with the GNOME session
        # (virtual head, GPU renderer) as the default boot target.
        default_target.symlink_to("/lib/systemd/system/graphical.target")
        boot = self.fs / "boot"
        shutil.copy2(self.kernel_objects / "arch/arm64/boot/Image", boot / ("vmlinuz-" + self.kernel_version))
        shutil.copy2(self.kernel_objects / ".config", boot / ("config-" + self.kernel_version))
        shutil.copy2(self.kernel_objects / "System.map", boot / ("System.map-" + self.kernel_version))
        shutil.copy2(self.kernel_objects / ("arch/arm64/boot/dts/cix/" + self.dtb_name), boot / self.dtb_name)
        self.run("board-install", [sys.executable, self.source / "board/install.py", "--board", self.board_label,
                                   "--root", self.fs, "--build", self.board_output])
        self.run("board-depmod", ["depmod", "-b", self.fs, self.kernel_version])
        qemu = self.fs / "usr/bin/y4-build-qemu-aarch64-static"
        shutil.copy2(shutil.which("qemu-aarch64-static"), qemu)
        if not (self.fs / "usr/bin/ssh-keygen").is_file():
            raise ValueError("Debian SSH key generator missing")
        if self.kernel_bundle:
            verify_graphics(self.fs)
            self.run("debian-arm64-ldconfig", ["chroot", self.fs,
                     "/usr/bin/y4-build-qemu-aarch64-static", "/sbin/ldconfig"])
        self.run("debian-arm64-systemd", ["chroot", self.fs, "/usr/bin/y4-build-qemu-aarch64-static",
                                           "/lib/systemd/systemd", "--version"])
        self.run("debian-arm64-dpkg", ["chroot", self.fs, "/usr/bin/y4-build-qemu-aarch64-static",
                                       "/usr/bin/dpkg", "--print-architecture"])
        policy = json.loads(lock_path.read_text())
        rootfs_host_policy.apply(self.fs, policy["host_policy"])
        if self.kernel_bundle:
            self.report["host_policy"] = rootfs_host_policy.validate(self.fs, policy)
        for key in (self.fs / "etc/ssh").glob("ssh_host_*_key*"):
            key.unlink()
        self.trim_production_rootfs(["chroot", self.fs, "/usr/bin/y4-build-qemu-aarch64-static"])
        qemu.unlink()
        write_json(self.out / "ROOTFS-FINGERPRINT.json",
                   {"schema": 1, "fingerprint": self.rootfs_fingerprint()})
        self.report["components"]["debian"] = {"version": "12", "architecture": "arm64", "init": "systemd", "filesystem": "ext4",
                                                   "base": self.report["inputs"]["debian_base"], "custom_network_migration": "not performed",
                                                   "root_growth_service": "y4-root-grow.service", "root_growth_before": "sysinit.target"}
        self.record()

    def boot_images(self, package):
        layouts = {}
        for sector, name in [(512, "partition-table.img"), (4096, "partition-table-4096.img")]:
            data, meta = gpt.packed(sector, self.disk_uuid, self.part_ids)
            (package / name).write_bytes(data)
            write_json(package / (name + ".json"), meta)
            layouts[sector] = meta
        grub = self.source / "boot/grub/BOOTAA64.EFI"
        self.run("grub-efi-check", ["grub-file", "--is-arm64-efi", grub])
        self.run("grub-checksum", ["sha256sum", "--check", "BOOTAA64.EFI.sha256"], self.source / "boot/grub")
        cfg = self.out / "grub.cfg"
        # The boot FAT kernel directory is named by the delivery version, not
        # the kernel release: per-slot isolation requires distinct directory
        # names across deliveries (kernel-ota-design 3). The version stamped
        # here is the same value /etc/y4-release.json carries.
        cfg.write_text(grub_cfg_text(self.board_label, self.delivery_version, self.dtb_name,
                                     self.part_ids[1], self.part_ids[2]))
        self.run("grub-script-check", ["grub-script-check", cfg])
        # Exact grub-editenv block format: the two header lines are what
        # grub's load_env validates; anything else is rejected silently.
        # Both slots start bound to the shipped delivery version, so the
        # migration delivery boots exactly like the previous layout.
        content = ("# GRUB Environment Block\n"
                   "# WARNING: Do not edit this file by tools other than grub-editenv!!!\n")
        for key, value in [("default", "0"), ("slot", "A"),
                           ("boot_kernel_a", self.delivery_version),
                           ("boot_kernel_b", self.delivery_version),
                           ("boot_count", "0"), ("boot_ok", "1")]:
            content += key + "=" + value + "\n"
        raw = content.encode()
        if len(raw) > 1024:
            raise ValueError("grubenv overflow")
        env_file = self.out / "grubenv"
        env_file.write_bytes(raw + b"#" * (1024 - len(raw)))
        self.report["boot_images"] = {}
        for sector in (512, 4096):
            suffix = "" if sector == 512 else "-4096"
            stage = "boot" + suffix
            boot = self.out / ("boot" + suffix + ".img")
            sparse_name = "boot_sparse" + suffix + ".img"
            part = layouts[sector]["partitions"][0]
            image_bytes = (part["last_lba"] - part["first_lba"] + 1) * sector
            with boot.open("xb") as stream:
                stream.truncate(image_bytes)
            self.run(stage + "-fat", ["mkfs.vfat", "-F", "32", "-S", sector, "-s", "1", "-n", "Y4DEB_BOOT", boot])
            self.run(stage + "-directories", ["mmd", "-i", boot, "::/EFI", "::/EFI/BOOT", "::/grub",
                                              "::/kernels", "::/kernels/" + self.delivery_version])
            for index, (source, target) in enumerate([
                    (grub, "::/EFI/BOOT/BOOTAA64.EFI"),
                    (cfg, "::/grub/grub.cfg"), (cfg, "::/EFI/BOOT/grub.cfg"),
                    (env_file, "::/grub/grubenv"), (env_file, "::/EFI/BOOT/grubenv"),
                    (self.kernel_objects / "arch/arm64/boot/Image",
                     "::/kernels/" + self.delivery_version + "/Image"),
                    (self.kernel_objects / ("arch/arm64/boot/dts/cix/" + self.dtb_name),
                     "::/kernels/" + self.delivery_version + "/" + self.dtb_name)]):
                self.run(stage + "-copy-%d" % index, ["mcopy", "-i", boot, source, target])
            self.run(stage + "-fat-check", ["fsck.fat", "-n", boot])
            self.run(stage + "-sparse", ["img2simg", boot, package / sparse_name, "1048576"])
            self.report["boot_images"][str(sector)] = dict(raw_path=str(boot), image_bytes=image_bytes,
                                                         sector_bytes=sector, sparse_name=sparse_name,
                                                         filesystem_check=True)
        self.record()
        return cfg

    def package(self):
        self.verify_kernel_rootfs(self.kernel_objects / "arch/arm64/boot/Image", self.fs)
        package = self.out / "package"
        package.mkdir()
        inventory = self.out / "ROOTFS-INVENTORY.json"
        image_plan = self.out / "ROOTFS-IMAGE.json"
        image_tool = self.source / "scripts/root_image.py"
        if getattr(self, "rootfs_reused", False):
            # The staged image artifacts were copied from the referenced
            # build; verify the copy and adopt its image report instead of
            # rebuilding the ext4 image.
            self.run("rootfs-reuse-verify", ["sha256sum", "--check", "SHA256SUMS"], package)
            plan = json.loads(image_plan.read_text())
            self.report["root_image"] = plan
            self.report["root_image"]["staging_root_directory"] = self.report["staging_root_directory"]
            self.root_image_bytes = plan["image_bytes"]
            self.report["components"]["debian"]["image_bytes"] = self.root_image_bytes
            self.report["components"]["debian"]["content_fingerprint_sha256"] = plan["source_fingerprint_sha256"]
            self.report["root_image_workspace"] = {"selected": "reused", "reason": "rootfs content identical to the referenced build"}
            cfg = self.boot_images(package)
            self.finish_package(package, cfg)
            return
        self.run("rootfs-inventory", [sys.executable, image_tool, self.fs, "--inventory", inventory, "--plan", image_plan])
        self.report["root_image"] = json.loads(image_plan.read_text())
        self.report["root_image"]["staging_root_directory"] = self.report["staging_root_directory"]
        self.root_image_bytes = self.report["root_image"]["image_bytes"]
        if self.root_image_bytes >= gpt.SLOT_BYTES:
            raise ValueError("Slot image does not fit the fixed 16 GiB slot partition")
        self.report["components"]["debian"]["image_bytes"] = self.root_image_bytes
        self.report["components"]["debian"]["content_fingerprint_sha256"] = self.report["root_image"]["source_fingerprint_sha256"]
        workspace = RootImageWorkspace(self.out, self.fs, self.root_image_bytes, self.args.root_image_workspace)
        self.report["root_image_workspace"] = workspace.report
        try:
            with workspace:
                print("root-image-workspace:", workspace.report["selected"], workspace.report["reason"], flush=True)
                def create_root_image(stage):
                    with workspace.path.open("xb") as stream:
                        stream.truncate(self.root_image_bytes)
                    self.run(stage, ["mke2fs", "-q", "-t", "ext4", "-F", "-b", "4096", "-i", "16384", "-m", "1", "-L", "Y4DEB_ROOT",
                                     "-U", self.root_uuid, "-O", "^metadata_csum_seed,^orphan_file", "-d", self.fs, workspace.path])
                try:
                    create_root_image("ext4")
                except (OSError, RuntimeError) as exc:
                    if workspace.report["selected"] != "tmpfs":
                        raise
                    workspace.fallback_to_disk("RAM image creation failed; original ext4 log retained: " + str(exc))
                    print("root-image-workspace: retrying on disk", flush=True)
                    create_root_image("ext4-disk-retry")
                self.run("rootfs-timestamps", [sys.executable, image_tool, "--image", workspace.path,
                         "--inventory", inventory, "--restore-mtimes", "--mountpoint", self.out / "rootfs-timestamps-mount",
                         "--report", self.out / "rootfs-timestamps.json"])
                workspace.capture_usage()
                self.run("ext4-check", ["e2fsck", "-fn", workspace.path])
                self.run("rootfs-content-verification", [sys.executable, image_tool, "--image", workspace.path,
                         "--inventory", inventory, "--verify", "--mountpoint", self.out / "rootfs-verification-mount",
                         "--report", self.out / "rootfs-content-verification.json"])
                self.report["root_image"]["verification"] = json.loads((self.out / "rootfs-content-verification.json").read_text())
                write_json(image_plan, self.report["root_image"])
                self.run("root-sparse", [sys.executable, self.source / "scripts/sparse_image.py", workspace.path,
                                         package / "rootfs_sparse.ext4", "--report", self.out / "root-sparse.json"])
        finally:
            self.record()
        cfg = self.boot_images(package)
        self.finish_package(package, cfg)

    def finish_package(self, package, cfg):
        (package / "mcu").mkdir()
        for suffix in (("bin", "hex") if self.board_label == "Y4" else ("bin",)):
            name = f"{self.board_label.lower()}-mcu.{suffix}"
            shutil.copy2(self.source / "mcu/firmware/build" / name, package / "mcu" / name)
        shutil.copy2(cfg, package / "grub.cfg")
        # The stamped release manifest beside the artifacts is the packing
        # gate's reference: ota_package.py verifies the OTA version and the
        # kernel release against it before signing (kernel-ota-design 4.4).
        shutil.copy2(self.fs / "etc/y4-release.json", package / "y4-release.json")
        (package / "tools").mkdir()
        for name in ["fastboot.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"]:
            source = self.input("windows_tools") / name
            shutil.copy2(source, package / "tools" / name)
        (package / "README.txt").write_text(
            "Internal build staging and audit inputs. Customer packages are in delivery/.\n")
        hashing_start = time.monotonic()
        manifest = {"schema": 1, "profile": "debian", "source_commit": self.commit,
                    "hardware": self.config["hardware"], "board_verified": False,
                    "components": self.report["components"], "root_uuid": str(self.root_uuid),
                    "disk_uuid": str(self.disk_uuid), "root_image": self.report["root_image"], "files": {str(p.relative_to(package)): item(p)
                    for p in sorted(package.rglob("*")) if p.is_file()}}
        write_json(package / "MANIFEST.json", manifest)
        self.report["timings_seconds"]["package_manifest_hashing"] = time.monotonic() - hashing_start
        checksum_files = dict(manifest["files"], **{"MANIFEST.json": item(package / "MANIFEST.json")})
        (package / "SHA256SUMS").write_text("".join(checksum_files[name]["sha256"] + "  " + name + "\n"
                                                   for name in sorted(checksum_files)))
        self.run("package-checksum", ["sha256sum", "--check", "SHA256SUMS"], package)
        self.report["package"] = {"directory": str(package), "manifest": checksum_files["MANIFEST.json"]}
        self.record()
        if self.args.spi_bin:
            print("standard delivery", flush=True)
            delivery = create_full(package, self.source / "scripts/flash", self.out / "delivery",
                                   self.args.jobs, spi_bin=self.args.spi_bin)
            self.report["delivery"] = delivery
            self.report["archive"] = delivery["archive"]
            self.report["timings_seconds"]["archive_sha256"] = delivery["archive"]["sha256_seconds"]
        else:
            self.report["scope"] = "system"
        self.report["complete"] = True
        self.report["completed_at"] = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
        self.record()

    def build(self):
        self.report["timings_seconds"] = {}
        start = time.monotonic()
        self.preflight()
        self.report["timings_seconds"]["preflight"] = time.monotonic() - start
        start = time.monotonic()
        self.snapshot()
        self.report["timings_seconds"]["source_snapshot"] = time.monotonic() - start
        self.record()
        candidate_image = None
        if self.kernel_bundle:
            candidate_image = self.kernel_bundle / "Image"
        elif self.args.reuse_components:
            candidate_image = self.args.reuse_components / "kernel/objects/arch/arm64/boot/Image"
        if self.args.component != "components":
            self.verify_kernel_rootfs(candidate_image)
        self.disk_uuid = uuid.uuid5(uuid.NAMESPACE_URL, "MYT/Y4/Debian/" + self.commit + "/" + self.out.name)
        # Partition identifiers are fixed build-independent constants: grub.cfg,
        # fstab and the OTA agent must address the same partitions in every
        # image ever flashed, so they cannot derive from the per-build disk GUID.
        part_namespace = uuid.uuid5(uuid.NAMESPACE_URL, "MYT/Y4/Debian/partitions-v1")
        self.part_ids = tuple(uuid.uuid5(part_namespace, label)
                              for label in ("boot", "slot-a", "slot-b", "data"))
        # Both slots carry the same fixed ext4 UUID (the identical image is
        # flashed to each); mounting is by PARTUUID only, never by filesystem
        # UUID, so the duplicate is inert.
        self.root_uuid = uuid.uuid5(part_namespace, "ext4-slot")
        if self.repackage_from:
            start = time.monotonic()
            self.repackage()
            self.report["timings_seconds"]["repackage"] = time.monotonic() - start
            self.record()
            return
        if self.args.reuse_components:
            start = time.monotonic()
            self.reuse_components(self.args.reuse_components)
            self.report["timings_seconds"]["hardware_reuse_verification"] = time.monotonic() - start
        else:
            # The three hardware components are independent of each other;
            # build them concurrently and let board (which consumes kernel
            # objects and the MCU image) run after all of them succeed.
            failures = []
            def run_component(name, method):
                try:
                    method()
                except BaseException as exc:
                    failures.append((name, exc))
            threads = [threading.Thread(target=run_component, args=("mcu", self.mcu)),
                       threading.Thread(target=run_component, args=("kernel", self.kernel))]
            for t in threads: t.start()
            for t in threads: t.join()
            if failures:
                raise failures[0][1]
        self.board()
        if self.args.component == "components":
            self.report["complete"] = True
            self.report["scope"] = "components"
            self.record()
            return
        start = time.monotonic()
        commands_before = len(self.report["commands"])
        self.filesystem()
        elapsed = time.monotonic() - start
        self.report["timings_seconds"]["filesystem_setup"] = max(0, elapsed - sum(
            row["seconds"] for row in self.report["commands"][commands_before:]))
        start = time.monotonic()
        commands_before = len(self.report["commands"])
        self.package()
        elapsed = time.monotonic() - start
        known = sum(row["seconds"] for row in self.report["commands"][commands_before:])
        known += self.report.get("archive", {}).get("seconds", 0)
        known += self.report["timings_seconds"]["package_manifest_hashing"] + self.report["timings_seconds"].get("archive_sha256", 0)
        self.report["timings_seconds"]["package_other"] = max(0, elapsed - known)
        self.record()


def use_bundled_components(args, board, scratch):
    """Use approved inputs from the same Git commit as the build sources."""
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.ref + "^{commit}"], cwd=ROOT, text=True,
    ).strip()
    present = subprocess.run(["git", "cat-file", "-e", commit + ":prebuilt"], cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if present.returncode:
        return  # Older source pins may still use externally supplied assets.
    args.ref = commit
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "archive", commit, "prebuilt/manifest.json",
                        "prebuilt/panel.json", "prebuilt/myt_desktop.gz", "prebuilt/" + board],
                       cwd=ROOT, stdout=archive, check=True)
        archive.seek(0)
        subprocess.run(["tar", "-xf", "-", "-C", scratch], stdin=archive, check=True)

    bundle = scratch / "prebuilt"
    index = json.loads((bundle / "manifest.json").read_text())
    if type(index.get("schema")) is not int or index["schema"] != 1:
        raise ValueError("Unsupported bundled build input manifest")
    selected = index["boards"][board]
    args.mcu_assets = bundle / board / "mcu"
    args.mcu_manifest_sha256 = selected["mcu_manifest_sha256"]
    spec = importlib.util.spec_from_file_location("mcu_assets", ROOT / "board/mcu_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.load_assets(args.mcu_assets, board, args.mcu_manifest_sha256)

    if not args.system_only and args.component in ("all", "check"):
        args.spi_bin = bundle / board / "myt-spi.bin"
        if sha(args.spi_bin) != selected["spi_sha256"]:
            raise ValueError(f"{board} bundled SPI SHA256 mismatch")

    args.panel_manifest = bundle / "panel.json"
    compressed = bundle / "myt_desktop.gz"
    if (sha(args.panel_manifest) != index["panel"]["manifest_sha256"]
            or sha(compressed) != index["panel"]["gzip_sha256"]):
        raise ValueError("Bundled panel checksum mismatch")
    args.panel_binary = bundle / "myt_desktop"
    with gzip.open(compressed, "rb") as source, args.panel_binary.open("xb") as output:
        shutil.copyfileobj(source, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", nargs="?", default="all", type=str.lower, choices=["all", "check", "board", "components"])
    spi = parser.add_mutually_exclusive_group()
    spi.add_argument("--spi-bin", type=Path, help="Override the board SPI BIN for a full Fastboot ZIP")
    spi.add_argument("--system-only", action="store_true", help="Build without a Fastboot SPI payload")
    parser.add_argument("--mcu-assets", type=Path)
    parser.add_argument("--mcu-manifest-sha256")
    parser.add_argument("--panel-binary", type=Path)
    parser.add_argument("--panel-manifest", type=Path)
    parser.add_argument("--private-components", type=Path)
    parser.add_argument("--component-output", type=Path)
    parser.add_argument("--version", help="Explicit delivery version, YYYYMMDD-rNN")
    parser.add_argument("--board", type=str.upper, choices=BOARD_PROFILES)
    parser.add_argument("--config", type=Path,
                        help="Override MYT_BUILD_CONFIG or the default scripts/build.json")
    parser.add_argument("--output", type=Path, default=ROOT.parent / "builds" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--jobs", type=int, default=32)
    parser.add_argument("--root-image-workspace", choices=["auto", "disk"], default="auto",
                        help="Use a bounded temporary RAM image when sufficient memory is available; disk forces the original path")
    parser.add_argument("--plan", action="store_true", help="Print board component selection without validating build inputs")
    parser.add_argument("--reuse-components", type=Path,
                        help="Reuse a verified hardware build only if source/config objects and input/artifact hashes match")
    parser.add_argument("--kernel-bundle", type=Path,
                        help="Adopt a verified prebuilt kernel bundle (Image/DTB/modules tarball + SHA256SUMS) instead of the source kernel build")
    parser.add_argument("--pg-from", type=Path,
                        help="Previous build directory providing the 6.1 kernel objects/source for the out-of-tree PG module (required with --kernel-bundle)")
    parser.add_argument("--kernel-source-build", action="store_true",
                        help="Force the 6.1 source kernel build instead of the configured prebuilt 6.6 bundle")
    parser.add_argument("--repackage-from", type=Path,
                        help="Kernel-only re-delivery: splice --kernel-bundle into this complete previous build's staged tree and repackage, skipping all component builds")
    parser.add_argument("--fresh-apt", action="store_true",
                        help="Use a separate empty download cache for this build; bootstrap always installs a new filesystem")
    parser.add_argument("--reuse-rootfs", type=Path,
                        help="Reuse the staged root image artifacts of this previous build when the rootfs content fingerprint (base+overlay+board package+scripts) matches; apt upgrade runs only on a fresh build")
    parser.add_argument("--board-mcu-source", type=Path)
    parser.add_argument("--board-kernel-objects", type=Path,
                        help="Exact existing kernel objects for compile-only board/PG build")
    parser.add_argument("--board-kernel-source", type=Path,
                        help="Matching kernel source for private external-module preparation")
    parser.add_argument("--board-pg-source", type=Path,
                        help="Pinned vendor Realtek PG input directory for compile-only build")
    args = parser.parse_args()
    if args.config is None and os.environ.get("MYT_BUILD_CONFIG"):
        args.config = Path(os.environ["MYT_BUILD_CONFIG"])
    explicit_config = args.config is not None
    if not explicit_config:
        args.config = ROOT / "scripts" / "build.json"
    if args.plan and args.board and not explicit_config and not args.config.exists():
        config = {"schema": 1}
    else:
        try:
            config = json.loads(args.config.read_text())
        except FileNotFoundError:
            parser.error(f"Build configuration does not exist: {args.config}")
    resolved_config = board_config(config, args.board)
    selected = resolve_board(resolved_config, args.board)
    label = selected[0]
    if args.plan:
        print(json.dumps({
            "component": args.component,
            "selection": board_selection(*selected),
            "image_build_requires_qualified_kernel": label == "Y12",
            "input_checks_executed": False,
            "hardware_access": False,
        }, indent=2))
        return
    if args.component == "board":
        if args.private_components or args.component_output:
            parser.error("Run private_components.py before a board-only build")
        command = [sys.executable, str(ROOT / "board/build.py"),
                   "--config", str(args.config.resolve()), "--board", label,
                   "--output", str(args.output),
                   "--jobs", str(args.jobs)]
        for option, value in [("--mcu-source", args.board_mcu_source),
                              ("--mcu-assets", args.mcu_assets),
                              ("--mcu-assets-manifest-sha256", args.mcu_manifest_sha256),
                              ("--kernel-objects", args.board_kernel_objects),
                              ("--kernel-source", args.board_kernel_source),
                              ("--realtek-pg", args.board_pg_source)]:
            if value is not None:
                command += [option, str(value)]
        raise SystemExit(subprocess.run(command).returncode)
    if args.component == "components" and args.repackage_from:
        parser.error("components does not accept --repackage-from")
    publish_to = None
    explicit = (args.spi_bin, args.mcu_assets, args.mcu_manifest_sha256,
                args.panel_binary, args.panel_manifest)
    if args.private_components:
        if args.component == "check" or not args.component_output:
            parser.error("Private component builds require all/components and --component-output")
        if any(explicit):
            parser.error("Choose private builds or explicit component inputs")
        if args.component == "all" and not args.system_only:
            private_config = json.loads(args.private_components.read_text())
            destination = private_config.get("publish_prebuilt_to")
            if destination is not None:
                if not isinstance(destination, str) or not destination:
                    parser.error("publish_prebuilt_to must name a local myt-edge checkout")
                publish_to = (args.private_components.resolve().parent / destination).resolve(strict=True)
        subprocess.run([sys.executable, str(ROOT / "scripts/private_components.py"),
                        "--board", label, "--config", str(args.private_components.resolve()),
                        "--output", str(args.component_output.resolve()), "--jobs", str(args.jobs)], check=True)
        args.mcu_assets = args.component_output / "mcu"
        args.mcu_manifest_sha256 = sha(args.mcu_assets / "manifest.json")
        args.panel_binary = args.component_output / "myt_desktop"
        args.panel_manifest = args.component_output / "panel.json"
        if args.component == "all" and not args.system_only:
            args.spi_bin = args.component_output / "myt-spi.bin"
    elif args.component_output:
        parser.error("--component-output requires --private-components")

    with ExitStack() as stack:
        manual = resolved_config.get("inputs", {})
        if (not args.private_components and not any(explicit)
                and not ({"mcu_assets", "mcu_vendor", "panel_binary"} & manual.keys())
                and not (args.repackage_from or args.reuse_components)):
            scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="myt-prebuilt-")))
            use_bundled_components(args, label, scratch)
        run_build(args, publish_to)


def run_build(args, publish_to):
    builder = Builder(args)
    if args.component == "check":
        print(json.dumps(builder.preflight(), indent=2))
        return
    try:
        builder.build()
        if publish_to is not None:
            from publish_prebuilt import publish
            verification = builder.out / "RELEASE-VERIFICATION.json"
            builder.run("verify-release-before-publish",
                        [sys.executable, ROOT / "scripts/verify_release.py", "--build",
                         builder.out, "--report", verification])
            result = json.loads(verification.read_text())
            if not (result["offline_complete"] and result["full_zip_payloads_verified"]):
                raise ValueError("Prebuilt inputs require a verified full Fastboot package")
            print("publish-prebuilt", flush=True)
            revision = publish(publish_to, args.component_output, builder.board_label,
                               builder.delivery_version)
            builder.report["published_prebuilt_commit"] = revision
            builder.record()
    except Exception as exc:
        if getattr(builder, "started", False):
            builder.report["failure"] = str(exc)
            builder.record()
        raise
    print(json.dumps({"complete": True, "source_commit": builder.commit, "output": str(builder.out),
                      "scope": args.component, "archive": builder.report.get("archive"), "board_verified": False}, indent=2))


if __name__ == "__main__":
    main()
