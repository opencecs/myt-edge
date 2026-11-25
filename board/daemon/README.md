# Debian MCU monitoring and update API

This component is a standalone root service for Debian 12. It does not mount
State/APP/ZFS, replace PID1, configure network interfaces or write DNS settings.
The network agent supplies current native netlink observations; the display
adapter supplies the new firmware's display frame.

y4-boardd is a Type=simple service. Its HTTP reactor supervises one MCU
monitoring child, one independent read-only PCIe switch sampler, and at most one
owned firmware-maintenance helper. Regular status collection does not execute
ip, ifconfig, ethtool or a shell. Blocked telemetry expires independently
of the HTTP listener. Actual Bond LACP/individual/negotiating state and member
masks come from the kernel collector, never from counting physical carriers.
The old y4-mcud service must not run concurrently.

Monitoring reuses master e890813f2a production I2C transport, UID coherence,
TMP75 PM transaction, SCMI/hwmon reads, staged token/CRC cooling, fan snapshots,
MCU health/backend decoders, physical PCIe slot inventory and disk activity.
On Y4, the Linux driver controls FAN1 and the MCU controls FAN3–FAN5. The
API retains the FAN6 field for protocol compatibility. On Y12, the SoC runs
the two fan groups and the board service reports fan and slot power telemetry.
The PCIe sampler imports the read-only
inventory/topology/BAR/temperature path, with no slot power/reset controller.
Its cached observations expire after 15 seconds. Physical network and logical
LAN/Bond address display observations expire after 3 seconds. Per-field error
and validity are retained, and missing addresses are empty/null rather than
last-known IPs.

All imported sources and exact hashes are recorded in vendor/PROVENANCE.json.
temperature_sample.c contains only the production temperature acquisition
prefix; product CLI/schema descriptors are omitted. poll_adapter.c implements
only the shared cooperative deadline interface. pcie.c provides a native
/proc boot-ID adapter rather than importing the OpenCecs boot/state code.

The monitoring process holds /run/y4-board/mcu.lock only during actual MCU
I2C work and uses nonblocking flock admission. Until current-boot initialization
is completed and boot.lock is free, it collects CPU/network/API observations
without touching MCU I2C. Boot helpers own the initial GUI/attempt publication. A firmware helper holds the
same lease throughout its operation. No monitor resets or reflashes the MCU.
The PCIe worker has its own lease and never owns MCU I2C.

## Root-local HTTP API

The default endpoint is /run/y4-board/api.sock, root:root mode0600 inside a
root0700 directory. Every accepted connection must have kernel SO_PEERCRED
uid0. No TCP listener, password database or plaintext network credential is
introduced. A remote management frontend can relay this API after enforcing
its own authenticated administrator permission.

| Method | Path | Behavior |
| --- | --- | --- |
| GET | /api/v1/hardware | Cached telemetry, per-field validity and boot initialization receipt. No hardware work is triggered. |
| GET | /api/v1/mcu/update | Latest job, actual helper phase/error and measured write/readback counters. |
| GET | /api/v1/mcu/firmware | Successfully published active package metadata, or active:false. |
| POST | /api/v1/mcu/firmware | On Y4, accept an internally signed MCU package with exact Content-Length, up to 1,049,736 bytes. Return 202 after durable upload and launch of the owned helper. On Y12, return 503 `mcu_install_unavailable`. |
| POST | /api/v1/mcu/cancel | One cooperative SIGTERM to the owned helper.202 is a cancellation request, not proof of exit. |

Example administrator requests on Y4

    curl --unix-socket /run/y4-board/api.sock http://localhost/api/v1/hardware
    curl --unix-socket /run/y4-board/api.sock \
      -H 'Content-Type: application/octet-stream' -H 'Expect:' \
      --data-binary @signed-mcu-package.bin \
      http://localhost/api/v1/mcu/firmware
    curl --unix-socket /run/y4-board/api.sock http://localhost/api/v1/mcu/update

HTTP framing is bounded to4096header bytes and eight simultaneous connections.
Chunked bodies, pipelining and Expect:100-continue are unavailable; disable
curl's Expect header as shown. Only one upload or update is admitted at a time.
Status requests remain independent of the monitor and flash helper. Upload
transport and API receipt creation can use normal Debian filesystem I/O; these
are not advertised as hard real-time deadlines.

## Signed update helper and persistence

The fixed helper invocation is:

    /usr/libexec/y4-board/y4-mcu-maintain
      --package /var/lib/y4-board/firmware-<job>.bin
      --key /usr/share/y4-board/mcu-public-key.bin
      --host-version <trusted /etc/y4-board/host-version>
      --status /run/y4-board/update-<job>.json
      --sequence-floor <minimum>

The helper owns signature/manifest/target/UID/ROM/readback/application
verification and must restore application mode after errors. Its success
receipt must contain schema1, completed:true, status:0, verified:true, a
24hex UID, an observed version, and the verified package sequence. Exit0 alone
is insufficient for publication. The API owns and reaps that exact child; a
10minute deadline marks the operation overdue without killing a permanent
write or replaying it.

Uploads have immutable private random names. The package SHA256, accepted
job intent and filesystem directory entry are synced before helper launch.
After actual child exit and successful hardware receipt validation, atomic
root0600 active.json is synced. It contains the package filename, SHA256,
job, UID, observed version and sequence. Boot initialization revalidates this
selection and its signature instead of downgrading to an older built-in image.
The sequence floor is the higher of the trusted built-in and active sequence.
A different package must have a sequence above that floor; uploading
the identical SHA256 is permitted as an explicit idempotent maintenance
request at the same sequence floor.

Failed uploads that never became a hardware job are removed. Accepted
candidates, failed helper logs and receipts remain. Admission stops after32
candidate packages or insufficient free space; the service does not erase
past evidence or an active package to make room.

If the daemon restarts while a job was accepted/running/overdue, its durable
receipt becomes interrupted_unknown, and new updates are refused. The
service does not assume that an unknown writer exited, replay a package, or
publish active metadata merely from a stale receipt. An administrator must
inspect the original helper, lease, observed MCU UID/version and status before
archiving that interrupted receipt and explicitly requesting further work.

## Build boundary

Running make against this directory's Makefile with an absolute external OUT
only compiles. For CIX ARM64 use the approved sysroot with both architecture
and generic system include directories, the pinned Debian 12 libcrypto
headers/library and the PAM headers/library. The public sysroot lock supplies
these build inputs. Runtime uses the Debian libssl3 and libpam0g packages.

SSH management calls systemd over D-Bus and applies local passwords through
Debian's chpasswd PAM stack. These operations run on a serialized worker so
the HTTP loop remains available for board observations and other requests.

The board daemon and web panel use the minimum kernel OOM score and opt
out of systemd-oomd victim selection under the product's default cgroups.
The services can still encounter failed allocations and unavailable dependencies.
