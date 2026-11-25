# Package and install a system

Choose the release for the physical board, `Y4` or `Y12`. Check its version, board and published checksums before installing it. To create the artifacts, start with the [build guide](DUAL-BOARD-BUILD.md).

| Deliverable | Use it for |
| --- | --- |
| `<BOARD>-<VERSION>-full.zip` | Set up or reinstall the system disk through USB Fastboot. It also carries the matching SPI firmware for a separate board firmware update. |
| `<BOARD>-<VERSION>-ota.zip` | Upgrade a running system to a newer release. Extract the signed `.y4pkg` for upload. |

## Full Fastboot package

The full ZIP contains `images/myt-spi.bin`, the system image, boot and GPT images for 512- and 4096-byte sectors, host flash scripts and checksums. Back up the system disk before reflashing. A standard reinstall writes the GPT, boot partition and both system slots while preserving the data-partition payload. Preserving access to existing data requires a compatible partition layout and filesystem.

### Verify the package and select the device

On Linux, install Fastboot and Python 3 on the flash host. The Windows package includes Fastboot and uses Windows PowerShell for the flash script. The board serial console uses 57600 baud.

1. Extract the ZIP and read its `README.txt`. On Linux, run `sha256sum -c SHA256SUMS` in the extracted directory. On Windows, run `Flash.cmd -VerifyOnly` from Command Prompt in the same directory.
2. Connect the board to the host's USB Fastboot connection and enter Fastboot. On a running MYT system, an administrator can use `sudo systemctl reboot bootloader`.
3. Run `fastboot devices` and replace `SERIAL` in the commands below with the exact serial for the intended board. On Windows, use the included `.\tools\fastboot.exe` in place of `fastboot`.

```sh
fastboot devices
fastboot -s SERIAL getvar debian-board
fastboot -s SERIAL getvar debian-flash-version
```

The board must match the package and the receiver must support the system-disk protocol required by the package, currently protocol 3.

### Update SPI when required

Use the matching SPI BIN to update DDR, CSU PM and UEFI. If the device already has compatible SPI firmware and only needs the operating system reinstalled, continue to the system install step.

```sh
fastboot -s SERIAL getvar myt-spi-version
fastboot -s SERIAL getvar myt-spi-layout
```

If the receiver reports version 1 and a compatible layout covering DDR, PM and UEFI, use the single combined write.

```sh
fastboot -s SERIAL flash myt-spi images/myt-spi.bin && fastboot -s SERIAL reboot
```

For an older receiver that lacks the combined-write commands, use the migration procedure in that release's `README.txt`. If the board, receiver or SPI layout cannot be confirmed, resolve it before writing. Check the release instructions for first-use firmware provisioning on new boards. If a system install is also required, re-enter Fastboot after the SPI update.

### Install the system disk

On Linux, run from the extracted package directory.

```sh
sh Flash.sh SERIAL auto && fastboot -s SERIAL reboot
```

On Windows, run from Command Prompt in the extracted package directory.

```bat
Flash.cmd -SerialNumber SERIAL && .\tools\fastboot.exe -s SERIAL reboot
```

Run the reboot command after the script reports success. The scripts verify the selected device and disk geometry before writing. If they stop, keep the files under the extracted package’s `logs/` directory and correct the reported problem before retrying. First boot provisions or mounts `/data`, expands the active root filesystem and runs the board initialization service.

### Quick erase and fresh data

Quick erase removes the selected system NVMe partition table and clears the boot, system-slot and `/data` filesystem headers before reinstalling. Back up any data you need first. A standard install preserves an existing data partition with a compatible layout and filesystem.

Confirm the board and serial as above. Query the active Fastboot receiver before choosing quick erase.

```sh
fastboot -s SERIAL getvar debian-quick-erase-version
```

The value must be `1`. If an older receiver does not support this query, use matching SPI firmware that provides quick erase, reboot, re-enter Fastboot and check the capability again.

Choose one of the following ways to erase and install.

- On Windows, run `Flash.cmd -SerialNumber SERIAL -QuickErase` from Command Prompt.
- On Linux, run `sh Flash.sh SERIAL auto --quick-erase`.
- To erase separately, run `fastboot -s SERIAL erase debian-system-quick`. After it succeeds, use the standard install command above without the quick-erase flag.

