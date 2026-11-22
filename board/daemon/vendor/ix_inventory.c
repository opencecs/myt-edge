/*
 * IX read-only inventory collector prerequisite.
 *
 * Read-only sequential enumeration of the supervisor-controlled PCI
 * discovery root (e.g. /sys/bus/pci/devices). No shell, no device
 * control, no network, no writes, no retries, no heap allocation
 * (opendir's libc-internal allocation only). The caller owns the whole
 * inventory; nothing is cached globally and no freshness/atomic-hotplug
 * guarantee is claimed beyond the captured monotonic timestamps.
 *
 * Per entry:
 *  - every readdir result is counted (including "." / ".." and invalid
 *    names) against Y4_IX_SCAN_ENTRY_MAX;
 *  - only canonical BDF names become nodes, bounded by
 *    Y4_IX_TOPOLOGY_MAX_NODES (exact 512 allowed, 513 is overflow);
 *  - the node directory is opened ONCE; the same borrowed FD drives
 *    identity-attribute reads, the actual-path check via
 *    /proc/self/fd, parent derivation and y4_ix_link_read_fd. It is
 *    closed only after the link read.
 *  - the actual final path component must still equal the validated
 *    BDF exactly (rename/deleted-suffix aliasing is refused);
 *  - the immediate actual parent component is taken verbatim from the
 *    real path: it becomes parent_bdf only when it is itself a
 *    canonical BDF; numeric non-BDF ancestors (domain:bus dirs) and
 *    the discovery root yield an empty parent. The parent is NEVER
 *    inferred from numeric bus order.
 *  - vendor/device/class are bounded 32-byte regular-file reads
 *    (O_NOFOLLOW|O_NONBLOCK) with short-read accumulation, overflow
 *    probe, and rejection of ANY NUL byte including a terminal one.
 *    The FIRST identity failure is preserved as identity_error; failed
 *    fields are never substituted with fabricated valid zeros.
 *
 * Any incomplete enumeration (readdir error, entry/node overflow,
 * closedir failure) preserves the nodes gathered so far, forces the
 * topology stage to ERROR and returns the negative errno - never
 * READY. Zero canonical nodes with a clean scan returns -EAGAIN.
 */
#define _GNU_SOURCE
#include "y4_os_ix_inventory.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define Y4_IX_IDENTITY_TEXT_MAX 32u
#define Y4_IX_EINTR_CAP 64u

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

/* Lowercase hex digit value, or -1. */
static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

/*
 * Bounded hex parse ("0x" prefix optional). -EBADMSG for any junk,
 * empty digit run or non-hex character; -EOVERFLOW past the 16/24-bit
 * bound. Never fabricates a value on error.
 */
static int32_t parse_hex_bounded(const char *s, size_t len, unsigned bits,
                                 uint32_t *out)
{
    const uint64_t mask =
        (bits >= 64u) ? UINT64_MAX : ((UINT64_C(1) << bits) - UINT64_C(1));
    size_t i = 0u;
    uint64_t acc = 0u;

    if (len >= 2u && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) i = 2u;
    if (len == i) return -EBADMSG;
    for (; i < len; i++) {
        int d = hexval(s[i]);
        if (d < 0) return -EBADMSG;
        if (acc > (mask >> 4)) return -EOVERFLOW;
        acc = (acc << 4) | (uint64_t)d;
        if (acc > mask) return -EOVERFLOW;
    }
    *out = (uint32_t)acc;
    return 0;
}

/*
 * Read one identity attribute from the borrowed node FD.
 * Bounded 32-byte regular-file read with positive short-read
 * accumulation and a one-byte overflow probe at capacity; at most
 * Y4_IX_EINTR_CAP bounded EINTR retries (then -EINTR, never a spin).
 * Symlink attributes are refused (-ELOOP), non-regular files are
 * refused (-EPERM), ANY NUL byte including a terminal one is refused
 * (-EBADMSG), oversize is refused (-EOVERFLOW). Returns 0 and the
 * parsed bounded value on success, otherwise the individual negative
 * errno and no valid-zero substitution.
 */
