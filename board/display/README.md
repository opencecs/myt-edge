# Y4 Debian MCU display adapter

This adapter publishes Y4 screen data through the MCU protocol 2 display
stream. The caller supplies a locked I2C descriptor and `y4_display_publish`
uses Linux `I2C_RDWR` directly.

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
logical address panels. The home page shows the tachometry and effective
duty of FAN3–FAN5, numbered 1–3, plus expansion slot states and the CPU,
主机内 and PCIe switch temperatures. Initialization phase/count/error is
visible on home and boot/fault pages. Missing data is displayed as unknown.

Actual Bond state and active/LACP member masks are separate from carrier.
The screen distinguishes negotiation, individual forwarding, one LACP
member and two proven members in one active aggregator. Two plugged
cables never by themselves produce an LACP label.

The signed MCU asset bundle carries the font notices from the private MCU
source. The board package installs these notices with the other MCU licenses.
