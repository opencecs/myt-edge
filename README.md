# MYT Edge

Build and package Debian 12 for MYT Y4 and Y12. Both boards use the same public build tools. Select the physical board with `--board Y4` or `--board Y12`.

| Repository | Contents |
| --- | --- |
| [myt-edge](https://github.com/opencecs/myt-edge) | Build orchestration, board services, image packaging, OTA packaging and Fastboot tools |
| [cix-kernel](https://github.com/opencecs/cix-kernel) | Linux kernel, Y4 and Y12 device trees, and kernel configuration |
| [cix-rootfs](https://github.com/opencecs/cix-rootfs) | Debian package inputs, desktop configuration, root filesystem overlays and vendor runtime files |

Both public and maintainer builds use the GitHub `main` branches listed above. Pin the three commits for each build. Keep previous local source trees only for historical reference.

Start with the [dual-board build guide](docs/DUAL-BOARD-BUILD.md). The public build uses approved signed MCU assets and desktop panel binaries together with the build dependencies listed there. Obtain the binaries and matching checksums from the maintainer.

- Add the matching official `myt-spi.bin` to generate a standard full Fastboot ZIP.
- Use the resulting system image and matching kernel files to prepare a system OTA. The device must trust the OTA signature.

The public system checks permanent MAC addresses before enabling usable factory-provisioned NICs. Manufacturing handles NIC NVM provisioning separately.

See the [build and flash guide](docs/BUILD-AND-FLASH-GUIDE.md), [board differences](docs/Y4-Y12-DIFFERENCES.md) and [board API](docs/board-api.md).

The maintainers compile the boot firmware, MCU and panel from private source repositories. MCU package signing takes place in the internal release environment. The public build verifies and packages the approved binary assets. See the [maintainer build procedure](docs/DUAL-BOARD-BUILD.md#maintainer-build-for-private-components) and [release evidence](RELEASES.md).
