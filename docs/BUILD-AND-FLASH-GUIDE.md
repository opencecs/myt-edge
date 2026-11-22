# Package and install a system

See the [build guide](DUAL-BOARD-BUILD.md) to assemble the sources and create a build. Every install targets one physical board. Check the board, release metadata and file hashes before installing. Back up the target disk before a full Fastboot install.

## Full Fastboot package

Build with the official SPI BIN for the selected board to produce `<BOARD>-<VERSION>-full.zip`. The ZIP contains `images/myt-spi.bin`, disk layout and boot images for 512- and 4096-byte sectors, the root filesystem, flash scripts and checksums. Follow the generated README for the receiver supported by that release.

After extraction, validate the package with `sha256sum -c SHA256SUMS` on Linux or `Flash.cmd -VerifyOnly` on Windows. Use the exact serial reported by Fastboot for the intended board.

The optional SPI update uses `fastboot -s SERIAL flash myt-spi images/myt-spi.bin` with a compatible receiver. Query `debian-board`, `myt-spi-version` and `myt-spi-layout` using the same serial first. The receiver must report the expected board, bundle version 1 and a compatible layout covering DDR, PM and UEFI. Use the package README for compatible legacy-receiver migration and any first-use board requirements.

To install the system, enter Fastboot and run `sh Flash.sh SERIAL auto` on Linux or `Flash.cmd -SerialNumber SERIAL` on Windows. The install writes GPT, the boot partition and both system slots. Data preservation depends on the existing disk having the compatible layout. Reboot after successful installation.

## System OTA

Use the selected board's build output and matching kernel `Image` and DTB. Schema 2 packages the root filesystem with its matching kernel and device tree. Use the board, version and kernel release recorded by that build.

```sh
python3 scripts/ota_package.py --board "$BOARD" --schema 2 \
  --version "$VERSION" --kernel "$KERNEL_RELEASE" \
  --image "$OUTPUT/package/rootfs_sparse.ext4" \
  --kernel-image "$KERNEL_IMAGE" --kernel-dtb "$KERNEL_DTB" \
  --key "$OTA_SIGNING_KEY" --delivery-dir "$OUTPUT/delivery" \
  --output "$OUTPUT/$BOARD-$VERSION.y4pkg"
```

Keep the signing key outside the source tree. Use the official signing process for OTA packages destined for factory systems. The target accepts a matching board, a newer version and a signature under its configured OTA public key.

Extract `<BOARD>-<VERSION>-ota.zip`, verify its checksums and upload the `.y4pkg` through the system upgrade interface after board initialization completes. The device writes the matched root filesystem and kernel to the inactive slot and reboots. Check the running version and confirmed slot after reboot.

Update DDR, PM or UEFI through the matching full Fastboot package when needed. The system image carries the signed MCU package for the board initialization service.
