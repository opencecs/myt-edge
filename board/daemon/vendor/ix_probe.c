/* Read-only IX8024 (1ec8:8024) PCI diagnostics collector.
 *
 * Filesystem surface, all relative to the borrowed device directory fd:
 *   config    binary PCI configuration space (4 KiB), pread only, 4-byte aligned
 *   resource  text BAR map, at most 4096 bytes, at least six lines
 *   driver    optional symlink to the bound driver directory
 *
 * No writes, mmap, ioctl, bus scans, retries, threads or heap allocation.
 * Cancellation/deadline is cooperative through y4_command_poll(), consulted
 * before and after every filesystem operation; a cancelled or expired call
 * aborts with -EPERM and clears the whole output record.
 *
 * Nothing here performs control: slot/link/device control and AER status
 * registers are reported, never written, and no temperature, power or reset
 * behaviour is inferred from them.
 */

#define _POSIX_C_SOURCE 200809L

#include "y4_os_ix_probe.h"

#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define IX_VENDOR_ID 0x1ec8u
#define IX_DEVICE_ID 0x8024u
#define IX_HEADER_BRIDGE 1u

#define IX_CONFIG_SIZE 0x1000u    /* PCIe configuration space size */
#define IX_CAP_MIN 0x40u          /* first legal classic capability pointer */
#define IX_CAP_LIMIT 0x100u       /* classic capabilities live below 0x100 */
#define IX_EXT_MIN 0x100u         /* first extended capability */
#define IX_CAP_WALK_MAX 48u       /* bounded classic traversal */
#define IX_EXT_WALK_MAX 960u       /* bounded extended traversal */

#define IX_CAP_PCIE_ID 0x10u
#define IX_EXT_AER_ID 0x0001u
#define IX_STATUS_CAPLIST 0x0010u

#define IX_RESOURCE_MAX 4096u
#define IX_RESOURCE_MIN_LINES 6
#define IX_RESOURCE_MAX_LINES 17
#define IX_RESOURCE_CHUNK 256u    /* multiple of 4: keeps every read aligned */

#define IX_DRIVER_LINK_MAX 256u

#define IX_RES_IO 0x0100u
#define IX_RES_MEM 0x0200u

struct ix_ctx {
    int dirfd;
    int fd;
    const struct y4_call *call;
    int cancelled;
};

static int ix_fatal(struct y4_ix_probe *out, int err)
{
    memset(out, 0, sizeof *out);
    return err;
}

/* Cooperative cancellation/deadline: the command module is the only clock. */
static int ix_poll(struct ix_ctx *ctx)
{
    int rc = ctx->call ? y4_command_poll(ctx->call) : 0;
    if (rc != 0) ctx->cancelled = rc;
    return rc;
}

/* Bounded read at an offset, polled on both sides. Short and interrupted reads
 * are hard errors: this module never retries. */
static int ix_read_some(struct ix_ctx *ctx, void *buf, size_t len, uint64_t off)
{
    int rc;
    ssize_t n;

    rc = ix_poll(ctx);
    if (rc != 0) return rc;
    n = pread(ctx->fd, buf, len, (off_t)off);
    int saved = errno;
    rc = ix_poll(ctx);
    if (rc != 0) return rc;
    return n < 0 ? -saved : (int)n;
}

static int ix_read(struct ix_ctx *ctx, void *buf, size_t len, uint64_t off)
{
    int n = ix_read_some(ctx, buf, len, off);

    if (n < 0) return n;
    if ((size_t)n != len) return -EIO;
    return 0;
}

/* Open one regular file inside the device directory without following symlinks
 * and without blocking on a non-regular replacement. */
