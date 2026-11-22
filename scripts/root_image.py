#!/usr/bin/env python3
"""Plan a content-preserving ext4 image and compare every staged filesystem object."""
import argparse
import base64
import contextlib
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

MIB = 1024**2
GIB = 1024**3
BLOCK = 4096
INODE_RATIO = 16384


def rounded(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def file_digest(path, before):
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if identity(before) != identity(os.fstat(stream.fileno())):
            raise ValueError('Source changed before hashing: ' + str(path))
        for block in iter(lambda: stream.read(4 * MIB), b''):
            digest.update(block)
        if identity(before) != identity(os.fstat(stream.fileno())):
            raise ValueError('Source changed during hashing: ' + str(path))
    return digest.hexdigest()


def inventory(root, hash_contents=True):
    root = Path(root)
    records, links = {}, {}
    logical, allocated, inodes = 0, 0, set()
    def visit(path, relative):
        nonlocal logical, allocated
        info = path.lstat()
        kind = stat.S_IFMT(info.st_mode)
        names = {stat.S_IFREG: 'file', stat.S_IFDIR: 'directory', stat.S_IFLNK: 'symlink',
                 stat.S_IFCHR: 'character', stat.S_IFBLK: 'block', stat.S_IFIFO: 'fifo', stat.S_IFSOCK: 'socket'}
        if kind not in names:
            raise ValueError('Unsupported source object: ' + str(path))
        row = dict(type=names[kind], uid=info.st_uid, gid=info.st_gid,
                   mode=stat.S_IMODE(info.st_mode), mtime_ns=info.st_mtime_ns,
                   xattrs={name: base64.b64encode(os.getxattr(path, name, follow_symlinks=False)).decode('ascii')
                           for name in sorted(os.listxattr(path, follow_symlinks=False))})
        key = info.st_dev, info.st_ino
        if key not in inodes:
            inodes.add(key)
            allocated += info.st_blocks * 512
            if kind == stat.S_IFREG:
                logical += rounded(info.st_size, BLOCK)
            elif kind == stat.S_IFDIR:
                logical += rounded(info.st_size, BLOCK)
            elif kind == stat.S_IFLNK and info.st_size > 60:
                logical += rounded(info.st_size, BLOCK)
        if kind == stat.S_IFREG:
            row['bytes'] = info.st_size
            if key in links:
                row['hardlink_to'] = links[key]
                if hash_contents:
                    row['sha256'] = records[links[key]]['sha256']
            else:
                if hash_contents:
                    row['sha256'] = file_digest(path, info)
                links[key] = relative
        elif kind == stat.S_IFLNK:
            row['target'] = os.readlink(path)
        elif kind in (stat.S_IFCHR, stat.S_IFBLK):
            row['device'] = [os.major(info.st_rdev), os.minor(info.st_rdev)]
        records[relative] = row
        if kind == stat.S_IFDIR:
            for child in sorted(path.iterdir(), key=lambda value: value.name):
                visit(child, child.name if relative == '.' else relative + '/' + child.name)
    visit(root, '.')
    encoded = json.dumps(records, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    return dict(schema=1, entries=len(records), unique_inodes=len(inodes), content_hashes=hash_contents,
                data_bytes=logical, allocated_bytes=allocated,
                fingerprint_sha256=hashlib.sha256(encoded).hexdigest(), records=records)


def plan(scan, minimum_bytes=GIB, reserve_bytes=GIB, alignment=256 * MIB):
    payload = max(scan['data_bytes'], scan['allocated_bytes'])
    inode_floor = (scan['unique_inodes'] + max(65536, scan['unique_inodes'] // 2)) * INODE_RATIO
    size = rounded(max(minimum_bytes, payload + payload // 4 + reserve_bytes, inode_floor), alignment)
    return dict(schema=1, mode='adaptive-content-preserving', image_bytes=size,
                block_bytes=BLOCK, inode_ratio=INODE_RATIO, alignment_bytes=alignment,
                minimum_headroom_bytes=reserve_bytes, headroom_percent=25,
                source_data_bytes=scan['data_bytes'], source_allocated_bytes=scan['allocated_bytes'],
                source_entries=scan['entries'], source_unique_inodes=scan['unique_inodes'],
                source_fingerprint_sha256=scan['fingerprint_sha256'],
                system_content_removed=False, first_boot_grow_service='y4-root-grow.service')


@contextlib.contextmanager
def mounted(image, destination, readonly=True):
    image = Path(image).resolve(strict=True)
    if not image.is_file():
        raise ValueError('A regular image file is required')
    destination = Path(destination)
    destination.mkdir(mode=0o700, exist_ok=False)
    options = 'loop,nosuid,nodev,noexec,' + ('ro,noload' if readonly else 'rw')
    attached = False
    try:
        subprocess.run(['mount', '-t', 'ext4', '-o', options, str(image), str(destination)], check=True)
        attached = True
        yield destination
    finally:
        if attached:
            subprocess.run(['umount', str(destination)], check=True)
        destination.rmdir()


def compare(expected, actual, ignore_fields=()):
    wanted = expected['records']
    found = dict(actual['records'])
    if 'lost+found' not in wanted and 'lost+found' in found:
        if any(name.startswith('lost+found/') for name in found):
            raise ValueError('Unexpected objects in image lost+found')
        if found['lost+found']['type'] != 'directory':
            raise ValueError('Unexpected image lost+found type')
        del found['lost+found']
    missing, extra = sorted(wanted.keys() - found.keys()), sorted(found.keys() - wanted.keys())
    if missing or extra:
        raise ValueError('Image content paths differ: ' + json.dumps(dict(missing=missing[:10], extra=extra[:10])))
    mismatches = []
    for name in wanted:
        before = {key: value for key, value in wanted[name].items() if key not in ignore_fields}
        after = {key: value for key, value in found[name].items() if key not in ignore_fields}
        if before != after:
            mismatches.append(dict(path=name, fields=[key for key in wanted[name].keys() | found[name].keys()
                                                     if key not in ignore_fields and wanted[name].get(key) != found[name].get(key)]))
            if len(mismatches) >= 10:
                break
    if mismatches:
        raise ValueError('Image object metadata/content differ: ' + json.dumps(mismatches))
    return dict(complete=True, entries_verified=len(wanted),
                source_fingerprint_sha256=expected['fingerprint_sha256'],
                content_sha256_verified=('sha256' not in ignore_fields and expected['content_hashes'] and actual['content_hashes']),
                metadata_verified=[field for field in ['type', 'uid', 'gid', 'mode', 'mtime_ns',
                                                       'hardlinks', 'symlinks', 'xattrs', 'devices']
                                   if field not in ignore_fields])


def restore_timestamps(image, expected, mountpoint):
    """mke2fs -d preserves whole seconds; restore source nanoseconds through ext4 itself."""
    with mounted(image, mountpoint, readonly=False) as root:
        before = inventory(root, hash_contents=False)
        compare(expected, before, ignore_fields=('sha256', 'mtime_ns'))
        restored = 0
        for name, row in expected['records'].items():
            if row['mtime_ns'] == before['records'][name]['mtime_ns']:
                continue
            path = root if name == '.' else root / name
            info = path.lstat()
            os.utime(path, ns=(info.st_atime_ns, row['mtime_ns']), follow_symlinks=False)
            restored += 1
    return dict(nanosecond_mtimes_restored=restored)


def verify(image, expected, mountpoint):
    with mounted(image, mountpoint) as root:
        actual = inventory(root)
    return compare(expected, actual)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('filesystem', type=Path, nargs='?')
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--image', type=Path)
    parser.add_argument('--mountpoint', type=Path)
    parser.add_argument('--report', type=Path)
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument('--restore-mtimes', action='store_true')
    operations.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.restore_mtimes or args.verify:
        if args.image is None or args.mountpoint is None:
            parser.error('--image and --mountpoint are required for image operations')
        expected = json.loads(args.inventory.read_text())
        operation = restore_timestamps if args.restore_mtimes else verify
        result = operation(args.image, expected, args.mountpoint)
    else:
        if args.filesystem is None or args.plan is None:
            parser.error('filesystem and --plan are required for an inventory')
        scan = inventory(args.filesystem)
        args.inventory.write_text(json.dumps(scan, indent=2, ensure_ascii=True) + '\n')
        result = plan(scan)
        args.plan.write_text(json.dumps(result, indent=2) + '\n')
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
