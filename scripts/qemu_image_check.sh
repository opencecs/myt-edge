#!/bin/bash
# QEMU verification for a Y4 Debian slot image, run BEFORE delivering it.
# Usage: qemu_image_check.sh <build-dir>
#  Part 1: loop-mount the image and verify the login-critical content.
#  Part 2: boot the image in qemu -M virt with init=/bin/sh and verify the
#          login mechanism (crypt/getent) using the image's own libcrypt.
set -eu
D="$1"
F="$D/package/rootfs_sparse.ext4"
HASH='$6$1Sn6zHZuYjgawejQ$dp5gpBPlG8V/eJsY3QLIYktOdMkXMTxcsqp8IMculcT5agm.YHBxdwA1b1ZHu6bpkYyR45Z.E4p7cQ2InUt9V0'
WORK=$(mktemp -d /tmp/y4-qemu-check.XXXXXX)
trap 'umount "$WORK/mnt" 2>/dev/null || true; losetup -d "$LOOP" 2>/dev/null || true; rm -rf "$WORK"' EXIT
mkdir -p "$WORK/mnt"

echo "== part 1: image content =="
simimg="$WORK/rootfs.raw"
simg2img "$F" "$simimg"
LOOP=$(losetup --find --show --read-only "$simimg")
mount -o ro "$LOOP" "$WORK/mnt"
python3 - "$WORK/mnt" <<'PYEOF'
import crypt, sys
root = sys.argv[1]
shadow = open(root + "/etc/shadow").read().splitlines()
rootline = [l for l in shadow if l.startswith("root:")][0]
fields = rootline.split(":")
h = fields[1]
assert len(fields) == 9, "shadow root line has %d fields" % len(fields)
assert "$6$1Sn6zHZuYjgawejQ" in h, "shadow root hash is not the production hash"
assert crypt.crypt("myt", h) == h, "crypt(myt) does not verify"
passwd = [l for l in open(root + "/etc/passwd") if l.startswith("root:")][0]
assert passwd.split(":")[1] == "x", "passwd root field is not x"
print("shadow 9-field + production hash + crypt(myt) + passwd x: OK")
PYEOF
echo "fstab binds: $(grep -c 'none bind' "$WORK/mnt/etc/fstab")"
test -x "$WORK/mnt/usr/bin/btop" && test -x "$WORK/mnt/usr/bin/htop" && echo "btop/htop OK"
test -f "$WORK/mnt/usr/share/y4-board/ota-public-key.bin" && echo "ota key OK"
umount "$WORK/mnt"
losetup -d "$LOOP"
echo "== part 1 PASS =="

echo "== part 2: qemu boot =="
KERNEL=$(ls "$D"/filesystem/boot/vmlinuz-* | head -1)
timeout 120 qemu-system-aarch64 -M virt -nodefaults -cpu cortex-a76 -smp 4 -m 4G \
  -kernel "$KERNEL" \
  -append "root=/dev/vda rw init=/bin/sh console=ttyAMA0 earlycon=pl011,mmio32,0x09000000" \
  -drive if=none,id=hd0,format=raw,file="$simimg" \
  -device virtio-blk-device,drive=hd0 -net none -nographic > "$WORK/qemu.log" 2>&1 <<QEOF || true
mount -o remount,rw /
python3 - <<'PY'
import crypt
h = [l.split(":")[1] for l in open("/etc/shadow") if l.startswith("root:")][0]
print("QEMU_CRYPT_OK" if crypt.crypt("myt", h) == h else "QEMU_CRYPT_FAIL")
print("QEMU_GETENT_OK" if open("/etc/passwd").read().split("root:")[1].split(":")[0] == "x" else "QEMU_PASSWD_FAIL")
PY
poweroff -f
QEOF
grep -E "QEMU_CRYPT_OK|QEMU_PASSWD_OK" "$WORK/qemu.log" && echo "== part 2 PASS ==" || { echo "== part 2 FAIL =="; tail -20 "$WORK/qemu.log"; exit 1; }
echo "ALL QEMU CHECKS PASS"
