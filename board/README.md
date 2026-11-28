# Y4 and Y12 Debian board integration

This directory adds normal Debian services, NetworkManager profiles, a root-local
MCU update API, native hardware collection and a token/CRC MCU display stream.
Debian retains systemd/PID1, apt/dpkg, ext4 and its native DNS resolver ownership.
No OpenCecs State/APP/ZFS or parallel network manager is installed.

See the [platform overview](../docs/PLATFORM-OVERVIEW.md) and [board differences](../docs/Y4-Y12-DIFFERENCES.md). The selected product determines the board API identity at build time.

## Component contracts

- daemon/README.md: native monitoring, root-peer Unix HTTP update/status API,
  persistent signed package selection and exclusive MCU ownership.
- factory/README.md: boot order, at most five attempts, real child ownership,
  protected receipts and degraded network readiness.
- The prebuilt MCU maintainer handles one-attempt signed MCU maintenance, UID/ROM protection,
  byte readback, restoration and compatibility/sequence policy.
- network/README.md: standard Debian NetworkManager profiles and GDBus route
  policy, native link/address/route/LACP observations and staging.
- display/README.md: atomic stream, freshness and GUI layout.
- debian/: packaging notices and the external vendor input boundaries.

Y4 V11 uses physical FAN3/FAN4/FAN5 under MCU control, displayed as fan 1/2/3.
Y4 MCU 0.1.48 sets 15% PWM before enabling 12 V. Auto and legacy modes hold
that duty. Manual mode applies the saved settings with a live host heartbeat
and returns to 15% on heartbeat timeout. Versions 0.1.43 through 0.1.47 use 20%.
Physical FAN1 has a separate SoC PWM. UEFI holds it at 50%. With valid temperature
and tachometer readings, Linux uses 30% below 45 °C, 50% from 45 °C to below
70 °C, and 100% at 70 °C and above. It uses 100% during its two-second startup
period or when sensor data is stale, invalid or indicates a stalled fan.
The device tree disables FAN2. FAN6 retains its reserved protocol field and is
hidden on the LCD and in the panel.
Screen labels are CPU, 主机内 and pcie switch.

Y12 chassis cooling is controlled by the SoC. Its MCU reports all twelve
slot power channels and leaves the reserved temperature fields invalid.
Both boards use the same boot initializer and permanent MAC inspection.
Y12 uses its device-tree MCU UART and GPIO descriptors and its own Linux
interface names. Factory NIC programming requires a verified MCU application
identity. Use the approved board assets in the Y12 system build. The MCU
upload API reports `mcu_install_unavailable` on Y12.

## Approved network behavior

LAN is independent DHCP. The two optical ports are members of bond10g, with
DHCP only on the Bond. Physical roles use stable PCI ancestry, never ethN order:
LAN a0e0000.pcie, SFP1 a0d0000.pcie, SFP2 a0c0000.pcie.

The standard 802.3ad profile uses fast LACP, min_links=1, bandwidth aggregator
selection and inactive-member receive filtering. A correctly configured peer
permits same-partner aggregation. The native telemetry distinguishes individual,
negotiating, single-member LACP and multi-member LACP states.

Bond is default while either optical link has verified carrier. LAN becomes
default only when both are known down; Bond resumes on link return. Unknown
samples are not link-down. No Internet/DNS probe controls the decision.
Per-source routing preserves same-subnet replies. No LAN/Bond bridge,
automatic forwarding or NAT is created. DNS remains with normal Debian/NM.

## Build a board package

Use the verified MCU artifact directory for the selected board. Its
manifest binds the signed firmware, its trust key and the accompanying ARM64
host tools. The package build checks the firmware signature, board, firmware
version, host compatibility and the supplied SHA256 inventory.

    python3 board/build.py --board Y4 --mcu-assets /path/to/Y4/mcu \
      --mcu-assets-manifest-sha256 SHA256_OF_OFFICIAL_MANIFEST \
      --dependencies /path/to/dependencies --architectures arm64 \
      --output /path/to/output --require-signed --make-deb

The output is an installable Debian package. The image builder installs it
offline after staging the target kernel modules and configuration. Building
and installing into an image never executes target code or changes hardware.

A regular build without Realtek PG inputs produces an inspection-only board
package. At boot it checks PCI identity, driver binding and permanent MAC
addresses. Unknown writers still block network readiness, and invalid or
duplicate addresses are excluded. Manufacturing builds can explicitly include
the separately licensed PG utility and its matching kernel build inputs.
