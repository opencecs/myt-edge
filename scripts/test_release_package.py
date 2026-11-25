#!/usr/bin/env python3
"""Check the customer boundary and that packaging does not change firmware."""
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

import release_package as delivery
from spi_bundle import PARTS, build_spi_bundle
from test_spi_bundle import component


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root/'system'
        self.output = self.root/'output'
        self.source = self.root/'host'
        (self.package/'tools').mkdir(parents=True)
        self.source.mkdir()
        for name in delivery.HOST_FILES:
            (self.source/name).write_text(name)
        for name in delivery.SYSTEM_PAYLOADS:
            (self.package/name).write_bytes(name.encode())
        for name in delivery.WINDOWS_TOOLS:
            (self.package/'tools'/name).write_bytes(name.encode())
        self.spi = self.root/'myt-spi.bin'

    def select(self, board):
        (self.package/'y4-release.json').write_text(json.dumps(dict(board=board,rootfs='20261113-r202')))
        names = set(delivery.SYSTEM_PAYLOADS) | {'tools/'+n for n in delivery.WINDOWS_TOOLS} | {'y4-release.json'}
        (self.package/'MANIFEST.json').write_text(json.dumps(dict(
            source_commit='0'*40, hardware=board+'-6187',
            files={n:delivery.item(self.package/n) for n in names})))
        parts = [component(spec, bytes([spec[1]])*(4096 if spec[1]==4 else 256))
                 for spec in PARTS]
        self.spi.write_bytes(build_spi_bundle(board, parts))

    def full(self):
        return delivery.create_full(self.package, self.source, self.output, jobs=2, spi_bin=self.spi)

    def test_both_boards_use_bin_unchanged(self):
        for board in ('Y4','Y12'):
            with self.subTest(board=board):
                self.select(board)
                self.output = self.root/board
                result = self.full()
                self.assertEqual(set(delivery.verify_full_delivery(self.package, result)), delivery.FULL_FILES)
                with zipfile.ZipFile(result['archive']['path']) as archive:
                    self.assertEqual(archive.read(delivery.BUNDLE_IMAGE), self.spi.read_bytes())
                    readme = archive.read("README.txt").decode("utf-8")
                    for instruction in (
                        "getvar debian-quick-erase-version",
                        "erase debian-system-quick",
                        "Flash.cmd -SerialNumber SERIAL -QuickErase",
                        "sh Flash.sh SERIAL auto --quick-erase",
                    ):
                        self.assertIn(instruction, readme)

    def test_invalid_input_stops_before_staging(self):
        self.select('Y4')
        original = self.spi.read_bytes()
        self.spi.unlink()
        with self.assertRaises(ValueError):
            self.full()
        self.spi.write_bytes(original[:-1])
        with self.assertRaises(ValueError):
            self.full()
        self.select('Y12')
        self.spi.write_bytes(original)
        with self.assertRaises(ValueError):
            self.full()
        self.assertFalse(self.output.exists())

    def test_tampered_system_input_stops_before_staging(self):
        self.select('Y4')
        (self.package/'rootfs_sparse.ext4').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Build payload checksum mismatch'):
            self.full()
        self.assertFalse(self.output.exists())

    def ota(self):
        path=self.root/'original.y4pkg'
        manifest=json.dumps(dict(type='sysroot',schema=2,version='20261113-r202')).encode()
        header=struct.pack('<8sIIQ',b'Y4PKG003',len(manifest),64,4096)
        path.write_bytes((header+manifest+b'S'*64).ljust(4096,b'\0')+b'P'*4096)
        return path

    def test_system_ota_contains_only_signed_package_readme_checksums(self):
        source=self.ota()
        result=delivery.create_ota(source,self.output,jobs=2)
        delivery.verify_archive(result['archive']['path'],result['files'])
        self.assertEqual(set(result['files']),{'Y4-20261113-r202.y4pkg','README.txt','SHA256SUMS'})
        self.assertEqual(result['system_package_sha256'],delivery.sha(source))
        self.assertEqual((self.output/'ota/Y4-20261113-r202.y4pkg').read_bytes(),source.read_bytes())

    def test_uefi_is_not_a_system_ota_package(self):
        self.select('Y4')
        with self.assertRaisesRegex(ValueError,'Not a recognized system OTA'):
            delivery.create_ota(self.spi,self.output)

    def test_truncated_system_ota_rejected(self):
        source=self.ota()
        source.write_bytes(source.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError,'length/type'):
            delivery.create_ota(source,self.output)

    def test_extra_report_in_delivery_rejected(self):
        result=delivery.create_ota(self.ota(),self.output,jobs=2)
        (Path(result['directory'])/'HARDWARE-VALIDATION.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'Unexpected or missing'):
            delivery.verify_directory(result['directory'],'ota',result['version'])

    def test_extra_zip_member_rejected(self):
        result=delivery.create_ota(self.ota(),self.output,jobs=2)
        with zipfile.ZipFile(result['archive']['path'],'a') as bundle:
            bundle.writestr('uefi.bin',b'wrong package')
        with self.assertRaisesRegex(ValueError,'Unexpected ZIP'):
            delivery.verify_archive(result['archive']['path'],result['files'])


if __name__=='__main__':
    unittest.main()
