#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build pinned private components for the public MYT system build."""
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
import tempfile

from spi_bundle import read_spi_bundle


COMPONENTS = ("uefi", "mcu", "panel")
Y4_HOST_PROGRAMS = ("mcud", "mcuctl", "hexverify", "y4-mcu-reset", "y4-mcu-flash")
Y4_UNITS = ("y4-mcud.service", "y4-mcu-boot.service")
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def local_path(base, value):
    if not isinstance(value, str) or not value:
        raise ValueError("Expected a nonempty path")
    return (base / Path(value).expanduser()).absolute()


def command(argv, *, env, cwd=None, capture=False):
    result = subprocess.run(list(map(str, argv)), cwd=cwd, env=env, check=True,
                            capture_output=capture, text=capture)
    return result.stdout.strip() if capture else None


def checkout(row, destination, base, env, *, sparse=None, prefix=""):
    revision = row[prefix + "commit"]
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Private component inputs require an exact full Git commit")
    remote = row[prefix + "repo"]
    if not isinstance(remote, str) or not remote:
        raise ValueError("Private component repository is required")
    if "://" not in remote and ":" not in remote:
        remote = local_path(base, remote)
    # Even a local repo is checked out in a scratch tree, which excludes dirty
    # source files and any pre-existing Git-ignored artifacts.
    command(["git", "clone", "--quiet", "--no-checkout", "--", remote, destination], env=env)
    if sparse:
        command(["git", "sparse-checkout", "set", "--no-cone", *sparse],
                env=env, cwd=destination)
    present = subprocess.run(["git", "cat-file", "-e", f"{revision}^{{commit}}"],
                             cwd=destination, env=env, capture_output=True).returncode == 0
    if not present:
        command(["git", "fetch", "--quiet", "--no-tags", "origin", revision],
                env=env, cwd=destination)
    command(["git", "checkout", "--quiet", "--detach", revision], env=env, cwd=destination)
    if command(["git", "rev-parse", "HEAD"], env=env, cwd=destination, capture=True) != revision:
        raise ValueError("Private component checkout differs from locked revision")
    return revision


