#!/usr/bin/env python3
"""Execute the production GPT C core in AAVMF against file-backed QEMU NVMe."""
import argparse
import hashlib
import json
import shutil
import stat
import struct
import subprocess
import time
import zlib
from pathlib import Path

MIB = 1024**2
ROOT_START = 210 * MIB


def boot_bytes(sector):
    return 512 * MIB


def slot_a_start(sector):
    return 10 * MIB + boot_bytes(sector)


def slot_b_start(sector):
    return slot_a_start(sector) + 16 * 1024**3


def data_start(sector):
    return slot_b_start(sector) + 16 * 1024**3


def run(command, **kwargs):
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def qemu_base(memory=1024):
    return ['qemu-system-aarch64', '-machine', 'virt,gic-version=3', '-cpu', 'cortex-a72',
            '-accel', 'tcg,thread=multi', '-smp', '2', '-m', str(memory), '-display', 'none',
            '-monitor', 'none', '-serial', 'stdio', '-net', 'none', '-no-reboot']


def boot_fixture(output, fixture, template, disk, sector, image_format='raw', timeout=180):
    output.mkdir(parents=True, exist_ok=False)
    for path in (fixture, template, disk):
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError('QEMU accepts regular test image files only: ' + str(path))
    boot = output / 'fixture-fat.img'
    with boot.open('xb') as stream:
        stream.truncate(64 * MIB)
    run(['mkfs.vfat', '-F', '32', boot], stdout=subprocess.DEVNULL)
    run(['mmd', '-i', boot, '::/EFI', '::/EFI/BOOT'])
    run(['mcopy', '-i', boot, fixture, '::/EFI/BOOT/BOOTAA64.EFI'])
    run(['mcopy', '-i', boot, template, '::/gpt-template.img'])
    variables = output / 'AAVMF_VARS.fd'
    shutil.copyfile('/usr/share/AAVMF/AAVMF_VARS.fd', variables)
    guard = output / 'non-target.raw'
    guard.write_bytes((b'QEMU non-target disk must remain byte-identical\n' * 25000)[:MIB])
    guard_digest = sha(guard)
    command = qemu_base() + [
        '-drive', 'if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd',
        '-drive', 'if=pflash,format=raw,file=' + str(variables),
        '-drive', 'if=none,id=boot,file=' + str(boot) + ',format=raw',
        '-device', 'virtio-blk-pci,drive=boot,bootindex=1',
        '-drive', 'if=none,id=nvm,file=' + str(disk) + ',format=' + image_format,
        '-device', 'nvme,drive=nvm,serial=Y4-GPT-QEMU,logical_block_size=' + str(sector)
                   + ',physical_block_size=' + str(sector),
        '-drive', 'if=none,id=guard,file=' + str(guard) + ',format=raw',
        '-device', 'virtio-blk-pci,drive=guard']
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    start = time.monotonic()
    with (output / 'console.log').open('wb') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            raise RuntimeError('Owned QEMU fixture timed out; original console retained')
    if exit_code:
        raise RuntimeError('QEMU fixture process failed; original console retained')
    text = subprocess.check_output(['mtype', '-i', str(boot), '::/gpt-result.txt'], text=True)
    result = {name: int(value, 0) for name, value in (line.split('=', 1) for line in text.splitlines())}
    assert sha(guard) == guard_digest, 'Non-target virtual disk was modified'
    result.update(seconds=time.monotonic()-start, fixture_sha256=sha(fixture), template_sha256=sha(template),
                  non_target_disk_unchanged=True, non_target_sha256=guard_digest)
    (output / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def validate_gpt(disk, sector, capacity, template, image_format='raw', scratch=None):
    counter = 0
    def read(offset, count):
        nonlocal counter
        if image_format == 'raw':
            with disk.open('rb') as stream:
                stream.seek(offset); data = stream.read(count)
        else:
            target = scratch / ('gpt-read-' + str(counter) + '.bin'); counter += 1
            options = 'driver=raw,offset=' + str(offset) + ',size=' + str(count) + ',file.driver=' + image_format + ',file.file.driver=file,file.file.filename=' + str(disk)
            run(['qemu-img', 'convert', '--image-opts', '-O', 'raw', options, target],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            data = target.read_bytes()
        assert len(data) == count
        return data
    last = capacity // sector - 1
    tables = []
    headers = []
    for mine, alternate in [(1, last), (last, 1)]:
        data = read(mine * sector, sector)
        values = struct.unpack_from('<8sIIIIQQQQ16sQIII', data)
        signature, revision, size, crc, reserved, current, other, first, usable, guid, table_lba, entries, entry_size, table_crc = values
        assert (signature, revision, size, reserved, current, other, entries, entry_size) == (b'EFI PART', 0x10000, 92, 0, mine, alternate, 128, 128)
        check = bytearray(data[:size]); struct.pack_into('<I', check, 16, 0)
        assert zlib.crc32(check) & 0xffffffff == crc
        assert first == 2 + 16384 // sector and usable == last - 16384 // sector - 1
        assert table_lba == (2 if mine == 1 else last - 16384 // sector)
        table = read(table_lba * sector, 16384)
        assert zlib.crc32(table) & 0xffffffff == table_crc
        tables.append(table); headers.append(values)
    assert tables[0] == tables[1] and headers[0][9] == headers[1][9]
    mbr = read(0, sector)
    assert mbr[510:512] == b'\x55\xaa' and mbr[450] == 0xee
    assert struct.unpack_from('<I', mbr, 458)[0] == min(last, 0xffffffff)
    original = template.read_bytes()
    assert headers[0][9] == original[sector + 56:sector + 72]
    table = tables[0]
    expected_end = ((capacity - 16384 - sector) // MIB) * MIB
    starts = (10 * MIB, slot_a_start(sector), slot_b_start(sector), data_start(sector))
    ends = (slot_a_start(sector), slot_b_start(sector), data_start(sector), expected_end)
    for index, label in enumerate(('boot', 'slot-a', 'slot-b', 'data')):
        entry = table[index * 128:(index+1) * 128]
        first, final = struct.unpack_from('<QQ', entry, 32)
        assert entry[:32] == original[2*sector + index*128:2*sector + index*128 + 32]
        assert entry[56:128].decode('utf-16le').rstrip('\x00') == label
        assert first * sector == starts[index]
        assert (final + 1) * sector == ends[index]
    assert not any(table[512:])
    return dict(sector_bytes=sector, capacity_bytes=capacity, slot_a_start=slot_a_start(sector),
                data_start=data_start(sector), slot_bytes=16 * 1024**3,
                data_bytes=expected_end-data_start(sector), primary_and_backup_crc_verified=True,
                template_guids_preserved=True, boot_bytes=boot_bytes(sector))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    harness_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).resolve().parents[2], text=True).strip()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    reports = []
    for sector, capacity in [(512, 256000000000), (4096, 512000000000)]:
        disk = output / ('nvme-' + str(sector) + '.raw')
        with disk.open('xb') as stream: stream.truncate(capacity)
        template = args.package / ('partition-table.img' if sector == 512 else 'partition-table-4096.img')
        for iteration in ('fresh', 'reflash'):
            case = output / (str(sector) + '-' + iteration)
            result = boot_fixture(case, args.fixture, template, disk, sector)
            assert result['STATUS'] == 0 and result['SECTOR'] == sector, result
            assert result['READS'] == 2 and result['WRITES'] == 2 and result['REENUMERATIONS'] == 1, result
            result.update(validate_gpt(disk, sector, capacity, template))
            result.update(case=case.name, complete=True)
            (case / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
            reports.append(result)
        # A CRC-corrupt template must fail before any target writes.
        bad = output / ('invalid-' + str(sector) + '.img')
        data = bytearray(template.read_bytes()); data[sector + 16] ^= 1; bad.write_bytes(data)
        with disk.open('rb') as stream:
            before_head = stream.read(65536); stream.seek(capacity-65536); before_tail = stream.read(65536)
        case = output / (str(sector) + '-invalid-crc')
        result = boot_fixture(case, args.fixture, bad, disk, sector)
        assert result['STATUS'] and result['WRITES'] == 0, result
        with disk.open('rb') as stream:
            assert stream.read(65536) == before_head
            stream.seek(capacity-65536); assert stream.read(65536) == before_tail
        result.update(case=case.name, rejected_without_writes=True)
        (case / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
        reports.append(result)
    manifest = json.loads((args.package / 'MANIFEST.json').read_text())
    fixture_build = json.loads((args.fixture.parent / 'BUILD.json').read_text())
    report = dict(complete=True, source_commit=manifest['source_commit'], package=str(args.package.resolve()),
                  harness_source_commit=harness_commit, harness_sha256=sha(Path(__file__)),
                  package_manifest_sha256=sha(args.package / 'MANIFEST.json'), fixture_sha256=sha(args.fixture),
                  fixture_build=fixture_build, tests=reports,
                  scope='AAVMF+QEMU NVMe; production GPT C core. CIX USB/PCIe physical policy remains real-board acceptance.')
    (output / 'VERIFICATION.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
