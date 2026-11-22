# Repository layout

- [myt-edge](https://github.com/opencecs/myt-edge) coordinates builds and packages and carries the board service.
- [cix-kernel](https://github.com/opencecs/cix-kernel) contains Linux, board device trees and per-board configuration.
- [cix-rootfs](https://github.com/opencecs/cix-rootfs) contains Debian inputs, overlays and supported runtime assets.

Pin a commit in each public repository when reproducing an image. The [build guide](DUAL-BOARD-BUILD.md) assembles those commits and lists the additional local binary inputs. Review the [board differences](Y4-Y12-DIFFERENCES.md) and [board API](board-api.md) when modifying board-specific behavior.

Y4 and Y12 share these three `main` branches. Apply common changes once in the owning repository. Select board-specific hardware configuration at build time. The maintainer build takes the same sources and adds private component binaries. Use previous local repositories and assembled trees for historical reference.
