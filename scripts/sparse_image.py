#!/usr/bin/env python3
"""Write Android sparse v1 RAW/FILL images, reading allocated extents once.

Host-file holes become explicit zero FILL records. They never become
DONT_CARE records, so previously used target disks receive the same writes.
Format: https://android.googlesource.com/platform/system/core/+/main/libsparse/sparse_format.h
"""
import argparse
import errno
import json
import os
import stat
import struct
import time
from pathlib import Path

HEADER = struct.Struct('<IHHHHIIII')
CHUNK = struct.Struct('<HHII')
MAGIC, RAW, FILL = 0xed26ff3a, 0xcac1, 0xcac2
UINT32_MAX = 0xffffffff
ZERO = b'\0' * 4


def allocated_blocks(fd, size, block):
    ranges = []
    position = 0
    try:
        while position < size:
            try:
                data = os.lseek(fd, position, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break
                raise
            hole = os.lseek(fd, data, os.SEEK_HOLE)
            if data < position or hole <= data:
                raise ValueError('Invalid filesystem extent boundary')
            begin, end = data // block, min(size, hole + block - 1) // block
            if ranges and begin <= ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], max(end, ranges[-1][1]))
            else:
                ranges.append((begin, end))
            position = hole
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
            raise
        return [(0, size // block)], False
    return ranges, True


class Writer:
    def __init__(self, stream, block, blocks):
        self.stream, self.block, self.blocks = stream, block, blocks
        self.count, self.logical, self.kind = 0, 0, None
        self.raw_blocks, self.fill_blocks = 0, 0
        # Match the installed libsparse producer's 64 MiB chunk boundaries.
        self.run_limit = min((UINT32_MAX - CHUNK.size) // block, 64 * 1048576 // block)
        self.stream.write(HEADER.pack(MAGIC, 1, 0, HEADER.size, CHUNK.size, block, blocks, 0, 0))

    def finish_chunk(self):
        if self.kind is None:
            return
        end = self.stream.tell()
        total = CHUNK.size + (self.run * self.block if self.kind == RAW else 4)
        self.stream.seek(self.start)
        self.stream.write(CHUNK.pack(self.kind, 0, self.run, total))
        self.stream.seek(end)
        self.kind = None

    def begin(self, kind, value=None):
        self.finish_chunk()
        self.kind, self.value, self.run = kind, value, 0
        self.start = self.stream.tell()
        self.stream.write(b'\0' * CHUNK.size)
        if kind == FILL:
            self.stream.write(value)
        self.count += 1

    def fill(self, value, count):
        while count:
            if self.kind != FILL or self.value != value or self.run == self.run_limit:
                self.begin(FILL, value)
            part = min(count, self.run_limit - self.run)
            self.run += part
            self.logical += part
            self.fill_blocks += part
            count -= part

    def raw(self, data):
        if self.kind != RAW or self.run == self.run_limit:
            self.begin(RAW)
        self.stream.write(data)
        self.run += 1
        self.logical += 1
        self.raw_blocks += 1

    def finish(self):
        self.finish_chunk()
        if self.logical != self.blocks or self.count > UINT32_MAX:
            raise ValueError('Sparse image block count mismatch')
        self.stream.seek(0)
        self.stream.write(HEADER.pack(MAGIC, 1, 0, HEADER.size, CHUNK.size,
                                      self.block, self.blocks, self.count, 0))


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def convert(source, output, block=1048576, use_extents=True):
    source, output = Path(source), Path(output)
    if block < 4 or block % 4 or block > 1048576:
        raise ValueError('Block size must be a multiple of four, at most 1 MiB')
    start = time.monotonic()
    with source.open('rb', buffering=0) as incoming:
        before = os.fstat(incoming.fileno())
        if not stat.S_ISREG(before.st_mode) or not before.st_size or before.st_size % block:
            raise ValueError('A nonempty block-aligned regular input file is required')
        blocks = before.st_size // block
        if blocks > UINT32_MAX:
            raise ValueError('Input exceeds Android sparse v1 block limits')
        ranges, supported = (allocated_blocks(incoming.fileno(), before.st_size, block)
                             if use_extents else ([(0, blocks)], False))
        read_blocks, at = 0, 0
        zero_block = b'\0' * block
        with output.open('xb') as target:
            writer = Writer(target, block, blocks)
            for begin, end in ranges:
                writer.fill(ZERO, begin - at)
                incoming.seek(begin * block)
                for _ in range(begin, end):
                    data = incoming.read(block)
                    if len(data) != block:
                        raise ValueError('Input shortened during conversion')
                    word = data[:4]
                    if (word == ZERO and data == zero_block) or data == word * (block // 4):
                        writer.fill(word, 1)
                    else:
                        writer.raw(data)
                    read_blocks += 1
                at = end
            writer.fill(ZERO, blocks - at)
            writer.finish()
        if identity(before) != identity(os.fstat(incoming.fileno())):
            raise ValueError('Input changed during sparse conversion')
    return dict(format='android-sparse-v1-raw-fill', input_bytes=before.st_size,
                output_bytes=output.stat().st_size, block_bytes=block, chunks=writer.count,
                raw_blocks=writer.raw_blocks, fill_blocks=writer.fill_blocks,
                input_bytes_read=read_blocks * block, hole_bytes_skipped=(blocks-read_blocks) * block,
                filesystem_extent_support=supported, dont_care_chunks=0,
                seconds=time.monotonic()-start)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--block-size', type=int, default=1048576)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = convert(args.source, args.output, args.block_size)
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
