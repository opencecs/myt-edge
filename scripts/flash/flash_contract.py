#!/usr/bin/env python3
"""Validate source UEFI geometry before and after an explicit Debian flash."""
import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

MIB = 1024**2
UINT64_MAX = 2**64 - 1


def checked_output(output):
    if re.search(r'\bFAILED\b|fastboot:\s*error:', output, re.I):
        raise ValueError('Fastboot reported failure; no later operation is permitted')
    return output


def verify_success(output):
    checked_output(output)
    if not re.search(r'\bOKAY\b', output):
        raise ValueError('Fastboot did not confirm successful completion')


def run_fastboot(command, log):
    """Stream both native output streams, retaining the exact original bytes."""
    with Path(log).open('xb') as stream:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            while True:
                data = process.stdout.read1(4096)
                if not data:
                    break
                stream.write(data); stream.flush()
                sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
            code = process.wait()
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate(); process.wait()
    if code:
        raise ValueError('Fastboot exited with status ' + str(code))
    output = Path(log).read_text(errors='replace')
    checked_output(output)
    if 'erase' in command or 'flash' in command:
        verify_success(output)


def verify_package(directory, extract_bootstrap=None):
    directory = Path(directory).resolve(strict=True)
    sums = {}
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        if not match:
            raise ValueError('Invalid package checksum line')
        digest, name = match.groups()
        path = Path(name)
        if (name in sums or '\\' in name or path.is_absolute() or
                any(p in ('', '.', '..') for p in name.split('/')) or ':' in name):
            raise ValueError('Unsafe or duplicate package path: ' + name)
        target = directory / path
        if target.is_symlink() or not target.is_file() or directory not in target.resolve().parents:
            raise ValueError('Invalid package file: ' + name)
        h = hashlib.sha256()
        with target.open('rb') as stream:
            for data in iter(lambda: stream.read(4 * MIB), b''):
                h.update(data)
        if h.hexdigest() != digest:
            raise ValueError('Package hash mismatch: ' + name)
        sums[name] = digest
    manifest = json.loads((directory / 'tools/package.json').read_text())
    files = manifest['files']
    required = {'Flash.cmd', 'Flash.sh', 'tools/Flash-Debian.ps1', 'tools/Flash-Contract.ps1', 'tools/flash_contract.py',
                'images/rootfs.img', 'images/gpt-512.img', 'images/gpt-4096.img',
                'images/boot-512.img', 'images/boot-4096.img'}
    if manifest.get('format') == 'y4-flash-v1':
        required.update(('images/uefi.bin', 'images/ddr.bin'))
        if extract_bootstrap is not None:
            raise ValueError('Bootstrap extraction requires a single-image v2 full package')
    elif manifest.get('format') == 'y4-flash-v2':
        required.update(('images/myt-spi.bin', 'tools/spi_bundle.py'))
        if manifest.get('board_check') is not True or manifest.get('hardware') not in ('Y4-6187', 'Y12-6187'):
            raise ValueError('Single-image package requires a verified board target')
        binaries = {p.name for p in (directory / 'images').iterdir() if p.suffix.lower() == '.bin'}
        if binaries != {'myt-spi.bin'}:
            raise ValueError('Expected myt-spi.bin as the only SPI image')
    else:
        raise ValueError('Unsupported flash package format')
    if set(sums) != set(files) | {'tools/package.json'} or not required <= set(files):
        raise ValueError('Incomplete package checksum inventory')
    for name, info in files.items():
        if info['sha256'] != sums[name] or info['bytes'] != (directory/name).stat().st_size:
            raise ValueError('Package manifest mismatch: ' + name)
    if manifest['format'] == 'y4-flash-v2':
        # Installed next to this script in v2 packages. Older packages do not contain it.
        from spi_bundle import read_spi_bundle
        board = manifest['hardware'].split('-')[0]
        bl33 = read_spi_bundle((directory / 'images/myt-spi.bin').read_bytes(), board)[2]
        if extract_bootstrap is not None:
            with extract_bootstrap.open('xb') as output:
                output.write(bl33)
            print(f'{hashlib.sha256(bl33).hexdigest()}  {extract_bootstrap}')


