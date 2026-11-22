#!/usr/bin/env python3
"""Check ota_package.py end to end: both schemas, the packing gates, the
signed manifest and the exact payload layout it declares.

Runs the packer as a subprocess against synthetic build outputs (tiny ext4
sparse image, fake kernel Image/DTB, a throwaway Ed25519 key) and asserts
the produced bytes: magic, header arithmetic, manifest offsets, payload
digests and the Ed25519 signature itself."""
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKER = HERE / "ota_package.py"
VERSION = "20260101-r1"
RELEASE = "6.6.89-cix-build-generic"
DTB_NAME = "sky1-y4.dtb"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_kernel(tmp, release=RELEASE, announce=True):
    image = tmp / "Image"
    payload = b"arm64-image-prefix"
    if announce:
        payload += b"\x1fLinux version " + release.encode() + b" (builder@y4) #1 SMP\n\x00padding"
    image.write_bytes(payload)
    dtb = tmp / DTB_NAME
    dtb.write_bytes(b"\xd0\x0d\xfe\xed" + b"dtb-payload" * 3)
    return image, dtb


def make_rootfs_sparse(tmp, name, kernel_image):
    """A tiny ext4 filesystem, sparse-encoded, whose /etc/y4-release.json
    stamps the given kernel Image the way build.py stamps a real one."""
    content = json.dumps({
        "rootfs": VERSION, "kernel": RELEASE,
        "kernel_image_sha256_16": sha(kernel_image.read_bytes())[:16],
    }).encode()
    destdir = tmp / (name + "-destdir")
    (destdir / "etc").mkdir(parents=True)
    (destdir / "lib/modules" / RELEASE).mkdir(parents=True)
    (destdir / "etc/y4-release.json").write_bytes(content)
    (destdir / ("lib/modules/" + RELEASE + "/placeholder.ko")).write_bytes(b"ko")
    raw = tmp / (name + ".raw")
    with raw.open("wb") as f:
        f.truncate(4 * 1024 * 1024)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-b", "1024", "-d", str(destdir), str(raw)], check=True)
    sparse = tmp / (name + ".sparse")
    subprocess.run(["img2simg", str(raw), str(sparse), "4096"], check=True)
    return sparse, content


def parse_package(path):
    blob = path.read_bytes()
    magic = blob[:8]
    manifest_bytes, signature_bytes, payload_bytes = struct.unpack_from("<IIQ", blob, 8)
    manifest = json.loads(blob[24:24 + manifest_bytes])
    signature = blob[24 + manifest_bytes:24 + manifest_bytes + signature_bytes]
    payload_at = (24 + manifest_bytes + signature_bytes + 4095) & ~4095
    return magic, manifest, signature, payload_at, payload_bytes, blob


def run(args, expect_ok=True):
    result = subprocess.run([sys.executable, str(PACKER)] + args,
                            capture_output=True, text=True)
    if expect_ok and result.returncode != 0:
        raise AssertionError("packer failed: " + result.stderr)
    if not expect_ok and result.returncode == 0:
        raise AssertionError("packer accepted: " + " ".join(args))
    return result