static int ix_open(struct ix_ctx *ctx, const char *name)
{
    struct stat st;
    int fd, rc;

    rc = ix_poll(ctx);
    if (rc != 0) return rc;
    fd = openat(ctx->dirfd, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    int saved = errno;
    rc = ix_poll(ctx);
    if (rc != 0) { if (fd >= 0) close(fd); return rc; }
    if (fd < 0) return -saved;
    rc = ix_poll(ctx);
    if (rc != 0) { close(fd); return rc; }
    int checked = fstat(fd, &st);
    saved = errno;
    rc = ix_poll(ctx);
    if (rc != 0) { close(fd); return rc; }
    if (checked != 0) { close(fd); return -saved; }
    if (!S_ISREG(st.st_mode)) { close(fd); return -EINVAL; }
    return fd;
}

static uint32_t ix_get32(const unsigned char *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* --------------------------------------------------------------------------
 * PCIe capability (classic list, id 0x10)
 * ------------------------------------------------------------------------ */

static int ix_collect_pcie(struct ix_ctx *ctx, struct y4_ix_probe *out,
                           uint32_t status)
{
    unsigned char raw[4];
    uint32_t ptr, w;
    uint16_t flags;
    uint32_t dctrl = 0, dstat = 0, lcap = 0, lctrl = 0, lstat = 0;
    uint32_t scap = 0, sctrl = 0, sstat = 0;
    int slot = 0, i, rc;
    uint64_t visited = 0;

    if ((status & IX_STATUS_CAPLIST) == 0) return -ENOTSUP;

    rc = ix_read(ctx, raw, 4, 0x34u);
    if (rc != 0) return rc;
    ptr = ix_get32(raw) & 0xffu;

    for (i = 0; i < (int)IX_CAP_WALK_MAX; i++) {
        uint32_t next;

        if (ptr == 0) return -ENOTSUP;
        if (ptr < IX_CAP_MIN || (ptr & 3u) != 0 || ptr + 4u > IX_CAP_LIMIT)
            return -EINVAL;
        if (visited & (UINT64_C(1) << (ptr / 4u))) return -ELOOP;
        visited |= UINT64_C(1) << (ptr / 4u);
        rc = ix_read(ctx, raw, 4, ptr);
        if (rc != 0) return rc;
        w = ix_get32(raw);
        if (w == UINT32_MAX) return -EIO;
        next = (w >> 8) & 0xffu;

        if ((w & 0xffu) == IX_CAP_PCIE_ID) {
            flags = (uint16_t)(w >> 16);
            if ((flags & 0x0fu) == 0u || (flags & 0x0fu) > 2u) return -EIO;
            slot = (flags & 0x0100u) != 0;
            /* Whole capability must stay inside the classic list window. */
            if (ptr + (slot ? 28u : 20u) > IX_CAP_LIMIT) return -EINVAL;
            rc = ix_read(ctx, raw, 4, (uint64_t)ptr + 8u);
            if (rc != 0) return rc;
            w = ix_get32(raw);
            dctrl = w & 0xffffu;
            dstat = w >> 16;
            rc = ix_read(ctx, raw, 4, (uint64_t)ptr + 12u);
            if (rc != 0) return rc;
            lcap = ix_get32(raw);
            if (lcap == UINT32_MAX) return -EIO;
            rc = ix_read(ctx, raw, 4, (uint64_t)ptr + 16u);
            if (rc != 0) return rc;
            w = ix_get32(raw);
            lctrl = w & 0xffffu;
            lstat = w >> 16;
            if (slot) {
                rc = ix_read(ctx, raw, 4, (uint64_t)ptr + 20u);
                if (rc != 0) return rc;
                scap = ix_get32(raw);
                rc = ix_read(ctx, raw, 4, (uint64_t)ptr + 24u);
                if (rc != 0) return rc;
                w = ix_get32(raw);
                sctrl = w & 0xffffu;
                sstat = w >> 16;
            }

            out->pcie_present = 1;
            out->pcie_offset = (uint16_t)ptr;
            out->pcie_version = (uint8_t)(flags & 0x0fu);
            out->pcie_type = (uint8_t)((flags >> 4) & 0x0fu);
            out->slot_implemented = slot;
            out->pcie_device_ctrl = dctrl;
            out->pcie_device_status = dstat;
            out->pcie_link_cap = lcap;
            out->pcie_link_ctrl = lctrl;
            out->pcie_link_status = lstat;
            /* Slot registers are never invented when SLOTIMPL is clear. */
            out->pcie_slot_cap = scap;
            out->pcie_slot_ctrl = sctrl;
            out->pcie_slot_status = sstat;
            return 0;
        }

        ptr = next;
    }
    return -ELOOP; /* traversal bound exhausted */
}

/* --------------------------------------------------------------------------
 * AER extended capability (id 1). Read only, never cleared.
 * ------------------------------------------------------------------------ */

static int ix_collect_aer(struct ix_ctx *ctx, struct y4_ix_probe *out)
{
    unsigned char raw[4];
    uint32_t off, w;
    int i, rc;

    uint64_t visited[16] = {0};
    off = IX_EXT_MIN;
    for (i = 0; i < (int)IX_EXT_WALK_MAX; i++) {
        uint32_t next;

        if (off == 0) return -ENOTSUP;
        if (off < IX_EXT_MIN || (off & 3u) != 0 || off + 4u > IX_CONFIG_SIZE)
            return -EINVAL;
        unsigned bit = off / 4u;
        if (visited[bit / 64u] & (UINT64_C(1) << (bit % 64u))) return -ELOOP;
        visited[bit / 64u] |= UINT64_C(1) << (bit % 64u);
        rc = ix_read(ctx, raw, 4, off);
        if (rc != 0) return rc; /* extended config unavailable: keep error */
        w = ix_get32(raw);
        if (w == UINT32_MAX) return -EIO;
        next = w >> 20; /* already a byte offset */

        if ((w & 0xffffu) == IX_EXT_AER_ID) {
            uint32_t us, um, usev, cs, cm;

            if (off + 24u > IX_CONFIG_SIZE) return -EINVAL;
            rc = ix_read(ctx, raw, 4, (uint64_t)off + 4u);
            if (rc != 0) return rc;
            us = ix_get32(raw);
            if (us == UINT32_MAX) return -EIO;
            rc = ix_read(ctx, raw, 4, (uint64_t)off + 8u);
            if (rc != 0) return rc;
            um = ix_get32(raw);
            rc = ix_read(ctx, raw, 4, (uint64_t)off + 12u);
            if (rc != 0) return rc;
            usev = ix_get32(raw);
            rc = ix_read(ctx, raw, 4, (uint64_t)off + 16u);
            if (rc != 0) return rc;
            cs = ix_get32(raw);
            if (cs == UINT32_MAX) return -EIO;
            rc = ix_read(ctx, raw, 4, (uint64_t)off + 20u);
            if (rc != 0) return rc;
            cm = ix_get32(raw);

            out->aer_present = 1;
            out->aer_uncorrectable_status = us;
            out->aer_uncorrectable_mask = um;
            out->aer_uncorrectable_severity = usev;
            out->aer_correctable_status = cs;
            out->aer_correctable_mask = cm;
            return 0;
        }

        off = next;
    }
    return -ELOOP; /* traversal bound exhausted */
}

/* --------------------------------------------------------------------------
 * resource file: strict hex "start end flags" lines
 * ------------------------------------------------------------------------ */

static int ix_is_space(unsigned char c)
{
    return c == ' ' || c == '\t' || c == '\r';
}

static int ix_hex_digit(unsigned char c, unsigned *d)
{
    if (c >= '0' && c <= '9') { *d = (unsigned)(c - '0'); return 1; }
    if (c >= 'a' && c <= 'f') { *d = (unsigned)(c - 'a') + 10u; return 1; }
    if (c >= 'A' && c <= 'F') { *d = (unsigned)(c - 'A') + 10u; return 1; }
    return 0;
}

static int ix_parse_hex(const unsigned char *p, size_t len, size_t *used,
                        uint64_t *out)
{
    size_t i = 0, digits = 0;
    uint64_t v = 0;
    unsigned d;

    /* sysfs prints BAR values as "0x...". The prefix is optional, but at least
     * one hex digit is required and no other character is tolerated. */
    if (len >= 2 && p[0] == '0' && (p[1] == 'x' || p[1] == 'X')) i = 2;

    while (i < len && ix_hex_digit(p[i], &d)) {
        if (digits >= 16u) return -EOVERFLOW;
        if (v > (UINT64_MAX - d) / 16u) return -EOVERFLOW;
        v = v * 16u + d;
        digits++;
        i++;
    }
    if (digits == 0) return -EINVAL;
    *used = i;
    *out = v;
    return 0;
}

/* Returns 0 for a parsed line, negative on malformed input. Every line must
 * carry exactly three hex values, so blank lines are rejected. Lines past the
 * first six are still validated in full, then ignored. */
static int ix_parse_line(const unsigned char *p, size_t len, size_t index,
                         struct y4_ix_probe *out)
{
    uint64_t v[3];
    size_t i = 0;
    int t, present;

    while (i < len && ix_is_space(p[i])) i++;
    if (i == len) return -EINVAL; /* blank line: not three hex values */

    for (t = 0; t < 3; t++) {
        size_t used = 0;
        int rc = ix_parse_hex(p + i, len - i, &used, &v[t]);

        if (rc != 0) return rc;
        i += used;
        if (i < len && !ix_is_space(p[i])) return -EINVAL;
        while (i < len && ix_is_space(p[i])) i++;
    }
    if (i != len) return -EINVAL; /* extra token */

    if (v[0] == 0 && v[1] == 0 && v[2] == 0) {
        present = 0;
    } else {
        if (v[0] == 0 && v[1] == 0) return -EINVAL; /* flags without range */
        if (v[1] < v[0]) return -EINVAL;            /* end before start */
        if (v[1] - v[0] == UINT64_MAX) return -EOVERFLOW;
        {
            /* Exactly one base flag bit: I/O or memory, never both/neither.
             * Extra attribute bits (prefetchable, 64-bit) are tolerated. */
            uint64_t io = v[2] & IX_RES_IO;
            uint64_t mem = v[2] & IX_RES_MEM;

            if (io == 0 && mem == 0) return -EINVAL;
            if (io != 0 && mem != 0) return -EINVAL;
        }
        present = 1;
    }

    if (index < Y4_IX_RESOURCE_COUNT) {
        struct y4_ix_resource *r = &out->resources[index];

        r->start = v[0];
        r->length = present ? (v[1] - v[0] + 1u) : 0u;
        r->flags = v[2];
        r->present = present;
    }
    return 0;
}

static int ix_parse_resources(const unsigned char *buf, size_t len,
                              struct y4_ix_probe *out)
{
    size_t pos = 0;
    size_t index = 0;

    while (pos < len) {
        size_t begin = pos, end, k;
        int rc;

        while (pos < len && buf[pos] != '\n') pos++;
        end = pos;
        if (pos < len) pos++;

        for (k = begin; k < end; k++)
            if (buf[k] == '\0') return -EINVAL;

        rc = ix_parse_line(buf + begin, end - begin, index, out);
        if (rc != 0) return rc;
        index++;
        if (index > (size_t)IX_RESOURCE_MAX_LINES) return -E2BIG;
    }

    if (index < (size_t)IX_RESOURCE_MIN_LINES) return -EINVAL;
    return 0;
}

static int ix_collect_resources(struct ix_ctx *ctx, struct y4_ix_probe *out)
{
    unsigned char buf[IX_RESOURCE_MAX];
    size_t total = 0;
    int fd, rc;

    fd = ix_open(ctx, "resource");
    if (fd < 0) return fd;
    ctx->fd = fd;

    rc = 0;
    while (total < IX_RESOURCE_MAX) {
        size_t want = IX_RESOURCE_CHUNK;
        int n;

        if (want > (size_t)IX_RESOURCE_MAX - total)
            want = (size_t)IX_RESOURCE_MAX - total;
        n = ix_read_some(ctx, buf + total, want, (uint64_t)total);
        if (n < 0) { rc = n; goto done; }
        total += (size_t)n;
        if ((size_t)n < want) break;
    }
    if (total == (size_t)IX_RESOURCE_MAX) {
        unsigned char extra;
        int n = ix_read_some(ctx, &extra, 1u, (uint64_t)IX_RESOURCE_MAX);

        if (n < 0) { rc = n; goto done; }
        if (n > 0) { rc = -EFBIG; goto done; }
    }

    rc = ix_parse_resources(buf, total, out);

done:
    close(fd);
    ctx->fd = -1;
    if (rc != 0) memset(out->resources, 0, sizeof out->resources);
    return rc;
}

/* --------------------------------------------------------------------------
 * driver symlink: optional, bounded basename, no embedded NUL
 * ------------------------------------------------------------------------ */

static int ix_collect_driver(struct ix_ctx *ctx, struct y4_ix_probe *out)
{
    char buf[IX_DRIVER_LINK_MAX];
    const char *base = NULL;
    ssize_t n;
    size_t len, blen, i;
    int rc, saved;

    rc = ix_poll(ctx);
    if (rc != 0) return rc;
    n = readlinkat(ctx->dirfd, "driver", buf, sizeof buf);
    saved = errno;
    rc = ix_poll(ctx);
    if (rc != 0) return rc;
    if (n < 0) {
        switch (saved) {
        case ENOENT:
        case ENOTDIR: return -ENOENT;
        case EACCES:
        case EPERM: return -EPERM;
        case ELOOP: return -ELOOP;
        case ENAMETOOLONG: return -ENAMETOOLONG;
        case EINVAL: return -EINVAL;
        default: return -EIO;
        }
    }
    len = (size_t)n;
    if (len >= sizeof buf) return -ENAMETOOLONG;
    if (len == 0) return -ENOENT;
    for (i = 0; i < len; i++)
        if (buf[i] == '\0') return -EINVAL;

    for (i = 0; i < len; i++)
        if (buf[i] == '/') base = buf + i + 1;
    if (base == NULL) base = buf;
    blen = len - (size_t)(base - buf);
    if (blen == 0) return -EINVAL;
    if (blen >= (size_t)Y4_IX_DRIVER_BASENAME_MAX) return -ENAMETOOLONG;
    if (blen == 1 && base[0] == '.') return -EINVAL;
    if (blen == 2 && base[0] == '.' && base[1] == '.') return -EINVAL;

    memcpy(out->driver_basename, base, blen);
    out->driver_basename[blen] = '\0';
    return 0;
}

/* --------------------------------------------------------------------------
 * entry point
 * ------------------------------------------------------------------------ */

int y4_ix_probe_read_fd(int dirfd, const struct y4_call *call,
                        struct y4_ix_probe *out)
{
    struct ix_ctx ctx;
    unsigned char raw[4];
    uint32_t id, status;
    int rc, config_fd;

    if (out == NULL) return -EINVAL;
    memset(out, 0, sizeof *out);
    if (dirfd < 0) return -EINVAL;

    ctx.dirfd = dirfd;
    ctx.fd = -1;
    ctx.call = call;
    ctx.cancelled = 0;

    rc = ix_poll(&ctx);
    if (rc != 0) return ix_fatal(out, ctx.cancelled);

    /* --- identity from configuration space --- */
    ctx.fd = ix_open(&ctx, "config");
    if (ctx.fd < 0)
        return ix_fatal(out, ctx.fd);

    rc = ix_read(&ctx, raw, 4u, 0u);
    if (rc != 0) {
        close(ctx.fd);
        return ix_fatal(out, rc);
    }
    id = ix_get32(raw);
    if ((id & 0xffffu) != IX_VENDOR_ID || (id >> 16) != IX_DEVICE_ID) {
        close(ctx.fd);
        return ix_fatal(out, -ENODEV);
    }

    rc = ix_read(&ctx, raw, 4u, 0x0cu);
    if (rc != 0) {
        close(ctx.fd);
        return ix_fatal(out, rc);
    }
    out->vendor = (uint16_t)(id & 0xffffu);
    out->device = (uint16_t)(id >> 16);
    out->header_type = (uint8_t)(raw[2] & 0x7fu);
    if (out->header_type != IX_HEADER_BRIDGE) {
        close(ctx.fd);
        return ix_fatal(out, -EINVAL);
    }

    rc = ix_read(&ctx, raw, 4u, 0x04u);
    if (rc != 0) {
        close(ctx.fd);
        return ix_fatal(out, rc);
    }
    status = ix_get32(raw) >> 16;

    /* --- PCIe section (independent) --- */
    rc = ix_collect_pcie(&ctx, out, status);
    if (ctx.cancelled) {
        close(ctx.fd);
        return ix_fatal(out, ctx.cancelled);
    }
    if (rc != 0) {
        out->pcie_error = rc;
        out->pcie_present = 0;
        out->pcie_offset = 0;
        out->pcie_version = 0;
        out->pcie_type = 0;
        out->slot_implemented = 0;
        out->pcie_device_ctrl = 0;
        out->pcie_device_status = 0;
        out->pcie_link_cap = 0;
        out->pcie_link_ctrl = 0;
        out->pcie_link_status = 0;
        out->pcie_slot_cap = 0;
        out->pcie_slot_ctrl = 0;
        out->pcie_slot_status = 0;
    } else {
        out->pcie_error = 0;
    }

    /* --- AER section (independent) --- */
    rc = ix_collect_aer(&ctx, out);
    if (ctx.cancelled) {
        close(ctx.fd);
        return ix_fatal(out, ctx.cancelled);
    }
    if (rc != 0) {
        out->aer_error = rc;
        out->aer_present = 0;
        out->aer_uncorrectable_status = 0;
        out->aer_uncorrectable_mask = 0;
        out->aer_uncorrectable_severity = 0;
        out->aer_correctable_status = 0;
        out->aer_correctable_mask = 0;
    } else {
        out->aer_error = 0;
    }

    config_fd = ctx.fd;
    ctx.fd = -1;

    /* --- resource section (independent) --- */
    rc = ix_collect_resources(&ctx, out);
    if (ctx.cancelled) {close(config_fd); return ix_fatal(out, ctx.cancelled);}
    out->resource_error = rc;

    /* --- driver section (independent; absence is not fatal) --- */
    rc = ix_collect_driver(&ctx, out);
    if (ctx.cancelled) {close(config_fd); return ix_fatal(out, ctx.cancelled);}
    out->driver_error = rc;
    if (rc != 0) memset(out->driver_basename, 0, sizeof out->driver_basename);
    /* Same open configuration object must still identify the same chip. */
    ctx.fd = config_fd;
    rc = ix_read(&ctx, raw, 4u, 0u);
    int closed = close(config_fd);
    if (rc) return ix_fatal(out, rc);
    if (closed) return ix_fatal(out, -errno);
    if (ix_get32(raw) != id) return ix_fatal(out, -ESTALE);
    return 0;
}
