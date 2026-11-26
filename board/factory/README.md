# Debian factory initialization

This is the boot-only orchestration layer for the reused MCU and Realtek factory
helpers. It uses Debian Python3, systemd and ordinary private runtime files. It
does not import OpenCecs PID1/State/ZFS or execute a shell. Fixed native helpers
are the only child commands.

Install:
- boot.py as /usr/libexec/y4-board/y4-board-boot, mode0755.
- compiled boot_helpers.c + board/display/y4_display.c as
  /usr/libexec/y4-board/y4-board-boot-helper, mode0755.
- MCU and NIC helpers at their documented fixed paths under the same directory.
- board/systemd/y4-board-init.service and y4-boardd.service in Debian's unit path.
- Built-in assets under /usr/share/y4-board and host ABI compatibility version
  in /etc/y4-board/host-version.

## Assets and activation

The built-in files are mcu-package.bin, mcu-public-key.bin and mcu-builtin.json.
The JSON recipe is schema1 with unsigned64 sequence, lower-case SHA256 and
target version. The package and key are root-owned, regular, single-link,
non-symlink and not group/world writable.

A successful API update publishes /var/lib/y4-board/active.json using schema1,
an immutable firmware-<32hex>.bin filename, SHA256 and unsigned64 sequence.
The initializer verifies protected file metadata and actual package hashes.
It selects the active package only when its sequence is at least the built-in
sequence. Equal sequences with different hashes are refused. A malformed active
selection is retained as a real failure; it is never quietly replaced with the
older built-in selection. The native MCU helper revalidates signature, target,
host compatibility and the selected inclusive sequence floor before hardware IO.

Complete firmware images back this directory with `/data/y4-board/state`, so
A/B system updates and boot rollback share the board's MCU selection. The first
boot of an image with this layout validates the available old slots' records
and signed packages, then imports the highest sequence. Matching sequences
must have the same package digest. Source directories stay intact. A shared
record takes precedence once the migration is complete.

The migration also carries an unsettled MCU update into the shared state.
The current boot never repeats an unknown write. A later boot can use the
normal signed recovery checks, and the daemon only clears the unknown outcome
after verifying the current MCU is operational. Missing mounts, invalid
receipts and ambiguous slot records stop firmware writes and preserve the
source records. Existing standalone board packages keep the original layout
until a full system image opts into shared state.

## Attempts, ownership and ordering

The root0700 /run/y4-board directory is preserved across service restarts.
boot-counter.json records the current kernel boot ID, consumed attempts,
monotonic budget, child PID/birth and bounded attempt history. A root0600
boot.lock flock serializes the initializer.

At most five total initialization attempts are consumed in one boot. A
reservation is synced before the MCU/PG path can start. Child launch has an
explicit pipe barrier: after fork, the parent records and syncs the exact child
identity before releasing it to exec. A parent crash before release therefore
cannot leave an unrecorded future writer.

Each attempt:
1. Publishes the actual attempt/5 and MCU phase.
2. Calls the MCU helper once and waits for its actual exit.
3. Requires a verified application UID and successful application-mode receipt.
4. Publishes NIC phase, then a fresh protected 64-byte UID observation.
5. In public builds, calls the NIC helper in read-only inspection mode. A
   factory build with PG enabled calls `--reconcile --attempt N` and reads the
   protected terminal receipt and completion marker before retrying.

The UID helper emits the existing Y4UIDS01 little-endian/CRC32C format using
CLOCK_MONOTONIC. It does not fabricate an SSD/random identity. An earlier UID
is explicitly revoked before maintenance. In a PG-enabled build, the NIC
helper reads all three physical NVM configurations before deciding which writes
are needed.

The ordinary operation budget is300seconds. Expiry sends one cooperative TERM
to the owned helper and stops further actions. A timeout is not an exit:
the Type=simple initializer retains the real child until waitpid completes,
while unrelated Debian startup and getty/login remain independent. ROM/PG
writers are never group-killed or automatically replayed. A corrupt/foreign
counter or interrupted ownership becomes an explicit unknown state.

An earlier attempt's write-attempted barrier plus missing, malformed or
untrusted complete marker always fences new writers and network activation,
even if a vendor writer closed inherited descriptors and the NIC flock appears
free. The gate and every subsequent attempt inspect all five directories.
Native --inspect cannot clear such an ambiguous programming barrier.

Hard immutable-asset/authentication failures stop early with the actual count.
They do not manufacture five attempts. All known retryable attempts repeat the
full observation/readback admission, not an unconditional permanent write.

## Continuing after failure

After known terminal failure, native --inspect observes normal drivers and
valid nonduplicate permanent MACs. A nonzero overall discovery status does not
discard independently usable roles. Failed MCU maintenance is reported as
failed, with its restored/safe flags retained; it is not called healthy merely
because a normal NIC can provide recovery access.

A live/unknown writer, an unresolved write barrier, or explicit NIC restoration
failure keeps networking fenced. Otherwise usable normal NICs can be released.
The runtime NetworkManager file contains narrowly matched device sections
managed=0 for invalid y4lan/y4sfp1/y4sfp2 only. It does not overwrite an
administrator's global unmanaged-device list or force valid ports managed.

The read-only --ready entry requires:
- a protected schema1 current-boot initialization.json;
- completed, network_safe and writers_settled true, with a nonzero usable mask;
- the initializer boot lock and NIC writer lock free;
- no unresolved prior programming barrier.

Network manager startup drop-ins use that gate. They do not require successful
factory programming, and NetworkManager can retry a late completion without
replaying initialization. Status lookup executes no hardware command.

## Services and monitoring

y4-board-init.service is Type=simple so a kernel-blocked factory child cannot
hold the default boot target in an activating oneshot. The initializer remains
its owner while that child is pending. RuntimeDirectoryPreserve=yes prevents a
service restart from deleting the shared attempts or leases.

y4-boardd.service runs the isolated monitor and root-local API. It can report
progress while initialization is active; firmware POST admission requires the
current-boot initialization to be completed and boot.lock free. Normal MCU
monitoring uses the same nonblocking mcu.lock as maintenance.

Both services use KillMode=process and SendSIGKILL=no. Cooperative shutdown
waits for actual permanent-writer cleanup; this deliberately does not promise
a forced finite shutdown when a kernel syscall is uninterruptible.
