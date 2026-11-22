#!/usr/bin/env python3
"""Build both actual boot variants and verify FAT32 on file-backed 512/4Kn devices."""
import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import threading
import uuid
from pathlib import Path

from build import Builder, ROOT


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(4 * 1024**2), b''):
            digest.update(data)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-build', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run as root: only newly allocated regular-file loop devices are mounted')
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'logs').mkdir()
    package = args.output / 'package'
    package.mkdir()
    builder = object.__new__(Builder)
    builder.out = args.output.resolve()
    builder.source = ROOT
    builder.kernel_objects = args.kernel_build.resolve(strict=True) / 'kernel/objects'
    builder.disk_uuid = uuid.uuid5(uuid.NAMESPACE_URL, str(builder.out))
    part_namespace = uuid.uuid5(uuid.NAMESPACE_URL, 'MYT/Y4/Debian/partitions-v1')
    builder.part_ids = tuple(uuid.uuid5(part_namespace, label)
                             for label in ('boot', 'slot-a', 'slot-b', 'data'))
    builder.root_image_bytes = 39 * 256 * 1024**2
    builder.board_label = 'Y4'
    builder.dtb_name = 'sky1-y4.dtb'
    builder.env = dict(os.environ)
    builder.report = dict(commands=[])
    builder.report_lock = threading.Lock()
    builder.delivery_version = '20260101-r1'
    cfg = builder.boot_images(package)
    # New-layout boot structure: per-slot kernel variables and a delivery
    # version named kernel directory (kernel-ota-design 4.1).
    cfg_text = Path(cfg).read_text()
    assert '${boot_kernel_a}/Image' in cfg_text and '${boot_kernel_b}/Image' in cfg_text
    assert 'set boot_kernel_a=' + builder.delivery_version in cfg_text
    grubenv = (args.output / 'grubenv').read_bytes()
    assert len(grubenv) == 1024
    assert b'\nboot_kernel_a=' + builder.delivery_version.encode() + b'\n' in grubenv
    assert b'\nboot_kernel_b=' + builder.delivery_version.encode() + b'\n' in grubenv
    assert b'\nboot_kernel=' not in grubenv
    kernel_dir = 'kernels/' + builder.delivery_version
    files = {'EFI/BOOT/BOOTAA64.EFI': ROOT / 'boot/grub/BOOTAA64.EFI',
             'grub/grub.cfg': cfg, 'EFI/BOOT/grub.cfg': cfg,
             kernel_dir + '/Image': builder.kernel_objects / 'arch/arm64/boot/Image',
             kernel_dir + '/sky1-y4.dtb': builder.kernel_objects / 'arch/arm64/boot/dts/cix/sky1-y4.dtb'}
    result = {}
    for sector in (512, 4096):
        description = builder.report['boot_images'][str(sector)]
        image = Path(description['raw_path']).resolve(strict=True)
        assert image.is_file() and image.stat().st_size == description['image_bytes']
        with image.open('rb') as stream:
            header = stream.read(512)
        bytes_per_sector = struct.unpack_from('<H', header, 11)[0]
        sectors_per_cluster = header[13]
        reserved = struct.unpack_from('<H', header, 14)[0]
        fats = header[16]
        total_sectors = struct.unpack_from('<I', header, 32)[0]
        fat_sectors = struct.unpack_from('<I', header, 36)[0]
        clusters = (total_sectors - reserved - fats * fat_sectors) // sectors_per_cluster
        assert bytes_per_sector == sector and sectors_per_cluster == 1
        assert clusters >= 65525 and header[82:90] == b'FAT32   '
        # mkfs.vfat may leave a few trailing sectors unused depending on
        # the partition size; the filesystem only has to fit the device.
        assert total_sectors * sector <= image.stat().st_size
        log = args.output / ('fat-check-' + str(sector) + '.log')
        with log.open('wb') as stream:
            subprocess.run(['fsck.fat', '-v', '-n', str(image)], check=True, stdout=stream, stderr=subprocess.STDOUT)
        expanded = args.output / ('expanded-' + str(sector) + '.img')
        subprocess.run(['simg2img', str(package / description['sparse_name']), str(expanded)], check=True)
        assert sha(expanded) == sha(image)
        expanded.unlink()
        loop = subprocess.check_output(['losetup', '--find', '--show', '--read-only', '--sector-size', str(sector), str(image)], text=True).strip()
        if not re.fullmatch(r'/dev/loop[0-9]+', loop):
            raise RuntimeError('Unexpected allocated image loop device: ' + loop)
        mountpoint = args.output / ('fat-mount-' + str(sector))
        mountpoint.mkdir()
        mounted = False
        try:
            assert int(subprocess.check_output(['blockdev', '--getss', loop], text=True)) == sector
            subprocess.run(['mount', '-t', 'vfat', '-o', 'ro,nosuid,nodev,noexec', loop, str(mountpoint)], check=True)
            mounted = True
            for name, source in files.items():
                assert sha(mountpoint / name) == sha(source), name
        finally:
            if mounted:
                subprocess.run(['umount', str(mountpoint)], check=True)
            backing = json.loads(subprocess.check_output(['losetup', '--json', '--output', 'NAME,BACK-FILE', loop], text=True))
            assert Path(backing['loopdevices'][0]['back-file']).resolve(strict=True) == image
            subprocess.run(['losetup', '--detach', loop], check=True)
            mountpoint.rmdir()
        result[str(sector)] = dict(image_bytes=image.stat().st_size, fat32_clusters=clusters,
                                  logical_sector_bytes=sector, sparse_roundtrip=True,
                                  filesystem_check=True, file_backed_device_mount=True,
                                  grub_kernel_dtb_hashes_verified=True)
    (args.output / 'VERIFICATION.json').write_text(json.dumps(result, indent=2) + '\n')
    print('PASS: 512/4Kn FAT32 geometry, independent fsck, sparse roundtrip, actual logical-sector loop mounts and all boot file hashes')


if __name__ == '__main__':
    main()