static int32_t read_identity_attr(int node_fd, const char *name, unsigned bits,
                                  uint32_t *out)
{
    char text[Y4_IX_IDENTITY_TEXT_MAX];
    int32_t rc = 0;
    size_t used = 0u;
    unsigned eintr = 0u;
    int fd = openat(node_fd, name,
                    O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);

    if (fd < 0) {
        if (errno == ELOOP) return -ELOOP;
        return -errno;
    }
    struct stat st;
    if (fstat(fd, &st) != 0) {
        rc = -errno;
    } else if (!S_ISREG(st.st_mode)) {
        rc = -EPERM;
    } else {
        for (;;) {
            if (used == Y4_IX_IDENTITY_TEXT_MAX) {
                /* Capacity reached: confirm EOF with a probe read. */
                char extra;
                ssize_t m = read(fd, &extra, 1);
                if (m < 0)
                    rc = (errno == EINTR) ? -EINTR : -errno;
                else if (m > 0)
                    rc = -EOVERFLOW;
                break;
            }
            ssize_t n = read(fd, text + used, Y4_IX_IDENTITY_TEXT_MAX - used);
            if (n < 0) {
                if (errno == EINTR) {
                    if (++eintr > Y4_IX_EINTR_CAP) {
                        rc = -EINTR;
                        break;
                    }
                    continue;
                }
                rc = -errno;
                break;
            }
            if (n == 0)
                break;
            used += (size_t)n;
        }
    }
    if (close(fd) != 0 && rc == 0) rc = -errno;
    if (rc != 0) return rc;

    if (used == 0u) return -EBADMSG;
    /* Reject ANY NUL byte over the whole accumulated payload, terminal
     * NUL included: real sysfs text never contains one. */
    for (size_t k = 0u; k < used; ++k) {
        if (text[k] == '\0') return -EBADMSG;
    }
    size_t len = used;
    if (text[len - 1u] == '\n') {
        len--;
        if (len > 0u && text[len - 1u] == '\r') len--;
    }
    if (len == 0u) return -EBADMSG;
    return parse_hex_bounded(text, len, bits, out);
}

/*
 * Verify the opened node FD's final actual path component (via
 * /proc/self/fd) still equals the validated BDF exactly, and derive
 * the IMMEDIATE actual parent component. parent_bdf (>=13 bytes) is
 * set only when that immediate parent is itself a canonical BDF;
 * otherwise (discovery root, numeric domain:bus ancestor, missing
 * component) it is set to the empty string. The parent is never
 * inferred from numeric bus order and no ancestor above the immediate
 * parent is inspected.
 */
static int32_t derive_parent(int node_fd, const char *bdf, char *parent_bdf)
{
    char fdpath[32];
    char target[PATH_MAX];
    parent_bdf[0] = '\0';

    int pn = snprintf(fdpath, sizeof fdpath, "/proc/self/fd/%d", node_fd);
    if (pn < 0 || (size_t)pn >= sizeof fdpath) return -EOVERFLOW;
    ssize_t n = readlink(fdpath, target, sizeof target - 1);
    if (n < 0) return -errno;
    if ((size_t)n >= sizeof target - 1) return -ENAMETOOLONG;
    target[n] = '\0';

    size_t len = (size_t)n;
    while (len > 0u && target[len - 1u] == '/') len--;
    if (len == 0u) return -ENOENT;
    size_t start = len;
    while (start > 0u && target[start - 1u] != '/') start--;
    if (len - start != strlen(bdf) ||
        strncmp(target + start, bdf, len - start) != 0)
        return -EPERM; /* renamed / aliased / deleted-suffix node */

    /* Immediate parent component of the actual path. */
    size_t plen = start;
    while (plen > 0u && target[plen - 1u] == '/') plen--;
    size_t pstart = plen;
    while (pstart > 0u && target[pstart - 1u] != '/') pstart--;
    size_t pl = plen - pstart;
    if (pl == 0u || pl > 12u) return 0; /* root or non-BDF-shaped parent */
    char p[13];
    memcpy(p, target + pstart, pl);
    p[pl] = '\0';
    if (y4_ix_bdf_valid(p)) memcpy(parent_bdf, p, pl + 1u);
    return 0;
}

