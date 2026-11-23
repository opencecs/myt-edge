# Y4 and Y12 Debian networking

This directory implements the product network configuration for Debian 12. NetworkManager remains
the sole owner of DHCP, address/route changes and resolver configuration.
`y4-network-policy` uses its authenticated system D-Bus API; the read-only
`y4_network_sample()` collector uses rtnetlink and sysfs without exec.

## Logical and physical interfaces

The Y4 Realtek drivers assign the physical names at registration from the CIX root-port identity:
a0e0000.pcie/r8125 becomes y4lan; a0d0000.pcie/r8127 becomes y4sfp1; and
a0c0000.pcie/r8127 becomes y4sfp2. The collector independently verifies PCI
vendor/device identity against10ec:8125 or10ec:8127 and checks the current
kernel ifindex. The packaged udev rule preserves these names, and factory inspection requires the expected name before releasing a port. The .link files cover older kernels. Y12 uses the corresponding y12lan, y12sfp1 and y12sfp2 names with the same role-based configuration.

y4lan obtains its own DHCP address. y4sfp1 and y4sfp2 are members of bond10g,
and only bond10g obtains the second logical DHCP address. Members have IPv4
and IPv6 configuration disabled. The two logical networks are not bridged;
this package enables neither forwarding nor NAT. Exact-interface arp_ignore,
arp_announce and loose rp_filter settings accompany per-source routing, so
different addresses on the same subnet retain their intended return path.
A duplicate exact IP on both logical interfaces is reported as EADDRINUSE
and the adapter withholds its generated main-table defaults. This is not
a guarantee about loops caused by unrelated external switch wiring.

The boot-time NIC inspection reads both optical ports' permanent MACs
independently of carrier. For the consecutive locally administered factory
pair, `bond10g` uses the SFP1 address with the first octet XORed with `0x80`.
The initializer publishes that explicit address in a runtime NetworkManager
profile before releasing the network. DHCPv4 uses it as the Ethernet client
ID. The identity stays tied to the same NICs across rootfs replacements and
slot changes. A missing or incompatible NIC pair uses the stable keyfile
setting, and initialization releases independently valid ports. This uses the
existing inspection, with no carrier wait or additional helper. The factory
inventory check covers address conflicts across devices and roles.

## Adaptive 10G operation

The standard NM bond profile uses802.3ad, min_links1, active/fast LACP, MII100ms,
ad_select=bandwidth, layer3+4 hashing and all_slaves_active0. It never assumes
that the peer supports or has configured a port channel. It does not change
the switch configuration, send a proprietary probe, or flip bond modes.

One connected10G can operate. Two connections to a non-LACP peer use one
selected port rather than transmit ordinary traffic over both independent
links. Two ports become an actual two-member LACP group only after valid
negotiation with the same partner/group. Misconfigured or nonresponsive
partners can still blackhole traffic; only board/peer acceptance can establish
interoperability. LACP also does not make an individual flow exceed a member's
link rate.

The native snapshot exposes actual bond mode, active aggregator ID/member
count and partner identity, along with per-port actor/partner state.
`bond_state=LACP_MULTI` requires both expected members in the SAME active
aggregator with both actor and partner Collecting/Distributing. Merely having
two physical carriers is never described as successful aggregation.
Known single-port fallback is INDIVIDUAL; unsupported or incomplete evidence
is UNKNOWN/NEGOTIATING, with an explicit error when unavailable.

## Default egress and same-subnet replies

The factory profiles put their automatic IPv4/IPv6 routes into reserved tables
1025(LAN) and1010(Bond). They initially install no default route in main254.
The policy adapter reads the current kernel carrier of each physical10G:
any positively observed10G carrier selects Bond; only two known-down/missing
10G links select LAN. Unknown link observations do not authorize LAN fallback.
DHCP delays, public reachability and DNS failures are not fallback triggers.

Through NM Device.GetAppliedConnection/Reapply with version CAS, the adapter
adds the selected default and connected-prefix routes to main254, while
generating exact-source rules into the corresponding DHCP routing table.
This preserves management replies bound to the LAN address even while Bond
is the default for unbound traffic. Gateway/addresses come from NM's current
IPConfig, and changes remain NM-owned. IPv6 uses the same physical selection.
Both configurations are prepared before a handoff. Publish the replacement
default and confirm it in the next applied snapshot before withdrawing the
previous one. A failed sample, missing gateway
or failed replacement keeps the previous default in place. Reapply failures
back off independently per interface. This is asynchronous policy; the kernel snapshot and operation
status remain distinct, and no zero-time failover deadline is claimed.

Only the two fixed product profile UUIDs and route-table conventions are
managed. Foreign profiles are not rewritten. Administrator profile routes and
rules are retained; legacy-only route arrays or incompatible route-table edits
are explicitly refused. Reapply uses NM1.42's preserve-external-ip flag.
Status is atomically published as root0600
`/run/y4-board/network-policy.json`; `observed_ipv4_egress` is the observation
before any requested reapply, not proof that the request completed in-kernel.