def main():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        key = tmp / "ota-test.key"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)], check=True)
        key_arg = ["--key", str(key)]

        image, dtb = make_kernel(tmp)
        sparse, content = make_rootfs_sparse(tmp, "slot", image)

        # Legacy schema 1 from an old build output: no anchor, loud but works.
        legacy = tmp / "legacy"
        legacy.mkdir()
        (legacy / "rootfs_sparse.ext4").write_bytes(sparse.read_bytes())
        run(["--image", str(legacy / "rootfs_sparse.ext4"), "--version", VERSION,
             "--kernel", RELEASE] + key_arg + ["--output", str(tmp / "s1-legacy.y4pkg")])
        magic, manifest, signature, payload_at, payload_bytes, blob = \
            parse_package(tmp / "s1-legacy.y4pkg")
        assert magic == b"Y4PKG001" and manifest["schema"] == 1 and "payloads" not in manifest
        assert payload_at + payload_bytes == (tmp / "s1-legacy.y4pkg").stat().st_size
        assert hashlib.sha256(blob[payload_at:payload_at + payload_bytes]).hexdigest() \
            == manifest["payload"]["sha256"]

        # A current build output: the anchor enables the gates.
        current = tmp / "current"
        current.mkdir()
        (current / "rootfs_sparse.ext4").write_bytes(sparse.read_bytes())
        (current / "y4-release.json").write_bytes(content)
        run(["--image", str(current / "rootfs_sparse.ext4"), "--version", VERSION,
             "--kernel", RELEASE] + key_arg + ["--output", str(tmp / "s1.y4pkg")])

        # Schema 2: the atomic rootfs+kernel pair.
        run(["--image", str(current / "rootfs_sparse.ext4"), "--version", VERSION,
             "--kernel", RELEASE, "--schema", "2",
             "--kernel-image", str(image), "--kernel-dtb", str(dtb)]
            + key_arg + ["--output", str(tmp / "s2.y4pkg"),
                         "--delivery-dir", str(tmp / "delivery"), "--jobs", "2"])
        magic, manifest, signature, payload_at, total, blob = parse_package(tmp / "s2.y4pkg")
        assert magic == b"Y4PKG003" and manifest["schema"] == 2
        with zipfile.ZipFile(tmp / "delivery" / f"Y4-{VERSION}-ota.zip") as bundle:
            assert set(bundle.namelist()) == {f"Y4-{VERSION}.y4pkg", "README.txt", "SHA256SUMS"}
            assert bundle.read(f"Y4-{VERSION}.y4pkg") == blob
        rootfs = manifest["payloads"]["rootfs"]
        kernel = manifest["payloads"]["kernel"]
        assert rootfs["offset"] == 0 and rootfs["bytes"] == manifest["payload"]["bytes"]
        assert rootfs["sha256"] == manifest["payload"]["sha256"]
        assert kernel["offset"] == (rootfs["bytes"] + 4095) & ~4095
        assert kernel["delivery_version"] == VERSION and kernel["kernel_release"] == RELEASE
        assert kernel["dtb_name"] == DTB_NAME
        assert kernel["bytes"] == kernel["image_bytes"] + kernel["dtb_bytes"]
        rootfs_region = blob[payload_at + rootfs["offset"]:payload_at + rootfs["bytes"]]
        assert hashlib.sha256(rootfs_region).hexdigest() == rootfs["sha256"]
        kernel_at = payload_at + kernel["offset"]
        kernel_region = blob[kernel_at:kernel_at + kernel["bytes"]]
        assert hashlib.sha256(kernel_region).hexdigest() == kernel["sha256"]
        assert kernel_region == image.read_bytes() + dtb.read_bytes()
        assert kernel["image_sha256"] == sha(image.read_bytes())
        assert kernel["dtb_sha256"] == sha(dtb.read_bytes())
        assert kernel_at + ((kernel["bytes"] + 4095) & ~4095) == (tmp / "s2.y4pkg").stat().st_size
        # the region beyond the exact kernel length is zero padding
        # The Ed25519 signature verifies against the manifest, and a tampered
        # manifest does not.
        manifest_file = tmp / "manifest.json"
        manifest_file.write_bytes(json.dumps(manifest, sort_keys=True).encode())
        pub_file = tmp / "pub.pem"
        pub_file.write_bytes(subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout"],
                                            check=True, capture_output=True).stdout)
        sig_file = tmp / "sig.bin"
        sig_file.write_bytes(signature)
        verify = subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin",
                                 "-inkey", str(pub_file), "-in", str(manifest_file),
                                 "-sigfile", str(sig_file)], capture_output=True)
        assert verify.returncode == 0 and b"Signature Verified" in verify.stdout
        tampered = json.loads(json.dumps(manifest))
        tampered["payloads"]["kernel"]["bytes"] += 1
        manifest_file.write_bytes(json.dumps(tampered, sort_keys=True).encode())
        verify = subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin",
                                 "-inkey", str(pub_file), "-in", str(manifest_file),
                                 "-sigfile", str(sig_file)], capture_output=True)
        assert verify.returncode != 0

        # Gates: wrong --version / --kernel are refused.
        run(["--image", str(current / "rootfs_sparse.ext4"), "--version", "19990101-r1",
             "--kernel", RELEASE] + key_arg + ["--output", str(tmp / "x1.y4pkg")], expect_ok=False)
        run(["--image", str(current / "rootfs_sparse.ext4"), "--version", VERSION,
             "--kernel", "6.1.0-cix-build-generic"] + key_arg + ["--output", str(tmp / "x2.y4pkg")],
            expect_ok=False)
        # UTS gate: strip the stamped Image digest from the anchor so the
        # UTS scan is what rejects these.
        anchor = json.loads(content)
        del anchor["kernel_image_sha256_16"]
        (current / "y4-release.json").write_text(json.dumps(anchor))
        silent_image, silent_dtb = make_kernel(tmp, announce=False)
        run(["--image", str(current / "rootfs_sparse.ext4"), "--version", VERSION,
             "--kernel", RELEASE, "--schema", "2",
             "--kernel-image", str(silent_image), "--kernel-dtb", str(silent_dtb)]
            + key_arg + ["--output", str(tmp / "x3.y4pkg")], expect_ok=False)
        wrong_image, wrong_dtb = make_kernel(tmp, release="6.6.90-cix-build-generic")
        run(["--image", str(current / "rootfs_sparse.ext4"), "--version", VERSION,
             "--kernel", RELEASE, "--schema", "2",
             "--kernel-image", str(wrong_image), "--kernel-dtb", str(wrong_dtb)]
            + key_arg + ["--output", str(tmp / "x4.y4pkg")], expect_ok=False)
        (current / "y4-release.json").write_bytes(content)
        # Schema 2 without the anchor is refused; schema 1 is the legacy path.
        run(["--image", str(legacy / "rootfs_sparse.ext4"), "--version", VERSION,
             "--kernel", RELEASE, "--schema", "2",
             "--kernel-image", str(image), "--kernel-dtb", str(dtb)]
            + key_arg + ["--output", str(tmp / "x5.y4pkg")], expect_ok=False)
        # A sparse image whose filesystem contradicts the package copy.
        other, _ = make_rootfs_sparse(tmp, "other", image)
        (current / "rootfs_sparse.ext4").write_bytes(other.read_bytes())
        run(["--image", str(current / "rootfs_sparse.ext4"), "--version", VERSION,
             "--kernel", RELEASE] + key_arg + ["--output", str(tmp / "x6.y4pkg")], expect_ok=False)
        print("test_ota_package: all checks passed")


if __name__ == "__main__":
    main()
