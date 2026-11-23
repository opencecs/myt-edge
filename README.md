# MYT Edge

MYT Edge builds a Debian 12 ARM64 system for the MYT Y4 and Y12 appliances based on CIX CP6187 (Sky1). It provides a host for networked NVMe storage, containerized services and PCIe accelerator workloads. The system includes a Debian desktop, board monitoring and an A/B system update mechanism.

## Supported hardware

| Board | Downstream PCIe slots | Cooling and monitoring | Build option |
| --- | --- | --- | --- |
| MYT Y4 (V11) | Four PCIe Gen4 ×4 slots | On-board status display, three MCU-controlled chassis fans and an independent SoC fan | `--board Y4` |
| MYT Y12 | Four PCIe Gen4 ×2 slots and eight Gen4 ×1 slots | Two SoC fan-control groups, six tachometer inputs and per-slot MCU power readings | `--board Y12` |

Both boards provide a 2.5GbE LAN port and two 10GbE optical ports. See the [platform overview](docs/PLATFORM-OVERVIEW.md) for hardware and workload requirements.

## Documentation

| Guide | Covers |
| --- | --- |
| [Build guide](docs/DUAL-BOARD-BUILD.md) | Inputs, source assembly and release builds for Y4 and Y12 |
| [Development guide](docs/DEVELOPMENT-HANDOFF.md) | Where to make changes and how to validate them |
| [Fastboot and OTA guide](docs/BUILD-AND-FLASH-GUIDE.md) | Package verification, installation, and system updates |
| [Board differences](docs/Y4-Y12-DIFFERENCES.md) and [board API](docs/board-api.md) | Board selection, slot mapping and management interfaces |
| [Release requirements](RELEASES.md) | Versioning, checksums and release validation records |

## Source repositories

| Repository | Contents |
| --- | --- |
| [myt-edge](https://github.com/opencecs/myt-edge) | Build orchestration, board services, packaging and Fastboot tools |
| [cix-kernel](https://github.com/opencecs/cix-kernel) | Linux kernel, Y4 and Y12 device trees, and kernel configuration |
| [cix-rootfs](https://github.com/opencecs/cix-rootfs) | Debian package inputs, desktop configuration, root filesystem overlays and vendor runtime files |

Both public and maintainer builds pin commits from the same three GitHub repositories. The [public build](docs/DUAL-BOARD-BUILD.md#build-a-public-system) selects the SPI firmware and signed MCU assets for `--board Y4` or `--board Y12` from [`prebuilt/`](prebuilt/) and includes the shared LAN panel. Prepare a system OTA separately with a signing key that the target device trusts.

On the public image, the board service checks permanent MAC addresses before allowing usable factory-provisioned NICs onto the network. Manufacturing handles NIC NVM provisioning separately. Boot firmware, MCU software and the panel are compiled by the maintainers, who sign MCU packages in their internal release environment.
