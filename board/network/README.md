# Y4 Debian networking

This directory implements the network portion of the Debian branch. The frozen
Debian12 base contains NetworkManager1.42.4 and GLib2.74.6. NetworkManager remains
the sole owner of DHCP, address/route changes and resolver configuration.
`y4-network-policy` uses its authenticated system D-Bus API; the read-only
`y4_network_sample()` collector uses rtnetlink and sysfs without exec.

The implementation and compile results are NOT_TESTED. No executable was run,
no service was started, no network was configured and no board was accessed.
Native and CIX ARM64 compilation are separate from runtime/hardware acceptance.

## Logical and physical interfaces

The Y4 Realtek drivers assign the physical names at registration from the CIX root-port identity:
a0e0000.pcie/r8125 becomes y4lan; a0d0000.pcie/r8127 becomes y4sfp1; and
a0c0000.pcie/r8127 becomes y4sfp2. The collector independently verifies PCI
vendor/device identity against10ec:8125 or10ec:8127 and checks the current
kernel ifindex. The packaged udev rule preserves these names, and factory inspection requires the expected name before releasing a port. The .link files cover older kernels.

y4lan obtains its own DHCP address. y4sfp1 and y4sfp2 are members of bond10g,
and only bond10g obtains the second logical DHCP address. Members have IPv4
and IPv6 configuration disabled. The two logical networks are not bridged;
this package enables neither forwarding nor NAT. Exact-interface arp_ignore,
arp_announce and loose rp_filter settings accompany per-source routing, so
different addresses on the same subnet retain their intended return path.
A duplicate exact IP on both logical interfaces is reported as EADDRINUSE
and the adapter withholds its generated main-table defaults. This is not
a guarantee about loops caused by unrelated external switch wiring.

## Adaptive 10G operation

The standard NM bond profile uses802.3ad, min_links1, active/fast LACP, MII100ms,
ad_select=bandwidth, layer3+4 hashing and all_slaves_active0. It never assumes
that the peer supports or has configured a port channel. It does not change
the switch configuration, send a proprietary probe, or flip bond modes.

The existing CIX6.1.44 bonding implementation already has single-port fallback:
`ad_port_selection_logic()` joins ports only for matching nonzero partner
system/key information. Ports without a replying partner remain separate
single-port aggregators. `ad_agg_selection_logic()` selects one active
aggregator, disables the old aggregator, then enables that selected
no-partner aggregator. When a valid configured partner negotiates the same
group, the kernel can join the members and reselect the aggregate. Therefore
no kernel patch and no unsupported NM lacp_strict option is added.

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

The frozen base has a stale regular resolv.conf pointing at a loopback stub,
but neither systemd-resolved nor resolvconf is installed. The NM configuration
retains NetworkManager's standard automatic resolver-backend selection, which
consumes DHCP DNS and maintains that regular file itself when no provider is
installed. Existing administrator systemd-resolved/resolvconf owners and
resolver symlinks remain under their standard manager. Neither board daemon
writes resolv.conf. No fixed upstream server or additional DNS daemon is
introduced. The adapter imposes no DNS priority on either logical interface; their
DHCP servers enter the standard resolver selection under NetworkManager and
administrator or VPN DNS ordering is preserved.

NetworkManager, systemd-networkd and ifupdown startup drop-ins require the
parent-provided read-only `/usr/libexec/y4-board/y4-board-boot --ready` gate and
order after y4-board-init.service. There is no Requires=initializer-success.
NetworkManager retries at2seconds without a start-limit budget so a genuinely
finished late PG worker can release networking without replaying factory work.
The gate must confirm current-boot completion, restored drivers, safe runtime
ownership and the absence of a live PG writer. Initializer exit/timeout alone
does not satisfy it. Unsafe NICs may be marked unmanaged by the parent's
protected per-boot NM configuration while other usable hardware is retained.

The base image's historical ifupdown eth0/eth1 entries must be retired only in
the disposable new-image staging area; preserve loopback and unrelated
administrator configuration. stage.py:stage_network(root) only rewrites exact
SHA-pinned vendor input and archives it first. Changed/custom files are retained
and reported, never silently filtered. The frozen vendor file's missing loopback
method is completed using the normal Debian iface lo inet loopback stanza.
The staging API never writes resolver contents or executes any service. Do not enable networkd for the same interfaces.
No install or service operation has been executed by this component task.

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

The existing CIX source is authoritative for the shipped kernel behavior.
The recent upstream lacp_strict change explicitly preserves that older
default behavior and makes strict LACP carrier behavior opt-in:
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


### Y4 network activation

The three network managers and the carrier policy require
`y4-network-ready.service`. It starts the board initializer through systemd
and waits for `y4-board-boot --ready`. A masked initializer or a missing,
unsafe or timed-out result blocks startup. Each consumer rechecks
current-boot readiness before launching. Local login remains available.

The readiness result requires the factory writers to be settled and the
normal NIC inspection to have found usable permanent addresses. Existing
per-port unmanaged rules still fence unusable NICs. Y12 retains its own
network startup policy.
