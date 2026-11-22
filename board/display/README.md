# Y4 Debian MCU display adapter

This is a protocol2 additive display stream for MCU0.1.34. It does not
control networking, fans, firmware, UID or eFuse. A caller supplies a locked
I2C descriptor; y4_display_publish uses Linux I2C_RDWR directly.

Physical order is LAN/SFP1/SFP2. Logical order is LAN/Bond. IPv4 is four
network-order bytes, never a formatted command result. The actual default
route is its own field and is not inferred from carrier or intended policy.
max_attempts=0 is normalized to5; initialization history is valid only
when initialization_valid=1. Unknown addresses are encoded as zeros.
The protected initialization receipt remains distinct from live health.

The wire contract is documented in mcu/include/protocol.h: BEGIN0x06,
DATA0x07, COMMIT0x08, status0x4e, read-only schema0x4f. Send32 individual
DATA writes. Each publication checks token, CRC8, ranges and coherent
commit. Partial transfers expire after2seconds. Network observations expire
within3seconds, reduced by host sample age and staging time. Expiry removes
both IP addresses, physical carrier/identity masks and default egress.
Current-boot initialization history remains visible after network expiry.

The firmware screen uses one physical row with allthree NICs and two
logical address panels. The home page retains actual FAN4/FAN5 tachometry,
effective duty, expansion slot states and three independent temperatures
labelled CPU, 主机内 and pcie switch. Initialization phase/count/error is
visible on home and boot/fault pages. Missing data is displayed as unknown.

Build-only evidence is outside source:
the firmware GUI build output.
The host adapter compiled with native GCC and CIX ARM64 sysroot under
-Wall/-Wextra/-Werror/-Wconversion/-Wshadow/-pedantic. Actual Cortex-M4
firmware compiled with Y4_LCD_HARDWARE_SPI=1 and Y4_FAN_HARDWARE_PWM=0.
No tests, firmware execution, GUI rendering or physical hardware operations
were performed. Optical fit/readability, protocol runtime and physical
link/IP transitions remain NOT_TESTED.

Actual Bond state and active/LACP member masks are separate from carrier.
The screen distinguishes negotiation, individual forwarding, one LACP
member and two proven members in one active aggregator. Two plugged
cables never by themselves produce an LACP label.

Chinese font data is the existing licensed Droid Sans Fallback subset,
regenerated with94glyphs (9212bitmap bytes). The uncompressed TTF digest
is embedded in the generated source; notices remain in mcu/licenses.
The optional generator needs Pillow and an explicit external font input.
