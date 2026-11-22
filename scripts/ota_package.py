#!/usr/bin/env python3
"""Create a signed Y4 OTA package (.y4pkg) from a built slot image.

Layout (little endian), schema 1 (single rootfs payload):
  magic "Y4PKG001" (or "Y4PKG002" with a zstd compressed payload),
  u32 manifest bytes, u32 signature bytes (64), u64 payload bytes,
  manifest JSON, Ed25519 signature over the manifest, payload
  (Android sparse ext4 image, padded to 4 KiB; the hash covers the pad).

Layout, schema 2 (kernel-ota-design 4.2, atomic rootfs+kernel pair): the
same 24 byte header with magic "Y4PKG003" and the total payload length,
then the manifest and signature, then two payloads back to back:
  payloads.rootfs at offset 0  (the sparse image, as schema 1)
  payloads.kernel at the next 4 KiB boundary
    = raw kernel Image followed by the raw DTB, contiguous.
Both payload offsets/lengths/digests live in the signed manifest. The
kernel payload carries the boot FAT pair Image+DTB only: modules and
firmware travel inside the rootfs, so one atomic package updates the
kernel and the rootfs that matches it.

The board's y4-boardd verifies and applies packages; nothing here touches
a device. Old boards reject unknown magic, so a schema 2 package can never
be applied by a pre-per-slot-kernel agent.

Packing gates (kernel-ota-design 4.4), all enforced before signing:
  - package/y4-release.json must stamp rootfs == --version and
    kernel == --kernel-release; the kernel Image digest prefix must match
    the stamped kernel_image_sha256_16 (a build output without the file
    packs schema 1 only, with a loud warning; schema 2 is refused without
    the anchor)
  - the sparse image itself is opened with debugfs: its
    etc/y4-release.json must equal the package copy and
    lib/modules/<kernel-release> must exist
  - the kernel Image must announce "Linux version <kernel-release>"
  - the kernel payload stays under the board's 256 MiB cap
"""
import argparse
import datetime
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

MAGIC = b"Y4PKG001"
MAGIC_ZSTD = b"Y4PKG002"
MAGIC_DUAL = b"Y4PKG003"
SLOT_BYTES = 16 * 1024**3
KERNEL_PAYLOAD_LIMIT = 256 * 1024**2


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(8 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sparse_logical_bytes(path):
    with path.open("rb") as stream:
        header = stream.read(28)
    if len(header) != 28:
        raise SystemExit("truncated sparse image")
    magic, major, minor, file_hdr, chunk_hdr, blk, total_blks, total_chunks, crc = \
        struct.unpack("<IHHHHIIII", header)
    if (magic, major, minor, file_hdr, chunk_hdr) != (0xED26FF3A, 1, 0, 28, 12):
        raise SystemExit("not an Android sparse image")
    return blk * total_blks


def sign_manifest(manifest_bytes, key):
    """Ed25519 sign with the production key. pkeyutl -rawin needs a file."""
    with tempfile.NamedTemporaryFile(prefix="y4-ota-manifest-", suffix=".json",
                                     dir="/dev/shm" if os.path.isdir("/dev/shm") else None,
                                     delete=False) as handle:
        handle.write(manifest_bytes)
        source = handle.name
    signature_path = source + ".sig"
    try:
        subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(key),
                        "-in", source, "-out", signature_path],
                       capture_output=True, check=True)
        signature = Path(signature_path).read_bytes()
    finally:
        os.unlink(source)
        if os.path.exists(signature_path):
            os.unlink(signature_path)
    if len(signature) != 64:
        raise SystemExit("unexpected Ed25519 signature length")
    return signature


def zstd_compress_file(src, level=19):
    """Stream-compress src with zstd, return (temp_path, compressed_size)."""
    dst = src.with_suffix(src.suffix + ".zst")
    if dst.exists():
        dst.unlink()
    subprocess.run(["zstd", "-T0", "-" + str(level), "-f", "-o", str(dst), str(src)], check=True)
    return dst, dst.stat().st_size


