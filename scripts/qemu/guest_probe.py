#!/usr/bin/python3
"""Test-copy-only systemd probe; never installed in the production image."""
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import time
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024**2), b''):
            h.update(data)
    return h.hexdigest()


def unit(name):
    text = subprocess.check_output(['systemctl', 'show', name, '--property=LoadState,ConditionResult,ActiveState,Result,ExecMainStatus,ActiveEnterTimestampMonotonic,ExecMainStartTimestampMonotonic'], text=True)
    return dict(line.split('=', 1) for line in text.splitlines())


def main():
    expected = json.loads(Path('/var/lib/y4-qemu/expected.json').read_text())
    result = dict(ok=False, kernel=os.uname().release, kernel_mode=expected['kernel_mode'], scope=expected['scope'])
    try:
        kernel_command_line = Path('/proc/cmdline').read_text().strip()
        actual_blacklist = [item.split('=', 1)[1] for item in kernel_command_line.split() if item.startswith('initcall_blacklist=')]
        assert actual_blacklist == ([','.join(expected['initcall_blacklist'])] if expected['initcall_blacklist'] else []), actual_blacklist
        if expected['kernel_release']:
            assert os.uname().release == expected['kernel_release']
        assert subprocess.check_output(['systemctl', 'is-active', 'multi-user.target'], text=True).strip() == 'active'
        root_options = subprocess.check_output(['findmnt', '--noheadings', '--raw', '--output', 'OPTIONS', '--mountpoint', '/'], text=True).strip()
        assert 'nodelalloc' not in root_options.split(','), root_options
        resume_units = {}
        for name in expected['host_policy']['systemd_masks']:
            item = unit(name); resume_units[name] = item
            assert item['LoadState'] == 'masked' and item['ActiveState'] == 'inactive', (name, item)
            if name.endswith('.service'):
                assert int(item['ExecMainStartTimestampMonotonic']) == 0, (name, item)
        for name in expected['host_policy']['forbidden_host_files']:
            target = Path('/') / name
            assert not target.is_symlink() and not target.exists(), name
        grow = unit('y4-root-grow.service'); sysinit = unit('sysinit.target')
        assert grow['Result'] == 'success' and grow['ExecMainStatus'] == '0', grow
        assert int(grow['ActiveEnterTimestampMonotonic']) > 0
        assert int(grow['ActiveEnterTimestampMonotonic']) <= int(sysinit['ActiveEnterTimestampMonotonic']), (grow, sysinit)
        ready = dict(line.split('=', 1) for line in Path('/run/y4-root-grow/result').read_text().splitlines())
        assert ready['STATUS'] == 'ready', ready
        device = ready['ROOT_DEVICE']
        info = os.stat('/')
        assert os.stat(device).st_rdev == info.st_dev
        root_sysfs = Path('/sys/dev/block/' + str(os.major(info.st_dev)) + ':' + str(os.minor(info.st_dev))).resolve()
        sector = int((root_sysfs.parent / 'queue/logical_block_size').read_text())
        assert sector == expected['sector'], sector
        with Path(device).open('rb') as stream:
            stream.seek(1024); superblock = stream.read(1024)
        get32 = lambda at: struct.unpack_from('<I', superblock, at)[0]
        assert struct.unpack_from('<H', superblock, 56)[0] == 0xef53
        filesystem_bytes = (get32(4) + (get32(0x150) << 32)) * (1024 << get32(24))
        uuid = superblock[104:120].hex()
        assert filesystem_bytes == expected['root_bytes'], (filesystem_bytes, expected['root_bytes'])
        assert uuid == expected['root_uuid'].replace('-', ''), uuid
        assert int(ready['PARTITION_BYTES']) == expected['root_bytes']
        assert int(ready['FILESYSTEM_BYTES']) == filesystem_bytes
        assert subprocess.check_output(['findmnt', '--noheadings', '--raw', '--output', 'FSTYPE', '--mountpoint', '/boot/efi'], text=True).strip() == 'vfat'
        for name, value in expected['efi_files'].items():
            assert digest('/boot/efi/' + name) == value, name
        for name, value in expected['critical_files'].items():
            assert digest('/' + name) == value, name
        packages = subprocess.check_output(['dpkg-query', '-W', '-f=${Package}\t${Architecture}\t${db:Status-Status}\t${Version}\n'])
        installed = sorted(line for line in packages.splitlines() if b'\tinstalled\t' in line)
        package_digest = hashlib.sha256(b'\n'.join(installed) + b'\n').hexdigest()
        assert package_digest == expected['package_inventory_sha256'], package_digest
        assert len(installed) == expected['installed_packages']
        path = Path('/var/lib/y4-qemu/metadata-file'); metadata = path.stat()
        assert path.read_bytes() == b'preserved content during ext4 growth\n'
        assert (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) == (1234, 2345, 0o751)
        assert metadata.st_ino == Path('/var/lib/y4-qemu/hardlink').stat().st_ino and metadata.st_nlink == 2
        assert os.readlink('/var/lib/y4-qemu/symlink') == 'metadata-file'
        for name, value in expected['fixture_xattrs'].items():
            assert hashlib.sha256(os.getxattr(path, name)).hexdigest() == value, name
        state_path = Path('/var/lib/y4-qemu/boot-state.json')
        previous = json.loads(state_path.read_text()) if state_path.exists() else None
        state = dict(uuid=uuid, filesystem_bytes=filesystem_bytes, packages_sha256=package_digest,
                     machine_id_sha256=digest('/etc/machine-id'), boot_count=previous['boot_count'] + 1 if previous else 1)
        if previous:
            for key in ('uuid', 'filesystem_bytes', 'packages_sha256', 'machine_id_sha256'):
                assert state[key] == previous[key], key
        state_path.write_text(json.dumps(state) + '\n')
        os.sync()
        login = {}
        deadline = time.monotonic() + 90
        for name in ('ssh.service', 'serial-getty@ttyAMA0.service'):
            item = unit(name)
            while item.get('ActiveState') != 'active' and time.monotonic() < deadline:
                time.sleep(.5); item = unit(name)
            login[name] = item
            started = int(item.get('ExecMainStartTimestampMonotonic', '0'))
            assert item.get('ActiveState') == 'active' and item.get('Result') == 'success' and started > 0, (name, item)
            assert started >= int(grow['ActiveEnterTimestampMonotonic']), (name, item, grow)
        failed = subprocess.run(['systemctl', '--failed', '--no-legend', '--plain'], capture_output=True, text=True).stdout
        result.update(ok=True, root_device=device, sector=sector, filesystem_bytes=filesystem_bytes,
                      filesystem_uuid=uuid, packages_sha256=package_digest, installed_packages=len(installed),
                      boot_efi_mounted=True, boot_efi_files_verified=True,
                      root_mount_options=root_options, resume_units=resume_units,
                      kernel_command_line=kernel_command_line,
                      metadata_verified=True, boot_count=state['boot_count'], grow_unit=grow,
                      sysinit_unit=sysinit, login_units=login, failed_units=failed)
    except Exception as exc:
        result.update(error=str(exc), exception=type(exc).__name__)
    print('Y4_QEMU_PROBE ' + json.dumps(result, sort_keys=True), flush=True)
    os.sync()
    subprocess.run(['systemctl', 'poweroff', '--no-block'])


if __name__ == '__main__':
    main()
