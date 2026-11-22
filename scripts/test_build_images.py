#!/usr/bin/env python3
"""Check accelerated build outputs against independent Android sparse and ZIP readers."""
import argparse
import hashlib
import os
import struct
import subprocess
import zipfile
from pathlib import Path
from sparse_image import convert
from parallel_zip import create


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1048576), b''):
            h.update(data)
    return h.digest()


def inspect(path):
    with path.open('rb') as stream:
        magic, major, minor, header, chunk_header, block, blocks, count, checksum = struct.unpack('<IHHHHIIII', stream.read(28))
        assert (magic, major, minor, header, chunk_header) == (0xed26ff3a, 1, 0, 28, 12)
        total = 0
        for _ in range(count):
            kind, reserved, size, record = struct.unpack('<HHII', stream.read(12))
            assert kind in (0xcac1, 0xcac2) and reserved == 0
            assert record == 12 + (size * block if kind == 0xcac1 else 4)
            total += size
            stream.seek(record - 12, 1)
        assert total == blocks and stream.read() == b''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    for name in ['holes', 'mixed', 'allocated-zero', 'wide']:
        raw = args.output / (name + '.raw')
        with raw.open('wb') as stream:
            stream.truncate((160 if name == 'wide' else 64) * 1048576)
            if name == 'mixed':
                stream.seek(4096); stream.write(b'boundary-test' * 500)
                stream.seek(17 * 1048576); stream.write(os.urandom(2 * 1048576))
                stream.seek(40 * 1048576); stream.write(b'\xff' * 1048576)
                stream.seek(63 * 1048576 + 4096); stream.write(b'tail')
            elif name == 'allocated-zero':
                stream.seek(8 * 1048576); stream.write(b'\0' * (3 * 1048576))
            elif name == 'wide':
                block = bytes(range(256)) * 4096
                for _ in range(80):
                    stream.write(block)
                block = b'\x12\x34\x56\x78' * (1048576 // 4)
                for _ in range(80):
                    stream.write(block)
        reference = args.output / (name + '-reference.simg')
        subprocess.run(['img2simg', str(raw), str(reference), '1048576'], check=True)
        for strategy in [True, False]:
            sparse = args.output / (name + '-' + str(strategy) + '.simg')
            expanded = sparse.with_suffix('.expanded')
            result = convert(raw, sparse, use_extents=strategy)
            inspect(sparse)
            subprocess.run(['simg2img', str(sparse), str(expanded)], check=True)
            assert digest(raw) == digest(expanded)
            assert digest(sparse) == digest(reference)
            assert result['dont_care_chunks'] == 0
    bad = args.output / 'unaligned.raw'; bad.write_bytes(b'bad')
    try:
        convert(bad, args.output / 'invalid.simg')
    except ValueError:
        pass
    else:
        raise AssertionError('Unaligned input accepted')
    package = args.output / 'package'; package.mkdir()
    (package / 'rootfs_sparse.ext4').write_bytes(os.urandom(2 * 1048576))
    (package / 'nested').mkdir(); (package / 'nested/file.txt').write_text('ZIP append compatibility')
    archive = args.output / 'test.zip'
    create(package, archive, 4, args.output / 'pigz.log')
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        for member in bundle.namelist():
            assert hashlib.sha256(bundle.read(member)).digest() == digest(package / member)
    subprocess.run(['7z', 't', '-bso0', '-bsp0', str(archive)], check=True)
    print('PASS: reference sparse bytes, independent expansion, explicit zero fills, ZIP append/readers')


if __name__ == '__main__':
    main()
