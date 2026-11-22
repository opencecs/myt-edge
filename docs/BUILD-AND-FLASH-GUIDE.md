# Package and install a system

See the [build guide](DUAL-BOARD-BUILD.md) to assemble the sources and create a build. Every install targets one physical board. Check the board, release metadata and file hashes before installing. Back up the target disk before a full Fastboot install.

## Full Fastboot package

The build adds the board-specific official SPI BIN at `images/myt-spi.bin` when you pass `--spi-bin`. The full package also contains the disk layout, boot image and root filesystem. Follow the generated package README for the actual device and receiver version.

After extraction, validate the package with `sha256sum -c SHA256SUMS` on Linux or `Flash.cmd -VerifyOnly` on Windows. Use the exact serial reported by Fastboot for the intended board.

The optional SPI update uses `fastboot -s SERIAL flash myt-spi images/myt-spi.bin` with a compatible receiver. Query `debian-board`, `myt-spi-version` and `myt-spi-layout` using the same serial first. The receiver must report the expected board, bundle version 1 and a compatible layout covering DDR, PM and UEFI. Use the package README for compatible legacy-receiver migration and any first-use board requirements.

To install the system, enter Fastboot and run `sh Flash.sh SERIAL auto` on Linux or `Flash.cmd -SerialNumber SERIAL` on Windows. The install writes GPT, the boot partition and both system slots. Data preservation depends on the existing disk having the compatible layout. Reboot after successful installation.

## System OTA

Use the selected board's own build output and matching kernel `Image` and DTB. Schema 2 packages the system with its matching kernel. Obtain the exact version and kernel release from the build's release metadata.

```sh
python3 scripts/ota_package.py --board "$BOARD" --schema 2 \
  --version "$VERSION" --kernel "$KERNEL_RELEASE" \
  --image "$OUTPUT/package/rootfs_sparse.ext4" \
  --kernel-image "$KERNEL_IMAGE" --kernel-dtb "$KERNEL_DTB" \
  --key "$OTA_SIGNING_KEY" --delivery-dir "$OUTPUT/delivery" \
  --output "$OUTPUT/system.y4pkg"
```

Keep the signing key outside the source tree. A package accepted by the builder is installable only when the target trusts the signing key and accepts the version and board. Extract the delivery ZIP, verify the hashes, and use the `.y4pkg` with the system upgrade interface. Update SPI separately with the matching full package when required.
