#!/usr/bin/env python3
"""Exercise real tmpfs images, retained ext4 metadata and disk fallbacks."""
import argparse
import errno
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

from root_image_workspace import GIB, MIB, RootImageWorkspace
from sparse_image import convert


def command(*argv):
    return subprocess.check_output(list(map(str, argv)), stderr=subprocess.STDOUT, text=True)


def verify_image(image, output):
    command('e2fsck', '-fn', image)
    def debug(request):
        return command('debugfs', '-R', request, image)
    stat = debug('stat /usr/bin/tool')
    assert re.search(r'Mode:\s+0751', stat), stat
    assert re.search(r'User:\s+1234\s+Group:\s+2345', stat), stat
    inode = re.search(r'Inode:\s+(\d+)', stat).group(1)
    assert re.search(r'Inode:\s+(\d+)', debug('stat /usr/bin/alias')).group(1) == inode
    assert '"/lib/systemd/systemd"' in debug('stat /sbin/init')
    attribute = output / 'attribute.bin'
    debug(f'ea_get -f {attribute} /usr/bin/tool user.y4')
    assert attribute.read_bytes() == b'preserved-attribute'
    content = output / 'tool.bin'
    debug(f'dump /usr/bin/tool {content}')
    assert content.read_bytes() == b'preserved-content\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run as root on the build host for actual tmpfs integration checks')
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.output / 'filesystem'
    for directory in ('usr/bin', 'sbin', 'var/lib/dpkg'):
        (source / directory).mkdir(parents=True)
    tool = source / 'usr/bin/tool'
    tool.write_bytes(b'preserved-content\n')
    tool.chmod(0o751)
    os.chown(tool, 1234, 2345)
    os.setxattr(tool, 'user.y4', b'preserved-attribute')
    os.link(tool, source / 'usr/bin/alias')
    (source / 'sbin/init').symlink_to('/lib/systemd/systemd')
    (source / 'var/lib/dpkg/status').write_text('Package: fixture\nStatus: install ok installed\n')
    with (source / 'sparse-file').open('wb') as stream:
        stream.truncate(8 * MIB)
    def output(name):
        value = args.output / name
        value.mkdir()
        return value
    for mode in ('disk', 'auto'):
        destination = output(mode)
        workspace = RootImageWorkspace(destination, source, 64 * MIB, mode)
        with workspace:
            assert workspace.report['selected'] == ('tmpfs' if mode == 'auto' else 'disk'), workspace.report
            with workspace.path.open('xb') as stream:
                stream.truncate(64 * MIB)
            command('mke2fs', '-q', '-t', 'ext4', '-F', '-b', '4096', '-m', '1',
                    '-O', '^metadata_csum_seed,^orphan_file', '-d', source, workspace.path)
            verify_image(workspace.path, destination)
            sparse = destination / 'root.simg'
            report = convert(workspace.path, sparse)
            assert report['dont_care_chunks'] == 0
            expanded = destination / 'expanded.ext4'
            command('simg2img', sparse, expanded)
            verify_image(expanded, destination)
            if mode == 'auto':
                assert 'tmpfs' in command('findmnt', '-n', '-o', 'FSTYPE', '-T', workspace.path)
                contender = RootImageWorkspace(output('concurrent'), source, 64 * MIB)
                with contender:
                    assert contender.report['selected'] == 'disk', contender.report
        if mode == 'auto':
            assert workspace.report['released'] and not workspace.mountpoint.exists(), workspace.report
            assert not workspace.path.exists()
    with patch('root_image_workspace.memory_info', return_value={'MemTotal': 32 * GIB, 'MemAvailable': GIB}):
        with RootImageWorkspace(output('low-memory'), source, 64 * MIB) as workspace:
            assert workspace.report['selected'] == 'disk', workspace.report
            assert 'insufficient RAM' in workspace.report['reason']
    with patch('root_image_workspace.shutil.which', return_value=None):
        with RootImageWorkspace(output('missing-tools'), source, 64 * MIB) as workspace:
            assert workspace.report['selected'] == 'disk', workspace.report
    workspace = RootImageWorkspace(output('no-space'), source, 64 * MIB)
    with workspace:
        assert workspace.report['selected'] == 'tmpfs', workspace.report
        try:
            with workspace.path.open('xb', buffering=0) as stream:
                for _ in range(65):
                    stream.write(b'x' * MIB)
        except OSError as exc:
            assert exc.errno == errno.ENOSPC, exc
            workspace.fallback_to_disk('test: actual tmpfs capacity exhausted')
        else:
            raise AssertionError('tmpfs size limit was not enforced')
        assert workspace.report['selected'] == 'disk' and workspace.report['released']
        workspace.path.write_bytes(b'disk retry remains writable')
    workspace = RootImageWorkspace(output('exception'), source, 64 * MIB)
    try:
        with workspace:
            assert workspace.report['selected'] == 'tmpfs', workspace.report
            raise RuntimeError('test failure during image creation')
    except RuntimeError as exc:
        assert str(exc) == 'test failure during image creation'
    assert workspace.report['released'] and not workspace.mountpoint.exists()
    print('PASS: real bounded tmpfs, ext4 content/mode/UID/GID/hardlink/symlink/xattr, sparse expansion, low memory/concurrency/ENOSPC fallback, exception cleanup')


if __name__ == '__main__':
    main()
