#!/usr/bin/env python3
"""Bound the RAM used by a disposable raw ext4 image; retain disk fallback."""
import fcntl
import os
import shutil
import subprocess
from pathlib import Path

MIB = 1024**2
GIB = 1024**3
LOCK = Path('/run/lock/y4-debian-root-image.lock')


def memory_info():
    return {line.split(':', 1)[0]: int(line.split()[1]) * 1024
            for line in Path('/proc/meminfo').read_text().splitlines()
            if line.startswith(('MemTotal:', 'MemAvailable:'))}


class RootImageWorkspace:
    def __init__(self, output, filesystem, raw_bytes, mode='auto'):
        self.output, self.filesystem = Path(output), Path(filesystem)
        self.raw_bytes, self.mode = raw_bytes, mode
        self.path = self.output / 'rootfs.ext4'
        self.mountpoint = self.output / 'root-image-tmpfs'
        self.lock_fd, self.mounted = None, False
        self.report = dict(requested=mode, selected='disk', raw_bytes=raw_bytes,
                           temporary_image_only=True, released=False)

    def __enter__(self):
        if self.mode == 'disk':
            self.report['reason'] = 'disk explicitly requested'
            return self
        try:
            if not all(shutil.which(name) for name in ('mount', 'umount', 'du')):
                self.report['reason'] = 'tmpfs host tools unavailable'
                return self
            self.lock_fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.fallback_to_disk('another build owns the RAM workspace budget')
                return self
            memory = memory_info()
            reserve = max(8 * GIB, memory['MemTotal'] // 4)
            cap = min(12 * GIB, memory['MemTotal'] // 2)
            payload = int(subprocess.check_output(
                ['du', '-sx', '--apparent-size', '--block-size=1', '--', str(self.filesystem)],
                text=True).split()[0])
            limit = min(self.raw_bytes, ((payload + max(GIB, payload // 4) + MIB - 1) // MIB) * MIB)
            self.report.update(mem_available_bytes=memory['MemAvailable'], reserve_bytes=reserve,
                               payload_apparent_bytes=payload, tmpfs_limit_bytes=limit,
                               tmpfs_cap_bytes=cap)
            if limit > cap or memory['MemAvailable'] < limit + reserve:
                self.fallback_to_disk('insufficient RAM for the bounded image plus system reserve')
                return self
            self.mountpoint.mkdir(mode=0o700)
            result = subprocess.run(['mount', '-t', 'tmpfs', '-o',
                                     f'size={limit},nr_inodes=64,mode=0700,nosuid,nodev,noexec',
                                     'y4-debian-root-image', str(self.mountpoint)],
                                    capture_output=True, text=True)
            if result.returncode:
                self.fallback_to_disk('tmpfs mount failed: ' + result.stderr.strip())
                return self
            self.mounted = True
            self.path = self.mountpoint / 'rootfs.ext4'
            self.report.update(selected='tmpfs', reason='bounded RAM workspace available',
                               mountpoint=str(self.mountpoint))
            return self
        except (OSError, subprocess.SubprocessError, KeyError, ValueError) as exc:
            self.fallback_to_disk('tmpfs setup unavailable: ' + str(exc))
            return self

    def capture_usage(self):
        if self.mounted:
            info = os.statvfs(self.mountpoint)
            self.report['tmpfs_used_bytes'] = (info.f_blocks - info.f_bfree) * info.f_frsize
            if self.path.is_file():
                self.report['raw_allocated_bytes'] = self.path.stat().st_blocks * 512

    def release(self):
        try:
            if self.mounted:
                self.capture_usage()
                result = subprocess.run(['umount', str(self.mountpoint)], capture_output=True, text=True)
                if result.returncode:
                    self.report['cleanup_error'] = result.stderr.strip()
                    raise RuntimeError('Cannot release RAM image mount: ' + str(self.mountpoint))
                self.mounted = False
                self.report['released'] = True
            if self.mountpoint.exists():
                self.mountpoint.rmdir()
        finally:
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None

    def fallback_to_disk(self, reason):
        self.release()
        self.path = self.output / 'rootfs.ext4'
        self.report.update(selected='disk', reason=reason)

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False
