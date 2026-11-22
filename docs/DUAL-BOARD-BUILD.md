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
| Kernel source and configuration, or a matching kernel bundle | The kernel `Image`, selected device tree and modules in the system image and OTA |
| Rootfs source checkout | Debian package versions, overlay files, CIX runtime binaries and QtScrcpy |
| Build dependencies and ARM64 sysroot | Host-side compilation of the board service and system staging |
| ARM64 GRUB `BOOTAA64.EFI` | The system disk's EFI system partition. Choose the build that matches the pinned GRUB checksum |
| Official MCU assets and manifest digest | Board-specific signed MCU payload and existing maintenance program staged into rootfs |
| Official desktop panel program and manifest | ARM64 panel program staged into rootfs |
| Official `myt-spi.bin` for the selected board | Optional. Added to a full Fastboot ZIP |
| Windows Fastboot tools | Windows flashing support in the full package |
| External OTA signing key | OTA signing. Its corresponding public key must already be trusted on the installation target |

The release operator obtains each board-specific binary through an approved binary channel and checks the digest from the same release metadata. Keep local config files and signing keys outside the Git checkout. The public build omits Realtek PG. The board service checks the NICs and their permanent MAC addresses before enabling networking, so factory NIC provisioning must already be complete.

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

Set `BOARD` to `Y4` or `Y12`, and use the same value for all the binary inputs. Set `VERSION` to the release identifier in `YYYYMMDD-rNN` form.

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

Append `--spi-bin "$SPI_BIN"` to the second command to produce the full Fastboot ZIP in `$OUTPUT/delivery`. Use the [flash guide](BUILD-AND-FLASH-GUIDE.md) for validation and installation.

## Maintainer build for private components

An authorized build host can compile the private components and consume their outputs in the same system build. Supply a local configuration with pinned repository commits and a fresh component output directory.

```sh
python3 scripts/build.py all --config "$LOCAL_CONFIG" --board "$BOARD" \
  --version "$VERSION" --output "$OUTPUT" --private-components "$PRIVATE_CONFIG" \
  --component-output "$COMPONENT_OUTPUT" --jobs 16
```

The build uses the generated SPI, MCU and panel artifacts. The component receipt records the source commits and output hashes. To build the component inputs separately, use the same configuration and board with `scripts/private_components.py`.

| Configuration entry | Required settings |
| --- | --- |
| `uefi` | `repo`, `commit`, `sdk`. Set `toolchain` when the SDK requires an external toolchain |
| `mcu` | `repo`, `commit`, `maintainer_repo`, `maintainer_commit`, `sysroot`, `crypto_prefix`, and `signing` |
| `mcu.signing` | `private_key`, `public_key`, `sequence`, `host_version`. Set `host_min` and `host_max` to specify a compatible host version range |
| `panel` | `repo`, `commit`, `version` |
| `tool_paths` | Optional array of local tool directories to add to `PATH` |

Keep this configuration, private source locations and signing keys in your authorized build environment. After verification, publish approved binaries and their pinned hashes through your release channel.
