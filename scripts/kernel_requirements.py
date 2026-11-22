#!/usr/bin/env python3
"""Validate the kernel payloads paired with a rootfs source snapshot.

inputs-kernel.json is an explicit acceptance pin for this rootfs, not a
permanent kernel version ceiling. After a rebuilt/new kernel is validated,
update its payload digests and provenance together with the rootfs recipe.
Optional DTB and modules-archive pins distinguish modular/DT-only repairs
whose Image bytes are unchanged. Old Image-only policies remain supported.
Historical snapshots without hardware-decoder defaults need no new manifest.
"""
import hashlib
import json
import re
from pathlib import Path

MANIFEST = "fetch/inputs-kernel.json"
CONFIG = "etc/chromium.d"
FEATURE = "AcceleratedVideoDecoder"


def _hardware_decoder_enabled(root):
    config = Path(root) / CONFIG
    if not config.is_dir():
        return False
    for path in config.iterdir():
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            values = re.findall(r"""--enable-features(?:=|\s+)["']?([^\s"']+)""", line)
            for value in values:
                if any(re.split(r"[:<]", name, maxsplit=1)[0] == FEATURE
                       for name in value.split(",")):
                    return True
    return False


def kernel_requirement(rootfs_source, staged_root=None, *, board="Y4"):
    """Read only this rootfs's policy; never borrow the live checkout policy."""
    rootfs_source = Path(rootfs_source)
    if board not in ("Y4", "Y12"):
        raise ValueError("Unknown kernel compatibility board: " + board)
    manifest_path = MANIFEST if board == "Y4" else "fetch/inputs-kernel-y12.json"
    path = rootfs_source / manifest_path
    enabled = _hardware_decoder_enabled(rootfs_source / "overlay")
    if staged_root is not None:
        enabled = enabled or _hardware_decoder_enabled(staged_root)
    if not path.exists():
        if board == "Y12":
            raise ValueError(f"Y12 kernel compatibility requires {path}")
        if path.is_symlink() or enabled:
            raise ValueError("Chromium hardware decoding requires " + str(path))
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("Kernel compatibility manifest must be a regular file: " + str(path))
    try:
        manifest = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise ValueError("Invalid kernel compatibility manifest: " + str(path)) from exc
    if not isinstance(manifest, dict) or type(manifest.get("schema")) is not int or manifest["schema"] != 1:
        raise ValueError("Expected kernel compatibility manifest schema 1")
    if board == "Y12" and (manifest.get("board") != board or
                          not {"dtb_sha256", "modules_archive_sha256"} <= manifest.keys()):
        raise ValueError("Y12 compatibility requires a Y12 manifest pinning DTB and modules")
    if not isinstance(manifest.get("image_sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", manifest["image_sha256"]):
        raise ValueError("Kernel compatibility manifest requires a valid Image SHA256")
    for field in ("dtb_sha256", "modules_archive_sha256"):
        if field in manifest and (not isinstance(manifest[field], str) or not re.fullmatch(
                r"[0-9a-f]{64}", manifest[field])):
            raise ValueError("Kernel compatibility manifest requires a valid " + field)
    if not isinstance(manifest.get("reason"), str) or not manifest["reason"].strip():
        raise ValueError("Kernel compatibility manifest requires a reason")
    source = manifest.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("base_commit"), str) or not re.fullmatch(
            r"[0-9a-f]{40}", source["base_commit"]):
        raise ValueError("Kernel compatibility manifest requires source.base_commit")
    if not isinstance(source.get("patch_sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", source["patch_sha256"]):
        raise ValueError("Kernel compatibility manifest requires source.patch_sha256")
    return manifest


def _verify_payload(path, expected, label, manifest_path):
    if path is None:
        raise ValueError("Kernel compatibility manifest requires the " + label + " payload")
    path = Path(path)
    if not path.is_file():
        raise ValueError("Kernel compatibility payload is missing: " + str(path))
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    actual = h.hexdigest()
    if actual != expected:
        raise ValueError("Kernel " + label + " is not validated for this rootfs: "
                         + str(path) + " (expected " + expected + ", got " + actual
                         + "). Validate the rebuilt kernel and update " + manifest_path
                         + " with its payload digests and provenance.")
    return actual


def verify_kernel_compatibility(rootfs_source, image=None, staged_root=None, *,
                                dtb=None, modules_archive=None, board="Y4"):
    """Validate actual payloads; optional strict pins fail if payloads are absent.

    An Image-only legacy policy can still be read before its source build. A
    policy pinning a module archive requires that validated bundle as an input;
    unarchived source/component reuse cannot silently bypass that constraint.
    """
    manifest = kernel_requirement(rootfs_source, staged_root, board=board)
    if manifest is None:
        return None
    manifest_path = MANIFEST if board == "Y4" else "fetch/inputs-kernel-y12.json"
    result = {"manifest": str(Path(rootfs_source) / manifest_path), **manifest}
    if image is not None or any(field in manifest for field in ("dtb_sha256", "modules_archive_sha256")):
        result["verified_image_sha256"] = _verify_payload(image, manifest["image_sha256"], "Image", manifest_path)
    for field, payload, label in (("dtb_sha256", dtb, "DTB"),
                                  ("modules_archive_sha256", modules_archive, "modules archive")):
        if field in manifest:
            result["verified_" + field] = _verify_payload(payload, manifest[field], label, manifest_path)
    return result
