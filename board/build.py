#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compile Y4 Debian board components; never execute a built program or test."""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent
sys.path.insert(0, str(SOURCE / "scripts"))
from board_config import board_config as select_board_config
from mcu_assets import MAINTAINER, SIGNED_FILES, load_assets

def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def item(path):
    path = Path(path)
    return {"bytes": path.stat().st_size, "sha256": sha(path)}

def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")

def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded

def sign_mcu(out, image, version, label, board_config, args, report):
    signing = board_config.get("signing", {})
    if signing:
        required = ["private_key", "public_key", "sequence"]
        if any(key not in signing for key in required):
            raise ValueError("board.signing requires private_key, public_key and sequence")
        host_version = board_config.get("host_version", (ROOT/"VERSION").read_text().strip())
        host_min = signing.get("host_min", host_version)
        host_max = signing.get("host_max", host_version)

        def version_tuple(value):
            if not isinstance(value, str) or not re.fullmatch(
                    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
                raise ValueError("Expected canonical MCU host version")
            return tuple(map(int, value.split(".")))

        if not version_tuple(host_min) <= version_tuple(host_version) <= version_tuple(host_max):
            raise ValueError("board.host_version is outside the MCU signing host range")
        package = out/"mcu/mcu-package.bin"
        builder = module(ROOT/"tools/build_mcu_package.py", "y4_mcu_builder")
        signed = builder.create(image, Path(signing["private_key"]),
            Path(signing["public_key"]), package, version, int(signing["sequence"]),
            host_min, host_max, board=label)
        signed["schema"] = 1
        signed["sha256"] = signed["package_sha256"]
        write_json(out/"mcu/mcu-builtin.json", signed)
        shutil.copy2(signing["public_key"], out/"mcu/mcu-public-key.bin")
        (out/"mcu/host-version").write_text(host_version+"\n")
        report["signed_mcu"] = signed
        for signed_name in ["mcu-package.bin", "mcu-builtin.json", "mcu-public-key.bin", "host-version"]:
            signed_path = out/"mcu"/signed_name
            report["artifacts"]["mcu/"+signed_name] = dict(path=str(signed_path.relative_to(out)), **item(signed_path))
    elif args.require_signed or args.make_deb:
        raise ValueError("A configured production MCU signer/trust key is required for an installable package; no development key is silently generated")
    else:
        report["signed_mcu"] = {"signed": False, "reason": "compile-only; signer not requested"}

def stage_prebuilt_mcu(out, label, assets, report):
    binary = f"firmware/build/{label.lower()}-mcu.bin"
    for name, data in assets.items():
        if name in SIGNED_FILES or name == binary:
            relative = "mcu/" + Path(name).name
        elif name == MAINTAINER:
            relative = "arm64/mcu-maintain/y4-mcu-maintain"
        elif name.startswith("licenses/"):
            relative = "mcu-" + name
        else:
            continue
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if name == MAINTAINER else 0o644)
        key = "arm64/y4-mcu-maintain" if name == MAINTAINER else relative
        report["artifacts"][key] = dict(path=relative, **item(target))
    signed = json.loads(assets["mcu-builtin.json"])
    report["mcu_version"] = signed["version"]
    report["signed_mcu"] = signed
    report["mcu_build"] = {"mode": "prebuilt", "board": label}
    verifier = module(ROOT / "package.py", "y4_prebuilt_mcu_verification")
    verifier.verify_signed_mcu(out, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", choices=["Y4", "Y12"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=SOURCE.parent / "inputs/build.json")
    parser.add_argument("--dependencies", type=Path)
    parser.add_argument("--mcu-source", type=Path)
    parser.add_argument("--mcu-assets", type=Path, help="Board-specific signed firmware and ARM64 host tools")
    parser.add_argument("--mcu-assets-manifest-sha256", help="Pin the prebuilt MCU asset manifest digest")
    parser.add_argument("--mcu-vendor", type=Path)
    parser.add_argument("--mcu-bin", type=Path, help="Use this newly compiled exact-version firmware rather than compiling another copy")
    parser.add_argument("--kernel-objects", type=Path)
    parser.add_argument("--kernel-source", type=Path)
    parser.add_argument("--realtek-pg", type=Path)
    parser.add_argument("--jobs", type=int, default=32)
    parser.add_argument("--architectures", choices=["both", "arm64", "native"], default="both")
    parser.add_argument("--pg-userspace-only", action="store_true",
                        help="Build the deb without pgdrv.ko (kernel-bundle images running a kernel "
                             "outside the 6.1 PG cohort); the module is still compiled and verified")
    parser.add_argument("--require-signed", action="store_true")
    parser.add_argument("--make-deb", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 32:
        raise ValueError("jobs must be 1..32")
    config = json.loads(args.config.read_text()) if args.config.is_file() else {}
    config = select_board_config(config, args.board)
    configured_hardware = config.get("hardware")
    label = args.board or {"Y4-6187": "Y4", "Y12-6187": "Y12"}.get(
        configured_hardware, "Y4" if configured_hardware is None else None)
    if label is None or configured_hardware not in (None, f"{label}-6187"):
        parser.error("board conflicts with configured hardware")
    if args.mcu_assets and (args.mcu_source or args.mcu_bin):
        parser.error("--mcu-assets is exclusive with --mcu-source and --mcu-bin")
    if args.mcu_assets_manifest_sha256 and not args.mcu_assets:
        parser.error("--mcu-assets-manifest-sha256 requires --mcu-assets")
    if args.mcu_assets and args.architectures == "native":
        parser.error("--mcu-assets requires an ARM64 build")
    if label == "Y12" and args.mcu_bin:
        parser.error("Y12 requires source MCU input")
    if args.mcu_source and label != "Y12":
        parser.error("--mcu-source is available for Y12 component compilation")
    prebuilt = load_assets(args.mcu_assets, label, args.mcu_assets_manifest_sha256) if args.mcu_assets else None
    inputs = config.get("inputs", {})
    board_config = config.get("board", {})
    deps_input = args.dependencies or inputs.get("dependencies")
    if not deps_input:
        parser.error("Provide --dependencies or inputs.dependencies")
    deps = Path(deps_input).resolve(strict=True)
    vendor_input = args.mcu_vendor or inputs.get("mcu_vendor")
    if not prebuilt and not vendor_input:
        parser.error("Provide --mcu-assets or the private MCU source dependencies")
    vendor = Path(vendor_input).resolve(strict=True) if vendor_input else None
    out = args.output.resolve()
    if out == SOURCE or SOURCE in out.parents or out in SOURCE.parents:
        raise ValueError("Output must be outside the source checkout")
    out.mkdir(parents=True, exist_ok=False)
    (out / "logs").mkdir()
    report = {
        "schema": 1, "board": label, "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_baseline": subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip(),
        "source_mode": "current working-tree bytes; includes uncommitted delivery changes",
        "tests_executed": False, "programs_executed": False, "hardware_access": False,
        "complete": False, "commands": [], "artifacts": {}, "source_files": {}
    }
    def record():
        write_json(out / "BUILD.json", report)
    def run(stage, command, cwd=None, env=None):
        print(stage, flush=True)
        began = time.monotonic()
        with (out / "logs" / (stage + ".log")).open("wb") as log:
            child = subprocess.run(list(map(str, command)), cwd=cwd or SOURCE,
                                   env=env, stdout=log, stderr=subprocess.STDOUT)
        report["commands"].append({"stage": stage, "command": list(map(str, command)),
                                   "exit": child.returncode, "seconds": round(time.monotonic()-began, 3)})
        record()
        if child.returncode:
            raise RuntimeError(stage + " failed; original compiler log retained")
    try:
        folders = [ROOT, SOURCE / "mcu/include"]
        if label == "Y4" and prebuilt is None:
            folders.append(SOURCE / "mcu/firmware")
        for folder in folders:
            for path in sorted(folder.rglob("*")):
                if path.is_file() and not path.is_symlink() and not any(x in path.relative_to(folder).parts for x in ("__pycache__", "build", ".git")):
                    report["source_files"][str(path.relative_to(SOURCE))] = item(path)
        if prebuilt is not None:
            stage_prebuilt_mcu(out, label, prebuilt, report)
        arches = ["native", "arm64"] if args.architectures == "both" else [args.architectures]
        for arch in arches:
            targets = ["daemon", "network", "nic", "boot-helpers"] if prebuilt is not None else ["all"]
            command = ["make", "-C", ROOT, "-j"+str(min(args.jobs, 8)), *targets,
                       "OUT="+str(out / arch), "DEVICE="+label, "CC="+("gcc" if arch=="native" else "aarch64-linux-gnu-gcc")]
            if arch == "arm64":
                command += ["SYSROOT="+str(deps/"cix-sysroot"),
                            "CRYPTO_PREFIX="+str(deps/"crypto-arm64-3.0.22-1~deb12u1")]
            run("host-"+arch, command)
            binaries = {
                "y4-boardd": out/arch/"daemon/y4-boardd",
                "y4-network-policy": out/arch/"network/y4-network-policy",
                "y4-nic-factory": out/arch/"nic/y4-nic-factory",
                "y4-board-boot-helper": out/arch/"boot/y4-board-boot-helper"
            }
            if prebuilt is None or arch == "arm64":
                binaries["y4-mcu-maintain"] = out/arch/"mcu-maintain/y4-mcu-maintain"
            for name, path in binaries.items():
                report["artifacts"][arch+"/"+name] = dict(path=str(path.relative_to(out)), **item(path))
                # readelf inspects bytes; no target program is invoked.
                run("elf-"+arch+"-"+name, ["readelf", "-h", "-d", "-V", path])
        if prebuilt is not None:
            pass  # Firmware and maintainer were verified before compiling public host components.
        elif label == "Y12":
            work = out / "mcu-source"
            mcu_source = (args.mcu_source or SOURCE / "mcu").resolve(strict=True)
            shutil.copytree(mcu_source, work,
                            ignore=shutil.ignore_patterns("vendor", "build", "__pycache__", ".git"))
            (work / "vendor").symlink_to(vendor, target_is_directory=True)
            (out / "mcu").mkdir()
            firmware = out / "mcu/y12-mcu.elf"
            run("firmware", ["make", "-C", work / "firmware", "y12-image",
                             "Y12_OUT=" + str(firmware)])
            for artifact in (firmware, firmware.with_suffix(".bin")):
                relative = str(artifact.relative_to(out))
                report["artifacts"][relative] = dict(path=relative, **item(artifact))
            header = (work / "include/y12_power_wire.h").read_text()
            report["mcu_version"] = ".".join(
                re.search(r"^#define Y12_FW_" + part + r" ([0-9]+)$", header, re.M).group(1)
                for part in ("MAJOR", "MINOR", "PATCH"))
            inputs = {str(path.relative_to(work)): item(path)
                      for folder in (work / "firmware", work / "include", work / "licenses")
                      for path in sorted(folder.rglob("*")) if path.is_file() and not path.is_symlink()}
            report["mcu_build"] = {"mode": "source compile", "board": label,
                                   "source": str(mcu_source), "source_files": inputs}
            header_path = "mcu-source/include/y12_power_wire.h"
            report["artifacts"][header_path] = dict(path=header_path, **item(out / header_path))
        else:
            version_header = (SOURCE/"mcu/include/protocol.h").read_text()
            version = ".".join(re.search(r"^#define Y4_FW_"+part+r" ([0-9]+)$", version_header, re.M).group(1)
                               for part in ["MAJOR", "MINOR", "PATCH"])
            (out/"mcu").mkdir()
            if args.mcu_bin:
                binary = args.mcu_bin.resolve()
                shutil.copy2(binary, out/"mcu/y4-mcu.bin")
                for suffix in ["hex", "elf"]:
                    other = binary.with_suffix("."+suffix)
                    if other.is_file():
                        shutil.copy2(other, out/("mcu/y4-mcu."+suffix))
                report["mcu_build"] = {"mode": "caller source-build reuse", "source_artifact": str(binary)}
            else:
                work = out/"mcu-source"
                shutil.copytree(SOURCE/"mcu", work, ignore=shutil.ignore_patterns("vendor", "build", "__pycache__", ".git"))
                (work/"vendor").symlink_to(vendor, target_is_directory=True)
                run("firmware", ["make", "-C", work/"firmware", "-j"+str(args.jobs), "all",
                                "EXTRA_CFLAGS=-DY4_LCD_HARDWARE_SPI=1 -DY4_FAN_HARDWARE_PWM=0"])
                for suffix in ["bin", "hex", "elf"]:
                    shutil.copy2(work/("firmware/build/y4-mcu."+suffix), out/("mcu/y4-mcu."+suffix))
                report["mcu_build"] = {"mode": "source compile", "tests_executed": False}
            for path in sorted((out/"mcu").iterdir()):
                report["artifacts"]["mcu/"+path.name] = dict(path=str(path.relative_to(out)), **item(path))
            report["mcu_version"] = version
        pg_source = args.realtek_pg or (Path(inputs["realtek_pg"]) if "realtek_pg" in inputs else None)
        kernel_objects = args.kernel_objects
        if pg_source:
            if not pg_source or not kernel_objects or not args.kernel_source:
                raise ValueError("PG compilation requires --realtek-pg, --kernel-objects and --kernel-source")
            kernel_objects = kernel_objects.resolve()
            kernel_source = args.kernel_source.resolve()
            private = out/"kernel-module-objects"
            private.mkdir()
            shutil.copy2(kernel_objects/".config", private/".config")
            before_config = sha(private/".config")
            cross = deps/"kernel-toolchain-12.3/bin/aarch64-none-linux-gnu-"
            # Independent generated headers/host tools. No shared object directory is mutated.
            run("kernel-module-prepare", ["make", "-C", kernel_source, "O="+str(private),
                 "ARCH=arm64", "CROSS_COMPILE="+str(cross), "LOCALVERSION=-generic", "-j"+str(args.jobs), "olddefconfig", "modules_prepare"])
            if sha(private/".config") != before_config:
                raise ValueError("Prepared module configuration differs from the supplied kernel configuration")
            expected_release = (kernel_objects/"include/config/kernel.release").read_text().strip()
            prepared_release = (private/"include/config/kernel.release").read_text().strip()
            if prepared_release != expected_release:
                raise ValueError("Prepared PG kernel release differs from the actual Debian kernel")
            shutil.copy2(kernel_objects/"Module.symvers", private/"Module.symvers")
            report["pg_kernel_input"] = {"source": str(kernel_source), "objects": str(kernel_objects),
                  "config": item(private/".config"), "module_symvers": item(private/"Module.symvers")}
            run("realtek-pg", [sys.executable, ROOT/"tools/build_realtek_pg.py",
                  "--vendor-source", pg_source, "--kernel-objects", private,
                  "--cross-prefix", cross, "--output", out/"pg", "--jobs", str(args.jobs)])
            pg_meta = json.loads((out/"pg/BUILD.json").read_text())
            report["realtek_pg"] = pg_meta
            for relative in pg_meta["files"]:
                path = out/"pg/stage"/relative
                report["artifacts"]["pg/"+relative] = dict(path=str(path.relative_to(out)), **item(path))
        else:
            report["realtek_pg"] = {"compiled": False, "reason": "No explicit matching kernel/vendor inputs supplied"}
        if prebuilt is None:
            image = out / f"mcu/{label.lower()}-mcu.bin"
            sign_mcu(out, image, report["mcu_version"], label, board_config, args, report)
        if label == "Y12" and report["signed_mcu"].get("signed"):
            verifier = module(ROOT / "package.py", "board_package_verifier")
            verifier.verify_signed_mcu(out, report)
        record()
        if args.make_deb:
            if "arm64" not in arches:
                raise ValueError("Installable package requires ARM64")
            deb_command = [sys.executable, ROOT/"package.py", "--build", out, "--output", out/"y4-board.deb"]
            if args.pg_userspace_only:
                deb_command.append("--pg-userspace-only")
            run("debian-package", deb_command)
            report["artifacts"]["y4-board.deb"] = dict(path="y4-board.deb", **item(out/"y4-board.deb"))
        report["complete"] = True
        report["completed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record()
        print(json.dumps({"complete": True, "output": str(out), "tests_executed": False,
                          "hardware_access": False, "signed": report["signed_mcu"].get("signed", False)}, indent=2))
    except Exception as exc:
        report["failure"] = str(exc)
        record()
        raise

if __name__ == "__main__":
    main()
