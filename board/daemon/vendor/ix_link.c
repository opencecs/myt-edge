#define _GNU_SOURCE
#include "y4_os_ix_link.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define Y4_IX_ATTR_MAX 64u
#define Y4_IX_EINTR_CAP 64u

static int hex_lower(char c)
{
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
}

/* Strict canonical "dddd:bb:dd.f": lowercase hex, device <= 0x1f, fn <= 7. */
int y4_ix_bdf_valid(const char *bdf)
{
    if (!bdf)
        return 0;
    for (unsigned i = 0; i < 4; ++i)
        if (!hex_lower(bdf[i]))
            return 0;
    if (bdf[4] != ':')
        return 0;
    for (unsigned i = 5; i < 7; ++i)
        if (!hex_lower(bdf[i]))
            return 0;
    if (bdf[7] != ':')
        return 0;
    for (unsigned i = 8; i < 10; ++i)
        if (!hex_lower(bdf[i]))
            return 0;
    if (bdf[10] != '.')
        return 0;
    if (bdf[11] < '0' || bdf[11] > '7')
        return 0;
    if (bdf[12] != '\0')
        return 0;
    if (bdf[8] > '1') /* device <= 31 (0x1f) */
        return 0;
    return 1;
}

/*
 * Fill *out with a deterministic fatal-error snapshot: every field UNSET
 * with the same negative errno, no timestamps, no values. Used before any
 * fatal return so out never retains stale valid values from an earlier
 * successful call.
 */
static void unset_snapshot(struct y4_ix_link *out, int rc)
{
    memset(out, 0, sizeof(*out));
    out->current_link_speed.status = (uint8_t)Y4_IX_FIELD_UNSET;
    out->current_link_speed.err = (int32_t)rc;
    out->max_link_speed.status = (uint8_t)Y4_IX_FIELD_UNSET;
    out->max_link_speed.err = (int32_t)rc;
    out->current_link_width.status = (uint8_t)Y4_IX_FIELD_UNSET;
    out->current_link_width.err = (int32_t)rc;
    out->max_link_width.status = (uint8_t)Y4_IX_FIELD_UNSET;
    out->max_link_width.err = (int32_t)rc;
    out->sample_mono_ns = 0u;
}

/*
 * Bounded read of one attribute via dev_fd. Refuses symlinks (O_NOFOLLOW)
 * and non-regular files; the open uses O_NONBLOCK so a FIFO cannot block
 * forever in open() before fstat rejects it as non-regular. Returns a
 * field status; *err receives the individual negative errno (-ENOENT,
 * -ELOOP, -EPERM, -EACCES/-EIO/..., -EOVERFLOW oversize, -EINTR retry
 * cap, -EBADMSG any NUL byte).
 * buf must hold cap+1 bytes; on OK it holds a NUL-terminated string of
 * at most cap bytes containing no NUL byte at all.
 */
static int read_attr(int dev_fd, const char *name, char *buf, size_t cap, int *err)
{
    int fd = openat(dev_fd, name,
                    O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0) {
        if (errno == ELOOP || errno == ENOTDIR) {
            *err = -ELOOP;   /* symlink (or symlink component) refused */
            return Y4_IX_FIELD_INVALID;
        }
        /* ENOENT and every other open failure: unreadable => MISSING. */
        *err = -errno;
        return Y4_IX_FIELD_MISSING;
    }
    struct stat st;
    if (fstat(fd, &st) != 0) {
        *err = -errno;
        (void)close(fd);
        return Y4_IX_FIELD_MISSING;
    }
    if (!S_ISREG(st.st_mode)) {
        *err = -EPERM; /* not a regular file: refused, not missing */
        (void)close(fd);
        return Y4_IX_FIELD_INVALID;
    }
    size_t total = 0;
    unsigned eintr = 0;
    for (;;) {
        ssize_t n = read(fd, buf + total, cap - total);
        if (n < 0) {
            if (errno == EINTR) {
                if (++eintr >= Y4_IX_EINTR_CAP) { /* bounded, never spins */
                    *err = -EINTR;
                    (void)close(fd);
                    return Y4_IX_FIELD_MISSING;
                }
                continue;
            }
            *err = -errno;
            (void)close(fd);
            return Y4_IX_FIELD_MISSING;
        }
        if (n == 0)
            break;
        total += (size_t)n;
        if (total == cap) {
            char extra;
            ssize_t m = read(fd, &extra, 1); /* overflow probe */
            if (m < 0) {
                if (errno == EINTR) {
                    /* cannot confirm no extra bytes: must not become OK */
                    *err = -EINTR;
                    (void)close(fd);
                    return Y4_IX_FIELD_MISSING;
                }
                *err = -errno;
                (void)close(fd);
                return Y4_IX_FIELD_MISSING;
            }
            if (m > 0) {
                *err = -EOVERFLOW; /* oversize: refused, not truncated */
                (void)close(fd);
                return Y4_IX_FIELD_INVALID;
            }
            break; /* exactly cap bytes, EOF confirmed */
        }
    }
    (void)close(fd);
    /* Reject ANY NUL byte in the attribute text. Real sysfs text never
     * contains a NUL (it always ends with '\n'), so a sole terminal NUL
     * is just as anomalous as an embedded one and both must be refused. */
    for (size_t i = 0; i < total; ++i) {
        if (buf[i] == '\0') {
            *err = -EBADMSG;
            return Y4_IX_FIELD_INVALID;
        }
    }
    buf[total] = '\0';
    *err = 0;
    return Y4_IX_FIELD_OK;
}

