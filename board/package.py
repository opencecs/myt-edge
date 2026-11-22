#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build an installable ARM64 Y4 board .deb from fully verified release inputs.
This tool never executes target binaries, services, PG commands or hardware IO.
A protocol-compatible signed MCU envelope is mandatory. If PG is included, verify its exact kernel cohort.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile

BOARD = Path(__file__).resolve().parent
SOURCE = BOARD.parent
PACKAGE_NAME = "y4-board"
PROGRAMS = {
    "y4-boardd": ("arm64/daemon/y4-boardd", "usr/sbin/y4-boardd"),
    "y4-network-policy": ("arm64/network/y4-network-policy", "usr/libexec/y4-board/y4-network-policy"),
    "y4-nic-factory": ("arm64/nic/y4-nic-factory", "usr/libexec/y4-board/y4-nic-factory"),
    "y4-mcu-maintain": ("arm64/mcu-maintain/y4-mcu-maintain", "usr/libexec/y4-board/y4-mcu-maintain"),
    "y4-board-boot-helper": ("arm64/boot/y4-board-boot-helper", "usr/libexec/y4-board/y4-board-boot-helper"),
}
LEGACY_UNITS = (
    "mcu-daemon.service", "mcu-daemon-y4.service", "mcu-flash.service",
    "mcu-boot.service", "y4-mcud.service", "y4-mcu-boot.service",
    "y4-app.service", "y4-netd.service",
)
ENABLED_UNITS = ("y4-board-init.service", "y4-boardd.service", "y4-network-policy.service")
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
KERNEL = re.compile(r"6\.1\.44-cix-[A-Za-z0-9_.+-]+\Z")

def sha(data):
    return hashlib.sha256(data).hexdigest()

def read_regular(path, maximum=64 * 1024 * 1024):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("regular input required: " + str(path))
    before = path.stat()
    if not 0 <= before.st_size <= maximum:
        raise ValueError("input size out of range: " + str(path))
    data = path.read_bytes()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("input changed while reading: " + str(path))
    return data

