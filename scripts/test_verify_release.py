#!/usr/bin/env python3
"""Exercise firmware provenance resolution without accepting missing evidence."""
import copy
import hashlib
import json
import struct
import uuid
import zlib
import tempfile
import unittest
from pathlib import Path
from verify_release import verify_slot_gpt
import gpt

class SlotGptTests(unittest.TestCase):
    def setUp(self):
        self.disk = uuid.uuid4()
        self.data, self.meta = gpt.packed(512, self.disk)
        self.root_bytes = 8*1024**3

    def check(self, data=None, meta=None, sector=512, root_bytes=None):
        return verify_slot_gpt(data if data is not None else self.data,
                               meta if meta is not None else self.meta,
                               sector, str(self.disk),
                               self.root_bytes if root_bytes is None else root_bytes)

    def test_current_slot_templates(self):
        for sector in (512, 4096):
            with self.subTest(sector=sector):
                data, meta = gpt.packed(sector, self.disk)
                self.assertEqual(self.check(data, meta, sector)['slot_bytes'], 16*1024**3)

    def test_oversized_root_rejected(self):
        with self.assertRaises(AssertionError):
            self.check(root_bytes=16*1024**3+4096)

    def test_metadata_disagrees_with_binary(self):
        meta = copy.deepcopy(self.meta)
        meta['partitions'][1]['last_lba'] -= 1
        with self.assertRaises(AssertionError):
            self.check(meta=meta)

    def test_unstable_partition_guids_rejected(self):
        ids = [uuid.uuid5(self.disk, name) for name in gpt.LABELS]
        data, meta = gpt.packed(512, self.disk, ids)
        with self.assertRaises(AssertionError):
            self.check(data, meta)

    def test_backup_crc_corruption_rejected(self):
        data = bytearray(self.data)
        data[-512+16] ^= 1
        meta = dict(self.meta, sha256=hashlib.sha256(data).hexdigest())
        with self.assertRaises(AssertionError):
            self.check(data, meta)

    def test_partition_extent_with_valid_crcs_rejected(self):
        data = bytearray(self.data)
        table = bytearray(data[1024:1024+16384])
        struct.pack_into('<Q', table, 128+40, self.meta['partitions'][1]['last_lba']-1)
        data[1024:1024+16384] = table
        data[-512-16384:-512] = table
        for offset in (512, len(data)-512):
            struct.pack_into('<I', data, offset+88, zlib.crc32(table)&0xffffffff)
            struct.pack_into('<I', data, offset+16, 0)
            struct.pack_into('<I', data, offset+16, zlib.crc32(data[offset:offset+92])&0xffffffff)
        meta = dict(self.meta, sha256=hashlib.sha256(data).hexdigest())
        with self.assertRaises(AssertionError):
            self.check(data, meta)

if __name__ == '__main__':
    unittest.main()