static int is_space(char c)
{
    return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

/*
 * Parse "<N>[.<F>] GT/s[ PCIe]" (optional surrounding whitespace).
 * Known speeds map to MT/s; the literal "Unknown" is UNSUPPORTED (-ENOSYS);
 * anything else (including well-formed but unmapped numbers) is INVALID
 * (-EBADMSG or -EOVERFLOW on numeric overflow).
 */
static int parse_speed(const char *s, uint32_t *mts_out, int *err)
{
    while (is_space(*s))
        s++;
    if (strncmp(s, "Unknown", 7) == 0) {
        const char *r = s + 7;
        while (is_space(*r))
            r++;
        if (*r == '\0') {
            *err = -ENOSYS;
            return Y4_IX_FIELD_UNSUPPORTED;
        }
        *err = -EBADMSG;
        return Y4_IX_FIELD_INVALID;
    }
    if (*s < '0' || *s > '9') {
        *err = -EBADMSG;
        return Y4_IX_FIELD_INVALID;
    }
    uint64_t int_part = 0;
    while (*s >= '0' && *s <= '9') {
        if (int_part > 1000u) {
            *err = -EOVERFLOW;
            return Y4_IX_FIELD_INVALID;
        }
        int_part = int_part * 10u + (uint64_t)(*s - '0');
        s++;
    }
    uint64_t frac = 0;
    unsigned frac_digits = 0;
    if (*s == '.') {
        s++;
        if (*s < '0' || *s > '9') {
            *err = -EBADMSG;
            return Y4_IX_FIELD_INVALID;
        }
        while (*s >= '0' && *s <= '9') {
            if (frac_digits >= 2) {
                *err = -EBADMSG;
                return Y4_IX_FIELD_INVALID;
            }
            frac = frac * 10u + (uint64_t)(*s - '0');
            frac_digits++;
            s++;
        }
    }
    if (*s != ' ' || s[1] != 'G' || s[2] != 'T' || s[3] != '/' || s[4] != 's') {
        *err = -EBADMSG;
        return Y4_IX_FIELD_INVALID;
    }
    s += 5;
    int separated = is_space(*s);
    while (is_space(*s))
        s++;
    if (separated && strncmp(s, "PCIe", 4) == 0) {
        s += 4;
        while (is_space(*s))
            s++;
    }
    if (*s != '\0') {
        *err = -EBADMSG;
        return Y4_IX_FIELD_INVALID;
    }
    uint64_t mts = int_part * 1000u;
    if (frac_digits == 1)
        mts += frac * 100u;
    else if (frac_digits == 2)
        mts += frac * 10u;
    switch (mts) {
    case 2500u:
    case 5000u:
    case 8000u:
    case 16000u:
    case 32000u:
    case 64000u:
        *mts_out = (uint32_t)mts;
        *err = 0;
        return Y4_IX_FIELD_OK;
    default:
        *err = -EBADMSG; /* well-formed but unmapped rate */
        return Y4_IX_FIELD_INVALID;
    }
}

/* Decimal lane count; true "0" is valid; overflow or junk is INVALID. */
static int parse_width(const char *s, uint32_t *width_out, int *err)
{
    while (is_space(*s))
        s++;
    if (*s < '0' || *s > '9') {
        *err = -EBADMSG;
        return Y4_IX_FIELD_INVALID;
    }
    uint64_t v = 0;
    while (*s >= '0' && *s <= '9') {
        v = v * 10u + (uint64_t)(*s - '0');
        if (v > 0xFFFFFFFFu) {
            *err = -EOVERFLOW;
            return Y4_IX_FIELD_INVALID;
        }
        s++;
    }
    while (is_space(*s))
        s++;
    if (*s != '\0') {
        *err = -EBADMSG;
        return Y4_IX_FIELD_INVALID;
    }
    *width_out = (uint32_t)v;
    *err = 0;
    return Y4_IX_FIELD_OK;
}

static void fill(struct y4_ix_field *f, int status, int err, uint32_t value)
{
    f->status = (uint8_t)status;
    f->reserved0 = 0;
    f->reserved1 = 0;
    f->err = (int32_t)err;
    f->value = (status == Y4_IX_FIELD_OK) ? value : 0u;
}

static void read_speed_field(int dev_fd, const char *name, struct y4_ix_field *f)
{
    char buf[Y4_IX_ATTR_MAX + 1];
    int err = 0;
    int st = read_attr(dev_fd, name, buf, Y4_IX_ATTR_MAX, &err);
    uint32_t v = 0;
    if (st == Y4_IX_FIELD_OK)
        st = parse_speed(buf, &v, &err);
    fill(f, st, err, v);
}

static void read_width_field(int dev_fd, const char *name, struct y4_ix_field *f)
{
    char buf[Y4_IX_ATTR_MAX + 1];
    int err = 0;
    int st = read_attr(dev_fd, name, buf, Y4_IX_ATTR_MAX, &err);
    uint32_t v = 0;
    if (st == Y4_IX_FIELD_OK)
        st = parse_width(buf, &v, &err);
    fill(f, st, err, v);
}

/* Extract the final path component of target (after any trailing '/'). */
static const char *final_component(const char *target)
{
    size_t len = strlen(target);
    while (len > 0 && target[len - 1] == '/')
        len--;
    if (len == 0)
        return target + strlen(target); /* empty: "" */
    size_t i = len;
    while (i > 0 && target[i - 1] != '/')
        i--;
    return target + i;
}

int y4_ix_link_read_fd(int dev_fd, const char *bdf, struct y4_ix_link *out)
{
    if (!out) return -EINVAL;
    if (dev_fd < 0 || !y4_ix_bdf_valid(bdf)) {
        unset_snapshot(out, -EINVAL);
        return -EINVAL;
    }
    struct stat st;
    if (fstat(dev_fd, &st) != 0) {
        int rc = -errno;
        unset_snapshot(out, rc);
        return rc;
    }
    if (!S_ISDIR(st.st_mode)) {
        unset_snapshot(out, -ENOTDIR);
        return -ENOTDIR;
    }
    /* Identity retention: the opened node's real final component must
     * still be exactly the validated BDF (defends against symlink +
     * rename aliasing tricks on the physical directory). */
    char fdpath[32];
    char target[PATH_MAX];
    int pn = snprintf(fdpath, sizeof(fdpath), "/proc/self/fd/%d", dev_fd);
    if (pn < 0 || (size_t)pn >= sizeof(fdpath)) {
        unset_snapshot(out, -EOVERFLOW);
        return -EOVERFLOW;
    }
    ssize_t n = readlink(fdpath, target, sizeof(target) - 1);
    if (n < 0) {
        int rc = -errno;
        unset_snapshot(out, rc);
        return rc;
    }
    if ((size_t)n >= sizeof(target) - 1) {
        unset_snapshot(out, -ENAMETOOLONG);
        return -ENAMETOOLONG;
    }
    target[n] = '\0';
    if (strcmp(final_component(target), bdf) != 0) {
        unset_snapshot(out, -EPERM);
        return -EPERM;
    }
    struct y4_ix_link r;
    memset(&r, 0, sizeof(r));
    r.unsupported = Y4_IX_UNSUP_TRAINING | Y4_IX_UNSUP_DOWNSHIFT | Y4_IX_UNSUP_AER;
    r.reserved0 = 0;
    read_speed_field(dev_fd, "current_link_speed", &r.current_link_speed);
    read_speed_field(dev_fd, "max_link_speed", &r.max_link_speed);
    read_width_field(dev_fd, "current_link_width", &r.current_link_width);
    read_width_field(dev_fd, "max_link_width", &r.max_link_width);
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        int rc = -errno;
        unset_snapshot(out, rc);
        return rc;
    }
    r.sample_mono_ns = (uint64_t)ts.tv_sec * 1000000000u + (uint64_t)ts.tv_nsec;
    *out = r;
    return 0;
}

int y4_ix_link_read(const char *pci_root, const char *bdf, struct y4_ix_link *out)
{
    if (!out) return -EINVAL;
    if (!pci_root || !y4_ix_bdf_valid(bdf)) {
        unset_snapshot(out, -EINVAL);
        return -EINVAL;
    }
    int root_fd = open(pci_root, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (root_fd < 0) {
        int rc = -errno;
        unset_snapshot(out, rc);
        return rc;
    }
    int dev_fd = openat(root_fd, bdf, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    int saved = errno;
    (void)close(root_fd);
    if (dev_fd < 0) {
        int rc = (saved == ENOENT || saved == ENOTDIR) ? -ENOENT : -saved;
        unset_snapshot(out, rc);
        return rc;
    }
    int rc = y4_ix_link_read_fd(dev_fd, bdf, out);
    (void)close(dev_fd);
    return rc;
}