The opt-in install flag runs the same native erase command before writing the new GPT, boot partition and both system slots. After installation, first boot creates a fresh `/data` filesystem. The quick erase clears and verifies only the first and last 1 MiB of the disk and the first 1 MiB of each of the boot, slot A, slot B and data partitions. Other sectors retain their contents, so this is not a secure erase.

The native erase step took 0.016 s on a physical Y4 with a 1 TB NVMe drive and 512-byte sectors. This measures erase only, excluding installation and first boot. Y12 was verified offline.

### Reset only the data partition from Debian

Use this procedure when Debian is already running and only `/data` needs to be reset. It deletes the persistent user files, container data and application state on that partition. Back up the data first. As root, check the mounted device and filesystem type.

```sh
findmnt -n -o SOURCE,FSTYPE,TARGET --mountpoint /data
```

On the intended device, remove the initialization stamp and reboot. The early boot service recreates the data filesystem before applications start.

```sh
mountpoint -q /data &&
rm -- /data/.y4-data-stamp &&
sync -f /data &&
systemctl reboot
```

After boot, run `journalctl -b -u y4-data-provision.service --no-pager` and `findmnt --mountpoint /data` to confirm the fresh filesystem and mount.

## System OTA

The standard release uses schema 2. The package contains the root filesystem and its matching kernel Image and board DTB. Kernel modules and the approved MCU package are part of the root filesystem. SPI firmware is updated through Fastboot.

### Prepare the release archive

Run from the assembled build tree or its matching `myt-edge` checkout. Set `BOARD` and `VERSION` to the values used for the system build and `OUTPUT` to its build directory. Set `OTA_SIGNING_KEY` to a protected signing key trusted by the target devices. Use the official signing process for factory devices.

```sh
KERNEL_OBJECTS="$OUTPUT/kernel/objects"
KERNEL_RELEASE=$(cat "$KERNEL_OBJECTS/include/config/kernel.release")
case "$BOARD" in
  Y4) DTB=sky1-y4.dtb ;;
  Y12) DTB=sky1-y12.dtb ;;
  *) printf '%s\n' "Choose Y4 or Y12" >&2; exit 1 ;;
esac
python3 scripts/ota_package.py --board "$BOARD" --schema 2 \
  --version "$VERSION" --kernel "$KERNEL_RELEASE" \
  --image "$OUTPUT/package/rootfs_sparse.ext4" \
  --kernel-image "$KERNEL_OBJECTS/arch/arm64/boot/Image" \
  --kernel-dtb "$KERNEL_OBJECTS/arch/arm64/boot/dts/cix/$DTB" \
  --key "$OTA_SIGNING_KEY" --delivery-dir "$OUTPUT/delivery" \
  --output "$OUTPUT/$BOARD-$VERSION.y4pkg"
```

Use the same release version for the full Fastboot ZIP and OTA ZIP. The target must run a compatible A/B boot layout, accept the board, see a newer version and trust the OTA signature. For the legacy single-kernel layout, use the matching full Fastboot package to migrate first.

### Apply the update

After board initialization completes, extract the OTA ZIP, verify `SHA256SUMS` and upload only the `.y4pkg` via the management panel's system update page. From a privileged terminal on the device, set `OTA_PACKAGE` to the absolute path of the extracted file and use the local API.

```sh
sudo curl --fail-with-body --unix-socket /run/y4-board/api.sock \
  -X POST -H 'Content-Type: application/octet-stream' -H 'Expect:' \
  --upload-file "$OTA_PACKAGE" http://localhost/api/v1/update
```

HTTP 202 means the device accepted the request. Wait for it to write and validate the inactive slot and reboot. Keep the device powered during this process.

## Check the running release

After either installation, use the panel, serial console or SSH to check the version, active slot, data mount and service state. The `version` API includes slot confirmation.

```sh
cat /etc/y4-release.json
findmnt /
findmnt /data
sudo curl --fail-with-body --unix-socket /run/y4-board/api.sock \
  http://localhost/api/v1/version
systemctl --failed
```

If a service fails after boot, capture the current boot journal for `y4-board-init` and `y4-boardd` together with the board, package version and serial output. For a release acceptance record, follow the [development guide](DEVELOPMENT-HANDOFF.md).