def verify_serial(output, serial):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', serial):
        raise ValueError('Invalid target serial format')
    reported = variable(output, 'serialno')
    # The UEFI DebianFastboot gadget reports getvar serialno with a 0x
    # prefix while the host-side enumeration carries the bare value;
    # accept either spelling so -s <enumerated-serial> callers work.
    if reported != serial and reported.removeprefix('0x') != serial.removeprefix('0x'):
        raise ValueError('Connected Fastboot serial does not match the requested target')


def verify_ready(output, name, expected='yes'):
    if variable(output, name) != expected:
        raise ValueError('UEFI did not confirm ' + name + '=' + expected)


def variable(output, name):
    checked_output(output)
    pattern = r'^\s*(?:\(bootloader\)\s*)?' + re.escape(name) + r':\s*(.*?)\s*$'
    values = set(re.findall(pattern, output, re.M))
    if len(values) != 1 or not next(iter(values)):
        raise ValueError('Missing or ambiguous Fastboot variable: ' + name)
    return values.pop()


def root_image_bytes(path):
    with Path(path).open('rb') as stream:
        data = stream.read(28)
    if len(data) != 28:
        raise ValueError('Truncated root sparse image')
    magic, major, minor, header, chunk, block, count, chunks, crc = struct.unpack('<IHHHHIIII', data)
    if ((magic, major, minor, header, chunk) != (0xed26ff3a, 1, 0, 28, 12) or
            not block or block % 4096 or block > MIB or not count or not chunks):
        raise ValueError('Unsupported root sparse image header')
    size = block * count
    if size % MIB or size > UINT64_MAX:
        raise ValueError('Root image size is not a valid whole MiB')
    return size


