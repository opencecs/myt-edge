#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Install y4-board into an explicit disposable Debian12 image filesystem.
Uses host dpkg metadata operations only. No target binary, maintainer script,
service or hardware operation is run. This is not a live-host installer.
"""
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time

BOARD = Path(__file__).resolve().parent
SOURCE = BOARD.parent
MAINTAINER_FILES = {"preinst", "postinst", "prerm", "postrm", "triggers", "config"}

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def checked_root(root):
    root = Path(root).resolve()
    if root == Path("/") or root == SOURCE or SOURCE in root.parents or root in SOURCE.parents:
        raise ValueError("independent disposable image root required")
    release = root / "etc/os-release"
    if not release.is_file() or root not in release.resolve().parents:
        raise ValueError("image OS identity is unavailable or escapes staging root")
    text = release.read_text()
    if not re.search(r'^ID=(?:"debian"|debian)$', text, re.M) or not re.search(r'^VERSION_ID="?12"?$', text, re.M):
        raise ValueError("Debian12 image root required")
    status = root / "var/lib/dpkg/status"
    if not status.is_file() or root not in status.resolve().parents:
        raise ValueError("an existing Debian package database is required")
    for name in MAINTAINER_FILES:
        for pattern in ("y4-board." + name, "y4-board:*." + name):
            for path in (root / "var/lib/dpkg/info").glob(pattern):
                if path.exists() or path.is_symlink():
                    raise ValueError("existing board maintainer actions must be reviewed before image upgrade: " + str(path))
    return root

def check_no_maintainer_actions(package, packaging):
    """Inspect Debian control archive bytes without invoking a target program."""
    raw = packaging.read_regular(package)
    if not raw.startswith(b"!<arch>\n"):
        raise ValueError("invalid Debian archive")
    at = 8
    found = False
    while at < len(raw):
        if len(raw) - at < 60 or raw[at + 58:at + 60] != b"\x60\n":
            raise ValueError("invalid Debian ar member header")
        name = raw[at:at + 16].decode("ascii").strip().rstrip("/")
        size_text = raw[at + 48:at + 58].decode("ascii").strip()
        if not size_text.isdigit():
            raise ValueError("invalid Debian ar member size")
        size = int(size_text)
        at += 60
        if size > len(raw) - at:
            raise ValueError("truncated Debian archive")
        if name.startswith("control.tar"):
            if found or size > 16 * 1024 * 1024:
                raise ValueError("invalid or duplicate control archive")
            found = True
            with tarfile.open(fileobj=io.BytesIO(raw[at:at + size]), mode="r:*") as archive:
                count = 0
                for entry in archive:
                    count += 1
                    rel = Path(entry.name)
                    if count > 64 or rel.is_absolute() or ".." in rel.parts or entry.issym() or entry.islnk():
                        raise ValueError("unexpected control archive contents")
                    if rel.name in MAINTAINER_FILES:
                        raise ValueError("board package must not execute maintainer scripts or triggers")
                    if entry.size > 4 * 1024 * 1024:
                        raise ValueError("oversized control file")
        at += size + size % 2
    if not found:
        raise ValueError("missing Debian control archive")

def _inside(root, path):
    parent = path.parent.resolve()
    if parent != root and root not in parent.parents:
        raise ValueError("image file operation escapes staging root")

def _enable(root, unit):
    directory = root / "etc/systemd/system/multi-user.target.wants"
    _inside(root, directory / unit)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / unit
    target = "/usr/lib/systemd/system/" + unit
    if path.is_symlink():
        if str(path.readlink()) == target:
            return
        raise ValueError("unexpected board service enablement link")
    if path.exists():
        raise ValueError("unexpected regular board service enablement file")
    path.symlink_to(target)

def _mask_legacy(root, units):
    masked, disabled = [], []
    for directory in (root / "etc/systemd/system").glob("*"):
        if not directory.name.endswith((".wants", ".requires")):
            continue
        for name in units:
            path = directory / name
            _inside(root, path)
            if path.is_symlink():
                path.unlink()
                disabled.append(str(path.relative_to(root)))
    for name in units:
        path = root / "etc/systemd/system" / name
        _inside(root, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            if str(path.readlink()) == "/dev/null":
                masked.append(name)
                continue
            path.unlink()
        elif path.exists():
            raise ValueError("legacy unit mask would overwrite a regular administrator unit: " + str(path))
        path.symlink_to("/dev/null")
        masked.append(name)
    return {"masked": masked, "disabled_enablement_links": disabled}

def install(build, root, board="Y4"):
    root = checked_root(root)
    packaging = load(BOARD / "package.py", "y4_board_packaging")
    network = load(BOARD / "network/stage.py", "y4_board_network_staging")
    verified = packaging.verify_build(build)
    if verified["report"].get("board", "Y4") != board:
        raise ValueError("Board component target does not match the image target")
    build = verified["build"]
    if root == build or root in build.parents:
        raise ValueError("component build and disposable root must be separate")
    pg = verified["pg"]
    kernel = pg["kernel_release"] if pg else "not-required"
    # Kernel-bundle builds ship the deb with pgdrv.ko omitted (the image
    # runs a different kernel than the 6.1 PG cohort); the deb receipt
    # carries the marker, and the 6.1 module-cohort checks do not apply.
    pg_module = "omitted-read-only" if pg is None else None
    deb_receipt_path = build / "y4-board.deb.json"
    if deb_receipt_path.is_file():
        deb_receipt = json.loads(packaging.read_regular(deb_receipt_path, 262144))
        pg_module = deb_receipt.get("pg_module")
        if ((pg is None) != (pg_module == "omitted-read-only")
                or pg_module not in (None, "omitted-read-only", "omitted-foreign-kernel")):
            raise ValueError("Debian package receipt PG mode differs from the verified build")
    if pg_module:
        if not any((root / "lib/modules").glob("*")) or not any((root / "boot").glob("config-*")):
            raise ValueError("image lacks a kernel module tree/configuration for the bundled kernel")
    else:
        module_directory = root / "lib/modules" / kernel
        if not module_directory.is_dir() or root not in module_directory.resolve().parents:
            raise ValueError("target kernel module tree does not match the PG cohort")
        kernel_config = root / "boot" / ("config-" + kernel)
        expected_config = pg["metadata"].get("kernel_config_sha256")
        if not kernel_config.is_file() or packaging.sha(packaging.read_regular(kernel_config)) != expected_config:
            raise ValueError("install after the exact target boot/config has been staged; PG kernel configuration differs")
    package = build / "y4-board.deb"
    package_record = verified["report"].get("artifacts", {}).get("y4-board.deb")
    if package_record and package_record.get("path") == "y4-board.deb":
        packaging.verify_record(package, package_record)
        receipt = json.loads(packaging.read_regular(build / "y4-board.deb.json", 262144))
        if receipt.get("board", "Y4") != board:
            raise ValueError("Debian package receipt target does not match the image target")
        if receipt.get("source_fingerprint") != verified["source_fingerprint"] or receipt.get("kernel_release") != kernel or receipt.get("package_version") != verified["version"]:
            raise ValueError("Debian package and verified source/kernel cohort differ")
    else:
        if package.exists() or package.is_symlink():
            raise ValueError("unrecorded Debian package exists; preserve it and create a fresh component build")
        packaging.build_package(build, package)
    check_no_maintainer_actions(package, packaging)
    # All image-conflict decisions run before dpkg touches the tree; an
    # installation must not abort after unpack or configure side effects.
    network.precheck(root, packaging.LEGACY_UNITS)
    work = Path(tempfile.mkdtemp(prefix="image-install-", dir=build))
    result = {"schema": 1, "root": str(root), "package": str(package), "kernel_release": kernel,
              "tests_executed": False, "target_programs_executed": False,
              "services_executed": False, "hardware_accessed": False, "commands": []}
    if pg_module:
        result["pg_module"] = pg_module
    def record():
        (work / "INSTALL.json").write_text(json.dumps(result, indent=2) + "\n")
    def run(name, args):
        start = time.monotonic()
        env = dict(os.environ, LC_ALL="C", DEBIAN_FRONTEND="noninteractive")
        child = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        (work / (name + ".log")).write_text(child.stdout)
        result["commands"].append({"stage": name, "command": args, "exit": child.returncode,
                                   "seconds": round(time.monotonic() - start, 3)})
        record()
        if child.returncode:
            raise RuntimeError(name + " failed; original image and logs retained: " + str(work))
    try:
        base_args = ["dpkg", "--root=" + str(root), "--force-architecture", "--force-confdef", "--force-confold", "--no-triggers"]
        run("unpack", base_args + ["--unpack", str(package)])
        run("configure", base_args + ["--configure", packaging.PACKAGE_NAME])
        # dpkg owns payload/conffiles. This step only performs SHA-pinned
        # vendor migration and image enablement, not an overwrite of the
        # administrator conffiles which dpkg just preserved.
        result["network_staging"] = network.stage_network(root, install_assets=False, board=board)
        result["legacy_services"] = _mask_legacy(root, packaging.LEGACY_UNITS)
        for unit in packaging.ENABLED_UNITS:
            _enable(root, unit)
        # Host depmod reads ELF data and writes the image's module metadata.
        # It never loads the ARM64 module or invokes a target executable.
        # With the PG module omitted there is nothing to index for the 6.1
        # cohort; the image kernel's depmod is run by the image build.
        if not pg_module:
            run("depmod", ["depmod", "-b", str(root), kernel])
        result["complete"] = True
        record()
        return result
    except Exception as exc:
        result["complete"] = False
        result["failure"] = str(exc)
        record()
        raise

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--build", required=True, type=Path)
    parser.add_argument("--board", choices=("Y4", "Y12"), default="Y4")
    args = parser.parse_args()
    print(json.dumps(install(args.build, args.root, args.board), indent=2))

if __name__ == "__main__":
    main()