def check_image_uts(image_path, kernel_release):
    """Every version-looking "Linux version <release>" token in the Image
    must name exactly the expected release (kernel-ota-design 4.4 packing
    gate; the board applies the same rule while streaming, so both sides
    must agree on the rule). Printf templates ("Linux version %s") and
    tokens cut short by the scan window are not announcements."""
    expected = kernel_release.encode()
    token = b"Linux version "
    found = 0
    tail = b""
    with image_path.open("rb") as stream:
        while True:
            block = stream.read(8 * 1024 * 1024)
            if not block:
                break
            window = tail + block
            at = 0
            while True:
                at = window.find(token, at)
                if at < 0:
                    break
                rest = window[at + len(token):]
                end = len(rest)
                for cut in (b" ", b"(", b"\n", b"\t", b"\0"):
                    stop = rest.find(cut)
                    if stop >= 0:
                        end = min(end, stop)
                complete = end < len(rest) or len(rest) > len(expected) + 4
                looks_like_version = complete and rest[:1].isdigit() and b"%" not in rest[:end]
                if looks_like_version:
                    if rest[:end] != expected:
                        raise SystemExit(
                            "kernel Image announces release %r, expected %r"
                            % (rest[:end].decode(errors="replace"), kernel_release))
                    found += 1
                at += len(token)
            tail = window[-(len(token) + len(expected) + 8):]
    if not found:
        raise SystemExit("kernel Image does not announce 'Linux version %s'" % kernel_release)


