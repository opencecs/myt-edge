#!/usr/bin/env python3
"""Build and check a board-specific SPI bundle containing three vendor updates."""
import argparse
import hashlib
import json
import struct
import zipfile
from pathlib import Path


CIX = struct.Struct("<8I")
BUNDLE = struct.Struct("<8sII")
DESCRIPTOR = struct.Struct("<I32s")
CIX_MAGIC = 0x55AA55AA
BUNDLE_MAGIC = b"MYTSPI01"
# Unknown CIX image type deliberately rejected by the legacy updater.
BUNDLE_TYPE = 0xFE
BOARDS = {"Y4": 4, "Y12": 12}
# Packaged name, CIX type, SPI start, maximum payload, vendor control word.
PARTS = (
    ("ddr.bin", 3, 0x3F8000, 0x4000, 0x80000008),
    ("pmcfg.bin", 4, 0x3FC000, 0x1000, 0x80000010),
    ("uefi.bin", 7, 0x3FE000, 0x202000, 0x80000000),
)
DATA_OFFSET = CIX.size + BUNDLE.size + DESCRIPTOR.size * len(PARTS)
DIGEST_SIZE = hashlib.sha256().digest_size
MAX_BUNDLE_SIZE = DATA_OFFSET + DIGEST_SIZE + sum(max(4112, CIX.size + part[3]) for part in PARTS)


def _board_number(board):
    if board not in BOARDS:
        raise ValueError(f"Unknown SPI bundle board {board!r}")
    return BOARDS[board]


def _validate_part(data, spec):
    name, kind, base, limit, flags = spec
    if len(data) < CIX.size:
        raise ValueError(f"{name}: truncated CIX header")
    magic, version, count, control, image_type, address, length, reserved = CIX.unpack_from(data)
    if (magic, version, count, control, image_type, address, reserved) != (
            CIX_MAGIC, 1, 1, flags, kind, base, 0):
        raise ValueError(f"{name}: unexpected CIX header")
    if not 0 < length <= limit or (kind == 4 and length != limit):
        raise ValueError(f"{name}: invalid CIX payload length {length}")
    end = CIX.size + length
    if len(data) != max(4112, end) or any(data[end:]):
        raise ValueError(f"{name}: invalid vendor container length or padding")


def _outer_header(size):
    return (CIX_MAGIC, 1, 1, 0x80000000, BUNDLE_TYPE, 0, size - CIX.size, 0)


def build_spi_bundle(board, parts):
    """Return one bundle from the complete vendor DDR, PM and BL33 containers."""
    board_number = _board_number(board)
    parts = tuple(parts)
    if len(parts) != len(PARTS):
        raise ValueError("Expected exactly DDR, PM and BL33")
    for data, spec in zip(parts, PARTS):
        _validate_part(data, spec)
    size = DATA_OFFSET + sum(map(len, parts)) + DIGEST_SIZE
    table = b"".join(DESCRIPTOR.pack(len(data), hashlib.sha256(data).digest()) for data in parts)
    body = b"".join((CIX.pack(*_outer_header(size)), BUNDLE.pack(BUNDLE_MAGIC, board_number, 1),
                     table, *parts))
    return body + hashlib.sha256(body).digest()


def read_spi_bundle(data, board):
    """Return complete vendor containers only after the entire bundle validates."""
    board_number = _board_number(board)
    if len(data) < DATA_OFFSET + DIGEST_SIZE:
        raise ValueError("Truncated SPI bundle")
    data_end = len(data) - DIGEST_SIZE
    if hashlib.sha256(memoryview(data)[:data_end]).digest() != data[data_end:]:
        raise ValueError("SPI bundle checksum mismatch")
    if CIX.unpack_from(data) != _outer_header(len(data)):
        raise ValueError("Invalid SPI bundle header or length")
    if BUNDLE.unpack_from(data, CIX.size) != (BUNDLE_MAGIC, board_number, 1):
        raise ValueError("SPI bundle board, signature or version mismatch")
    cursor = DATA_OFFSET
    parts = []
    for index, spec in enumerate(PARTS):
        entry = CIX.size + BUNDLE.size + index * DESCRIPTOR.size
        length, digest = DESCRIPTOR.unpack_from(data, entry)
        if length > data_end - cursor:
            raise ValueError(f"{spec[0]}: truncated bundle")
        blob = data[cursor:cursor + length]
        _validate_part(blob, spec)
        if hashlib.sha256(blob).digest() != digest:
            raise ValueError(f"{spec[0]}: SPI bundle hash mismatch")
        parts.append(blob)
        cursor += length
    if cursor != data_end:
        raise ValueError("Unexpected data after the last SPI container")
    return parts


