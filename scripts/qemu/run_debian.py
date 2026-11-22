#!/usr/bin/env python3
"""Boot the final, complete Debian image on QEMU NVMe and verify early growth.

The cix-virt mode boots the exact delivered CIX Image with only its board-only
reboot_reason_init initcall blacklisted in QEMU's diagnostic command line.
Generic ARM64 kernels remain an explicit alternative. Neither mode validates CIX
hardware. Only regular test image files are accepted. Instrumentation is added
to a private copy, never the package, its filesystem staging tree or any physical device.
"""
import argparse
import gzip
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gpt
from run_efi import MIB, boot_bytes, slot_a_start, data_start, boot_fixture, validate_gpt, qemu_base


def run(argv, **kwargs):
    return subprocess.run(list(map(str, argv)), check=True, **kwargs)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(4*MIB), b''): h.update(data)
    return h.hexdigest()


def cpio_entry(name, data, mode):
    name = name.encode() + b'\0'
    fields = [1, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0]
    output = b'070701' + b''.join(f'{value:08x}'.encode() for value in fields) + name
    output += b'\0' * (-len(output) % 4)
    output += data + b'\0' * (-len(data) % 4)
    return output


def copy_extents(source, destination, offset=0):
    with source.open('rb', buffering=0) as incoming, destination.open('r+b', buffering=0) as outgoing:
        position, length = 0, source.stat().st_size
        while position < length:
            try: begin = os.lseek(incoming.fileno(), position, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == 6: break
                raise
            end = min(os.lseek(incoming.fileno(), begin, os.SEEK_HOLE), length)
            incoming.seek(begin); outgoing.seek(offset + begin)
            while begin < end:
                data = incoming.read(min(4*MIB, end-begin))
                if not data: raise ValueError('Source shortened during test-copy construction')
                outgoing.write(data); begin += len(data)
            position = end


def prepare_base(build, output):
    package = build / 'package'; fs = build / 'filesystem'
    manifest = json.loads((package / 'MANIFEST.json').read_text())
    report = json.loads((build / 'BUILD.json').read_text())
    assert report['complete']
    root = output / 'instrumented-root.ext4'
    run(['simg2img', package / 'rootfs_sparse.ext4', root])
    staging = output / 'probe'; staging.mkdir()
    dtb = 'sky1-' + report['board'].lower() + '.dtb'
    policy = json.loads((build / 'source/rootfs/fetch/rootfs-packages.json').read_text())['host_policy']
    version = json.loads((package / 'y4-release.json').read_text())['rootfs']
    kernel_directory = 'kernels/' + version + '/'
    critical = ['var/lib/dpkg/status', 'usr/bin/apt', 'usr/bin/dpkg', 'usr/lib/systemd/systemd',
                'usr/libexec/y4-root-grow', 'usr/lib/systemd/system/y4-root-grow.service',
                'boot/vmlinuz-' + report['components']['kernel']['version'], 'boot/' + dtb,
                'etc/fstab', 'etc/systemd/sleep.conf.d/90-y4-always-on.conf']
    if report['board'] == 'Y4':
        critical.append('usr/local/sbin/y4-mcud')
    package_data = subprocess.check_output(['dpkg-query', '--admindir=' + str(fs / 'var/lib/dpkg'),
                                          '-W', '-f=${Package}\t${Architecture}\t${db:Status-Status}\t${Version}\n'])
    installed = sorted(line for line in package_data.splitlines() if b'\tinstalled\t' in line)
    expected = dict(host_policy=policy, root_uuid=manifest['root_uuid'], critical_files={name: digest(fs / name) for name in critical},
                    efi_files={'EFI/BOOT/BOOTAA64.EFI': digest(build / 'source/boot/grub/BOOTAA64.EFI'),
                               'grub/grub.cfg': digest(package / 'grub.cfg'),
                               kernel_directory + 'Image': report['components']['kernel']['image']['sha256'],
                               kernel_directory + dtb: report['components']['kernel']['dtb']['sha256']},
                    package_inventory_sha256=hashlib.sha256(b'\n'.join(installed) + b'\n').hexdigest(),
                    installed_packages=len(installed))
    (staging / 'metadata-file').write_bytes(b'preserved content during ext4 growth\n')
    acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in
          [(1, 7, 0xffffffff), (2, 4, 2345), (4, 5, 0xffffffff), (16, 5, 0xffffffff), (32, 1, 0xffffffff)])
    capability = struct.pack('<IIIII', 0x02000001, 1024, 0, 0, 0)
    xattrs = {'user.y4-qemu': b'preserved-xattr', 'system.posix_acl_access': acl, 'security.capability': capability}
    expected['fixture_xattrs'] = {name: hashlib.sha256(value).hexdigest() for name, value in xattrs.items()}
    for index, value in enumerate(xattrs.values()): (staging / ('xattr-' + str(index))).write_bytes(value)
    # Case-specific sector/root_bytes are supplied by the initramfs kernel command
    # line to a small per-case expectations file, written below before each boot.
    (staging / 'expected.json').write_text(json.dumps(expected))
    service = '[Unit]\nDescription=QEMU-only acceptance probe\nAfter=multi-user.target y4-root-grow.service\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /var/lib/y4-qemu/guest_probe.py\nStandardOutput=journal+console\n'
    timer = '[Unit]\nDescription=QEMU-only acceptance timer\n[Timer]\nOnBootSec=20\nUnit=y4-qemu-probe.service\n[Install]\nWantedBy=timers.target\n'
    (staging / 'probe.service').write_text(service); (staging / 'probe.timer').write_text(timer)
    commands = ['mkdir /var/lib/y4-qemu',
                f'write {Path(__file__).with_name("guest_probe.py")} /var/lib/y4-qemu/guest_probe.py',
                f'write {staging / "probe.service"} /etc/systemd/system/y4-qemu-probe.service',
                f'write {staging / "probe.timer"} /etc/systemd/system/y4-qemu-probe.timer',
                'symlink /etc/systemd/system/timers.target.wants/y4-qemu-probe.timer /etc/systemd/system/y4-qemu-probe.timer',
                f'write {staging / "expected.json"} /var/lib/y4-qemu/expected.json',
                f'write {staging / "metadata-file"} /var/lib/y4-qemu/metadata-file',
                'set_inode_field /var/lib/y4-qemu/metadata-file uid 1234',
                'set_inode_field /var/lib/y4-qemu/metadata-file gid 2345',
                'set_inode_field /var/lib/y4-qemu/metadata-file mode 0100751',
                'link /var/lib/y4-qemu/metadata-file /var/lib/y4-qemu/hardlink',
                'set_inode_field /var/lib/y4-qemu/metadata-file links_count 2',
                'symlink /var/lib/y4-qemu/symlink metadata-file']
    if not (fs / 'etc/systemd/system/timers.target.wants').is_dir():
        commands.insert(0, 'mkdir /etc/systemd/system/timers.target.wants')
    for index, name in enumerate(xattrs):
        commands.append(f'ea_set -f {staging / ("xattr-" + str(index))} /var/lib/y4-qemu/metadata-file {name}')
    instructions = output / 'instrument.debugfs'; instructions.write_text('\n'.join(commands) + '\n')
    with (output / 'instrument.log').open('wb') as log:
        run(['debugfs', '-w', '-f', instructions, root], stdout=log, stderr=subprocess.STDOUT)
        run(['e2fsck', '-fn', root], stdout=log, stderr=subprocess.STDOUT)
    return root, expected, report


