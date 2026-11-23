# Release evidence

Use the same explicit `YYYYMMDD-rNN` version for the full Fastboot and system OTA packages for a given board. Publish only approved release artifacts after verifying the exact packages. Keep test packages in the validation environment.

| Record | Required information |
| --- | --- |
| Sources | Board, release version and pinned `myt-edge`, `cix-kernel` and `cix-rootfs` commits |
| Binary inputs | Kernel bundle, signed MCU assets and manifest, panel binary and manifest, SPI BIN for the selected board, with checksums |
| Deliverables | `<BOARD>-<VERSION>-full.zip` and `<BOARD>-<VERSION>-ota.zip`, with SHA256 checksums |
| Validation | Build, package and device checks performed on this board and version, with results for the delivered artifacts |

Public builds use the boot firmware, internally signed MCU packages and shared panel pinned in [`prebuilt/`](prebuilt/) at the selected `myt-edge` commit. The [maintainer sync](docs/DUAL-BOARD-BUILD.md#refresh-the-public-build-inputs) updates component binaries after offline verification of an internal full build. Record hardware acceptance separately when releasing a system. System OTA signing must use a key trusted by the target. See the [build and flash guide](docs/BUILD-AND-FLASH-GUIDE.md) for package validation and installation.
