#!/bin/sh
# Explicit invocation only; package verification never accesses a device.
# Protocol 3: flashing writes the GPT and the boot/slot-a/slot-b partitions.
# The data partition is provisioned by the installed system on first boot and
# is never written here, so reinstalls keep user data. Stale-signature
# cleanup happens on first boot inside the installed system, so no erase step
# belongs to this flow. erase:debian-system remains a manual UEFI maintenance
# command, not part of this flow.
set -eu
cd -- "$(dirname -- "$0")"
if [ "${1:-}" = '--extract-spi-bootstrap' ]; then
  if [ "$#" -ne 2 ] || [ -z "$2" ]; then
    echo 'Usage: ./Flash.sh --extract-spi-bootstrap NEW_FILE' >&2
    exit 2
  fi
  exec python3 ./tools/flash_contract.py package --directory . --extract-spi-bootstrap="$2"
fi
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo 'Usage: ./Flash.sh SERIAL [auto|512|4096]' >&2
  echo '       ./Flash.sh --extract-spi-bootstrap NEW_FILE (offline, for a legacy receiver)' >&2
  echo 'Installs Debian by writing the GPT and the boot/slot-a/slot-b partitions.' >&2
  echo 'The data partition is left untouched for first-boot provisioning.' >&2
  exit 2
fi
serial=$1
case "$serial" in ''|*[!A-Za-z0-9._-]*) echo 'Invalid target serial' >&2; exit 2;; esac
sector=${2:-auto}
case "$sector" in auto|512|4096) ;; *) echo 'Invalid sector constraint' >&2; exit 2;; esac
command -v python3 >/dev/null
command -v fastboot >/dev/null
python3 ./tools/flash_contract.py package --directory .
mkdir -p logs
log=$(mktemp -d "$PWD/logs/flash-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
invoke() {
  name=$1
  shift
  if ! python3 ./tools/flash_contract.py run --log "$log/$name.log" -- fastboot -s "$serial" "$@"; then
    echo "Flash stopped; original logs: $log" >&2
    exit 1
  fi
}
invoke serial getvar serialno
python3 ./tools/flash_contract.py serial --log "$log/serial.log" --expected "$serial"
target=$(python3 ./tools/flash_contract.py target --manifest tools/package.json)
if [ "$target" != legacy ]; then
  invoke board getvar debian-board
  python3 ./tools/flash_contract.py board --manifest tools/package.json --log "$log/board.log"
fi
invoke protocol getvar debian-flash-version
invoke disk getvar debian-disk-info
detected=$(python3 ./tools/flash_contract.py prepare --version-log "$log/protocol.log" --disk-log "$log/disk.log" \
  --image images/rootfs.img --sector "$sector" --output "$log/layout.json")
case "$detected" in 512) gpt=images/gpt-512.img; boot=images/boot-512.img;;
  4096) gpt=images/gpt-4096.img; boot=images/boot-4096.img;;
  *) echo 'Unsupported system NVMe geometry' >&2; exit 1;; esac
echo 'Writing the GPT and the boot/slot-a/slot-b partitions; data is provisioned on first boot.'
invoke gpt -S 64M flash gpt "$gpt"
invoke gpt-ready getvar debian-gpt-ready
python3 ./tools/flash_contract.py ready --log "$log/gpt-ready.log" --name debian-gpt-ready
for partition in boot slot-a slot-b data; do
  invoke "$partition-size" getvar "partition-size:$partition"
  python3 ./tools/flash_contract.py verify --layout "$log/layout.json" --partition "$partition" --log "$log/$partition-size.log"
done
invoke boot -S 64M flash boot "$boot"
invoke slot-a -S 64M flash slot-a images/rootfs.img
invoke slot-b -S 64M flash slot-b images/rootfs.img
invoke slot-a-final getvar partition-size:slot-a
python3 ./tools/flash_contract.py verify --layout "$log/layout.json" --partition slot-a --log "$log/slot-a-final.log"
echo "Debian written to both slots. First boot provisions the data partition, clears stale signatures, grows the slot filesystem, then starts normal services. Logs: $log"
echo 'For the board SPI update, follow README.txt. Reboot explicitly when ready.'
