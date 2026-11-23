# Y4 / Y12 network identity

The factory service reads the 96-bit MCU UID and derives locally administered
unicast EUI-48 addresses for LAN, SFP1 and SFP2. The MCU supplies its identity;
the host factory service writes and verifies the NIC NVM. The derivation is
shared across both models and does not use a model namespace.

A 46-bit hashed base followed by two consecutive addresses cannot guarantee
fleet-wide uniqueness. Both equal bases and overlapping address ranges can
collide. The I/G and U/L bits classify the addresses; they do not allocate
them. See [RFC 9542](https://www.rfc-editor.org/rfc/rfc9542.html) and the
[IEEE RA EUI guidelines](https://standards.ieee.org/wp-content/uploads/import/documents/tutorials/eui.pdf).

Check all legacy-derived addresses in a fleet inventory before approving a
production batch. Provide all previous and new MCU UIDs from both models,
one 24-digit hexadecimal UID per line, in production order. Run from the `myt-edge` repository root.

```sh
make -C board/factory/nic OUT=/tmp/myt-nic-audit audit-macs
/tmp/myt-nic-audit/audit-macs < /path/to/fleet-uids.txt
```

The checker uses the same C derivation as the runtime. It checks all four
addresses for each UID, including the `bond10g` MAC derived from the permanent
SFP pair. It detects duplicates across devices and roles in the supplied
inventory, including a bond address overlapping another device's physical
port. It performs the check offline. Preserve shipped assignments. A future
globally unique allocation needs reservations for all four roles from an
IEEE-assigned block under a persistent, shared uniqueness constraint.
Centrally reserved local ranges provide uniqueness within the managed
deployment scope.
