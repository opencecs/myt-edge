#ifndef Y4_OS_IX_LINK_H
#define Y4_OS_IX_LINK_H
#include <stdint.h>
/*
 * IX-02 candidate (isolated slice): read-only PCIe sysfs link snapshot.
 * Not HOST discovery, not power/reset, no register writes, no shell,
 * no global mutable state. Read-back only; nothing is fabricated:
 * every field carries an explicit status plus the individual negative
 * errno that produced it, and callers must not treat zero/absent
 * values as "link up" or "Gen4 x8".
 */
enum y4_ix_field_status {
    Y4_IX_FIELD_OK = 0,           /* value valid */
    Y4_IX_FIELD_MISSING = 1,      /* attribute absent or unreadable */
    Y4_IX_FIELD_INVALID = 2,      /* present but refused or unparseable */
    Y4_IX_FIELD_UNSUPPORTED = 3,  /* kernel reports "Unknown" (speed) */
    Y4_IX_FIELD_UNSET = 4         /* fatal error: no snapshot taken */
};

struct y4_ix_field {
    uint8_t status;      /* enum y4_ix_field_status */
    uint8_t reserved0;
    uint16_t reserved1;
    int32_t err;         /* 0 when status == OK; otherwise the individual
                           negative errno for THIS field:
                           -ENOENT  attribute absent
                           -EACCES/-EIO/... open or read failure
                           -ELOOP   attribute is a symlink (refused)
                           -EPERM   attribute is not a regular file
                           -EOVERFLOW attribute oversize or numeric overflow
                           -EBADMSG malformed content or any NUL byte
                           -ENOSYS  speed reported as "Unknown"
                           -EINTR   EINTR retry cap exceeded
                           Meaningful only when status != OK. */
    uint32_t value;      /* speed: MT/s (2500/5000/8000/16000/32000/64000);
                            width: lanes; meaningful only when status == OK.
                            value 0 with OK is a true reported zero, distinct
                            from MISSING/INVALID (read failure). */
};

/*
 * Bits in y4_ix_link.unsupported. This slice explicitly does NOT report
 * link training state, downshift/degradation history, or AER data; it
 * never fabricates them. The bits are always set on a successful
 * snapshot so absence can never be misread as "everything is fine".
 */
#define Y4_IX_UNSUP_TRAINING  0x1u /* link training state */
#define Y4_IX_UNSUP_DOWNSHIFT 0x2u /* downshift / degradation history */
#define Y4_IX_UNSUP_AER       0x4u /* AER / error counters */

struct y4_ix_link {
    struct y4_ix_field current_link_speed;
    struct y4_ix_field max_link_speed;
    struct y4_ix_field current_link_width;
    struct y4_ix_field max_link_width;
    uint32_t unsupported;      /* Y4_IX_UNSUP_* mask, set on success */
    uint32_t reserved0;
    uint64_t sample_mono_ns;   /* CLOCK_MONOTONIC sample time, ns */
};

/*
 * Snapshot one PCI node discovered under pci_root.
 *
 * pci_root is a SUPERVISOR-CONTROLLED kernel discovery root such as
 * /sys/bus/pci/devices; it is not a public arbitrary-path API. Real
 * sysfs BDF entries are symlinks into /sys/devices/... (often outside
 * the discovery directory), so the node itself may resolve anywhere:
 * the implementation anchors on the opened directory FDs and only
 * requires that the opened node's final path component equal the
 * exactly validated BDF (identity retention, no rename games).
 *
 * bdf must be canonical sysfs form "dddd:bb:dd.f" (lowercase hex,
 * device <= 0x1f, function <= 7). Attribute symlinks are refused and
 * attributes must be regular files.
 *
 * Returns 0 when a snapshot was taken (individual fields may still be
 * MISSING/INVALID/UNSUPPORTED with their own err). Negative -errno on
 * fatal errors: -EINVAL (bad args/BDF), -errno (root open or other
 * failure), -ENOENT (node not found), -EPERM (opened node identity does
 * not match the BDF), -ENAMETOOLONG (unresolvable node path). On every
 * fatal error *out is reset to a deterministic all-zero snapshot with
 * all field statuses UNSET and err set to the returned errno; it never
 * retains stale values from a previous call.
 */
/* Pure canonical BDF validation; no filesystem access. Returns0/1. */
int y4_ix_bdf_valid(const char *bdf);
/* Internal collector entry: borrowed directory FD remains owned by caller.
 * Same identity/attribute checks as path reader. No reopen of the discovery
 * path, no close/offset change of the borrowed directory. Caller serializes
 * FD lifetime and must not treat sequential attributes as an atomic sample.
 * Invalid negative FD => EINVAL; closed FD => EBADF; non-directory => ENOTDIR.
 */
int y4_ix_link_read_fd(int dev_fd, const char *bdf, struct y4_ix_link *out);
int y4_ix_link_read(const char *pci_root, const char *bdf, struct y4_ix_link *out);
#endif