def relative_file(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError("relative artifact path required")
    path = root / rel
    if root not in path.resolve().parents:
        raise ValueError("artifact escapes build directory: " + relative)
    return path

def verify_record(path, record):
    if not isinstance(record, dict) or not isinstance(record.get("bytes"), int) or not SHA256.fullmatch(str(record.get("sha256", ""))):
        raise ValueError("incomplete artifact digest record: " + str(path))
    data = read_regular(path)
    if len(data) != record["bytes"] or sha(data) != record["sha256"]:
        raise ValueError("artifact checksum mismatch: " + str(path))
    return data

def artifact(build, report, key, relative):
    row = report.get("artifacts", {}).get(key)
    if not row or row.get("path") != relative:
        raise ValueError("missing exact artifact record: " + key)
    return verify_record(relative_file(build, relative), row)

def elf(data, machine, types):
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("little-endian ELF64 artifact required")
    kind, observed = struct.unpack_from("<HH", data, 16)
    if observed != machine or kind not in types:
        raise ValueError("wrong ELF architecture/type")

def version(text):
    if not isinstance(text, str) or not SEMVER.fullmatch(text):
        raise ValueError("canonical three-part version required")
    values = tuple(map(int, text.split(".")))
    if any(value > 65535 for value in values):
        raise ValueError("version component out of range")
    return values

def source_file(relative, report):
    row = report.get("source_files", {}).get(relative)
    if not row:
        raise ValueError("source was not captured by component build: " + relative)
    return verify_record(relative_file(SOURCE, relative), row)

def verify_sources(report):
    rows = report.get("source_files")
    if not isinstance(rows, dict) or not rows:
        raise ValueError("source fingerprint manifest required")
    for relative, row in rows.items():
        verify_record(relative_file(SOURCE, relative), row)

def verify_signed_mcu(build, report):
    """Verify the same canonical MCU1 Ed25519 envelope as the target maintainer."""
    signed = report.get("signed_mcu", {})
    if signed.get("signed") is not True or signed.get("protocol") != 2:
        raise ValueError("configured production-signed MCU package required")
    board = report.get("board", "Y4")
    if board not in ("Y4", "Y12"):
        raise ValueError("Unknown MCU build board")
    image_name = f"mcu/{board.lower()}-mcu.bin"
    image = artifact(build, report, image_name, image_name)
    prebuilt = report.get("mcu_build", {}).get("mode") == "prebuilt"
    if board == "Y4":
        header = source_file("mcu/include/protocol.h", report).decode("utf8")
    elif prebuilt:
        header = source_file("mcu/include/y12_power_wire.h", report).decode("utf8")
    else:
        header_path = "mcu-source/include/y12_power_wire.h"
        header = artifact(build, report, header_path, header_path).decode("utf8")
    expected = ".".join(re.search(rf"^#define {board}_FW_{name} ([0-9]+)$", header, re.M).group(1)
                        for name in ("MAJOR", "MINOR", "PATCH"))
    if (board == "Y4" and version(expected) < (0, 1, 34)) or report.get("mcu_version") != expected or signed.get("version") != expected:
        raise ValueError("MCU protocol and signed firmware version cohort mismatch")
    package_path = relative_file(build, "mcu/mcu-package.bin")
    key_path = relative_file(build, "mcu/mcu-public-key.bin")
    builtin_path = relative_file(build, "mcu/mcu-builtin.json")
    host_path = relative_file(build, "mcu/host-version")
    package = read_regular(package_path, 1049736)
    key = read_regular(key_path, 32)
    builtin = json.loads(read_regular(builtin_path, 16384))
    host = read_regular(host_path, 128).decode("ascii").strip()
    host_version = version(host)
    if len(key) != 32 or key in (bytes(32), bytes([255]) * 32):
        raise ValueError("nonblank raw32-byte MCU trust public key required")
    if len(package) < 72 + 64 + 64:
        raise ValueError("truncated MCU signed envelope")
    magic, fmt, header_size, kind, manifest_size, signature_size, flags, image_size, reserved, image_hash = struct.unpack_from("<8sHHIIHHQQ32s", package)
    if (magic, fmt, header_size, kind, signature_size, flags, reserved) != (b"Y4UPKG1\0", 1, 72, 2, 64, 0, 0):
        raise ValueError("unsupported MCU signed envelope")
    if not 64 <= image_size <= 1048576 or not 1 <= manifest_size <= 1024 or len(package) != 72 + manifest_size + 64 + image_size:
        raise ValueError("MCU envelope lengths invalid")
    manifest_raw = package[72:72 + manifest_size]
    manifest = manifest_raw.decode("ascii")
    if not manifest.startswith("Y4MCU1\n") or not manifest.endswith("\n"):
        raise ValueError("MCU manifest framing invalid")
    fields = {}
    for line in manifest.splitlines()[1:]:
        if line.count("=") != 1:
            raise ValueError("MCU manifest field framing invalid")
        name, value = line.split("=", 1)
        if name in fields:
            raise ValueError("duplicate MCU manifest field")
        fields[name] = value
    if set(fields) != {"board", "soc", "mcu", "protocol", "version", "host-min", "host-max", "sequence", "image-bytes", "sha256"}:
        raise ValueError("unknown or missing MCU manifest field")
    if (fields["board"], fields["soc"], fields["mcu"], fields["protocol"], fields["version"]) != (
            f"MYT-AI-NAS-{board}", "6187", "GD32F407", "2", expected):
        raise ValueError("MCU manifest target mismatch")
    if not fields["sequence"].isdigit() or not fields["image-bytes"].isdigit():
        raise ValueError("MCU manifest counters invalid")
    sequence = int(fields["sequence"])
    if not 1 <= sequence < 2 ** 64 or str(sequence) != fields["sequence"] or str(image_size) != fields["image-bytes"]:
        raise ValueError("MCU manifest canonical counters invalid")
    if not version(fields["host-min"]) <= host_version <= version(fields["host-max"]):
        raise ValueError("host version outside signed MCU compatibility range")
    signature = package[72 + manifest_size:72 + manifest_size + 64]
    payload = package[72 + manifest_size + 64:]
    if payload != image or image_hash.hex() != sha(image) or fields["sha256"] != sha(image):
        raise ValueError("signed MCU payload and compiled image differ")
    stack, reset = struct.unpack_from("<II", image)
    if stack & 7 or not 0x20000000 < stack <= 0x20020000 or not reset & 1 or not 0x08000008 <= reset & ~1 < 0x08000000 + len(image):
        raise ValueError("GD32F407 firmware vectors invalid")
    # Build-host verification only; this does not run the target update program.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(key).verify(signature, package[:72] + manifest_raw)
    expected_meta = {
        "object": "MCU", "version": expected, "sequence": sequence,
        "image_bytes": len(image), "image_sha256": sha(image),
        "package_bytes": len(package), "package_sha256": sha(package),
        "host_min": fields["host-min"], "host_max": fields["host-max"],
        "protocol": 2, "signed": True, "flashed": False,
    }
    if board == "Y12":
        expected_meta["board"] = board
    for name, value in expected_meta.items():
        if signed.get(name) != value or builtin.get(name) != value:
            raise ValueError("signed MCU metadata mismatch: " + name)
    if builtin.get("schema") != 1 or builtin.get("sha256") != sha(package):
        raise ValueError("builtin MCU descriptor hash/schema mismatch")
    # All installed signed assets are also bound to the component build's
    # explicit bytes/SHA256 records, including its configured trust public key.
    for relative in ("mcu/mcu-package.bin", "mcu/mcu-public-key.bin", "mcu/mcu-builtin.json", "mcu/host-version"):
        artifact(build, report, relative, relative)
    normalized = dict(expected_meta, schema=1, sha256=sha(package))
    return {"package": package, "public_key": key, "builtin": (json.dumps(normalized, indent=2) + "\n").encode(),
            "host": (host + "\n").encode(), "host_version": host, "metadata": expected_meta}

def verify_pg(build, report):
    meta = report.get("realtek_pg", {})
    kernel = meta.get("kernel_release", "")
    if not KERNEL.fullmatch(kernel) or not str(meta.get("vermagic", "")).startswith(kernel + " ") or "aarch64" not in meta.get("vermagic", ""):
        raise ValueError("exact CIX6.1.44 ARM64 PG module cohort required")
    if meta.get("binary_dynamic_dependencies") != [] or meta.get("module_license") != "GPL":
        raise ValueError("unexpected PG executable or driver contract")
    recorded = json.loads(read_regular(relative_file(build, "pg/BUILD.json"), 262144))
    if meta != recorded:
        raise ValueError("PG build metadata and board manifest differ")
    kernel_input = report.get("pg_kernel_input", {})
    if kernel_input:
        for field, name in (("kernel_config_sha256", "config"), ("kernel_symvers_sha256", "module_symvers")):
            if meta.get(field) != kernel_input.get(name, {}).get("sha256"):
                raise ValueError("PG kernel configuration/symbol CRC cohort mismatch")
    module = verify_record(relative_file(build, "pg/stage/lib/modules/pgdrv.ko"), meta.get("files", {}).get("lib/modules/pgdrv.ko"))
    program = verify_record(relative_file(build, "pg/stage/usr/libexec/opencecs/rtnicpg"), meta.get("files", {}).get("usr/libexec/opencecs/rtnicpg"))
    elf(module, 183, {1})
    elf(program, 183, {2, 3})
    pin = json.loads(source_file("board/third_party/realtek-pg/inputs.json", report))
    pg_sources = {}
    for row in pin["files"]:
        data = read_regular(relative_file(build, "pg/source/" + row["file"]))
        if len(data) != row["bytes"] or sha(data) != row["sha256"]:
            raise ValueError("PG vendor input does not match pinned source: " + row["file"])
        if row["file"] in ("pgdrv.c", "pgdrv.h", "Makefile"):
            pg_sources[row["file"]] = data
        elif row["file"] == "rtnicpg-aarch64-armv8" and data != program:
            raise ValueError("staged PG utility differs from pinned executable")
    return {"kernel_release": kernel, "module": module, "program": program,
            "metadata": meta, "sources": pg_sources}

def verify_build(build):
    build = Path(build).resolve()
    report = json.loads(read_regular(build / "BUILD.json", 16 * 1024 * 1024))
    if report.get("schema") != 1 or report.get("failure"):
        raise ValueError("successful component build manifest required")
    if not re.fullmatch(r"[0-9a-f]{40}", str(report.get("source_baseline", ""))):
        raise ValueError("source baseline commit required")
    # complete may still be false while board/build.py is making its .deb.
    for row in report.get("commands", []):
        if row.get("exit") != 0:
            raise ValueError("component build contains a failed command")
    verify_sources(report)
    programs = {}
    for name, (relative, destination) in PROGRAMS.items():
        data = artifact(build, report, "arm64/" + name, relative)
        elf(data, 183, {2, 3})
        programs[destination] = data
    mcu = verify_signed_mcu(build, report)
    pg = verify_pg(build, report) if report["realtek_pg"].get("compiled") is not False else None
    sources = dict(report["source_files"])
    if report.get("board") == "Y12" and report.get("mcu_build", {}).get("mode") != "prebuilt":
        mcu_sources = report.get("mcu_build", {}).get("source_files", {})
        if "include/y12_power_wire.h" not in mcu_sources:
            raise ValueError("Y12 MCU source fingerprint manifest required")
        for relative, row in mcu_sources.items():
            path = "mcu-source/" + relative
            verify_record(relative_file(build, path), row)
            sources[path] = row
    if report.get("mcu_build", {}).get("mode") == "prebuilt":
        for name, row in report["artifacts"].items():
            if name.startswith(("mcu/", "mcu-licenses/")) or name == "arm64/y4-mcu-maintain":
                sources["prebuilt/" + name] = row
    fingerprint = sha(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode())
    return {"build": build, "report": report, "programs": programs, "mcu": mcu, "pg": pg,
            "source_fingerprint": fingerprint, "version": mcu["host_version"] + "+y4." + fingerprint[:12]}

def _put(stage, relative, data, mode=0o644):
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("relative package path required")
    path = stage / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)

