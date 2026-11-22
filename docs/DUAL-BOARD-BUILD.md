# Build Debian for Y4 or Y12

Use a Linux build host with enough space for the kernel, the root filesystem staging tree and the disk images. The build preflight checks the host tools and supplied inputs. The root filesystem build needs privileges for its Linux filesystem preparation steps.

## Obtain the inputs

Use the three GitHub `main` branches for both public and maintainer builds. Run the initial checkouts on Linux. The kernel source has file names such as `aux.c` that Windows treats as reserved.

```sh
git clone --branch main https://github.com/opencecs/myt-edge.git
git clone --branch main https://github.com/opencecs/cix-kernel.git
git clone --branch main https://github.com/opencecs/cix-rootfs.git
```

Pin the three source revisions as full commits for each build. The assembly command records them in `SOURCES.json` and creates a fresh tree. Existing local source trees are kept for historical reference.

| Input | Where it is used |
| --- | --- |
| Qualified Linux 6.6.89 kernel bundle for the selected board | The kernel `Image`, device tree and modules in the system image and OTA |
| Rootfs source checkout | Debian package versions, overlay files, CIX runtime binaries and QtScrcpy |
| Build dependencies and ARM64 sysroot | Host-side compilation of the board service and system staging |
| ARM64 GRUB `BOOTAA64.EFI` | The system disk's EFI system partition. Choose the build that matches the pinned GRUB checksum |
| Official MCU assets and manifest digest | Board-specific signed MCU payload and existing maintenance program staged into rootfs |
| Official desktop panel program and manifest | ARM64 panel program staged into rootfs |
| Official `myt-spi.bin` for the selected board | Required for a full Fastboot ZIP. It combines DDR, PM and UEFI |
| Windows Fastboot tools | Windows flashing support in the full package |
| External OTA signing key | Separate OTA signing step. The installation target must already trust its public key |

Obtain the board-specific binaries and verify them against the hashes provided with the release. Set up the paths for the approved inputs in a local config outside the source tree. Production MCU packages are signed internally. The public build validates the supplied package against the production MCU public key and checks its board, compatibility and manifest hashes before packaging it.

The public build omits Realtek PG and uses read-only NIC inspection. Factory provisioning must already have configured at least one usable port. The network gate releases the valid ports after initialization confirms that no programming operation remains pending.

## Assemble the source tree

Run this from the pinned `myt-edge` checkout. Use existing paths for the input checkouts and a fresh path for the assembled tree and build output.

```sh
python3 scripts/assemble_sources.py \
  --kernel "$KERNEL_CHECKOUT" --kernel-ref "$KERNEL_COMMIT" \
  --rootfs "$ROOTFS_CHECKOUT" --rootfs-ref "$ROOTFS_COMMIT" \
  --grub "$GRUB_EFI" --output "$ASSEMBLED"
cd "$ASSEMBLED"
```

Use `scripts/build.example.json` as the starting point for your local config. Supply the build input paths for both boards you plan to build. Supply the matching Linux 6.6.89 kernel bundle for the selected board. Follow the [kernel build instructions](https://github.com/opencecs/cix-kernel#构建当前内核包) to generate this input.

## Build and package

Set `BOARD` to `Y4` or `Y12`, and use the same value for all the binary inputs. For a formal release, pass the assigned version explicitly as `VERSION` in `YYYYMMDD-rNN` form. Use that same board and version for both Fastboot and OTA packaging.

```sh
python3 scripts/build.py check --config "$LOCAL_CONFIG" --board "$BOARD" \
  --version "$VERSION" --mcu-assets "$MCU_ASSETS" \
  --mcu-manifest-sha256 "$MCU_MANIFEST_SHA256" \
  --panel-binary "$PANEL_BINARY" --panel-manifest "$PANEL_MANIFEST"

python3 scripts/build.py all --config "$LOCAL_CONFIG" --board "$BOARD" \
  --version "$VERSION" --output "$OUTPUT" --mcu-assets "$MCU_ASSETS" \
  --mcu-manifest-sha256 "$MCU_MANIFEST_SHA256" \
  --panel-binary "$PANEL_BINARY" --panel-manifest "$PANEL_MANIFEST"
```

Append `--spi-bin "$SPI_BIN"` to the build command to produce the full Fastboot ZIP in `$OUTPUT/delivery`. With the same build output, run the separate OTA packaging and signing command in the [flash guide](BUILD-AND-FLASH-GUIDE.md). The guide also covers verifying and installing both deliverables.

## Maintainer build for private components

An authorized build host can compile the private components and consume their outputs in the same system build. Supply a local configuration with pinned repository commits and a fresh component output directory.

```sh
python3 scripts/build.py all --config "$LOCAL_CONFIG" --board "$BOARD" \
  --version "$VERSION" --output "$OUTPUT" --private-components "$PRIVATE_CONFIG" \
  --component-output "$COMPONENT_OUTPUT" --jobs 16
```

The internal release configuration supplies the pinned private sources and the MCU signing environment. The build uses the generated SPI, signed MCU assets and panel binary. The component receipt records the source commits and output hashes. Keep internal configuration and private signing keys in the authorized build environment. Share approved binary artifacts and checksum manifests through the release channel. The system OTA uses a separate signing step after the system build.
