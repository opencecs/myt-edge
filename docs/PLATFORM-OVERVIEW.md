# MYT Edge platform

MYT Edge provides Debian 12 ARM64 for MYT Y4 and Y12 systems based on the CIX CP6187 (Sky1) SoC. The public projects maintain the Linux kernel and device trees, Debian filesystem and software inputs, board management services, and release tools.

## Hardware

| Feature | MYT Y4 (V11) | MYT Y12 |
| --- | --- | --- |
| Build target and Linux device tree | `Y4`, `sky1-y4.dtb` | `Y12`, `sky1-y12.dtb` |
| Downstream PCIe expansion | Four Gen4 ×4 slots | Four Gen4 ×2 slots and eight Gen4 ×1 slots |
| Cooling | MCU controls chassis FAN3–FAN5, shown as fans 1–3. SoC cooling uses its own FAN1 control | SoC controls two PWM groups with six tachometer inputs |
| Board monitoring | On-board status display and fan and temperature reporting | Per-slot power readings for all 12 slots |
| Networking | One 2.5GbE LAN port and two 10GbE optical ports | One 2.5GbE LAN port and two 10GbE optical ports |

The two optical ports support LACP aggregation with a compatible link partner. Match the physical board when building or installing. Its Linux device tree, MCU package and SPI bundle each have a board-specific variant. Detailed fan and slot wiring is in the [board differences](Y4-Y12-DIFFERENCES.md).

## Uses

- **Networked storage.** Use compatible NVMe devices and Debian storage services to provide file or storage access over the network. Expansion slot counts and lane widths describe the board layout. Check a device's negotiated link and compatibility after installation.
- **PCIe accelerator workloads.** The project supplies Linux host integration for supported M50 and RK182X devices. Workload containers supply card-specific runtime software, models and firmware tools. A working kernel driver binding verifies host detection. Check the specific card, runtime and workload separately. [RK182X host integration](https://github.com/opencecs/cix-kernel/blob/main/drivers/misc/rockchip-rkep/MYT-INTEGRATION.md)
- **Edge services and local management.** The system provides a Debian desktop, Docker and standard network services. The [board API](board-api.md) exposes board-specific fan and telemetry data to management clients.

Use the [build guide](DUAL-BOARD-BUILD.md) to prepare the system, the [development guide](DEVELOPMENT-HANDOFF.md) to locate changes and their validation steps, and the [Fastboot and OTA guide](BUILD-AND-FLASH-GUIDE.md) to install or update it.