def parts_from_full_zip(path, board, *, legacy_test_fixture=False):
    """Read verified inputs from an existing full release ZIP without extracting the OS."""
    _board_number(board)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        filenames = ["images/" + name for name, *_ in PARTS]
        for name in ("SHA256SUMS", "tools/package.json"):
            if names.count(name) != 1:
                raise ValueError(f"Expected one {name} in {path}")
        for name in ("SHA256SUMS", "tools/package.json"):
            if archive.getinfo(name).file_size > 1024 * 1024:
                raise ValueError(f"{name}: oversized release metadata")
        sums = {}
        for line in archive.read("SHA256SUMS").decode("ascii").splitlines():
            digest, name = line.split("  ", 1)
            if name in sums:
                raise ValueError(f"Duplicate checksum for {name}")
            sums[name] = digest
        encoded = archive.read("tools/package.json")
        if hashlib.sha256(encoded).hexdigest() != sums.get("tools/package.json"):
            raise ValueError("Full package manifest checksum mismatch")
        manifest = json.loads(encoded)
        version = manifest.get("format")
        if (version not in ("y4-flash-v1", "y4-flash-v2") or manifest.get("hardware") != board + "-6187"
                or manifest.get("board_check") is not True):
            raise ValueError(f"Expected a board-checked {board} full release")
        combined = "images/myt-spi.bin"
        combined_present = (names.count(combined), int(combined in manifest["files"]), int(combined in sums))
        if combined_present not in ((0, 0, 0), (1, 1, 1)):
            raise ValueError("Inconsistent combined SPI archive inventory")
        if version == "y4-flash-v2":
            if (not combined_present[0] or
                    any(n in names or n in manifest["files"] or n in sums for n in filenames)):
                raise ValueError("Expected a single SPI image in the v2 release inventory")
            size = archive.getinfo(combined).file_size
            maximum = DATA_OFFSET + DIGEST_SIZE + sum(max(4112, CIX.size + part[3]) for part in PARTS)
            record = manifest["files"][combined]
            if size != record["bytes"] or size > maximum:
                raise ValueError("Invalid combined release image size")
            data = archive.read(combined)
            digest = hashlib.sha256(data).hexdigest()
            if digest != sums[combined] or digest != record["sha256"]:
                raise ValueError("Combined release image checksum mismatch")
            return read_spi_bundle(data, board)
        if any(names.count(name) != 1 for name in filenames):
            raise ValueError("Missing or repeated legacy SPI release member")
        if not combined_present[0] and not legacy_test_fixture:
            raise ValueError("Legacy UEFI lacks myt-spi support; --legacy-test-fixture is for offline tests only")
        result = []
        for name, spec in zip(filenames, PARTS):
            record = manifest["files"][name]
            size = archive.getinfo(name).file_size
            if not 4112 <= size <= max(4112, CIX.size + spec[3]) or record["bytes"] != size:
                raise ValueError(f"{name}: invalid archive size")
            blob = archive.read(name)
            digest = hashlib.sha256(blob).hexdigest()
            if digest != record["sha256"] or digest != sums.get(name):
                raise ValueError(f"{name}: release checksum mismatch")
            _validate_part(blob, spec)
            result.append(blob)
        if combined_present[0]:
            size = DATA_OFFSET + sum(map(len, result)) + DIGEST_SIZE
            record = manifest["files"][combined]
            if archive.getinfo(combined).file_size != size or record["bytes"] != size:
                raise ValueError("Combined release image size mismatch")
            data = archive.read(combined)
            digest = hashlib.sha256(data).hexdigest()
            if (digest != sums[combined] or digest != record["sha256"]
                    or read_spi_bundle(data, board) != result):
                raise ValueError("Combined release image differs from verified components")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True, choices=BOARDS)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--full-zip", type=Path, help="full release containing the matching myt-spi image")
    source.add_argument("--bundle", type=Path, help="standalone board-specific myt-spi.bin")
    parser.add_argument("--extract-bootstrap", action="store_true",
                        help="extract the complete vendor-wrapped BL33 receiver for a legacy board")
    parser.add_argument("--legacy-test-fixture", action="store_true",
                        help="allow offline parser fixtures from legacy full ZIPs; do not flash these fixtures")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.legacy_test_fixture and not args.full_zip:
        parser.error("--legacy-test-fixture requires --full-zip")
    if bool(args.bundle) != args.extract_bootstrap:
        parser.error("--bundle and --extract-bootstrap must be used together")
    if args.bundle:
        image = read_spi_bundle(args.bundle.read_bytes(), args.board)[2]
    else:
        sources = parts_from_full_zip(args.full_zip, args.board, legacy_test_fixture=args.legacy_test_fixture)
        image = build_spi_bundle(args.board, sources)
        if read_spi_bundle(image, args.board) != sources:
            raise ValueError("Generated SPI bundle failed to round-trip")
    with args.output.open("xb") as out:
        out.write(image)
    print(f"{hashlib.sha256(image).hexdigest()}  {args.output}")
