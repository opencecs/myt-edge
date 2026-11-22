#!/usr/bin/env python3
"""Validate variable-size GPT templates with independent GPT readers."""
import argparse
import struct
import subprocess
import uuid
import zlib
from pathlib import Path

import gpt


def check_header(data, my_lba, other_lba, sector):
    fields = struct.unpack_from('<8sIIIIQQQQ16sQIII', data)
    assert fields[0:3] == (b'EFI PART', 0x10000, 92)
    assert fields[5:7] == (my_lba, other_lba)
    block = bytearray(data[:fields[2]])
    struct.pack_into('<I', block, 16, 0)
    assert zlib.crc32(block) & 0xffffffff == fields[3]
    assert fields[11:13] == (128, 128)
    return fields


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    guid = uuid.UUID('bad64798-1f11-4e34-a867-86ba4f7b8201')
    for sector in (512, 4096):
        for root_bytes in (64 * gpt.MIB, 10240 * gpt.MIB, 16384 * gpt.MIB):
            data, meta = gpt.packed(sector, guid, root_bytes=root_bytes)
            last = meta['total_lbas'] - 1
            primary = check_header(data[sector:2*sector], 1, last, sector)
            backup = check_header(data[-sector:], last, 1, sector)
            table = data[2*sector:2*sector+16384]
            assert table == data[-sector-16384:-sector]
            assert primary[13] == backup[13] == zlib.crc32(table) & 0xffffffff
            assert primary[9] == guid.bytes_le
            assert primary[10] == 2 and backup[10] == last - 16384 // sector
            assert data[510:512] == b'\x55\xaa' and data[450] == 0xee
            starts = []
            for index, label in enumerate(('boot', 'root')):
                entry = table[index*128:(index+1)*128]
                begin, end = struct.unpack_from('<QQ', entry, 32)
                assert entry[16:32] == uuid.uuid5(guid, label).bytes_le
                assert entry[56:128].decode('utf-16le').rstrip('\0') == label
                assert begin * sector % gpt.MIB == 0
                assert (end + 1) * sector % gpt.MIB == 0
                starts.append((begin, end))
            assert (starts[0][1]-starts[0][0]+1)*sector == gpt.boot_bytes(sector)
            assert starts[1][0]*sector == gpt.root_start(sector)
            assert (starts[1][1]-starts[1][0]+1)*sector == root_bytes
            assert starts[0][1] < starts[1][0] <= starts[1][1] <= primary[8]
            if sector == 512 and root_bytes == 64*gpt.MIB:
                disk = args.output / 'independent-reader.img'
                with disk.open('xb') as stream:
                    stream.truncate(meta['template_bytes'])
                    stream.write(data[:2*sector+16384])
                    stream.seek((last-16384//sector)*sector)
                    stream.write(table)
                    stream.write(data[-sector:])
                result = subprocess.run(['sgdisk', '--verify', str(disk)], check=True,
                                        text=True, capture_output=True)
                assert 'No problems found' in result.stdout, result.stdout
            expanded = gpt.plan(256*1024**3, sector, root_bytes)
            assert expanded['partitions'][1]['first_lba'] == starts[1][0]
            assert expanded['partitions'][1]['last_lba'] > starts[1][1]
    for kwargs in [dict(root_bytes=0), dict(root_bytes=gpt.MIB+1), dict(root_bytes=True),
                   dict(root_bytes=2**64)]:
        try:
            gpt.packed(512, guid, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid image size accepted: ' + str(kwargs))
    for capacity, sector in [(gpt.MIN_BYTES-512, 512), (2**65, 512),
                             (gpt.MIN_BYTES, 1024), (True, 512)]:
        try:
            gpt.plan(capacity, sector)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid disk geometry accepted')
    print('PASS: variable image size, 512/4Kn, GPT primary/backup CRC/GUID/geometry, independent sgdisk, bounds')


if __name__ == '__main__':
    main()
