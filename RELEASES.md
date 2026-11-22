# Release evidence

A release records the board, version, source commits, binary input checksums and generated package checksums. The release operator publishes build and device validation results for the exact artifacts tested.

The full Fastboot ZIP includes the platform SPI BIN. A system OTA contains the root filesystem and matching kernel. See the [build and flash guide](docs/BUILD-AND-FLASH-GUIDE.md) for package validation and installation.
