# Build Debian for Y4 or Y12

Build from the three public source repositories on Linux. For a formal release, fix the source commits, select the physical board and use one release version for the Fastboot and OTA deliverables. See the [platform overview](PLATFORM-OVERVIEW.md) for supported hardware and the [development guide](DEVELOPMENT-HANDOFF.md) for making changes.

## Prepare the host

Use a Linux host with Python 3, Git and sufficient space for the source checkout, package cache, root filesystem staging directory and disk images. The full build needs privileges for mounting filesystems and creating a Debian ARM64 userspace. On an x86-64 host, set up ARM64 emulation with `qemu-aarch64-static` and binfmt.

Install the host build tools before running preflight. These include `make`, `gcc`, an ARM64 cross-compiler, `debootstrap`, `dpkg-deb`, `grub-file`, `grub-script-check`, `depmod`, `fdtget`, `zstd`, `pigz`, ext4 and FAT utilities, `mtools` and Android sparse image tools. `build.sh check` reports missing tools or mismatched inputs.

## Obtain the inputs

Check out the repositories under one directory. Run the checkouts on Linux because the kernel tree contains names that Windows reserves.

```sh
git clone --branch main https://github.com/opencecs/myt-edge.git
git clone --branch main https://github.com/opencecs/cix-kernel.git
git clone --branch main https://github.com/opencecs/cix-rootfs.git
```

Use the source commits and binary checksums recorded for the intended release. Get the approved binary inputs and their manifests from the release operator.

| Input | Required content |
| --- | --- |
| Kernel bundle | Qualified Linux 6.6.89 `Image`, board DTB, modules archive, configuration and checksums. Match the [Y4 pin](https://github.com/opencecs/cix-rootfs/blob/main/fetch/inputs-kernel.json) or [Y12 pin](https://github.com/opencecs/cix-rootfs/blob/main/fetch/inputs-kernel-y12.json). See the [kernel rebuild instructions](https://github.com/opencecs/cix-kernel#构建当前内核包) when changing the kernel. |
| Build dependencies | ARM64 `cix-sysroot` and `kernel-toolchain-12.3`, including `bin/aarch64-none-linux-gnu-gcc` under the toolchain directory. |
| GRUB | ARM64 `BOOTAA64.EFI` matching the digest in `boot/grub/BOOTAA64.EFI.sha256`. It goes on the boot partition of the system disk. |
| MCU assets | Signed firmware package, matching ARM64 maintenance programs and board manifest. Use the manifest SHA256 supplied by the release operator. |
| Management panel | ARM64 panel binary and its matching JSON manifest. The standard image uses the LAN profile. |
| Combined SPI BIN | The board-specific official `myt-spi.bin`, which contains DDR, CSU PM and UEFI. Required for the full Fastboot ZIP. |
| Windows Fastboot tools | `fastboot.exe`, `AdbWinApi.dll` and `AdbWinUsbApi.dll` in the directory configured by `inputs.windows_tools`. |

Debian packages are fetched according to the [root filesystem lock](https://github.com/opencecs/cix-rootfs/blob/main/fetch/rootfs-packages.json). The public build consumes the internally signed MCU assets and validates their board, signature and compatibility. Factory provisioning must already have left at least one usable network port. The public image inspects NIC identity and permanent MAC addresses at boot.

## Assemble the source tree

Run this from the `myt-edge` checkout. Set `KERNEL_CHECKOUT` and `ROOTFS_CHECKOUT` to the other two clone paths, the three `*_COMMIT` variables to full Git commit IDs, and `GRUB_EFI` to the approved EFI file. Set `ASSEMBLED` to a fresh path outside the three checkouts.

```sh
python3 scripts/assemble_sources.py \
  --edge-ref "$EDGE_COMMIT" \
  --kernel "$KERNEL_CHECKOUT" --kernel-ref "$KERNEL_COMMIT" \
  --rootfs "$ROOTFS_CHECKOUT" --rootfs-ref "$ROOTFS_COMMIT" \
  --grub "$GRUB_EFI" --output "$ASSEMBLED"
cd "$ASSEMBLED"
```

The resulting `SOURCES.json` records the source commits. Make a local copy of [scripts/build.example.json](../scripts/build.example.json) outside the assembled tree and point `LOCAL_CONFIG` to it. Fill in the dependency and Windows tools directories, the absolute path to `kernel/debian.config` in the assembled tree, and the qualified kernel bundle paths under `boards.Y4.inputs.kernel_bundle` and `boards.Y12.inputs.kernel_bundle` as needed.

## Build a public system

Choose `BOARD` as `Y4` or `Y12`. Set `VERSION` to the assigned `YYYYMMDD-rNN` release version and `OUTPUT` to an unused path outside the source trees. Set `MCU_ASSETS`, `MCU_MANIFEST_SHA256`, `PANEL_BINARY`, `PANEL_MANIFEST` and `SPI_BIN` from the release inputs for that board.

```sh
set -- --config "$LOCAL_CONFIG" --board "$BOARD" --version "$VERSION" \
  --output "$OUTPUT" --mcu-assets "$MCU_ASSETS" \
  --mcu-manifest-sha256 "$MCU_MANIFEST_SHA256" \
  --panel-binary "$PANEL_BINARY" --panel-manifest "$PANEL_MANIFEST" \
  --spi-bin "$SPI_BIN"
bash build.sh check "$@" && bash build.sh all "$@"
```

For a system image or OTA build on hardware with compatible SPI firmware, omit `--spi-bin "$SPI_BIN"` from the argument list. The build writes the system artifacts to `$OUTPUT/package`. Supplying SPI also produces `$OUTPUT/delivery/<BOARD>-<VERSION>-full.zip`. Generate the system OTA separately from that same build with the [OTA signing command](BUILD-AND-FLASH-GUIDE.md#system-ota).

## Maintainer build for private components

Use the same assembled public source tree and board configuration. `PRIVATE_CONFIG` is a protected local file that pins the private source commits and provides the external toolchain and MCU signing configuration. `COMPONENT_OUTPUT` is an unused directory.

```sh
bash build.sh all --config "$LOCAL_CONFIG" --board "$BOARD" \
  --version "$VERSION" --output "$OUTPUT" --private-components "$PRIVATE_CONFIG" \
  --component-output "$COMPONENT_OUTPUT"
```

This builds the SPI BIN, signs the MCU package and produces the panel binary before assembling the system. The private component receipt records source revisions and output hashes. The system OTA still has a separate signing step. Production signing keys stay in the internal signing environment.

## Verify release outputs

```sh
python3 scripts/verify_release.py --build "$OUTPUT"
```

This checks the staged files, full package when present, disk geometry and image integrity offline. Record the three source commits, input and output checksums, board and assigned version. For device validation, follow the [flash guide](BUILD-AND-FLASH-GUIDE.md). Publish the approved archives with their checksums and [release evidence](../RELEASES.md).
