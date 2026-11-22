import hashlib
import json
import struct
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import spi_bundle as spi


def component(spec, payload):
    _, kind, address, _, flags = spec
    header = struct.pack("<8I", spi.CIX_MAGIC, 1, 1, flags, kind, address, len(payload), 0)
    return (header + payload).ljust(4112, b"\0")


def word(data, offset, value):
    result = bytearray(data)
    struct.pack_into("<I", result, offset, value)
    return bytes(result)


def refresh_footer(bundle):
    return bytes(bundle[:-32]) + hashlib.sha256(bundle[:-32]).digest()


class SpiBundleTest(unittest.TestCase):
    def setUp(self):
        self.parts = [component(spec, bytes([kind]) * size)
                      for spec, kind, size in zip(spi.PARTS, (3, 4, 7), (0x5a0, 4096, 8192))]
        self.bundle = spi.build_spi_bundle("Y4", self.parts)

    def test_round_trip_and_wire_layout(self):
        for board, board_number in (("Y4", 4), ("Y12", 12)):
            with self.subTest(board=board):
                bundle = spi.build_spi_bundle(board, self.parts)
                self.assertEqual(spi.read_spi_bundle(bundle, board), self.parts)
                self.assertEqual(struct.unpack_from("<8I", bundle),
                                 (0x55aa55aa, 1, 1, 0x80000000, 0xfe, 0, len(bundle) - 32, 0))
                self.assertEqual(struct.unpack_from("<8sII", bundle, 32), (b"MYTSPI01", board_number, 1))
                self.assertEqual(bundle[156:-32], b"".join(self.parts))
                self.assertEqual(bundle[-32:], hashlib.sha256(bundle[:-32]).digest())
                for n, part in enumerate(self.parts):
                    self.assertEqual(struct.unpack_from("<I32s", bundle, 48 + n * 36),
                                     (len(part), hashlib.sha256(part).digest()))

    def test_unsupported_boards_and_wrong_target(self):
        with self.assertRaises(ValueError):
            spi.read_spi_bundle(self.bundle, "Y12")
        with self.assertRaises(ValueError):
            spi.build_spi_bundle("Y42", self.parts)
        # A bit flip in the sender's board ID must not turn a Y4 bundle into Y12.
        with self.assertRaisesRegex(ValueError, "checksum"):
            spi.read_spi_bundle(word(self.bundle, 40, 12), "Y12")

    def test_reject_outer_corruption_before_return(self):
        for offset in (0, 4, 8, 12, 16, 20, 24, 28, 32, 40, 44,
                       48, 52, 84, 88, 120, 124):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                spi.read_spi_bundle(refresh_footer(word(self.bundle, offset, 0x77777777)), "Y4")
        for bad in (b"", self.bundle[:154], self.bundle[:-1], self.bundle + b"X",
                    refresh_footer(word(self.bundle[:-32] + b"X" + bytes(32), 24,
                                        len(self.bundle) + 1 - 32))):
            with self.subTest(length=len(bad)), self.assertRaises(ValueError):
                spi.read_spi_bundle(bad, "Y4")

    def test_legacy_or_wrong_order(self):
        for bad in (self.parts, self.parts[::-1], self.parts + self.parts[:1]):
            with self.subTest(length=len(bad)):
                if bad == self.parts:
                    for single in bad:
                        with self.assertRaises(ValueError):
                            spi.read_spi_bundle(single, "Y4")
                else:
                    with self.assertRaises(ValueError):
                        spi.build_spi_bundle("Y4", bad)

    def test_malformed_components(self):
        for n, original in enumerate(self.parts):
            invalid = [word(original, offset, 0xffffffff)
                       for offset in (0, 4, 8, 12, 16, 20, 24, 28)]
            invalid += [original[:-1], original + b"\0", word(original, 24, 0)]
            if n == 0:
                invalid.append(original[:-1] + b"\1")  # nonzero vendor padding
            if n == 1:
                invalid.append(component(spi.PARTS[1], bytes(4095)))
            for bad in invalid:
                parts = self.parts.copy()
                parts[n] = bad
                with self.subTest(index=n), self.assertRaises(ValueError):
                    spi.build_spi_bundle("Y4", parts)

    def test_even_rehashed_wrong_nested_address_is_refused(self):
        bundle = bytearray(self.bundle)
        blob = word(self.parts[2], 20, 0x10000)
        bundle[-len(blob) - 32:-32] = blob
        bundle[124:156] = hashlib.sha256(blob).digest()
        with self.assertRaises(ValueError):
            spi.read_spi_bundle(refresh_footer(bundle), "Y4")

    def test_last_component_corruption(self):
        with self.assertRaises(ValueError):
            spi.read_spi_bundle(self.bundle[:-1] + bytes([self.bundle[-1] ^ 1]), "Y4")
        bundle = bytearray(self.bundle)
        bundle[-33] ^= 1
        with self.assertRaisesRegex(ValueError, "uefi.bin: SPI bundle hash"):
            spi.read_spi_bundle(refresh_footer(bundle), "Y4")

    def zip_source(self, destination, *, duplicate=False, bad_size=False, wrong_hash=False,
                   combined=None, extra_combined=False, version=1, singles=True):
        names = ["images/" + spec[0] for spec in spi.PARTS]
        items = dict(zip(names, self.parts)) if singles else {}
        if combined is not None:
            items["images/myt-spi.bin"] = combined
        manifest = dict(format=f"y4-flash-v{version}", hardware="Y4-6187", board_check=True, files={})
        for name, data in items.items():
            manifest["files"][name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        if bad_size:
            manifest["files"][names[1]]["bytes"] += 1
        if wrong_hash:
            manifest["files"][names[2]]["sha256"] = "0" * 64
        items["tools/package.json"] = json.dumps(manifest).encode()
        sums = [f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in items.items()]
        with zipfile.ZipFile(destination, "w") as archive:
            for name, data in items.items():
                archive.writestr(name, data)
            archive.writestr("SHA256SUMS", "".join(sums))
            if duplicate:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(names[1], self.parts[1])
            if extra_combined:
                archive.writestr("images/myt-spi.bin", self.bundle)

    def test_archive_board_hash_sizes_unique_members(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "full.zip"
            self.zip_source(path)
            self.assertEqual(spi.parts_from_full_zip(path, "Y4", legacy_test_fixture=True), self.parts)
            with self.assertRaisesRegex(ValueError, "Legacy"):
                spi.parts_from_full_zip(path, "Y4")
            with self.assertRaises(ValueError):
                spi.parts_from_full_zip(path, "Y12")
            for defect in ("duplicate", "bad_size", "wrong_hash"):
                self.zip_source(path, **{defect: True})
                with self.subTest(defect=defect), self.assertRaises(ValueError):
                    spi.parts_from_full_zip(path, "Y4", legacy_test_fixture=True)

    def test_modern_full_zip_requires_consistent_combined_image(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "full.zip"
            self.zip_source(path, combined=self.bundle)
            self.assertEqual(spi.parts_from_full_zip(path, "Y4"), self.parts)
            other = self.parts.copy()
            other[1] = component(spi.PARTS[1], b"P" * 4096)
            self.zip_source(path, combined=spi.build_spi_bundle("Y4", other))
            with self.assertRaisesRegex(ValueError, "differs"):
                spi.parts_from_full_zip(path, "Y4")
            self.zip_source(path, extra_combined=True)
            with self.assertRaisesRegex(ValueError, "inventory"):
                spi.parts_from_full_zip(path, "Y4", legacy_test_fixture=True)

    def test_single_file_release(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "full.zip"
            self.zip_source(path, combined=self.bundle, singles=False, version=2)
            self.assertEqual(spi.parts_from_full_zip(path, "Y4"), self.parts)
            for singles, bundle in ((True, self.bundle), (False, None),
                                    (False, spi.build_spi_bundle("Y12", self.parts)),
                                    (False, self.bundle[:-1])):
                self.zip_source(path, combined=bundle, singles=singles, version=2)
                with self.subTest(singles=singles), self.assertRaises(ValueError):
                    spi.parts_from_full_zip(path, "Y4", legacy_test_fixture=True)

if __name__ == "__main__":
    unittest.main()