def geometry(version_output, disk_output, image, expected_sector='auto', *, quick_erase=False):
    if variable(version_output, 'debian-flash-version') != '3':
        raise ValueError('This package requires its source-built Debian UEFI flash protocol 3; update UEFI first')
    info = variable(disk_output, 'debian-disk-info')
    found = re.fullmatch(r'block-size=(512|4096),lastLBA=([0-9]+),bytes=([0-9]+)', info)
    if not found:
        raise ValueError('Invalid UEFI disk geometry response')
    sector, last, capacity = map(int, found.groups())
    boot_bytes = 512 * MIB
    slot_bytes = 16 * 1024**3
    slot_a_start = 10*MIB + boot_bytes
    data_start_bytes = slot_a_start + 2 * slot_bytes
    if (capacity > UINT64_MAX or last >= UINT64_MAX or
            capacity != (last+1)*sector or expected_sector not in ('auto', str(sector))):
        raise ValueError('UEFI disk capacity or requested sector size is inconsistent')
    image_bytes = root_image_bytes(image)
    if image_bytes > slot_bytes:
        raise ValueError('Slot image does not fit the fixed 16 GiB slot partition')
    if capacity < data_start_bytes + MIB:
        raise ValueError('System NVMe is smaller than the complete Debian layout')
    if quick_erase and capacity < data_start_bytes + 2 * MIB:
        raise ValueError('System NVMe is too small for quick erase')
    usable_last = last - 16384//sector - 1
    data_end = ((usable_last+1)*sector//MIB)*MIB
    data_bytes = data_end - data_start_bytes
    if data_bytes < MIB:
        raise ValueError('System NVMe leaves no usable data partition')
    return dict(protocol=3, sector_bytes=sector, disk_bytes=capacity, last_lba=last,
                slot_image_bytes=image_bytes, expected_boot_bytes=boot_bytes,
                expected_slot_a_bytes=slot_bytes, expected_slot_b_bytes=slot_bytes,
                expected_data_bytes=data_bytes, slot_a_start_bytes=slot_a_start,
                data_start_bytes=data_start_bytes,
                capacity_source='UEFI system NVMe Block I/O')


def verify_quick_erase_gpt(path, sector, last_lba):
    # Packed GPT: MBR, primary header/table, backup table/header.
    if path.stat().st_size != 3 * sector + 32768:
        raise ValueError('Invalid packed GPT template size')
    with path.open('rb') as stream:
        stream.seek(sector)
        header = stream.read(40)
    if len(header) != 40 or not header.startswith(b'EFI PART'):
        raise ValueError('Invalid packed GPT primary header')
    backup_lba = struct.unpack_from('<Q', header, 32)[0]
    if backup_lba <= 1 or backup_lba > last_lba:
        raise ValueError('Packed GPT template does not fit the system NVMe')


def verify_partition(output, name, expected):
    value = variable(output, 'partition-size:' + name)
    if not re.fullmatch(r'(?:0x)?[0-9a-fA-F]+', value) or int(value, 16) != expected:
        raise ValueError('UEFI partition readback does not match planned ' + name + ' size')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    package = sub.add_parser('package')
    package.add_argument('--directory', type=Path, default=Path('.'))
    package.add_argument('--extract-spi-bootstrap', type=Path, metavar='NEW_FILE',
                         help='extract the verified BL33 container for a legacy receiver; no device access')
    for command in ('target', 'board'):
        target = sub.add_parser(command)
        target.add_argument('--manifest', type=Path, required=True)
        if command == 'board':
            target.add_argument('--log', type=Path, required=True)
    run = sub.add_parser('run')
    run.add_argument('--log', type=Path, required=True)
    run.add_argument('arguments', nargs=argparse.REMAINDER)
    serial = sub.add_parser('serial')
    serial.add_argument('--log', type=Path, required=True)
    serial.add_argument('--expected', required=True)
    ready = sub.add_parser('ready')
    ready.add_argument('--log', type=Path, required=True)
    ready.add_argument('--name', choices=['debian-clean-ready', 'debian-gpt-ready',
                                          'debian-quick-erase-version'], required=True)
    ready.add_argument('--expected', choices=['yes', 'no', '1'], default='yes')
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--version-log', type=Path, required=True)
    prepare.add_argument('--disk-log', type=Path, required=True)
    prepare.add_argument('--image', type=Path, required=True)
    prepare.add_argument('--sector', choices=['auto', '512', '4096'], default='auto')
    prepare.add_argument('--quick-erase', action='store_true')
    prepare.add_argument('--output', type=Path, required=True)
    verify = sub.add_parser('verify')
    verify.add_argument('--layout', type=Path, required=True)
    verify.add_argument('--partition', choices=['boot', 'slot-a', 'slot-b', 'data'], required=True)
    verify.add_argument('--log', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'package':
        verify_package(args.directory, args.extract_spi_bootstrap)
        print('Complete package hashes verified; no device accessed')
    elif args.command in ('target', 'board'):
        manifest = json.loads(args.manifest.read_text())
        hardware = manifest.get('hardware', 'Y4-6187')
        if hardware not in ('Y4-6187', 'Y12-6187'):
            raise ValueError('Unsupported package target hardware')
        target = hardware.split('-')[0]
        if args.command == 'target':
            print(target if manifest.get('board_check') is True or target == 'Y12' else 'legacy')
        else:
            verify_ready(args.log.read_text(), 'debian-board', target)
    elif args.command == 'run':
        command = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
        if not command:
            parser.error('run requires a native Fastboot command')
        run_fastboot(command, args.log)
    elif args.command == 'serial':
        verify_serial(args.log.read_text(), args.expected)
    elif args.command == 'ready':
        verify_ready(args.log.read_text(), args.name, args.expected)
    elif args.command == 'prepare':
        value = geometry(args.version_log.read_text(), args.disk_log.read_text(), args.image,
                         args.sector, quick_erase=args.quick_erase)
        if args.quick_erase:
            sector = value['sector_bytes']
            verify_quick_erase_gpt(args.image.with_name(f'gpt-{sector}.img'), sector, value['last_lba'])
        args.output.write_text(json.dumps(value, indent=2) + '\n')
        print(value['sector_bytes'])
    else:
        layout = json.loads(args.layout.read_text())
        key = 'expected_' + args.partition.replace('-', '_') + '_bytes'
        verify_partition(args.log.read_text(), args.partition, layout[key])
        print(args.partition + ' partition capacity verified')


if __name__ == '__main__':
    main()