static void topology_reset(struct y4_ix_topology *t)
{
    memset(t, 0, sizeof *t);
    t->upstream_index = -1;
    t->soc_parent_index = -1;
    for (unsigned i = 0; i < 4; ++i) t->downstream_index[i] = -1;
}
static int capture_error(struct y4_ix_inventory *out, int error)
{
    topology_reset(&out->topology);
    out->scan_error = error;
    out->topology.stage = Y4_IX_TOPOLOGY_ERROR;
    out->topology.error = error;
    return error;
}
static void link_unset(struct y4_ix_link *l, int error)
{
    memset(l, 0, sizeof *l);
    struct y4_ix_field *f[] = {&l->current_link_speed, &l->max_link_speed,
                              &l->current_link_width, &l->max_link_width};
    for (unsigned i = 0; i < 4; ++i) {
        f[i]->status = Y4_IX_FIELD_UNSET;
        f[i]->err = error;
    }
}
static int sample_clock(uint64_t *out)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) return -errno;
    if (ts.tv_sec < 0 || ts.tv_nsec < 0 || ts.tv_nsec >= 1000000000L ||
        (uint64_t)ts.tv_sec > (UINT64_MAX - (uint64_t)ts.tv_nsec) / UINT64_C(1000000000))
        return -ERANGE;
    *out = (uint64_t)ts.tv_sec * UINT64_C(1000000000) + (uint64_t)ts.tv_nsec;
    return 0;
}

int y4_ix_inventory_read(const char *root, struct y4_ix_inventory *out)
{
    if (out != NULL) memset(out, 0, sizeof *out); /* reset even on bad args */
    if (out == NULL) return -EINVAL;
    topology_reset(&out->topology);
    if (root == NULL) return capture_error(out, -EINVAL);

    int clock_error = sample_clock(&out->begin_mono_ns);
    if (clock_error) return capture_error(out, clock_error);

    DIR *d = opendir(root); /* only libc-internal allocation */
    if (d == NULL) {
        int rc = -errno;
        return capture_error(out, rc);
    }
    int root_fd = dirfd(d);
    if (root_fd < 0) {
        int rc = -errno;
        (void)closedir(d);
        return capture_error(out, rc);
    }

    uint32_t entries = 0u;
    uint32_t count = 0u;
    int scan_error = 0;

    for (;;) {
        errno = 0; /* distinguish end-of-dir from readdir failure */
        struct dirent *e = readdir(d);
        if (e == NULL) {
            if (errno != 0) scan_error = -errno;
            break;
        }
        if (entries == (uint32_t)Y4_IX_SCAN_ENTRY_MAX) {
            scan_error = -EOVERFLOW; /* entry cap: 2049th readdir result */
            break;
        }
        entries++;
        if (!y4_ix_bdf_valid(e->d_name)) continue; /* bounded, ignored */
        if (count == (uint32_t)Y4_IX_TOPOLOGY_MAX_NODES) {
            scan_error = -EOVERFLOW; /* node cap: 513th canonical node */
            break;
        }

        struct y4_ix_node *nd = &out->nodes[count];
        memset(nd, 0, sizeof *nd);
        memcpy(nd->bdf, e->d_name, sizeof nd->bdf); /* BDF is exactly 12+NUL */

        int nfd = openat(root_fd, e->d_name,
                         O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        if (nfd < 0) {
            nd->identity_error = -errno; /* node recorded with its error */
            link_unset(&nd->link, nd->identity_error);
            count++;
            continue;
        }

        int32_t perr = derive_parent(nfd, nd->bdf, nd->parent_bdf);
        if (perr != 0) {
            nd->identity_error = perr;
        } else {
            int32_t ierr = read_identity_attr(nfd, "vendor", 16u, &nd->vendor);
            if (ierr == 0)
                ierr = read_identity_attr(nfd, "device", 16u, &nd->device);
            if (ierr == 0)
                ierr = read_identity_attr(nfd, "class", 24u, &nd->pci_class);
            nd->identity_error = ierr; /* first failure only, never zeroed */
        }

        /* Same borrowed FD, no reopen, closed only after the link read. */
        int lrc = y4_ix_link_read_fd(nfd, nd->bdf, &nd->link);
        if (lrc != 0 && nd->identity_error == 0) nd->identity_error = lrc;

        if (close(nfd) != 0 && nd->identity_error == 0)
            nd->identity_error = -errno;
        count++;
    }

    if (closedir(d) != 0 && scan_error == 0) scan_error = -errno;

    out->entries_seen = entries;
    out->count = count;

    clock_error = sample_clock(&out->end_mono_ns);
    if (clock_error) return capture_error(out, clock_error);
    if (out->end_mono_ns < out->begin_mono_ns)
        return capture_error(out, -EPROTO);
    if (scan_error) return capture_error(out, scan_error);
    if (count == 0u) {
        out->topology.stage = Y4_IX_TOPOLOGY_INITIALIZING;
        out->topology.error = -EAGAIN;
        return -EAGAIN;
    }

    /* Clean scan: hand the captured nodes to the pure reducer. */
    return y4_ix_topology_reduce(out->nodes, count, &out->topology);
}
