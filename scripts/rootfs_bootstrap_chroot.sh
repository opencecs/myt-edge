#!/bin/bash
# Private mount namespace for dpkg maintainer scripts. The staging root never sees host disks.
set -euo pipefail
root=$1
shift
test -f "$root/etc/debian_version"
mount -t tmpfs -o mode=0755,nosuid tmpfs "$root/dev"
for n in null zero full random urandom tty; do touch "$root/dev/$n"; mount --bind "/dev/$n" "$root/dev/$n"; done
mkdir -p "$root/dev/pts" "$root/dev/shm"
mount -t devpts -o newinstance,ptmxmode=0666,mode=0620 devpts "$root/dev/pts"
ln -s pts/ptmx "$root/dev/ptmx"
ln -s /proc/self/fd "$root/dev/fd"
mount -t tmpfs -o nosuid,nodev tmpfs "$root/dev/shm"
mount -t proc -o nosuid,nodev,noexec proc "$root/proc"
mount -t tmpfs -o mode=0555,nosuid,nodev,noexec tmpfs "$root/sys"
mount -o remount,ro "$root/sys"
mount -t tmpfs -o nosuid,nodev tmpfs "$root/run"
install -m 0644 /etc/resolv.conf "$root/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' >"$root/usr/sbin/policy-rc.d"
chmod 0755 "$root/usr/sbin/policy-rc.d"
exec "$@"
