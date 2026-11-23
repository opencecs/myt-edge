# Repository layout

| Repository | Scope |
| --- | --- |
| [myt-edge](https://github.com/opencecs/myt-edge) | Build orchestration, board services, image and OTA packaging, Fastboot host tools and [approved prebuilt components](../prebuilt/) |
| [cix-kernel](https://github.com/opencecs/cix-kernel) | Linux drivers, ARM64 kernel configuration and device trees for supported boards |
| [cix-rootfs](https://github.com/opencecs/cix-rootfs) | Debian package inputs, system and desktop overlays and supported vendor runtime files |

Y4 and Y12 share the `main` branch in each repository. Implement shared behavior in its owning repository. Place hardware-specific values in the matching board configuration or device tree, and use the build's board selection to choose those files.

The [development guide](DEVELOPMENT-HANDOFF.md) maps common changes to their source paths and shows how to validate them. The [build guide](DUAL-BOARD-BUILD.md) assembles pinned commits into a fresh tree. Its `SOURCES.json` records the exact inputs. Keep source changes in the owning repository so a fresh assembly reproduces them.
