# Develop and validate MYT Edge

Use the [platform overview](PLATFORM-OVERVIEW.md) to choose the target hardware and the [repository guide](REPOSITORY-GUIDE.md) to find the source to change. Y4 and Y12 share the Debian system, build tools and board service. The board selection determines the device tree, board package and approved firmware inputs.

## Locate the change

| Change | Start here |
| --- | --- |
| Boot flow, initialization, hardware status, fans, networking or system OTA | [board/](../board/README.md) in this repository |
| Board API fields or client behavior | [Board API](board-api.md) and [daemon code](../board/daemon/) |
| Device tree, PCIe, a driver or ARM64 kernel configuration | [cix-kernel](https://github.com/opencecs/cix-kernel) |
| Debian packages, system or desktop configuration, vendor runtime integration | [cix-rootfs](https://github.com/opencecs/cix-rootfs) |
| Board assembly, input checks, image layout or package format | [Build scripts](../scripts/) and [host flash scripts](../scripts/flash/) |
| UEFI, MCU firmware or the management panel implementation | Maintainer source repositories. The public build consumes the approved binary inputs listed in the [build guide](DUAL-BOARD-BUILD.md#obtain-the-inputs). |

Read the [board differences](Y4-Y12-DIFFERENCES.md) before changing hardware behavior. Preserve the existing board API field meanings, units and error semantics when making compatible changes. Update the API reference and examples with any intentional interface change. For changes to signed MCU assets or the host interface, coordinate supported board, firmware version and host version with the maintainer. Relevant wire definitions are in [mcu/include](../mcu/include/).

## Select and build the target

1. Start from the current `main` revisions of the three repositories. Pin the commits used in the assembled tree and keep local build configuration outside the source checkouts.
2. Run the plan from the `myt-edge` checkout to confirm board selection and the expected artifacts.

   ```sh
   bash build.sh --board Y4 --plan
   bash build.sh --board Y12 --plan
   ```

   The plan prints the selection without validating inputs. Use [build preflight](DUAL-BOARD-BUILD.md#build-a-public-system) for the actual input checks.
3. Assemble and build with matching kernel, MCU assets and SPI inputs for the selected physical board. Review the generated `SOURCES.json` and save it with the results. Use separate output directories for Y4 and Y12.

## Test the affected paths

The following checks run on a Linux development host from the `myt-edge` checkout. They use synthetic data or mocked Fastboot commands.

```sh
python3 scripts/test_board_config.py
python3 scripts/test_release_package.py
python3 scripts/test_ota_package.py
python3 scripts/test_flash_spi_bootstrap.py
```

Use the affected kernel or root filesystem checks when work crosses repositories. For a complete build, run `python3 scripts/verify_release.py --build "$OUTPUT"` from the assembled tree as described in the [build guide](DUAL-BOARD-BUILD.md#verify-release-outputs). Record build, packaging and device results separately.

## Validate and release

On a designated test board, compare the running system version and board identity with the candidate. For a hardware or networking change, check the relevant [board API](board-api.md) fields and reproduce the affected behavior. Changes to Fastboot or OTA require testing the affected procedure on matching hardware. Verify the [full install](BUILD-AND-FLASH-GUIDE.md#full-fastboot-package) or the [signed OTA and confirmed boot slot](BUILD-AND-FLASH-GUIDE.md#system-ota) as appropriate.

Use one assigned `YYYYMMDD-rNN` version for a board's full package and system OTA. Record the three source commits, per-board input hashes, output archive hashes and validation results for the exact packages delivered. See the [release evidence requirements](../RELEASES.md).