# No /etc/systemd/system payload: /etc is administrator territory and dpkg
# would overwrite local masks and enablement choices at unpack time, before
# any protection check could run. Enablement and legacy masking are
# image-staging duties of board/install.py, which validates the existing
# state (precheck) before dpkg touches the tree.

def populate(stage, verified, pg_userspace_only=False):
    report = verified["report"]
    for destination, data in verified["programs"].items():
        _put(stage, destination, data, 0o755)
    _put(stage, "usr/libexec/y4-board/y4-board-boot", source_file("board/factory/boot.py", report), 0o755)
    pg = verified["pg"]
    kernel = pg["kernel_release"] if pg else "not-required"
    pg_module_omitted = not pg or pg_userspace_only
    if pg:
        _put(stage, "usr/libexec/y4-board/rtnicpg", pg["program"], 0o700)
        if not pg_module_omitted:
            _put(stage, "lib/modules/" + kernel + "/extra/y4/pgdrv.ko", pg["module"])
    assets = verified["mcu"]
    _put(stage, "usr/share/y4-board/mcu-package.bin", assets["package"])
    _put(stage, "usr/share/y4-board/mcu-public-key.bin", assets["public_key"])
    _put(stage, "usr/share/y4-board/mcu-builtin.json", assets["builtin"])
    _put(stage, "etc/y4-board/host-version", assets["host"])
    for name in ("y4-board-init.service", "y4-boardd.service"):
        _put(stage, "usr/lib/systemd/system/" + name, source_file("board/systemd/" + name, report))
    network = BOARD / "network"
    board = report.get("board", "Y4")
    profile_path = network / "stage.py"
    spec = importlib.util.spec_from_file_location("board_network_stage", profile_path)
    network_stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(network_stage)
    for pattern, directory, mode in (
            ("profiles/*.nmconnection", "etc/NetworkManager/system-connections", 0o600),
            ("links/*.link", "usr/lib/systemd/network", 0o644)):
        for path in sorted(network.glob(pattern)):
            data = source_file(str(path.relative_to(SOURCE)), report)
            if path.suffix == ".nmconnection":
                data = network_stage.network_profile(data, board)
            else:
                data = network_stage.network_names(data, board)
            _put(stage, directory + "/" + path.name, data, mode)
    for path in sorted((network / "conf").glob("*.conf")):
        destination = "etc/sysctl.d/" if path.name == "70-y4-multihome.conf" else "etc/NetworkManager/conf.d/"
        data = source_file(str(path.relative_to(SOURCE)), report)
        _put(stage, destination + path.name, network_stage.network_names(data, board))
    for path in sorted((network / "systemd").rglob("*.conf")):
        destination = "etc/systemd/system/" + str(path.relative_to(network / "systemd"))
        _put(stage, destination, source_file(str(path.relative_to(SOURCE)), report))
    ready = network_stage.READY_UNIT
    _put(stage, "usr/lib/systemd/system/" + ready,
         source_file("board/network/systemd/" + ready, report))
    policy = source_file("board/network/systemd/y4-network-policy.service", report)
    _put(stage, "usr/lib/systemd/system/y4-network-policy.service", policy)
    # udev must not autoload the factory PG driver (it carries PCI aliases),
    # and the frozen base lacks the standard net_setup_link rule the .link
    # files depend on. Both are plain payload files in vendor locations.
    _put(stage, "etc/modprobe.d/y4-blacklist-pgdrv.conf",
         source_file("board/network/modprobe.d/y4-blacklist-pgdrv.conf", report))
    link_rule = source_file("board/network/udev/78-y4-net-setup-link.rules", report)
    _put(stage, "usr/lib/udev/rules.d/78-y4-net-setup-link.rules",
         network_stage.network_names(link_rule, board), 0o644)
    doc = "usr/share/doc/y4-board/"
    for name in ("README.Debian", "copyright", "LVGL-LICENCE.txt", "LICENSE-SOURCES.json"):
        _put(stage, doc + name, source_file("board/debian/" + name, report))
    _put(stage, doc + "networking.md", source_file("board/network/README.md", report))
    _put(stage, doc + "NOTICE", read_regular(SOURCE / "NOTICE", 1048576))
    _put(stage, doc + "Apache-2.0.txt", read_regular(SOURCE / "LICENSE", 1048576))
    if report.get("mcu_build", {}).get("mode") == "prebuilt":
        for name in sorted(report["artifacts"]):
            if re.fullmatch(r"mcu-licenses/[A-Za-z0-9_.+-]+", name):
                _put(stage, doc + name, artifact(verified["build"], report, name, name))
    else:
        mcu_licenses = (verified["build"] / "mcu-source/licenses"
                        if report.get("board") == "Y12" else SOURCE / "mcu/licenses")
        for path in sorted(mcu_licenses.iterdir()):
            if path.is_file() and not path.is_symlink():
                _put(stage, doc + "mcu-licenses/" + path.name, read_regular(path, 1048576))
    if pg:
        _put(stage, doc + "pgdrv-source/COPYING", source_file("board/debian/GPL-2.0.txt", report))
        for name, data in pg["sources"].items():
            _put(stage, doc + "pgdrv-source/" + name, data)
    receipts = {
        "schema": 1, "board": board, "package_version": verified["version"],
        "source_baseline": report["source_baseline"],
        "source_fingerprint": verified["source_fingerprint"], "kernel_release": kernel,
        "mcu": assets["metadata"], "mcu_public_key_sha256": sha(assets["public_key"]),
        "tests_executed": False, "hardware_accessed": False, "automatic_service_start_on_install": False,
    }
    if not pg:
        receipts["pg_module"] = "omitted-read-only"
    else:
        receipts["pg_program_sha256"] = sha(pg["program"])
        if pg_module_omitted:
            receipts["pg_module"] = "omitted-foreign-kernel"
        else:
            receipts["pg_module_sha256"] = sha(pg["module"])
    _put(stage, "usr/share/y4-board/package.json", (json.dumps(receipts, indent=2) + "\n").encode())
    files = {}
    for path in sorted(stage.rglob("*")):
        if path.is_file() and not path.is_symlink():
            raw = path.read_bytes()
            files[str(path.relative_to(stage))] = {"bytes": len(raw), "sha256": sha(raw), "mode": oct(path.stat().st_mode & 0o777)}
    _put(stage, "usr/share/y4-board/files.json", (json.dumps(files, indent=2) + "\n").encode())
    return receipts

