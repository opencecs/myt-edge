#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Prepare the Debian 12 ARM64 build sysroot from checksummed APT packages."""

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def verify(path, record):
    if not path.is_file() or path.stat().st_size != record["bytes"]:
        return False
    with path.open("rb") as archive:
        return hashlib.file_digest(archive, "sha256").hexdigest() == record["sha256"]


def download(record, downloads, cache, mirror):
    name = record["sha256"] + ".deb"
    if cache is not None:
        cached = cache / name
        if verify(cached, record):
            return cached
    archive = downloads / name
    url = record["url"]
    if mirror is not None:
        url = mirror.rstrip("/") + url.removeprefix("https://deb.debian.org")
    subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "2",
         "--connect-timeout", "15", "--max-time", "120", "--output", archive, url],
        check=True,
    )
    if not verify(archive, record):
        raise ValueError("Debian package SHA256 or size mismatch: " + url)
    return archive


def prepare(output, cache, mirror):
    manifest = Path(__file__).with_name("public-sysroot-packages.json")
    lock = json.loads(manifest.read_text(encoding="utf-8"))
    if (lock["schema"], lock["suite"], lock["architecture"]) != (1, "bookworm", "arm64"):
        raise ValueError("Expected Debian bookworm ARM64 sysroot lock")
    output.mkdir(parents=True, exist_ok=True)
    target = output / "cix-sysroot"
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    with tempfile.TemporaryDirectory(prefix=".public-sysroot-", dir=output) as scratch:
        staging = Path(scratch)
        packages = staging / "packages"
        packages.mkdir()
        sysroot = staging / "sysroot"
        sysroot.mkdir()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            downloads = [pool.submit(download, record, packages, cache, mirror)
                         for record in lock["packages"].values()]
            for task in downloads:
                subprocess.run(["dpkg-deb", "--extract", task.result(), sysroot], check=True)
        # The cross-link flags look for the loader in /usr/lib first. Use the
        # same Debian loader as libc instead of the build host's cross SDK.
        libraries = sysroot / "usr/lib/aarch64-linux-gnu"
        loader = libraries / "ld-linux-aarch64.so.1"
        loader.symlink_to("../../../lib/aarch64-linux-gnu/ld-linux-aarch64.so.1")
        if not loader.is_file():
            raise ValueError("Missing ARM64 Debian loader")
        sysroot.rename(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="inputs.dependencies directory")
    parser.add_argument("--cache", type=Path, help="Optional cache of <sha256>.deb files")
    parser.add_argument("--mirror", help="Optional HTTPS APT mirror root")
    args = parser.parse_args()
    if args.mirror is not None and not args.mirror.startswith("https://"):
        parser.error("--mirror must use HTTPS")
    try:
        sysroot = prepare(args.output.resolve(), args.cache, args.mirror)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Public sysroot preparation failed: {error}\n")
    print(sysroot)


if __name__ == "__main__":
    main()
