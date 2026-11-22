# MYT Edge

Build and package Debian 12 for MYT Y4 and Y12. Both boards use the same public build tools. Select the physical board with `--board Y4` or `--board Y12`.

| Repository | Contents |
| --- | --- |
| [myt-edge](https://github.com/opencecs/myt-edge) | Build orchestration, board services, image packaging, OTA packaging and Fastboot tools |
| [cix-kernel](https://github.com/opencecs/cix-kernel) | Linux kernel, Y4 and Y12 device trees, and kernel configuration |
| [cix-rootfs](https://github.com/opencecs/cix-rootfs) | Debian package inputs, desktop configuration, root filesystem overlays and vendor runtime files |

Start with the [dual-board build guide](docs/DUAL-BOARD-BUILD.md). A source checkout also needs the build dependencies and board-specific binaries listed in that guide. Obtain official binaries and their checksums from the maintainer's release channel.

- Build a system image using the official MCU assets and desktop panel binary.
- Add the matching official `myt-spi.bin` to generate a full Fastboot ZIP.
- Generate an OTA from the system image and matching kernel files. Installation requires a signature trusted by the target system.

On factory-provisioned hardware, the public build can omit the Realtek programmer. The board service checks the installed network devices and their permanent MAC addresses before enabling networking. NIC NVM provisioning is a separate manufacturing operation.

See the [build and flash guide](docs/BUILD-AND-FLASH-GUIDE.md), [board differences](docs/Y4-Y12-DIFFERENCES.md) and [board API](docs/board-api.md).