def build_package(build, output, pg_userspace_only=False):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("new .deb output required")
    if output.resolve() == SOURCE or SOURCE in output.resolve().parents:
        raise ValueError("package output must stay outside source checkout")
    verified = verify_build(build)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=output.stem + "-stage-", dir=output.parent))
    receipts = populate(stage, verified, pg_userspace_only)
    debian = stage / "DEBIAN"
    debian.mkdir()
    size = sum(path.stat().st_size for path in stage.rglob("*") if path.is_file() and not path.is_symlink())
    control = (
        "Package: y4-board\nVersion: " + verified["version"] + "\nArchitecture: arm64\n"
        "Maintainer: Y4 Debian maintainers <noreply@example.invalid>\nSection: admin\nPriority: optional\n"
        "Depends: python3, network-manager (>= 1.42), libglib2.0-0 (>= 2.74), libssl3, libc6 (>= 2.34), systemd, kmod\n"
        "Installed-Size: " + str((size + 1023) // 1024) + "\n"
        "X-MYT-Board: " + receipts["board"] + "\n"
        "X-Y4-Kernel-Release: " + receipts["kernel_release"] + "\n"
        "Description: Y4 Debian MCU, factory NIC and native networking integration\n"
        " Signed MCU maintenance, bounded board initialization, kernel status GUI\n"
        " and NetworkManager-owned DHCP, adaptive LACP and carrier egress policy.\n"
        " No maintainer script starts services or accesses hardware at install.\n")
    (debian / "control").write_text(control)
    conffiles = sorted("/" + str(path.relative_to(stage)) for path in (stage / "etc").rglob("*")
                       if path.is_file() and not path.is_symlink())
    (debian / "conffiles").write_text("\n".join(conffiles) + "\n")
    md5sums = []
    for path in sorted(stage.rglob("*")):
        if path.is_file() and not path.is_symlink() and debian not in path.parents:
            md5sums.append(hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest() + "  " + str(path.relative_to(stage)))
    (debian / "md5sums").write_text("\n".join(md5sums) + "\n")
    command = ["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(stage), str(output)]
    child = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = output.with_suffix(output.suffix + ".log")
    log.write_text(child.stdout)
    if child.returncode:
        raise RuntimeError("dpkg-deb build failed; staged files and log retained: " + str(log))
    result = dict(receipts, output=str(output), stage=str(stage), bytes=output.stat().st_size,
                  sha256=sha(read_regular(output)), command=command, built=True,
                  services_executed=False, target_programs_executed=False)
    output.with_suffix(output.suffix + ".json").write_text(json.dumps(result, indent=2) + "\n")
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pg-userspace-only", action="store_true",
                        help="Omit pgdrv.ko from the deb payload (kernel-bundle images whose kernel "
                             "differs from the 6.1 PG cohort); the module is still built and verified")
    args = parser.parse_args()
    print(json.dumps(build_package(args.build, args.output, args.pg_userspace_only), indent=2))

if __name__ == "__main__":
    main()
