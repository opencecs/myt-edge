# SPDX-License-Identifier: Apache-2.0
"""Read prebuilt ARM64 host tools and signed MCU firmware for a single board."""
import hashlib
import json
from pathlib import Path
import re
import struct

SIGNED_FILES = ("mcu-package.bin", "mcu-builtin.json", "mcu-public-key.bin", "host-version")
MAINTAINER = "host/y4-mcu-maintain"
Y4_HOSTS = ("mcud", "mcuctl", "hexverify", "y4-mcu-reset", "y4-mcu-flash")
Y4_UNITS = ("y4-mcud.service", "y4-mcu-boot.service")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
LICENSE_PATH = re.compile(r"licenses/[A-Za-z0-9_.+-]+")
MAX_FILE_BYTES = 16 * 1024 * 1024

def sha256(data):
    return hashlib.sha256(data).hexdigest()

def artifact_path(root, name):
    path = root / name
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root and root in parent.parents):
        raise ValueError("symlink not permitted in MCU asset path: " + name)
    if root not in path.resolve().parents or not path.is_file():
        raise ValueError("missing or invalid MCU asset: " + name)
    return path

def read_asset(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("oversized MCU asset: " + path.name)
    return data

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field in MCU asset manifest: " + key)
        result[key] = value
    return result

def load_assets(directory, board, manifest_sha256=None):
    """Return verified artifact bytes. A caller may pin the manifest's external digest."""
    if board not in ("Y4", "Y12"):
        raise ValueError("unknown MCU asset board")
    root = Path(directory).resolve(strict=True)
    manifest = read_asset(artifact_path(root, "manifest.json"), 65536)
    if manifest_sha256 is not None and (
            not DIGEST.fullmatch(manifest_sha256) or sha256(manifest) != manifest_sha256):
        raise ValueError("MCU asset manifest SHA256 mismatch")
    meta = json.loads(manifest, object_pairs_hook=unique_object)
    if (not isinstance(meta, dict) or type(meta.get("schema")) is not int or meta["schema"] != 1
            or meta.get("board") != board or not isinstance(meta.get("files"), dict)):
        raise ValueError("MCU asset manifest board or schema mismatch")
    binary = f"firmware/build/{board.lower()}-mcu.bin"
    required = set(SIGNED_FILES) | {binary, MAINTAINER}
    if board == "Y4":
        required.update({"firmware/build/y4-mcu.hex", *("host/" + name for name in Y4_HOSTS),
                         *("system/" + name for name in Y4_UNITS)})
    records = meta["files"]
    if not required <= records.keys() or any(
            not LICENSE_PATH.fullmatch(path) for path in records.keys() - required):
        raise ValueError("missing required or unsupported file in MCU asset manifest")
    checked = {}
    for name, record in sorted(records.items()):
        limit = 1048576 if name.startswith("licenses/") else MAX_FILE_BYTES
        if (not isinstance(record, dict) or type(record.get("bytes")) is not int
                or not 0 < record["bytes"] <= limit or not DIGEST.fullmatch(str(record.get("sha256", "")))):
            raise ValueError("invalid MCU asset digest record: " + name)
        data = read_asset(artifact_path(root, name), record["bytes"])
        if len(data) != record["bytes"] or sha256(data) != record["sha256"]:
            raise ValueError("MCU asset checksum mismatch: " + name)
        if name.startswith("host/") and (len(data) < 64 or data[:6] != b"\x7fELF\x02\x01"
                                        or struct.unpack_from("<H", data, 18)[0] != 183
                                        or struct.unpack_from("<H", data, 16)[0] not in (2, 3)):
            raise ValueError("AArch64 executable required for MCU host tool: " + name)
        checked[name] = data
    return checked