These are normal keyfile profiles, editable with Debian's NetworkManager tools.
When replacing this default policy deliberately, disable y4-network-policy
and update the standard profiles/routing design together. Tables1010/1025
and priorities11010/11025 are reserved while the policy service is enabled.

## DNS and boot coordination

The Y4/Y12 image uses systemd-resolved for the local DNS listener at
`127.0.0.53`. NetworkManager supplies upstream DNS through its resolved
plugin using the same DHCP, static profile and VPN configuration. The
rootfs points `/etc/resolv.conf` at the resolver's runtime stub file. During
image construction the builder's resolver is available in the chroot's
private `/run` and is discarded at the end of the build.

Docker starts after the local listener is initialized, without waiting for a
lease. Host-network containers copy the same stub address at start and can
resolve when DHCP DNS arrives or changes. User-defined bridges still use
Docker's own DNS, and per-container DNS overrides still apply. Docker's
legacy default bridge reads the separate upstream file maintained by
systemd-resolved, so it retains Docker's normal upstream snapshot behavior.

NetworkManager, systemd-networkd and ifupdown startup require
`y4-network-ready.service` on both boards. It starts the board initializer and
waits for `y4-board-boot --ready`. The check requires a completed current-boot
initialization, settled writers and usable NICs with valid permanent addresses.
A masked initializer, timed-out check or unresolved write blocks networking.
The startup check leaves local login available. The initializer can mark
unusable ports unmanaged in the per-boot NetworkManager configuration while
retaining other usable ports.

The base image's historical ifupdown eth0/eth1 entries must be retired only in
the disposable new-image staging area; preserve loopback and unrelated
administrator configuration. stage.py:stage_network(root) only rewrites exact
SHA-pinned vendor input and archives it first. Changed/custom files are retained
and reported, never silently filtered. The frozen vendor file's missing loopback
method is completed using the normal Debian iface lo inet loopback stanza.
The staging API never writes resolver contents or executes any service. Do not enable networkd for the same interfaces.

## Build and installation contract

Compile-only, using separate output paths:

```sh
make -C board/network OUT=/absolute/external/native-output
make -C board/network CC=aarch64-linux-gnu-gcc AR=aarch64-linux-gnu-ar \
  SYSROOT=/path/to/cix-sysroot \
  OUT=/absolute/external/aarch64-output
```

The policy links against GIO/GObject/GLib. The collector itself uses only libc
and can be compiled directly into the host MCU daemon. The supplied SDK needs
both its multiarch and generic include directories, plus its existing
ld-linux-aarch64 DSO at link time; no replacement guard implementation or
runtime library shim is introduced.

Install the policy binary under /usr/libexec/y4-board/. Install:
- profiles/*.nmconnection under /etc/NetworkManager/system-connections,
  root0600 (preserve admin edits through normal package conffile policy);
- links/*.link under /usr/lib/systemd/network, root0644;
- conf/70-y4-network.conf under /etc/NetworkManager/conf.d;
- conf/70-y4-multihome.conf under /etc/sysctl.d;
- systemd/*service.d/*.conf under the equivalent /etc/systemd/system paths;
- systemd/y4-network-policy.service under /usr/lib/systemd/system and enable
  it for multi-user.target in image staging.

All boot gate, signed-MCU maintenance and factory ownership checks belong to
the parent board initializer. This directory does not introduce PID1, State
or ZFS dependencies.

## Primary references

See the [CIX kernel repository](https://github.com/opencecs/cix-kernel) for the shipped kernel implementation. The upstream LACP change makes strict carrier behavior opt-in.
[upstream commit32b0b895](https://git.zx2c4.com/wireguard-linux/commit/?id=32b0b8953343eaceaa816a9ead3b6bb66355c64e).
Native NetworkManager APIs and profile properties are specified in the
[Device D-Bus API](https://www.networkmanager.dev/docs/api/1.42.4/gdbus-org.freedesktop.NetworkManager.Device.html),
[IPv4 settings](https://www.networkmanager.dev/docs/api/1.42.4/settings-ipv4.html)
and [NetworkManager configuration](https://www.networkmanager.dev/docs/api/1.42.4/NetworkManager.conf.html).

## Boundary notes

- The multi-home sysctl file (70-y4-multihome.conf) applies to the fixed
  product interface names y4lan and bond10g, which are created by the shipped
  systemd .link files. Interfaces renamed or added by an administrator are
  outside this package's scope and receive no Y4 sysctl policy.
- DNS stays with NetworkManager's native resolver ownership; this package
  sets no dns-priority anywhere, so administrator and VPN DNS ordering is
  preserved.