def debugfs_gate(image, package, kernel_release):
    """Open the sparse image and cross-check the filesystem it carries."""
    raw = image.with_suffix(".gate-raw.tmp")
    subprocess.run(["simg2img", str(image), str(raw)], check=True)
    try:
        staged = subprocess.run(
            ["debugfs", "-R", "cat etc/y4-release.json", str(raw)],
            capture_output=True, check=True).stdout
        reference = (package / "y4-release.json").read_bytes()
        if staged.strip() != reference.strip():
            raise SystemExit("sparse etc/y4-release.json differs from package/y4-release.json")
        listing = subprocess.run(
            ["debugfs", "-R", "ls -l lib/modules", str(raw)],
            capture_output=True, check=True).stdout.decode(errors="replace")
        if kernel_release not in listing:
            raise SystemExit("sparse lib/modules lacks " + kernel_release)
    finally:
        raw.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path, help="rootfs_sparse.ext4 from the build package")
    parser.add_argument("--zstd", action="store_true", help="compress the rootfs payload with zstd (Y4PKG002/Y4PKG003)")
    parser.add_argument("--version", required=True, help="target rootfs version, YYYYMMDD-rNN")
    parser.add_argument("--kernel", required=True, help="kernel release the slot was built with")
    parser.add_argument("--board", choices=("Y4", "Y12"), default="Y4")
    parser.add_argument("--schema", type=int, choices=(1, 2), default=1,
                        help="1: rootfs only (legacy); 2: rootfs+kernel atomic pair (Y4PKG003)")
    parser.add_argument("--kernel-image", type=Path, help="schema 2: kernel Image for the boot FAT payload")
    parser.add_argument("--kernel-dtb", type=Path, help="schema 2: DTB for the boot FAT payload")
    parser.add_argument("--key", type=Path, required=True, help="External OTA signing key trusted by the intended device")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--delivery-dir", type=Path, help="also export the standard system-OTA ZIP to this directory")
    parser.add_argument("--jobs", type=int, default=8, choices=range(1, 33))
    args = parser.parse_args()
    if args.delivery_dir and ((args.delivery_dir/'ota').exists() or
                              (args.delivery_dir/f'Y4-{args.version}-ota.zip').exists()):
        raise SystemExit("OTA delivery output already exists")

    if args.image.stat().st_size == 0:
        raise SystemExit("empty image")
    logical = sparse_logical_bytes(args.image)
    if not logical or logical > SLOT_BYTES:
        raise SystemExit(f"image is {logical} bytes, a slot is {SLOT_BYTES}")
    if not args.key.is_file() or args.key.stat().st_mode & 0o077:
        raise SystemExit("signing key must be a regular 0600 file")
    if args.board == "Y12" and args.schema != 2:
        raise SystemExit("Y12 requires schema 2")
    if args.schema == 2 and args.zstd:
        raise SystemExit("schema 2 packs an uncompressed rootfs payload: the kernel "
                         "payload is raw and the board reads the pair without a "
                         "compressed-stream window")
    package = args.image.parent
    release_reference = package / "y4-release.json"
    if release_reference.is_file():
        reference = json.loads(release_reference.read_text())
        if reference.get("board", "Y4") != args.board:
            raise SystemExit("Stamped release board differs from requested OTA board")
        # Packing gate: the stamped build identity binds the versions this
        # package may claim (kernel-ota-design 4.4).
        if reference.get("rootfs") != args.version:
            raise SystemExit("package/y4-release.json stamps rootfs %r, --version is %r"
                             % (reference.get("rootfs"), args.version))
        if reference.get("kernel") != args.kernel:
            raise SystemExit("package/y4-release.json stamps kernel %r, --kernel is %r"
                             % (reference.get("kernel"), args.kernel))
        if args.schema == 2 and reference.get("kernel_image_sha256_16"):
            actual = sha256_file(args.kernel_image)[:16]
            if actual != reference["kernel_image_sha256_16"]:
                raise SystemExit("kernel Image digest %s differs from the stamped %s"
                                 % (actual, reference["kernel_image_sha256_16"]))
        debugfs_gate(args.image, package, args.kernel)
    else:
        if args.schema == 2:
            raise SystemExit("schema 2 requires package/y4-release.json from a current build; refusing")
        print("WARNING: no package/y4-release.json in %s; packing a legacy "
              "schema 1 package without the build-identity gates" % package,
              file=sys.stderr)

    kernel_meta = None
    if args.schema == 2:
        if not args.kernel_image or not args.kernel_dtb or not args.kernel_image.is_file() \
                or not args.kernel_dtb.is_file():
            raise SystemExit("schema 2 needs --kernel-image and --kernel-dtb files")
        check_image_uts(args.kernel_image, args.kernel)
        image_bytes = args.kernel_image.stat().st_size
        dtb_bytes = args.kernel_dtb.stat().st_size
        kernel_bytes = image_bytes + dtb_bytes
        if kernel_bytes > KERNEL_PAYLOAD_LIMIT:
            raise SystemExit("kernel payload is %d bytes, the board accepts at most %d"
                             % (kernel_bytes, KERNEL_PAYLOAD_LIMIT))
        kernel_sha = hashlib.sha256()
        for path in (args.kernel_image, args.kernel_dtb):
            with path.open("rb") as stream:
                while True:
                    block = stream.read(8 * 1024 * 1024)
                    if not block:
                        break
                    kernel_sha.update(block)
        kernel_meta = {
            "bytes": kernel_bytes,
            "sha256": kernel_sha.hexdigest(),
            "delivery_version": args.version,
            "kernel_release": args.kernel,
            "image_bytes": image_bytes,
            "image_sha256": sha256_file(args.kernel_image),
            "dtb_name": args.kernel_dtb.name,
            "dtb_bytes": dtb_bytes,
            "dtb_sha256": sha256_file(args.kernel_dtb),
        }

    payload_bytes = args.image.stat().st_size
    # Pad the payload to a 4 KiB multiple: the board reads it with O_DIRECT,
    # which requires block aligned lengths. The hash covers the padded bytes.
    payload_pad = (-payload_bytes) % 4096
    digest = hashlib.sha256()
    with args.image.open("rb") as stream:
        while True:
            block = stream.read(8 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
    if payload_pad:
        digest.update(b"\0" * payload_pad)
    payload_sha = digest.hexdigest()
    payload_bytes += payload_pad
    compressed_bytes = 0
    padded_source = None
    if args.zstd:
        # The pad is part of the hashed logical stream, so the compressor
        # must see it: compress image+pad, then align the compressed payload
        # itself to 4 KiB (the board reads it with O_DIRECT).
        if payload_pad:
            padded_source = args.image.with_suffix(args.image.suffix + ".pad")
            with padded_source.open("wb") as out, args.image.open("rb") as src:
                while True:
                    block = src.read(8 * 1024 * 1024)
                    if not block:
                        break
                    out.write(block)
                out.write(b"\0" * payload_pad)
        zst_path, zst_size = zstd_compress_file(padded_source or args.image)
        if padded_source is not None:
            padded_source.unlink()
        zst_pad = (-zst_size) % 4096
        compressed_bytes = zst_size + zst_pad
        print(f"zstd: {payload_bytes} -> {zst_size} bytes "
              f"({payload_bytes * 100 // max(zst_size, 1)}% ratio, aligned {compressed_bytes})")
    kernel_offset = (payload_bytes + 4095) & ~4095
    if kernel_meta:
        # The file region is padded to 4 KiB so the board's O_DIRECT reads
        # never fall off an unaligned end; the manifest keeps the exact
        # unpadded kernel length.
        total_payload = kernel_offset + ((kernel_meta["bytes"] + 4095) & ~4095)
    else:
        total_payload = payload_bytes
    manifest = json.dumps({
        "schema": args.schema,
        "type": "sysroot",
        "board": args.board,
        "version": args.version,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "kernel": args.kernel,
        "payload": {"name": args.image.name, "bytes": payload_bytes, "sha256": payload_sha},
        **({"compressed_bytes": compressed_bytes} if compressed_bytes else {}),
        **({"payloads": {
            "rootfs": {"name": args.image.name, "bytes": payload_bytes,
                       "sha256": payload_sha, "offset": 0,
                       **({"compressed_bytes": compressed_bytes} if compressed_bytes else {})},
            "kernel": {"offset": kernel_offset, **kernel_meta},
        }} if kernel_meta else {}),
    }, sort_keys=True).encode()
    signature = sign_manifest(manifest, args.key)
    if args.schema == 2:
        magic = b"Y12PKG03" if args.board == "Y12" else MAGIC_DUAL
    elif args.zstd:
        magic = MAGIC_ZSTD
    else:
        magic = MAGIC
    header = magic + struct.pack("<IIQ", len(manifest), len(signature), total_payload)
    # The board reads the payload with O_DIRECT (its buffered read path
    # corrupts large reads), which requires a block-aligned file offset.
    pad = (-(len(header) + len(manifest) + len(signature))) % 4096
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("wb") as out:
        out.write(header)
        out.write(manifest)
        out.write(signature)
        out.write(b"\0" * pad)
        payload_src = Path(zst_path) if args.zstd else args.image
        with payload_src.open("rb") as stream:
            while True:
                block = stream.read(8 * 1024 * 1024)
                if not block:
                    break
                out.write(block)
        if payload_pad and not args.zstd:
            out.write(b"\0" * payload_pad)
        if args.zstd:
            out.write(b"\0" * zst_pad)
            zst_path.unlink()
        if kernel_meta:
            # The kernel payload starts exactly on its manifest boundary.
            out.write(b"\0" * (kernel_offset - payload_bytes))
            for path in (args.kernel_image, args.kernel_dtb):
                with path.open("rb") as stream:
                    while True:
                        block = stream.read(8 * 1024 * 1024)
                        if not block:
                            break
                        out.write(block)
            written = kernel_offset + kernel_meta["bytes"]
            out.write(b"\0" * (total_payload - written))
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(args.output)
    if args.delivery_dir:
        from release_package import create_ota, verify_archive
        delivery = create_ota(args.output, args.delivery_dir, args.jobs)
        verify_archive(delivery['archive']['path'], delivery['files'])
        print(json.dumps({"delivery": delivery['archive']}, indent=2))
    print(json.dumps({
        "package": str(args.output),
        "bytes": args.output.stat().st_size,
        "schema": args.schema,
        "version": args.version,
        "kernel": args.kernel,
        "rootfs_payload_sha256": payload_sha,
        **({"kernel_payload": kernel_meta} if kernel_meta else {}),
    }, indent=2))


if __name__ == "__main__":
    sys.exit(main())