def item(path):
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
        raise ValueError(f"Missing or nonregular component artifact: {path.name}")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"bytes": path.stat().st_size, "sha256": digest}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def copy_artifact(source, target):
    item(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def verify_arm64(path):
    with path.open("rb") as stream:
        header = stream.read(20)
    if header[:7] != b"\x7fELF\x02\x01\x01" or header[18:20] != b"\xb7\x00":
        raise ValueError(f"Expected AArch64 ELF: {path.name}")


def verify_host_program(path, env):
    verify_arm64(path)
    versions = command(["readelf", "--version-info", path], env=env, capture=True)
    glibc = re.findall(r"GLIBC_([0-9]+)\.([0-9]+)", versions)
    if any(tuple(map(int, version)) > (2, 36) for version in glibc):
        raise ValueError(f"MCU host program exceeds the Debian 12 ABI: {path.name}")


def firmware_version(source, board):
    header = "protocol.h" if board == "Y4" else "y12_power_wire.h"
    text = (source / "include" / header).read_text()
    fields = []
    for part in ("MAJOR", "MINOR", "PATCH"):
        match = re.search(rf"^#define {board}_FW_{part}\s+([0-9]+)$", text, re.M)
        if match is None:
            raise ValueError(f"Missing {board} firmware version field {part}")
        fields.append(match.group(1))
    return ".".join(fields)


def build_uefi(row, base, work, out, board, jobs, env):
    source = work / "uefi"
    revision = checkout(row, source, base, env, sparse=("/*", "!/vendor/"))
    built = work / "uefi-build"
    sdk = local_path(base, row["sdk"]).resolve(strict=True)
    argv = [sys.executable, "-B", source / "tools/build.py", "--source", sdk,
            "--board", board, "--jobs", jobs, "--output", built]
    if row.get("toolchain"):
        argv += ["--toolchain", local_path(base, row["toolchain"]).resolve(strict=True)]
    command(argv, env=env, cwd=source)
    output = built / "delivery/myt-spi.bin"
    result = out / output.name
    read_spi_bundle(output.read_bytes(), board)
    copy_artifact(output, result)
    return {"commit": revision, **item(result)}


def build_panel(row, base, work, out, jobs, env):
    source = work / "panel"
    revision = checkout(row, source, base, env)
    version = row["version"]
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise ValueError("Panel version must have three numeric components")
    epoch = command(["git", "show", "-s", "--format=%ct", "HEAD"], env=env,
                    cwd=source, capture=True)
    stamp = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
    panel_env = dict(env, GOTOOLCHAIN="local")
    command(["npm", "--prefix", "web", "ci", "--prefer-offline", "--no-fund", "--no-audit"],
            env=panel_env, cwd=source)
    # The distribution uses the LAN profile and its checked-in no-FRP wrapper.
    command(["make", f"-j{jobs}", "build-arm64-lan-test", f"VERSION={version}",
             f"COMMIT={revision[:7]}", f"BUILDTIME={stamp:%Y-%m-%dT%H:%M:%SZ}"],
            env=panel_env, cwd=source)
    output = source / "dist/bin/myt-desktop-linux-arm64-lan-test"
    verify_arm64(output)
    result = out / "myt_desktop"
    copy_artifact(output, result)
    receipt = {"schema": 1, "commit": revision, "version": version + "-lan-test",
               "profile": "nofrp", "arch": "arm64", **item(result)}
    write_json(out / "panel.json", receipt)
    return receipt


def sign_mcu(maintainer, row, base, image, version, board, assets):
    signer_path = maintainer / "board/tools/build_mcu_package.py"
    spec = importlib.util.spec_from_file_location("private_component_mcu_signer", signer_path)
    if spec is None or spec.loader is None:
        raise ValueError("Missing pinned MCU package signer")
    signer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(signer)
    signing = row["signing"]
    sequence = signing["sequence"]
    host_version = signing["host_version"]
    host_min = signing.get("host_min", host_version)
    host_max = signing.get("host_max", host_version)
    versions = (host_min, host_version, host_max)
    if (not all(isinstance(v, str) and SEMVER.fullmatch(v) for v in versions)
            or sorted(versions, key=lambda v: tuple(map(int, v.split(".")))) != list(versions)):
        raise ValueError("Host version must be in the configured MCU package range")
    if type(sequence) is not int or not 1 <= sequence < 2**64:
        raise ValueError("MCU sequence must be a positive uint64")
    key = local_path(base, signing["private_key"])
    public = local_path(base, signing["public_key"])
    result = signer.create(image, key, public, assets / "mcu-package.bin", version,
                           sequence, host_min, host_max, board=board)
    result.update(schema=1, sha256=result["package_sha256"])
    write_json(assets / "mcu-builtin.json", result)
    copy_artifact(public, assets / "mcu-public-key.bin")
    (assets / "host-version").write_text(host_version + "\n", encoding="ascii")


def build_mcu(row, base, work, out, board, jobs, env):
    source, maintainer = work / "mcu", work / "maintainer"
    revision = checkout(row, source, base, env)
    maintainer_revision = checkout(row, maintainer, base, env, sparse=("/board/",),
                                   prefix="maintainer_")
    # The shared maintainer includes the protocol from the independently
    # pinned MCU checkout. Keep this link inside the throwaway tree.
    (maintainer / "mcu").symlink_to(source, target_is_directory=True)
    sysroot = local_path(base, row["sysroot"]).resolve(strict=True)
    crypto = local_path(base, row["crypto_prefix"]).resolve(strict=True)
    flags = ["EXTRA_CFLAGS=-DY4_LCD_HARDWARE_SPI=1 -DY4_FAN_HARDWARE_PWM=0"] if board == "Y4" else []
    command(["make", f"-j{jobs}", "all" if board == "Y4" else "y12-image", *flags],
            env=env, cwd=source / "firmware")

    assets = out / "mcu"
    image = Path("firmware/build") / f"{board.lower()}-mcu.bin"
    copy_artifact(source / image, assets / image)
    version = firmware_version(source, board)
    if board == "Y4":
        copy_artifact(source / image.with_suffix(".hex"), assets / image.with_suffix(".hex"))
        cflags = ("-Os -std=c11 -Wall -Wextra -Werror -Wno-misleading-indentation "
                  f"-Wno-format-truncation --sysroot={sysroot} "
                  f"-isystem {sysroot}/usr/include/aarch64-linux-gnu -isystem {sysroot}/usr/include")
        command(["make", f"-j{jobs}", *Y4_HOST_PROGRAMS, "CC=aarch64-linux-gnu-gcc",
                 "CFLAGS=" + cflags], env=env, cwd=source / "host")
        for name in Y4_HOST_PROGRAMS:
            verify_host_program(source / "host" / name, env)
            copy_artifact(source / "host" / name, assets / "host" / name)
        for name in Y4_UNITS:
            copy_artifact(source / "system" / name, assets / "system" / name)

    binaries = work / "maintainer-build"
    command(["make", f"-j{jobs}", "mcu-maintain", f"DEVICE={board}", f"OUT={binaries}",
             "CC=aarch64-linux-gnu-gcc", f"SYSROOT={sysroot}", f"CRYPTO_PREFIX={crypto}"],
            env=env, cwd=maintainer / "board")
    maintenance = binaries / "mcu-maintain/y4-mcu-maintain"
    verify_host_program(maintenance, env)
    copy_artifact(maintenance, assets / "host/y4-mcu-maintain")
    for license_path in sorted((source / "licenses").iterdir()):
        if license_path.is_file() and not license_path.is_symlink():
            copy_artifact(license_path, assets / "licenses" / license_path.name)
    sign_mcu(maintainer, row, base, assets / image, version, board, assets)
    files = {path.relative_to(assets).as_posix(): item(path)
             for path in sorted(assets.rglob("*")) if path.is_file()}
    write_json(assets / "manifest.json", {"schema": 1, "board": board, "files": files})
    return {"commit": revision, "maintainer_commit": maintainer_revision, "version": version,
            "manifest": item(assets / "manifest.json")}


def build(config_file, output, board, components, jobs):
    components = tuple(components)
    if (board not in ("Y4", "Y12") or type(jobs) is not int or not 1 <= jobs <= 32
            or not components or len(set(components)) != len(components)
            or any(component not in COMPONENTS for component in components)):
        raise ValueError("Select a supported board, distinct components and 1..32 jobs")
    config_file = config_file.resolve(strict=True)
    config = json.loads(config_file.read_text())
    if not isinstance(config, dict):
        raise ValueError("Expected a private components configuration object")
    base = config_file.parent
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("Private component output must be a new directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    tools = config.get("tool_paths", [])
    if not isinstance(tools, list):
        raise ValueError("tool_paths must be a list of host tool directories")
    paths = [local_path(base, path).resolve(strict=True) for path in tools]
    if not all(path.is_dir() for path in paths):
        raise ValueError("tool_paths must contain directories")
    env = dict(os.environ, PATH=os.pathsep.join([*map(str, paths), os.environ.get("PATH", "")]),
               GIT_TERMINAL_PROMPT="0", PYTHONDONTWRITEBYTECODE="1")
    with tempfile.TemporaryDirectory(prefix=".myt-components-", dir=output.parent) as tmp:
        work = Path(tmp)
        delivery = work / "delivery"
        delivery.mkdir()
        manifests = {}
        for component in components:
            print(f"Building private {component} for {board}", flush=True)
            if component == "uefi":
                manifests[component] = build_uefi(config[component], base, work, delivery, board, jobs, env)
            elif component == "mcu":
                manifests[component] = build_mcu(config[component], base, work, delivery, board, jobs, env)
            else:
                manifests[component] = build_panel(config[component], base, work, delivery, jobs, env)
        receipt = {"schema": 1, "board": board, "components": manifests}
        write_json(delivery / "BUILD.json", receipt)
        delivery.rename(output)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--board", required=True, choices=("Y4", "Y12"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jobs", type=int, choices=range(1, 33), default=8, metavar="1..32")
    parser.add_argument("--component", choices=COMPONENTS, action="append",
                        help="Select a component; default: build all three")
    args = parser.parse_args()
    components = tuple(dict.fromkeys(args.component)) if args.component else COMPONENTS
    try:
        print(json.dumps(build(args.config, args.output, args.board, components, args.jobs),
                         indent=2, sort_keys=True))
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Private component build failed: {error}\n")


if __name__ == "__main__":
    main()
