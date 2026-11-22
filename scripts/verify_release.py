#!/usr/bin/env python3
"""Verify a delivered package and bind QEMU evidence to its exact payload."""
import argparse
import hashlib
import json
import struct
import uuid
import zlib
from pathlib import Path
from release_package import verify_full_delivery


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(4 * 1024**2), b''):
            digest.update(data)
    return digest.hexdigest()



def verify_slot_gpt(data, meta, sector, disk_uuid, root_image_bytes):
    """Check the A/B contract independently of the GPT generator."""
    mib = 1024**2
    boot_bytes, slot_bytes = 512*mib, 16*1024**3
    labels = ('boot', 'slot-a', 'slot-b', 'data')
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, 'MYT/Y4/Debian/partitions-v1')
    ids = [str(uuid.uuid5(namespace, label)) for label in labels]
    starts = (10*mib, 522*mib, 522*mib+slot_bytes, 522*mib+2*slot_bytes)
    sizes = (boot_bytes, slot_bytes, slot_bytes, 64*mib)
    capacity = starts[3] + sizes[3] + mib
    assert sector in (512, 4096) and meta['sector_bytes'] == sector
    assert 0 < root_image_bytes <= slot_bytes and root_image_bytes % 4096 == 0
    assert meta['profile'] == 'debian' and meta['required_flash_protocol'] == 3
    assert meta['adaptive_data_partition'] is True
    assert meta['slot_bytes'] == slot_bytes and meta['template_bytes'] == capacity
    assert meta['disk_guid'] == str(uuid.UUID(disk_uuid)) and meta['part_ids'] == ids
    assert meta['sha256'] == hashlib.sha256(data).hexdigest()
    assert len(data) == 3*sector + 2*16384
    total = capacity//sector
    first, last = 2+16384//sector, total-16384//sector-2
    assert (meta['total_lbas'], meta['first_usable_lba'], meta['last_usable_lba']) == (total, first, last)
    assert data[510:512] == b'\x55\xaa' and data[450] == 0xee
    assert struct.unpack_from('<II', data, 454) == (1, min(total-1, 0xffffffff))
    table = data[2*sector:2*sector+16384]
    assert table == data[-sector-16384:-sector]
    assert not any(table[4*128:])
    for header, mine, alternate, entries in (
            (data[sector:2*sector], 1, total-1, 2),
            (data[-sector:], total-1, 1, total-1-16384//sector)):
        fields = struct.unpack_from('<8sIIIIQQQQ16sQIII', header)
        signature, revision, size, crc, reserved = fields[:5]
        assert (signature, revision, size, reserved) == (b'EFI PART', 0x10000, 92, 0)
        check = bytearray(header[:size])
        struct.pack_into('<I', check, 16, 0)
        assert zlib.crc32(check) & 0xffffffff == crc
        assert fields[5:9] == (mine, alternate, first, last)
        assert fields[9] == uuid.UUID(disk_uuid).bytes_le
        assert fields[10:13] == (entries, 128, 128)
        assert fields[13] == zlib.crc32(table) & 0xffffffff
    partitions = []
    for index, (label, start, size, ident) in enumerate(zip(labels, starts, sizes, ids)):
        entry = table[index*128:(index+1)*128]
        kind = ('c12a7328-f81f-11d2-ba4b-00a0c93ec93b' if index == 0
                else 'b921b045-1df0-41c3-af44-4c6f280d3fae')
        assert entry[:16] == uuid.UUID(kind).bytes_le
        assert entry[16:32] == uuid.UUID(ident).bytes_le
        begin, end, attributes = struct.unpack_from('<QQQ', entry, 32)
        assert (begin*sector, (end-begin+1)*sector, attributes) == (start, size, 0)
        assert first <= begin <= end <= last
        assert entry[56:].decode('utf-16le').rstrip('\0') == label
        partitions.append(dict(label=label, first_lba=begin, last_lba=end,
                               type_guid=kind, unique_guid=ident))
    assert meta['partitions'] == partitions
    return dict(boot_bytes=boot_bytes, slot_bytes=slot_bytes,
                root_start=starts[1], slot_b_start=starts[2], data_start=starts[3],
                partition_guids_verified=True, primary_and_backup_crc_verified=True)

def sparse(path):
    with path.open('rb') as stream:
        magic, major, minor, header, chunk_header, block, blocks, chunks, crc = struct.unpack('<IHHHHIIII', stream.read(28))
        assert (magic, major, minor, header, chunk_header, block) == (0xed26ff3a, 1, 0, 28, 12, 1048576)
        logical, first = 0, None
        for index in range(chunks):
            position = stream.tell()
            kind, reserved, count, total = struct.unpack('<HHII', stream.read(12))
            assert kind in (0xcac1, 0xcac2) and reserved == 0 and count > 0
            assert total == 12 + (count * block if kind == 0xcac1 else 4)
            if index == 0:
                first = stream.read(2048) if kind == 0xcac1 else stream.read(4) * 512
            logical += count
            assert position + total <= path.stat().st_size
            stream.seek(position + total)
        assert logical == blocks and stream.tell() == path.stat().st_size
    return dict(logical_bytes=blocks*block, chunks=chunks, dont_care_chunks=0), first


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--qemu-efi', type=Path)
    parser.add_argument('--qemu-debian', type=Path)
    parser.add_argument('--delivery', type=Path, help='standard delivery receipt for a packaging-only export')
    parser.add_argument('--report', type=Path, help='write verification evidence outside the immutable original build')
    args = parser.parse_args()
    out = args.build.resolve(strict=True)
    build = json.loads((out/'BUILD.json').read_text())
    assert build['complete'] and not build['device_access'] and not build['board_verified']
    assert all(command['exit'] == 0 for command in build['commands'])
    package = out/'package'
    manifest = json.loads((package/'MANIFEST.json').read_text())
    assert manifest['source_commit'] == build['source_commit']
    files = manifest['files']
    expected = set(files) | {'MANIFEST.json', 'SHA256SUMS'}
    assert expected == {str(p.relative_to(package)) for p in package.rglob('*') if p.is_file()}
    for name, item in files.items():
        path = package/name
        assert path.stat().st_size == item['bytes'] and sha(path) == item['sha256'], name
    sums = {}
    for line in (package/'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ', 1)
        assert name not in sums
        sums[name] = digest
    assert set(sums) == set(files) | {'MANIFEST.json'}
    for name, value in sums.items():
        assert value == (files[name]['sha256'] if name in files else sha(package/name))
    root = manifest['root_image']
    assert root['mode'] == 'adaptive-content-preserving' and root['system_content_removed'] is False
    assert root['verification']['complete'] and root['verification']['content_sha256_verified']
    scan = json.loads((out/'ROOTFS-INVENTORY.json').read_text())
    fingerprint = hashlib.sha256(json.dumps(scan['records'], sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    assert fingerprint == root['source_fingerprint_sha256'] == root['verification']['source_fingerprint_sha256']
    assert scan['entries'] == root['verification']['entries_verified']
    root_sparse, first = sparse(package/'rootfs_sparse.ext4')
    assert root_sparse['logical_bytes'] == root['image_bytes']
    sb = first[1024:2048]
    assert struct.unpack_from('<H', sb, 56)[0] == 0xef53
    blocks = struct.unpack_from('<I', sb, 4)[0] + (struct.unpack_from('<I', sb, 0x150)[0] << 32)
    block_bytes = 1024 << struct.unpack_from('<I', sb, 24)[0]
    assert blocks * block_bytes == root['image_bytes'] and block_bytes == 4096
    assert str(uuid.UUID(bytes=sb[104:120])) == manifest['root_uuid']
    layouts = {}
    for sector, suffix in ((512, ''), (4096, '-4096')):
        meta = json.loads((package/('partition-table'+suffix+'.img.json')).read_text())
        data = (package/('partition-table'+suffix+'.img')).read_bytes()
        layout = verify_slot_gpt(data, meta, sector, manifest['disk_uuid'], root['image_bytes'])
        boot_sparse, bpb = sparse(package/('boot_sparse'+suffix+'.img'))
        assert boot_sparse['logical_bytes'] == layout['boot_bytes']
        bps = struct.unpack_from('<H', bpb, 11)[0]
        reserved = struct.unpack_from('<H', bpb, 14)[0]
        total = struct.unpack_from('<I', bpb, 32)[0]
        fat_sectors = struct.unpack_from('<I', bpb, 36)[0]
        assert bpb[13] == 1 and bpb[82:90] == b'FAT32   '
        clusters = (total-reserved-bpb[16]*fat_sectors)//bpb[13]
        assert bps == sector and total*sector <= layout['boot_bytes'] and clusters >= 65525
        layouts[str(sector)] = dict(layout, fat32_clusters=clusters)
    delivery = json.loads(args.delivery.read_text()) if args.delivery else build.get('delivery')
    archive_meta = delivery['archive'] if delivery else None
    if delivery:
        verify_full_delivery(package, delivery)
    result = dict(source_commit=build['source_commit'], offline_complete=True, complete=False,
                  archive=archive_meta, full_zip_payloads_verified=bool(delivery),
                  retained_files_verified=scan['entries'], root_image_bytes=root['image_bytes'],
                  no_system_content_removed=True, layouts=layouts, root_sparse=root_sparse,
                  real_board_verified=False, device_access=False)
    if args.qemu_efi and not args.qemu_debian:
        raise ValueError("QEMU firmware evidence requires the matching Debian boot report")
    if args.qemu_debian:
        debian = json.loads(args.qemu_debian.read_text())
        assert debian['complete'] and debian['source_commit'] == build['source_commit']
        assert debian['root_sparse_sha256'] == files['rootfs_sparse.ext4']['sha256']
        assert debian['harness_sha256'] == {name: sha(out/'source/scripts/qemu'/name)
                                            for name in ('run_debian.py', 'run_efi.py', 'guest_probe.py')}
        if args.qemu_efi:
            efi = json.loads(args.qemu_efi.read_text())
            assert efi['complete'] and efi['source_commit'] == build['source_commit']
            assert efi['package_manifest_sha256'] == sha(package/'MANIFEST.json')
            assert efi['fixture_sha256'] == debian['fixture_sha256']
            assert efi['harness_sha256'] == sha(out/'source/scripts/qemu/run_efi.py')
            for evidence in (efi, debian):
                fixture = evidence['fixture_build']
                assert fixture['complete'] and fixture['artifact_sha256'] == evidence['fixture_sha256']
            templates = {files['partition-table.img']['sha256'], files['partition-table-4096.img']['sha256']}
            assert {case['template_sha256'] for case in efi['tests'] if case.get('complete')} == templates
            expected_efi = {str(sector)+'-'+kind for sector in (512, 4096)
                            for kind in ('fresh', 'reflash', 'invalid-crc')}
            assert len(efi['tests']) == 6 and {case['case'] for case in efi['tests']} == expected_efi
            for case in efi['tests']:
                assert case['non_target_disk_unchanged']
                if case['case'].endswith('invalid-crc'):
                    assert case['STATUS'] != 0 and case['WRITES'] == 0 and case['rejected_without_writes']
                else:
                    assert case['complete'] and case['STATUS'] == 0
                    assert case['READS'] == 2 and case['WRITES'] == 2 and case['REENUMERATIONS'] == 1
                    assert case['primary_and_backup_crc_verified'] and case['template_guids_preserved']
        elif debian.get('fixture_sha256') or debian.get('fixture_build'):
            raise ValueError("Fixture mode requires matching QEMU firmware evidence")
        kernel_mode = debian['kernel_mode']
        test_kernel = debian['test_kernel']
        assert kernel_mode == test_kernel['mode'] and kernel_mode in ('generic', 'cix-virt')
        assert debian['test_kernel_sha256'] == test_kernel['sha256']
        if kernel_mode == 'cix-virt':
            assert test_kernel['sha256'] == build['components']['kernel']['image']['sha256']
            assert test_kernel['production_sha256'] == test_kernel['sha256']
            assert test_kernel['initcall_blacklist'] == ['reboot_reason_init']
            assert debian.get('generic_kernel_sha256') is None
        else:
            assert debian['generic_kernel_sha256'] == test_kernel['sha256']
        assert len(debian['cases']) == 2 and {case['sector'] for case in debian['cases']} == {512, 4096}
        for case in debian['cases']:
            assert case['postboot_e2fsck'] is True and case['fat32_bpb_verified']
            first, second = case['first_boot'], case['second_boot']
            assert first['boot_count'] == 1 and second['boot_count'] == 2
            for boot in (first, second):
                assert boot['ok'] and boot['qemu_exit'] == 0
                assert boot['filesystem_bytes'] == case['layout']['slot_bytes']
                assert uuid.UUID(boot['filesystem_uuid']) == uuid.UUID(manifest['root_uuid'])
                assert boot['boot_efi_mounted'] and boot['boot_efi_files_verified'] and boot['metadata_verified']
                assert 'nodelalloc' not in boot['root_mount_options'].split(',')
                policy = json.loads((out / 'source/rootfs/fetch/rootfs-packages.json').read_text())['host_policy']
                assert set(boot['resume_units']) == set(policy['systemd_masks'])
                for name, item in boot['resume_units'].items():
                    assert item['LoadState'] == 'masked'
                    assert item['ActiveState'] == 'inactive'
                    if name.endswith('.service'):
                        assert int(item['ExecMainStartTimestampMonotonic']) == 0
                actual_blacklist = [part.split('=', 1)[1] for part in boot['kernel_command_line'].split()
                                    if part.startswith('initcall_blacklist=')]
                expected_blacklist = [','.join(test_kernel['initcall_blacklist'])] if test_kernel['initcall_blacklist'] else []
                assert actual_blacklist == expected_blacklist
                actual_masks = sorted(part.split('=', 1)[1] for part in boot['kernel_command_line'].split()
                                      if part.startswith('systemd.mask='))
                assert actual_masks == sorted(debian.get('qemu_masked_units', []))
                assert set(actual_masks) <= {'y4-network-ready.service', 'y4-boardd.service'}
            assert first['packages_sha256'] == second['packages_sha256']
        result.update(complete=True, qemu_efi=(dict(path=str(args.qemu_efi), sha256=sha(args.qemu_efi)) if args.qemu_efi else None),
                      qemu_debian=dict(path=str(args.qemu_debian), sha256=sha(args.qemu_debian)),
                      qemu_kernel_mode=kernel_mode, test_kernel=test_kernel,
                      qemu_boundary=('QEMU GPT and complete Debian userspace; delivered CIX Image with one QEMU-only board-initcall skip.'
                                     if kernel_mode == 'cix-virt' else 'QEMU GPT and complete Debian userspace with a generic test kernel.')
                                    + ' CIX UEFI/USB/physical PCIe routing/FAN1 require real-board acceptance.')
    (args.report or out/'RELEASE-VERIFICATION.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
