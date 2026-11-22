#!/usr/bin/env python3
"""Verify adaptive sizing with real ext4 and complete metadata/content comparison."""
import argparse
import os
import subprocess
from pathlib import Path

from root_image import MIB, inventory, plan, restore_timestamps, verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.output / 'filesystem'
    (source / 'usr/bin').mkdir(parents=True)
    (source / 'etc').mkdir()
    tool = source / 'usr/bin/tool'
    tool.write_bytes(b'preserve all content\n' + os.urandom(128 * 1024))
    tool.chmod(0o751)
    os.chown(tool, 1234, 2345)
    os.setxattr(tool, 'user.preserve', b'full-xattr\0value')
    os.utime(tool, ns=(1700000000123456789, 1700000000987654321))
    os.link(tool, source / 'usr/bin/hardlink')
    (source / 'etc/absolute-link').symlink_to('/usr/bin/tool')
    (source / 'etc/unusual\n名称').write_text('non-ASCII and newline path')
    os.mkfifo(source / 'etc/fifo', 0o640)
    os.mknod(source / 'etc/null', 0o20600, os.makedev(1, 3))
    with (source / 'sparse').open('wb') as stream:
        stream.truncate(8 * MIB)
        stream.seek(6 * MIB)
        stream.write(b'last extent')
    for program, arguments in [('setfacl', ['-m', 'u:3456:r--', str(tool)]),
                               ('setcap', ['cap_net_bind_service=ep', str(tool)])]:
        subprocess.run([program, *arguments], check=True)
    expected = inventory(source)
    recipe = plan(expected)
    assert recipe['system_content_removed'] is False
    image = args.output / 'rootfs.ext4'
    with image.open('xb') as stream:
        stream.truncate(recipe['image_bytes'])
    subprocess.run(['mke2fs', '-q', '-t', 'ext4', '-F', '-b', '4096', '-i', '16384', '-m', '1',
                    '-O', '^metadata_csum_seed,^orphan_file', '-d', str(source), str(image)], check=True)
    print(restore_timestamps(image, expected, args.output / 'timestamp-mount'))
    subprocess.run(['e2fsck', '-fn', str(image)], check=True)
    print(verify(image, expected, args.output / 'verify-mount'))
    second = source / 'new-large-file'
    with second.open('wb') as stream:
        stream.truncate(2 * 1024**3)
    larger = plan(inventory(source))
    assert larger['image_bytes'] > recipe['image_bytes']
    assert larger['image_bytes'] % (256 * MIB) == 0
    assert inventory(source)['records']['usr/bin/tool']['sha256'] == expected['records']['usr/bin/tool']['sha256']
    print('PASS: full files, nanosecond timestamps, owners, modes, hardlinks, symlinks, ACL/capability/xattrs, special nodes, adaptive sizing')


if __name__ == '__main__':
    main()