def boot_linux(output, disk, sector, kernel, ramdisk, timeout, kernel_arguments=()):
    output.mkdir(parents=True, exist_ok=False)
    command = qemu_base(3072) + ['-kernel', str(kernel), '-initrd', str(ramdisk), '-append',
        'console=ttyAMA0 loglevel=4 systemd.log_target=console systemd.show_status=1 systemd.unit=multi-user.target panic=15' + ''.join(' ' + value for value in kernel_arguments),
        '-drive', 'if=none,id=nvm,file=' + str(disk) + ',format=qcow2',
        '-device', 'nvme,drive=nvm,serial=Y4-DEBIAN-QEMU,logical_block_size=' + str(sector) + ',physical_block_size=' + str(sector)]
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    start = time.monotonic()
    with (output / 'console.log').open('wb') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try: code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            raise RuntimeError('Owned QEMU Debian boot timed out; console retained: ' + str(output))
    text = (output / 'console.log').read_text(errors='replace')
    markers = [line.split('Y4_QEMU_PROBE ', 1)[1] for line in text.splitlines() if 'Y4_QEMU_PROBE {' in line]
    if not markers: raise RuntimeError('Debian probe never completed; console retained: ' + str(output))
    result = json.loads(markers[-1]); result.update(qemu_exit=code, seconds=time.monotonic()-start)
    (output / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
    assert result['ok'] and code == 0, result
    return result


def provision_test_gpt(base, template, sector, capacity):
    raw = template.read_bytes()
    disk_guid = uuid.UUID(bytes_le=raw[sector+56:sector+72])
    part_ids = [uuid.UUID(bytes_le=raw[2*sector+i*128+16:2*sector+i*128+32])
                for i in range(4)]
    packed, _ = gpt.packed(sector, disk_guid, part_ids, capacity=capacity)
    primary_bytes = 2*sector + 16384
    with base.open('r+b') as stream:
        stream.truncate(capacity)
        stream.write(packed[:primary_bytes])
        stream.seek(capacity - (len(packed) - primary_bytes))
        stream.write(packed[primary_bytes:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, help='Optional private firmware GPT regression fixture')
    parser.add_argument('--kernel', type=Path, required=True)
    parser.add_argument('--kernel-mode', choices=('generic', 'cix-virt'), default='generic',
                        help='cix-virt requires the exact delivered Image and adds one QEMU-only initcall blacklist')
    parser.add_argument('--initrd', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args()
    harness_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).resolve().parents[2], text=True).strip()
    harness_files = {name: digest(Path(__file__).with_name(name)) for name in ('run_debian.py', 'run_efi.py', 'guest_probe.py')}
    for path in (args.kernel, args.initrd, *([args.fixture] if args.fixture else [])):
        assert stat.S_ISREG(path.stat().st_mode)
    build_report = json.loads((args.build / 'BUILD.json').read_text())
    production_kernel_sha = build_report['components']['kernel']['image']['sha256']
    kernel_sha, initrd_sha = digest(args.kernel), digest(args.initrd)
    blacklist = ['reboot_reason_init'] if args.kernel_mode == 'cix-virt' else []
    if args.kernel_mode == 'cix-virt':
        assert kernel_sha == production_kernel_sha, 'cix-virt must boot the exact delivered CIX Image'
        scope = ('Complete delivered Debian userspace and exact delivered CIX Image on QEMU virt; '
                 'QEMU command line only: initcall_blacklist=reboot_reason_init. '
                 'CIX board SMC/reset, UEFI hardware, USB flash and physical FAN1 remain real-board validation.')
    else:
        scope = ('Complete delivered Debian userspace on QEMU virt with a generic ARM64 kernel. '
                 'The delivered CIX kernel and all physical CIX hardware remain real-board validation.')
    # QEMU virt has no board MCU or factory-programmed NICs.
    simulated_missing_units = ['y4-network-ready.service', 'y4-boardd.service']
    kernel_arguments = ['systemd.mask=' + unit for unit in simulated_missing_units]
    if blacklist:
        kernel_arguments.append('initcall_blacklist=' + ','.join(blacklist))
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    root_image, expected, build_report = prepare_base(args.build.resolve(strict=True), out)
    cases = []
    for sector, capacity in [(512, 256000000000), (4096, 64*1024**3)]:
        case = out / str(sector); case.mkdir()
        package = args.build / 'package'
        boot_image = case / 'boot-for-vm.img'
        run(['simg2img', package / ('boot_sparse-4096.img' if sector == 4096 else 'boot_sparse.img'), boot_image])
        with boot_image.open('rb') as stream:
            bpb = stream.read(512)
        bytes_per_sector = struct.unpack_from('<H', bpb, 11)[0]
        sectors_per_cluster = bpb[13]
        reserved, fats = struct.unpack_from('<H', bpb, 14)[0], bpb[16]
        total_sectors = struct.unpack_from('<I', bpb, 32)[0]
        fat_sectors = struct.unpack_from('<I', bpb, 36)[0]
        clusters = (total_sectors - reserved - fats * fat_sectors) // sectors_per_cluster
        assert bytes_per_sector == sector and boot_image.stat().st_size == boot_bytes(sector)
        assert total_sectors * sector <= boot_bytes(sector) and clusters >= 65525
        assert struct.unpack_from('<H', bpb, 17)[0] == 0 and struct.unpack_from('<H', bpb, 22)[0] == 0
        base = case / 'base-disk.raw'
        with base.open('xb') as stream: stream.truncate(slot_a_start(sector) + root_image.stat().st_size)
        copy_extents(root_image, base, slot_a_start(sector))
        copy_extents(boot_image, base, 10*MIB)
        # Independent base copies let case-specific expectations be installed
        # without touching the production image or sharing mutable guest data.
        template = package / ('partition-table.img' if sector == 512 else 'partition-table-4096.img')
        disk = case / 'nvme.qcow2'
        if args.fixture is None:
            provision_test_gpt(base, template, sector, capacity)
        run(['qemu-img', 'create', '-f', 'qcow2', '-F', 'raw', '-b', base, disk, str(capacity)], stdout=subprocess.DEVNULL)
        if args.fixture:
            boot_fixture(case / 'uefi', args.fixture, template, disk, sector, 'qcow2')
        layout = validate_gpt(disk, sector, capacity, template, 'qcow2', case)
        config = dict(expected, sector=sector, root_bytes=layout['slot_bytes'],
                      kernel_mode=args.kernel_mode, scope=scope,
                      initcall_blacklist=blacklist,
                      kernel_release=build_report['components']['kernel']['version'] if blacklist else None)
        # Update the test-only config through a COW block view; qemu-img dd writes
        # are deliberately avoided. A tiny per-case initramfs file is copied into
        # the mounted test root before exec of the real Debian systemd.
        prefix = args.initrd.read_bytes()
        init_script = b'''#!/bin/sh
set -eu
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
Y4_QEMU_LOAD_MODULES
count=0
while [ ! -b /dev/nvme0n1p2 ]; do count=$((count+1)); [ "$count" -le 60 ] || { echo Y4_QEMU_ROOT_MISSING; exec /bin/sh; }; sleep 1; done
mkdir -p /newroot
mount -t ext4 -o rw /dev/nvme0n1p2 /newroot
cp /qemu-expected.json /newroot/var/lib/y4-qemu/expected.json
mount --move /proc /newroot/proc
mount --move /sys /newroot/sys
mount --move /dev /newroot/dev
exec /sbin/switch_root /newroot /sbin/init
'''
        modules = (b'for module in virtio_pci nvme ext4 vfat nls_ascii nls_cp437 nls_utf8; do modprobe "$module" || true; done'
                   if args.kernel_mode == 'generic' else b'# CIX virtio/NVMe/ext4/VFAT and default FAT charset are built in.')
        init_script = init_script.replace(b'Y4_QEMU_LOAD_MODULES', modules)
        overlay = cpio_entry('init', init_script, 0o100755) + cpio_entry('qemu-expected.json', json.dumps(config).encode(), 0o100644) + cpio_entry('TRAILER!!!', b'', 0)
        ramdisk = case / 'initrd.gz'; ramdisk.write_bytes(prefix + gzip.compress(overlay, mtime=0))
        runtime_initrd_sha = digest(ramdisk)
        first = boot_linux(case / 'boot-1', disk, sector, args.kernel, ramdisk, args.timeout, kernel_arguments)
        second = boot_linux(case / 'boot-2', disk, sector, args.kernel, ramdisk, args.timeout, kernel_arguments)
        assert first['boot_count'] == 1 and second['boot_count'] == 2
        assert first['filesystem_uuid'] == second['filesystem_uuid']
        # Read the guest's enlarged filesystem through qemu's offset view and
        # independently run e2fsck after its orderly systemd shutdown.
        expanded = case / 'root-after.ext4'
        options = 'driver=raw,offset=' + str(slot_a_start(sector)) + ',size=' + str(layout['slot_bytes']) + ',file.driver=qcow2,file.file.driver=file,file.file.filename=' + str(disk)
        with (case / 'postboot-e2fsck.log').open('wb') as log:
            run(['qemu-img', 'convert', '--image-opts', '-O', 'raw', '-S', '4k', options, expanded], stdout=log, stderr=subprocess.STDOUT)
            run(['e2fsck', '-fn', expanded], stdout=log, stderr=subprocess.STDOUT)
        expanded.unlink()
        cases.append(dict(sector=sector, capacity_bytes=capacity, layout=layout, fat32_bpb_verified=True,
                          fat32_cluster_count=clusters, runtime_initrd_sha256=runtime_initrd_sha,
                          first_boot=first, second_boot=second, postboot_e2fsck=True))
    root_sha = digest(args.build / 'package/rootfs_sparse.ext4')
    fixture_build = json.loads((args.fixture.parent / 'BUILD.json').read_text()) if args.fixture else None
    notes = ['Both boards use the common always-on host policy. The guest checks masked units, removed host payloads and the delivered ext4 allocation policy.']
    if args.kernel_mode == 'cix-virt':
        notes += ['The kernel SHA matches BUILD.components.kernel.image.sha256; production Image, GRUB, UEFI and kernel command line are unchanged.',
                  'Only the QEMU diagnostic command line blacklists reboot_reason_init, whose board-only SMC is unavailable on virt. The test initramfs supplies userspace/instrumentation; CIX storage/filesystem drivers are built in.']
    else:
        notes += ['Generic test kernels must supply matching storage and FAT charset modules before switch_root; the production filesystem contains only CIX modules.',
                  'Production CIX kernel has FAT/VFAT/CP437/ISO8859_1 built in and defaults to iso8859-1. Production kernel and fstab are not modified for QEMU.']
    report = dict(complete=True, source_commit=build_report['source_commit'], production_build=str(args.build),
                  harness_source_commit=harness_commit, harness_sha256=harness_files,
                  root_sparse_sha256=root_sha, production_root_sparse_sha256=root_sha,
                  fixture_sha256=digest(args.fixture) if args.fixture else None, kernel_mode=args.kernel_mode,
                  test_kernel_sha256=kernel_sha, production_kernel_sha256=production_kernel_sha,
                  generic_kernel_sha256=kernel_sha if args.kernel_mode == 'generic' else None,
                  generic_initrd_sha256=initrd_sha, fixture_build=fixture_build,
                  test_kernel=dict(mode=args.kernel_mode, path=str(args.kernel), sha256=kernel_sha,
                                   production_sha256=production_kernel_sha, initcall_blacklist=blacklist,
                                   release=cases[0]['first_boot']['kernel']),
                  initrd=dict(path=str(args.initrd), sha256=initrd_sha, role='QEMU-only test initramfs'),
                  notes=notes, cases=cases, scope=scope, qemu_masked_units=simulated_missing_units)
    (out / 'VERIFICATION.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
