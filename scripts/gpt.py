#!/usr/bin/env python3
"""Create a Debian A/B slot GPT template for source-built UEFI adaptation.

Four fixed partitions: boot (512 MiB ESP holding the kernel repository),
slot-a and slot-b (16 GiB each, receiving the identical root image), and
data (all remaining capacity, provisioned on first boot and never rewritten
by OTA). Partition GUIDs are fixed constants derived from a
build-independent namespace, so grub.cfg, fstab and the OTA agent address
the same partitions in every image ever flashed.
"""
import hashlib
import struct
import uuid
import zlib

MIB = 1024 * 1024
GIB = 1024 * MIB
BOOT_START = 10 * MIB
BOOT_BYTES = 512 * MIB
SLOT_BYTES = 16 * GIB
SLOTA_START = BOOT_START + BOOT_BYTES
SLOTB_START = SLOTA_START + SLOT_BYTES
DATA_START = SLOTB_START + SLOT_BYTES
DATA_MINIMUM_BYTES = 64 * MIB
UINT64_MAX = 2**64 - 1
LABELS = ("boot", "slot-a", "slot-b", "data")
TYPES = (uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b"),
         uuid.UUID("b921b045-1df0-41c3-af44-4c6f280d3fae"),
         uuid.UUID("b921b045-1df0-41c3-af44-4c6f280d3fae"),
         uuid.UUID("b921b045-1df0-41c3-af44-4c6f280d3fae"))
PART_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "MYT/Y4/Debian/partitions-v1")
PART_IDS = tuple(uuid.uuid5(PART_NAMESPACE, label) for label in LABELS)


def boot_bytes(sector):
    if sector not in (512, 4096):
        raise ValueError("Only 512/4096-byte logical sectors are supported")
    return BOOT_BYTES


def slot_a_start(sector):
    return BOOT_START + boot_bytes(sector)


def data_start(sector):
    return SLOTB_START + SLOT_BYTES


def minimum_capacity(sector=512):
    if sector not in (512, 4096):
        raise ValueError("Only 512/4096-byte logical sectors are supported")
    value = data_start(sector) + DATA_MINIMUM_BYTES + MIB
    if value > UINT64_MAX:
        raise ValueError("Layout exceeds the 64-bit byte capacity limit")
    return value


MIN_BYTES = minimum_capacity()


def plan(capacity, sector):
    if (type(capacity) is not int or type(sector) is not int or
            sector not in (512, 4096) or capacity < minimum_capacity(sector) or
            capacity > UINT64_MAX or capacity % sector):
        raise ValueError("Disk must fit the slot layout and have 512/4096-byte logical sectors")
    last = capacity // sector - 16384 // sector - 2
    return {"total_lbas": capacity // sector,
            "first_usable_lba": 2 + 16384 // sector,
            "last_usable_lba": last,
            "partitions": [
                {"label": "boot", "first_lba": BOOT_START // sector,
                 "last_lba": SLOTA_START // sector - 1},
                {"label": "slot-a", "first_lba": SLOTA_START // sector,
                 "last_lba": SLOTA_START // sector + SLOT_BYTES // sector - 1},
                {"label": "slot-b", "first_lba": SLOTB_START // sector,
                 "last_lba": SLOTB_START // sector + SLOT_BYTES // sector - 1},
                {"label": "data", "first_lba": DATA_START // sector,
                 "last_lba": ((last + 1) * sector // MIB) * MIB // sector - 1}]}


def packed(sector, disk_guid, part_ids=PART_IDS, *, capacity=None):
    if len(part_ids) != 4 or len(set(str(value) for value in part_ids)) != 4:
        raise ValueError("Exactly four distinct partition identifiers are required")
    template_bytes = minimum_capacity(sector) if capacity is None else capacity
    layout = plan(template_bytes, sector)
    ids = [uuid.UUID(str(value)) for value in part_ids]
    table = bytearray(16384)
    for i, (part, kind, ident) in enumerate(zip(layout["partitions"], TYPES, ids)):
        at = i * 128
        table[at:at + 16] = kind.bytes_le
        table[at + 16:at + 32] = ident.bytes_le
        struct.pack_into("<QQQ", table, at + 32, part["first_lba"], part["last_lba"], 0)
        name = part["label"].encode("utf-16le")
        table[at + 56:at + 56 + len(name)] = name
        part.update(unique_guid=str(ident), type_guid=str(kind))
    crc = zlib.crc32(table) & 0xffffffff
    last = layout["total_lbas"] - 1

    def header(mine, alternate, entries_lba):
        value = bytearray(sector)
        struct.pack_into("<8sIIIIQQQQ16sQIII", value, 0, b"EFI PART", 0x10000,
                         92, 0, 0, mine, alternate, layout["first_usable_lba"],
                         layout["last_usable_lba"], disk_guid.bytes_le,
                         entries_lba, 128, 128, crc)
        struct.pack_into("<I", value, 16, zlib.crc32(value[:92]) & 0xffffffff)
        return value

    mbr = bytearray(sector)
    mbr[450] = 0xee
    struct.pack_into("<II", mbr, 454, 1, min(last, 0xffffffff))
    mbr[510:512] = b"\x55\xaa"
    image = bytes(mbr + header(1, last, 2) + table + table +
                  header(last, 1, last - 16384 // sector))
    return image, dict(layout, sector_bytes=sector, template_bytes=template_bytes,
                       disk_guid=str(disk_guid), slot_bytes=SLOT_BYTES,
                       part_ids=[str(value) for value in ids],
                       sha256=hashlib.sha256(image).hexdigest(), profile="debian",
                       adaptive_data_partition=True, required_flash_protocol=3,
                       capacity_source="UEFI system NVMe Block I/O")
