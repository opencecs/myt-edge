# Build Debian for Y4 or Y12

Build from the three public source repositories on Linux. For a formal release, fix the source commits, select the physical board and use one release version for the Fastboot and OTA deliverables. See the [platform overview](PLATFORM-OVERVIEW.md) for supported hardware and the [development guide](DEVELOPMENT-HANDOFF.md) for making changes.

## Prepare the host

Use a Linux host with Python 3.11 or later, Git and sufficient space for the source checkout, package cache, root filesystem staging directory and disk images. The full build needs privileges for mounting filesystems and creating a Debian ARM64 userspace. On an x86-64 host, set up ARM64 emulation with `qemu-aarch64-static` and binfmt.

Install the host build tools before running preflight. These include `make`, `gcc`, an ARM64 cross-compiler, `pkg-config`, `curl`, `debootstrap`, `dpkg-deb`, `grub-file`, `grub-script-check`, `depmod`, `fdtget`, `zstd`, `pigz`, ext4 and FAT utilities, `mtools` and Android sparse image tools. `build.sh check` reports missing tools or mismatched inputs.

## Obtain the inputs

Check out the repositories under one directory. Run the checkouts on Linux because the kernel tree contains names that Windows reserves.

```sh
git clone --branch main https://github.com/opencecs/myt-edge.git
git clone --branch main https://github.com/opencecs/cix-kernel.git
git clone --branch main https://github.com/opencecs/cix-rootfs.git
```

Use the source commits and binary checksums recorded for the intended release. Download the board-specific binary inputs from [MYT Edge Releases](https://github.com/opencecs/myt-edge/releases).

| Input | Required content |
| --- | --- |
| Kernel bundle | Qualified Linux 6.6.89 `Image`, board DTB, modules archive, configuration and checksums. Match the [Y4 pin](https://github.com/opencecs/cix-rootfs/blob/main/fetch/inputs-kernel.json) or [Y12 pin](https://github.com/opencecs/cix-rootfs/blob/main/fetch/inputs-kernel-y12.json). See the [kernel rebuild instructions](https://github.com/opencecs/cix-kernel#构建当前内核包) when changing the kernel. |
| Build dependencies | ARM64 `cix-sysroot` and `kernel-toolchain-12.3`, including `bin/aarch64-none-linux-gnu-gcc` under the toolchain directory. |
| GRUB | ARM64 `BOOTAA64.EFI` matching the digest in `boot/grub/BOOTAA64.EFI.sha256`. It goes on the boot partition of the system disk. |
| MCU assets | Signed firmware package, matching ARM64 maintenance programs and board manifest from the board archive. |
| Management panel | ARM64 panel binary and its matching JSON manifest. The standard image uses the LAN profile. |
| Combined SPI BIN | The board-specific official `myt-spi.bin`, which contains DDR, CSU PM and UEFI. Required for the full Fastboot ZIP. |
| Windows Fastboot tools | `fastboot.exe`, `AdbWinApi.dll` and `AdbWinUsbApi.dll` in the directory configured by `inputs.windows_tools`. |

For the Windows tools, download [Android Platform Tools 35.0.2 for Windows](https://dl.google.com/android/repository/platform-tools_r35.0.2-win.zip). Verify SHA256 `2975a3eac0b19182748d64195375ad056986561d994fffbdc64332a516300bb9` and set `inputs.windows_tools` to the extracted `platform-tools` directory.

On an x86-64 Linux host, download [Arm GNU Toolchain 12.3.Rel1 for AArch64 Linux](https://armkeil.blob.core.windows.net/developer/Files/downloads/gnu/12.3.rel1/binrel/arm-gnu-toolchain-12.3.rel1-x86_64-aarch64-none-linux-gnu.tar.xz). Verify SHA256 `960ec0bce309528f603639d8228ef39e6fb9185289ff42b01aa3b4de315accef` and unpack it as `kernel-toolchain-12.3` under `inputs.dependencies`.

Obtain the pinned GRUB EFI and qualified board kernel bundle separately from the release operator.

Prepare the ARM64 sysroot from pinned Debian 12 packages. From the `myt-edge` checkout, set `DEPENDENCIES` to the absolute directory you will use as `inputs.dependencies` and run:

```sh
python3 scripts/prepare_public_sysroot.py --output "$DEPENDENCIES"
```

The script uses `curl` and `dpkg-deb` to download, verify and extract the locked packages into `$DEPENDENCIES/cix-sysroot`. Use a fresh output directory if you already have a sysroot.

Debian packages are fetched according to the [root filesystem lock](https://github.com/opencecs/cix-rootfs/blob/main/fetch/rootfs-packages.json). The public build validates the board, signature and compatibility of the signed MCU assets. Factory provisioning must already have left at least one usable network port. The public image inspects NIC identity and permanent MAC addresses at boot. The public build omits the Realtek PG factory program.

### Download validated components for your board

The [component release](https://github.com/opencecs/myt-edge/releases/tag/build-components-20261123) provides the combined SPI BIN, signed MCU assets with their maintenance programs, and LAN management panel. Anyone can download these inputs without access to the private repositories. Maintainers publish these archives as release attachments outside the source checkouts. Choose the archive for your physical board and check the download before extracting it.

```sh
set -eu
BOARD=Y4    # Use Y12 for a Y12
case "$BOARD" in
  Y4|Y12) ;;
  *) printf '%s\n' 'BOARD must be Y4 or Y12' >&2; exit 1 ;;
esac
COMPONENT_TAG=build-components-20261123
RELEASE_URL="https://github.com/opencecs/myt-edge/releases/download/$COMPONENT_TAG"
ARCHIVE="$BOARD-build-components.tar.gz"
INPUT_DIR="$PWD/build-inputs/$COMPONENT_TAG/$BOARD"

mkdir -p "$(dirname "$INPUT_DIR")"
mkdir "$INPUT_DIR"
(
  cd "$INPUT_DIR"
  curl --fail --location --retry 3 -o "$ARCHIVE" "$RELEASE_URL/$ARCHIVE"
  curl --fail --location --retry 3 -o SHA256SUMS.txt "$RELEASE_URL/SHA256SUMS.txt"
  CHECKSUM=$(awk -v archive="$ARCHIVE" '
    NF == 2 && $2 == archive && length($1) == 64 && $1 ~ /^[[:xdigit:]]+$/ {
      count++
      digest = $1
    }
    END {
      if (count != 1) exit 1
      print digest "  " archive
    }
  ' SHA256SUMS.txt)
  printf '%s\n' "$CHECKSUM" | sha256sum --check --strict -
  mkdir components
  tar -xzf "$ARCHIVE" -C components
)

COMPONENTS="$INPUT_DIR/components"
MCU_ASSETS="$COMPONENTS/mcu"
MCU_MANIFEST_SHA256=$(sha256sum < "$MCU_ASSETS/manifest.json")
MCU_MANIFEST_SHA256=${MCU_MANIFEST_SHA256%% *}
PANEL_BINARY="$COMPONENTS/myt_desktop"
PANEL_MANIFEST="$COMPONENTS/panel.json"
SPI_BIN="$COMPONENTS/myt-spi.bin"
```

Keep these variables in the same shell for the build below. The component release is a set of build inputs. The full Fastboot ZIP is generated by the public build, and the system OTA is signed separately with a key trusted by the device.

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

Use the `BOARD` and component paths from the verified download. Set `VERSION` to the assigned `YYYYMMDD-rNN` release version and `OUTPUT` to an unused path outside the source trees.

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
