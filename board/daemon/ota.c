/* SPDX-License-Identifier: Apache-2.0 */
/* OTA update pipeline: one process, inside y4-boardd.
 *
 * Nothing here forks: the whole apply (verify, slot write, hash, grubenv,
 * reboot request) is a resumable state machine advanced by the daemon's
 * existing poll loop in bounded slices. The reboot is requested over
 * systemd D-Bus after the inactive slot has been verified.
 *
 *   POST /api/v1/update          streams a signed .y4pkg to /data
 *   (state machine)              verifies, writes the inactive slot,
 *                                sets grubenv, reboots cleanly
 *   GET  /api/v1/update/status   phase and byte progress
 *   POST /api/v1/update/confirm  manual promote of the running slot
 *
 * Package layout (all integers little endian):
 *   0  magic "Y4PKG001" (8)
 *   8  u32 manifest_bytes
 *   12 u32 signature_bytes (64, Ed25519)
 *   16 u64 payload_bytes
 *   24 manifest JSON
 *   .. Ed25519 signature over the manifest bytes
 *   .. payload: an Android sparse ext4 image, at most one slot long
 */
#define _GNU_SOURCE
#include "board_daemon.h"
#include "y4_grubenv.h"
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <ftw.h>
#include <inttypes.h>
#include <linux/fs.h>
#include <openssl/evp.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <gio/gio.h>
#include <sys/ioctl.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/reboot.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>
#include <zstd.h>

#define OTA_DIR "/data/y4-board/ota"
#define OTA_KEY "/usr/share/y4-board/ota-public-key.bin"
#define OTA_LIMIT (24ULL << 30)
#define OTA_HEADER_BYTES 24u
#define OTA_MAGIC_UNCOMPRESSED "Y4PKG001"
#define OTA_MAGIC_ZSTD "Y4PKG002"
#define OTA_MAGIC_DUAL "Y4PKG003"
#define OTA_SIG_BYTES 64u
#define OTA_MANIFEST_MAX 65536u
#define OTA_SLOT_BYTES (16ULL << 30)
#define OTA_IO_CHUNK (4u << 20)
#define OTA_TICK_BYTES (16u << 20)
#define OTA_KERNEL_LIMIT (256ULL << 20)  /* kernel payload cap (design N1) */
#define OTA_KERNEL_MARGIN (64ULL << 20)  /* free /boot/efi space demanded (design 5) */
#define OTA_KERNELS_DIR "/boot/efi/kernels"
#define SPARSE_MAGIC 0xed26ff3aU
#define SPARSE_RAW 0xcac1U
#define SPARSE_FILL 0xcac2U
#define SPARSE_DONT_CARE 0xcac3U
#define SPARSE_CRC32 0xcac4U

enum ota_state {
  OTA_IDLE = 0,
  OTA_ACCEPTED,
  OTA_OPEN,
  OTA_VERIFY,
  OTA_MANIFEST,
  OTA_VERSION,
  OTA_SLOT,
  OTA_DEVICE,
  OTA_WRITE,
  OTA_HASH,
  OTA_READBACK,
  OTA_KERNEL_WRITE,
  OTA_KERNEL_VERIFY,
  OTA_KERNEL_PUBLISH,
  OTA_GRUBENV,
  OTA_READY,
  OTA_REBOOTING,
  OTA_FAILED
};

struct ota_job {
  int state;
  int code;
  uint64_t started_ms, completed_ms;
  char id[33], package[PATH_MAX];
  char version[64], slot[8];
  int pkg_fd, out_fd, verify_fd;
  char manifest[OTA_MANIFEST_MAX + 1];
  size_t manifest_bytes;
  uint64_t payload_offset, payload_bytes, cursor, written, image_total, verify_at;
  EVP_MD_CTX *digest;
  EVP_MD_CTX *logical;
  EVP_MD_CTX *disk_digest;
  unsigned char *buffer;
  unsigned char *win;   /* aligned window over the payload */
  uint64_t win_base;    /* payload-relative offset of win[0] */
  size_t win_len;
  uint32_t blk_sz, total_chunks, chunk_index;
  uint64_t chunk_left;
  int chunk_type, chunk_header_done;
  char kernel[96], device[128], payload_sha[65], payload_hash[65], logical_hash[65];
  int target_index;
  int compressed;
  uint64_t zcursor_raw;
  uint64_t zpayload_bytes;  /* compressed payload length in the file */
  int zstd_done;
  ZSTD_DCtx *dctx;
  ZSTD_inBuffer zin;
  ZSTD_outBuffer zout;
  unsigned char *zin_buf;  /* aligned window over the compressed payload */
  /* Schema 2: the kernel payload written beside the rootfs (design N1-N6).
   * k_cursor/k_win are payload-relative within the kernel region; uts_tail
   * carries the scan overlap so a release string split across chunk
   * boundaries is still seen whole. */
  int schema;
  uint64_t rootfs_bytes;  /* the rootfs payload length: the write walk and
                           * its trailing pad stop here, never at the dual-payload
                           * package total */
  uint64_t k_offset, k_bytes, k_padded, k_image_bytes, k_dtb_bytes;
  uint64_t k_cursor, k_written, k_write_phase, uts_pos;
  char k_dtb_name[64], k_release[96], k_delivery[64], k_sha[65], k_hash[65];
  char k_stage_dir[PATH_MAX];
  int k_fd, uts_found;
  size_t uts_tail_len;
  EVP_MD_CTX *k_digest;
};
static struct ota_job ota = {
  .pkg_fd = -1, .out_fd = -1, .verify_fd = -1, .k_fd = -1,
};
static int ota_upload_fd = -1;
static uint64_t ota_received;
static EVP_MD_CTX *ota_upload_digest;
/* Buffered writes to this board's filesystem corrupt large files
 * intermittently (measured), so the received package is staged in an
 * aligned buffer and written with O_DIRECT. */
static unsigned char *ota_upload_stage;
static size_t ota_upload_used;

static const char *state_name(int state) {
  switch (state) {
    case OTA_ACCEPTED: return "accepted";
    case OTA_OPEN: return "open";
    case OTA_VERIFY: return "verifying";
    case OTA_MANIFEST: return "manifest";
    case OTA_VERSION: return "version";
    case OTA_SLOT: return "slot";
    case OTA_DEVICE: return "device";
    case OTA_WRITE: return "writing";
    case OTA_HASH: return "hashing";
    case OTA_READBACK: return "verifying";
    case OTA_KERNEL_WRITE: return "writing-kernel";
    case OTA_KERNEL_VERIFY: return "verifying-kernel";
    case OTA_KERNEL_PUBLISH: return "publishing-kernel";
    case OTA_GRUBENV: return "grubenv";
    case OTA_READY: return "ready";
    case OTA_REBOOTING: return "rebooting";
    case OTA_FAILED: return "failed";
    default: return "idle";
  }
}

/* ---- helpers ---- */

static int read_all(const char *path, char *out, size_t cap, size_t *used) {
  int fd = open(path, O_RDONLY | O_CLOEXEC);
  if (fd < 0) return -errno;
  size_t at = 0;
  while (at + 1 < cap) {
    ssize_t n = read(fd, out + at, cap - 1 - at);
    if (n < 0) { if (errno == EINTR) continue; int e = -errno; close(fd); return e; }
    if (!n) break;
    at += (size_t)n;
  }
  close(fd);
  out[at] = 0;
  if (used) *used = at;
  return 0;
}

/* Exact-length read: the trust anchor is a raw 32-byte key and a short read
 * must never be accepted as a valid (different) key. */
static int read_exact(const char *path, unsigned char *out, size_t bytes) {
  int fd = open(path, O_RDONLY | O_CLOEXEC);
  if (fd < 0) return -errno;
  size_t at = 0;
  while (at < bytes) {
    ssize_t n = read(fd, out + at, bytes - at);
    if (n < 0) { if (errno == EINTR) continue; int e = -errno; close(fd); return e; }
    if (!n) break;
    at += (size_t)n;
  }
  unsigned char extra;
  int more = read(fd, &extra, 1);
  close(fd);
  return at == bytes && more == 0 ? 0 : -EINVAL;
}

/* Delivery versions and DTB names become FAT path components: restrict
 * them to a safe charset up front (no traversal, no FAT-hostile chars). */
static int valid_name(const char *name, size_t cap) {
  size_t len = strlen(name);
  if (!len || len >= cap || !strcmp(name, ".") || !strcmp(name, "..")) return 0;
  for (size_t i = 0; i < len; i++) {
    unsigned char c = (unsigned char)name[i];
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '.' || c == '-' || c == '_')) return 0;
  }
  return 1;
}

static int json_string(const char *text, const char *key, char *out, size_t cap) {
  char pattern[96];
  if ((size_t)snprintf(pattern, sizeof pattern, "\"%s\"", key) >= sizeof pattern) return -EINVAL;
  const char *at = strstr(text, pattern);
  if (!at) return -ENODATA;
  at += strlen(pattern);
  while (*at == ' ' || *at == '\t') at++;
  if (*at != ':') return -EINVAL;
  at++;
  while (*at == ' ' || *at == '\t') at++;
  if (*at != '"') return -EINVAL;
  at++;
  const char *end = strchr(at, '"');
  if (!end || (size_t)(end - at) >= cap) return -EINVAL;
  memcpy(out, at, (size_t)(end - at));
  out[end - at] = 0;
  return 0;
}

static int json_u64(const char *text, const char *key, uint64_t *out) {
  char pattern[96];
  if ((size_t)snprintf(pattern, sizeof pattern, "\"%s\"", key) >= sizeof pattern) return -EINVAL;
  const char *at = strstr(text, pattern);
  if (!at) return -ENODATA;
  at += strlen(pattern);
  while (*at == ' ' || *at == '\t') at++;
  if (*at != ':') return -EINVAL;
  at++;
  while (*at == ' ' || *at == '\t') at++;
  char *endp;
  unsigned long long value = strtoull(at, &endp, 10);
  if (endp == at) return -EINVAL;
  *out = value;
  return 0;
}

/* Locate the JSON object bound to key inside base[0..base_len): the value
 * must be a brace-balanced object. Keys like "offset" appear in more than
 * one payload object, so schema 2 members are always read through this
 * bounded lookup instead of a flat search. */
static int json_find_object(const char *base, size_t base_len, const char *key,
                            const char **start, size_t *len) {
  char pattern[96];
  if ((size_t)snprintf(pattern, sizeof pattern, "\"%s\"", key) >= sizeof pattern) return -EINVAL;
  const char *at = memmem(base, base_len, pattern, strlen(pattern));
  if (!at) return -ENODATA;
  at += strlen(pattern);
  const char *end = base + base_len;
  while (at < end && (*at == ' ' || *at == '\t')) at++;
  if (at >= end || *at != ':') return -EINVAL;
  at++;
  while (at < end && (*at == ' ' || *at == '\t')) at++;
  if (at >= end || *at != '{') return -EINVAL;
  int depth = 0, in_string = 0;
  const char *p = at;
  while (p < end) {
    char c = *p;
    if (in_string) {
      if (c == '\\') p++;          /* skip the escaped character */
      else if (c == '"') in_string = 0;
    } else if (c == '"') {
      in_string = 1;
    } else if (c == '{') {
      depth++;
    } else if (c == '}') {
      if (--depth == 0) { *start = at; *len = (size_t)(p - at) + 1; return 0; }
    }
    p++;
  }
  return -EINVAL;
}

static int json_u64_in(const char *obj, size_t obj_len, const char *key, uint64_t *out) {
  char pattern[96];
  if ((size_t)snprintf(pattern, sizeof pattern, "\"%s\"", key) >= sizeof pattern) return -EINVAL;
  const char *at = memmem(obj, obj_len, pattern, strlen(pattern));
  if (!at) return -ENODATA;
  at += strlen(pattern);
  const char *end = obj + obj_len;
  while (at < end && (*at == ' ' || *at == '\t')) at++;
  if (at >= end || *at != ':') return -EINVAL;
  at++;
  while (at < end && (*at == ' ' || *at == '\t')) at++;
  char *endp;
  unsigned long long value = strtoull(at, &endp, 10);
  if (endp == at) return -EINVAL;
  *out = value;
  return 0;
}

static int json_string_in(const char *obj, size_t obj_len, const char *key,
                          char *out, size_t cap) {
  char pattern[96];
  if ((size_t)snprintf(pattern, sizeof pattern, "\"%s\"", key) >= sizeof pattern) return -EINVAL;
  const char *at = memmem(obj, obj_len, pattern, strlen(pattern));
  if (!at) return -ENODATA;
  at += strlen(pattern);
  const char *end = obj + obj_len;
  while (at < end && (*at == ' ' || *at == '\t')) at++;
  if (at >= end || *at != ':') return -EINVAL;
  at++;
  while (at < end && (*at == ' ' || *at == '\t')) at++;
  if (at >= end || *at != '"') return -EINVAL;
  at++;
  const char *close = memchr(at, '"', (size_t)(end - at));
  if (!close || (size_t)(close - at) >= cap) return -EINVAL;
  memcpy(out, at, (size_t)(close - at));
  out[close - at] = 0;
  return 0;
}

/* "YYYYMMDD-rNN" -> (date, serial); returns 0 on a well formed version. */
static int version_parse(const char *text, long *date, long *serial) {
  char tail[8];
  if (sscanf(text, "%8ld-r%7ld%c", date, serial, tail) != 2) return -EINVAL;
  if (*date < 20000101L || *date > 29991231L || *serial <= 0) return -EINVAL;
  return 0;
}

static int running_partition(char *disk, size_t disk_cap, int *index) {
  struct stat st;
  if (stat("/", &st)) return -errno;
  char link[128];
  (void)snprintf(link, sizeof link, "/sys/dev/block/%u:%u",
                 major(st.st_dev), minor(st.st_dev));
  /* Sysfs links are relative ("../../devices/..."); resolve before opening
   * files below them. */
  char *resolved = realpath(link, NULL);
  if (!resolved) return -errno;
  int result = -EINVAL;
  const char *base = strrchr(resolved, '/');
  char partition[PATH_MAX];
  char value[16];
  if (base && (size_t)snprintf(partition, sizeof partition, "%s/partition", resolved) <
      sizeof partition && !read_all(partition, value, sizeof value, NULL)) {
    int parsed = atoi(value);
    if (parsed >= 1) {
      base++;
      /* Strip the trailing partition index and the 'p' separator that NVMe
       * names carry (nvme0n1p2 -> nvme0n1). */
      size_t cut = strlen(base);
      while (cut && base[cut - 1] >= '0' && base[cut - 1] <= '9') cut--;
      if (cut && base[cut - 1] == 'p') cut--;
      if (cut && cut < disk_cap) {
        memcpy(disk, base, cut);
        disk[cut] = 0;
        *index = parsed;
        result = 0;
      }
    }
  }
  free(resolved);
  return result;
}

static int slot_device(int index, char *out, size_t cap) {
  char disk[64];
  int running = 0;
  int e = running_partition(disk, sizeof disk, &running);
  if (e) return e;
  char candidate[128];
  struct stat st;
  if ((size_t)snprintf(candidate, sizeof candidate, "/dev/%sp%d", disk, index) < sizeof candidate &&
      !stat(candidate, &st) && S_ISBLK(st.st_mode)) {
    if ((size_t)snprintf(out, cap, "%s", candidate) >= cap) return -ENAMETOOLONG;
    return 0;
  }
  if ((size_t)snprintf(candidate, sizeof candidate, "/dev/%s%d", disk, index) < sizeof candidate &&
      !stat(candidate, &st) && S_ISBLK(st.st_mode)) {
    if ((size_t)snprintf(out, cap, "%s", candidate) >= cap) return -ENAMETOOLONG;
    return 0;
  }
  return -ENOENT;
}

static int device_bytes(const char *path, uint64_t *bytes) {
  int fd = open(path, O_RDONLY | O_CLOEXEC);
  if (fd < 0) return -errno;
  uint64_t value = 0;
  if (ioctl(fd, BLKGETSIZE64, &value)) { int e = -errno; close(fd); return e; }
  close(fd);
  *bytes = value;
  return 0;
}

/* Queue the normal reboot target so systemd stops services and unmounts
 * filesystems in dependency order. Manager.Reboot() skips those jobs and
 * enters late shutdown directly. Keep this an IPC request: a direct
 * reboot(2) was measured to wedge the board's SCMI/MCU reset handshake. */
static int request_reboot(void) {
  GError *error = NULL;
  GDBusConnection *bus = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
  if (!bus) {
    fprintf(stderr, "y4-board ota: reboot bus unavailable: %s\n",
            error && error->message ? error->message : "unknown");
    if (error) g_error_free(error);
    return -ENOSYS;
  }
  /* Match the normal shutdown job mode and require the returned job path. */
  GVariant *reply = g_dbus_connection_call_sync(
      bus, "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
      "org.freedesktop.systemd1.Manager", "StartUnit",
      g_variant_new("(ss)", "reboot.target", "replace-irreversibly"),
      G_VARIANT_TYPE("(o)"),
      G_DBUS_CALL_FLAGS_NO_AUTO_START, 30000, NULL, &error);
  if (!reply) {
    fprintf(stderr, "y4-board ota: reboot request failed: %s\n",
            error && error->message ? error->message : "unknown");
    if (error) g_error_free(error);
    g_object_unref(bus);
    return -EIO;
  }
  g_variant_unref(reply);
  g_object_unref(bus);
  return 0;
}

static void ota_release(void) {
  if (ota.pkg_fd >= 0) { close(ota.pkg_fd); ota.pkg_fd = -1; }
  if (ota.out_fd >= 0) { close(ota.out_fd); ota.out_fd = -1; }
  if (ota.verify_fd >= 0) { close(ota.verify_fd); ota.verify_fd = -1; }
  if (ota.k_fd >= 0) { close(ota.k_fd); ota.k_fd = -1; }
  if (ota.digest) { EVP_MD_CTX_free(ota.digest); ota.digest = NULL; }
  if (ota.logical) { EVP_MD_CTX_free(ota.logical); ota.logical = NULL; }
  if (ota.disk_digest) { EVP_MD_CTX_free(ota.disk_digest); ota.disk_digest = NULL; }
  if (ota.k_digest) { EVP_MD_CTX_free(ota.k_digest); ota.k_digest = NULL; }
  free(ota.buffer);
  ota.buffer = NULL;
  free(ota.win);
  ota.win = NULL;
  ota.win_len = 0;
  ota.win_base = 0;
  ZSTD_freeDCtx(ota.dctx);
  ota.dctx = NULL;
  free(ota.zin_buf);
  ota.zin_buf = NULL;
}

static void ota_fail(int code) {
  int during = (int)ota.state;
  ota.code = code ? code : -EIO;
  ota.state = OTA_FAILED;
  ota_release();
  fprintf(stderr, "y4-board ota: failed status=%d during state=%s\n", ota.code, state_name(during));
}

/* ---- steps ---- */

static int step_open(void) {
  struct stat st;
  ota.pkg_fd = open(ota.package, O_RDONLY | O_DIRECT | O_CLOEXEC);
  if (ota.pkg_fd < 0) ota.pkg_fd = open(ota.package, O_RDONLY | O_CLOEXEC);
  if (ota.pkg_fd < 0) return -errno;
  if (fstat(ota.pkg_fd, &st) || !S_ISREG(st.st_mode)) return -EINVAL;
  /* O_DIRECT needs block aligned reads, so the header region is read into an
   * aligned buffer and parsed from there. */
  unsigned char *meta = NULL;
  if (posix_memalign((void **)&meta, 4096, OTA_MANIFEST_MAX + 4096)) return -ENOMEM;
  size_t meta_bytes = 4096;
  ssize_t got = pread(ota.pkg_fd, meta, meta_bytes, 0);
  if (got != (ssize_t)meta_bytes) { free(meta); return -EINVAL; }
  int y12 = !strcmp(Y4_DEVICE_ID, "Y12");
  if (y12 != (memcmp(meta, "Y12PKG03", 8) == 0)) {
    free(meta);
    return -EXDEV;
  }
  int compressed = memcmp(meta, "Y4PKG002", 8) == 0;
  ota.schema = 0;
  if (!compressed && memcmp(meta, "Y4PKG001", 8)) {
    /* Schema 2 (Y4PKG003): the atomic rootfs+kernel pair. Pre-per-slot
     * agents reject this magic outright, which is what keeps old boards
     * from receiving a package they cannot host. */
    if (!y12 && memcmp(meta, "Y4PKG003", 8)) { free(meta); return -EINVAL; }
    ota.schema = 2;
  }
  ota.compressed = compressed;
  uint32_t manifest_bytes, signature_bytes;
  uint64_t payload_bytes;
  memcpy(&manifest_bytes, meta + 8, 4);
  memcpy(&signature_bytes, meta + 12, 4);
  memcpy(&payload_bytes, meta + 16, 8);
  if (!manifest_bytes || manifest_bytes > OTA_MANIFEST_MAX ||
      signature_bytes != OTA_SIG_BYTES || !payload_bytes ||
      payload_bytes > OTA_SLOT_BYTES * 2) { free(meta); return -EINVAL; }
  size_t needed = OTA_HEADER_BYTES + manifest_bytes + signature_bytes;
  if (needed > meta_bytes) {
    size_t bigger = (needed + 4095u) & ~(size_t)4095u;
    unsigned char *again = NULL;
    if (posix_memalign((void **)&again, 4096, bigger)) { free(meta); return -ENOMEM; }
    if (pread(ota.pkg_fd, again, bigger, 0) != (ssize_t)bigger) { free(again); free(meta); return -EINVAL; }
    free(meta);
    meta = again;
    meta_bytes = bigger;
  }
  memcpy(ota.manifest, meta + OTA_HEADER_BYTES, manifest_bytes);
  ota.manifest[manifest_bytes] = 0;
  free(meta);
  ota.manifest_bytes = manifest_bytes;
  ota.payload_bytes = payload_bytes;
  /* The payload starts on a 4 KiB boundary so it can be read with O_DIRECT. */
  ota.payload_offset = (OTA_HEADER_BYTES + manifest_bytes + signature_bytes + 4095u) & ~(uint64_t)4095u;
  if (ota.compressed) {
    /* The file carries the compressed stream; its length is declared in the
     * manifest (compressed_bytes) and is unrelated to the logical payload
     * length, which stays in the header for the chunk walk. */
    if ((uint64_t)st.st_size <= ota.payload_offset) return -EINVAL;
    ota.zpayload_bytes = (uint64_t)st.st_size - ota.payload_offset;
    uint64_t declared = 0;
    if (!json_u64(ota.manifest, "compressed_bytes", &declared) && declared &&
        declared != ota.zpayload_bytes)
      return -EINVAL;
  } else if ((uint64_t)st.st_size != ota.payload_offset + payload_bytes) {
    return -EINVAL;
  }
  return 0;
}

static int step_verify(void) {
  unsigned char key[32], signature[OTA_SIG_BYTES];
  int e = read_exact(OTA_KEY, key, sizeof key);
  if (e) return e;
  /* Read the signature through an aligned window: O_DIRECT rejects the
   * unaligned offset of the raw signature field. */
  size_t sig_at = OTA_HEADER_BYTES + ota.manifest_bytes;
  size_t window = (sig_at + OTA_SIG_BYTES + 4095u) & ~(size_t)4095u;
  unsigned char *meta = NULL;
  if (posix_memalign((void **)&meta, 4096, window)) return -ENOMEM;
  if (pread(ota.pkg_fd, meta, window, 0) != (ssize_t)window) { free(meta); return -EIO; }
  memcpy(signature, meta + sig_at, OTA_SIG_BYTES);
  free(meta);
  EVP_PKEY *pkey = EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519, NULL, key, sizeof key);
  if (!pkey) return -EIO;
  EVP_MD_CTX *ctx = EVP_MD_CTX_new();
  int ok = 0;
  if (ctx && EVP_DigestVerifyInit(ctx, NULL, NULL, NULL, pkey) == 1 &&
      EVP_DigestVerify(ctx, signature, sizeof signature,
                       (const unsigned char *)ota.manifest, ota.manifest_bytes) == 1)
    ok = 1;
  EVP_MD_CTX_free(ctx);
  EVP_PKEY_free(pkey);
  return ok ? 0 : -EKEYREJECTED;
}

static int step_manifest(void) {
  char type[32] = {0};
  uint64_t payload_bytes = 0, schema = 1;
  int e;
  (void)json_u64(ota.manifest, "schema", &schema);
  if ((schema == 2) != (ota.schema == 2)) return -EINVAL;
  if ((e = json_string(ota.manifest, "type", type, sizeof type))) return e;
  if (strcmp(type, "sysroot")) return -EINVAL;
  char board[8] = {0};
  e = json_string(ota.manifest, "board", board, sizeof board);
  if (!e && strcmp(board, Y4_DEVICE_ID)) return -EXDEV;
  if (e && (e != -ENODATA || !strcmp(Y4_DEVICE_ID, "Y12"))) return -EXDEV;

  if ((e = json_string(ota.manifest, "version", ota.version, sizeof ota.version))) return e;
  if ((e = json_string(ota.manifest, "kernel", ota.kernel, sizeof ota.kernel))) return e;
  if ((e = json_string(ota.manifest, "sha256", ota.payload_sha, sizeof ota.payload_sha))) return e;
  if ((e = json_u64(ota.manifest, "bytes", &payload_bytes))) return e;
  if (ota.schema != 2) {
    if (payload_bytes != ota.payload_bytes) return -EINVAL;
    ota.rootfs_bytes = payload_bytes;
    return 0;
  }
  /* Schema 2: the signed payload table is the authority for both halves.
   * The flat top-level fields must agree with payloads.rootfs. */
  const char *payloads, *rootfs, *kern;
  size_t payloads_len, rootfs_len, kern_len;
  if ((e = json_find_object(ota.manifest, ota.manifest_bytes, "payloads",
                            &payloads, &payloads_len))) return e;
  if ((e = json_find_object(payloads, payloads_len, "rootfs", &rootfs, &rootfs_len))) return e;
  if ((e = json_find_object(payloads, payloads_len, "kernel", &kern, &kern_len))) return e;
  uint64_t rbytes = 0, roffset = 1, kbytes = 0, koffset = 1;
  char rsha[65] = {0};
  if ((e = json_u64_in(rootfs, rootfs_len, "bytes", &rbytes))) return e;
  if ((e = json_u64_in(rootfs, rootfs_len, "offset", &roffset))) return e;
  if ((e = json_string_in(rootfs, rootfs_len, "sha256", rsha, sizeof rsha))) return e;
  if (rbytes != payload_bytes || roffset != 0 || strcmp(rsha, ota.payload_sha))
    return -EINVAL;
  ota.rootfs_bytes = rbytes;
  char ksha[65] = {0}, kdv[64] = {0}, krel[96] = {0}, dtb_name[64] = {0};
  uint64_t ibytes = 0, dbytes = 0;
  if ((e = json_u64_in(kern, kern_len, "bytes", &kbytes))) return e;
  if ((e = json_u64_in(kern, kern_len, "offset", &koffset))) return e;
  if ((e = json_string_in(kern, kern_len, "sha256", ksha, sizeof ksha))) return e;
  if ((e = json_u64_in(kern, kern_len, "image_bytes", &ibytes))) return e;
  if ((e = json_u64_in(kern, kern_len, "dtb_bytes", &dbytes))) return e;
  if ((e = json_string_in(kern, kern_len, "delivery_version", kdv, sizeof kdv))) return e;
  if ((e = json_string_in(kern, kern_len, "kernel_release", krel, sizeof krel))) return e;
  if ((e = json_string_in(kern, kern_len, "dtb_name", dtb_name, sizeof dtb_name))) return e;
  if (!kbytes || kbytes > OTA_KERNEL_LIMIT) return -EINVAL;
  if (koffset != ((rbytes + 4095u) & ~(uint64_t)4095u)) return -EINVAL;
  /* The file region behind the kernel pair is padded to 4 KiB (O_DIRECT
   * reads never fall off an unaligned end), so the header total covers the
   * padded region while the manifest keeps the exact kernel length. */
  if (ota.payload_bytes != koffset + ((kbytes + 4095u) & ~(uint64_t)4095u))
    return -EINVAL;
  if (!ibytes || !dbytes || kbytes != ibytes + dbytes) return -EINVAL;
  /* The kernel pair must be the delivery this rootfs belongs to: the
   * manifest binds rootfs version, kernel release and delivery version
   * into one identity (kernel-ota-design 3/4.4). */
  if (strcmp(kdv, ota.version) || strcmp(krel, ota.kernel)) return -EINVAL;
  if (!valid_name(dtb_name, sizeof dtb_name) || !valid_name(kdv, sizeof kdv))
    return -EINVAL;
  (void)snprintf(ota.k_dtb_name, sizeof ota.k_dtb_name, "%s", dtb_name);
  (void)snprintf(ota.k_release, sizeof ota.k_release, "%s", krel);
  (void)snprintf(ota.k_delivery, sizeof ota.k_delivery, "%s", kdv);
  (void)snprintf(ota.k_sha, sizeof ota.k_sha, "%s", ksha);
  ota.k_offset = koffset;
  ota.k_bytes = kbytes;
  ota.k_image_bytes = ibytes;
  ota.k_dtb_bytes = dbytes;
  ota.k_padded = (kbytes + 4095u) & ~(uint64_t)4095u;
  /* The board must already run the per-slot kernel layout: a schema 2
   * package publishes /boot/efi/kernels/<delivery version>/ and rebinds
   * boot_kernel_a/b, which a legacy boot FAT can neither host nor revert
   * (design 6: legacy boards migrate by USB reflash). Refused here, before
   * a single byte of the 7 GiB rootfs is written. */
  struct y4_slot_state layout;
  int le = y4_grubenv_read(Y4_GRUBENV_DEFAULT, &layout);
  if (le || !layout.have_boot_kernel_a) {
    fprintf(stderr, "y4-board ota: schema 2 refused: boot layout is legacy or unreadable (status %d); "
                    "migrate this board by USB reflash\n", le);
    return -EINVAL;
  }
  return 0;
}

static int step_version(void) {
  char release[2048], current[64] = {0};
  if (read_all("/etc/y4-release.json", release, sizeof release, NULL)) return 0;
  if (json_string(release, "rootfs", current, sizeof current)) return 0;
  long have_date = 0, have_serial = 0, want_date = 0, want_serial = 0;
  if (version_parse(current, &have_date, &have_serial) ||
      version_parse(ota.version, &want_date, &want_serial)) return 0;
  if (want_date < have_date || (want_date == have_date && want_serial <= have_serial))
    return -ESTALE;
  return 0;
}

static int step_slot(void) {
  char disk[64];
  int running = 0;
  int e = running_partition(disk, sizeof disk, &running);
  if (e) return e;
  if (running != 2 && running != 3) return -EINVAL;
  ota.target_index = running == 2 ? 3 : 2;
  (void)snprintf(ota.slot, sizeof ota.slot, "%s", ota.target_index == 2 ? "A" : "B");
  return 0;
}

static int step_device(void) {
  int e = slot_device(ota.target_index, ota.device, sizeof ota.device);
  if (e) return e;
  uint64_t bytes = 0;
  if ((e = device_bytes(ota.device, &bytes))) return e;
  if (bytes != OTA_SLOT_BYTES) return -EINVAL;
  return 0;
}

/* Read [pos, pos+need) of the payload through an O_DIRECT window. The
 * buffered read path on this board corrupts large reads (measured), so the
 * package is read with O_DIRECT; a window is required because O_DIRECT
 * needs block aligned offsets and lengths. */
static int window_fill_compressed(uint64_t pos);

static int window_need(uint64_t pos, size_t need) {
  if (ota.compressed) {
    if (pos >= ota.win_base && pos + need <= ota.win_base + (uint64_t)ota.win_len)
      return 0;
    return window_fill_compressed(pos);
  }
  if (pos >= ota.win_base && pos + need <= ota.win_base + (uint64_t)ota.win_len)
    return 0;
  uint64_t base = pos & ~(uint64_t)4095u;
  /* The rootfs walk is bounded by its own payload region: in a schema 2
   * package the file continues with the kernel payload, which the trailing
   * pad handling must never swallow (measured: an unaligned kernel length
   * made the final sub-4 KiB window request zero bytes -> -EIO). */
  if (base >= ota.rootfs_bytes) return -EIO;
  size_t want = OTA_IO_CHUNK + 4096u;
  if (base + (uint64_t)want > ota.rootfs_bytes)
    want = (size_t)(ota.rootfs_bytes - base);
  want &= ~(size_t)4095u;   /* ext4 O_DIRECT needs block aligned lengths */
  if (!want) return -EIO;
  ssize_t n = pread(ota.pkg_fd, ota.win, want, (off_t)(ota.payload_offset + base));
  if (n <= 0) return -EIO;
  if (ota.win_len) {
    /* Long sequential buffered reads on this kernel return corrupted data
     * (measured). Dropping each window right after its last use keeps every
     * read short and faithful; the numbers below are the measured-stable
     * parameters: 64 KiB reads, 64 MiB windows, DONTNEED after use. */
    (void)posix_fadvise(ota.pkg_fd, (off_t)(ota.payload_offset + ota.win_base),
                        (off_t)ota.win_len, POSIX_FADV_DONTNEED);
  }
  ota.win_base = base;
  ota.win_len = (size_t)n;
  if (pos + need > ota.win_base + (uint64_t)ota.win_len) return -EIO;
  return 0;
}

static int step_write_start(void) {
  /* The buffered write path on this board corrupts large writes (measured:
   * random single-bit flips, reproducible with plain cp). O_DIRECT bypasses
   * the page cache and was byte-exact in the same test, so the slot is
   * written directly and read back for verification afterwards. */
  ota.out_fd = open(ota.device, O_WRONLY | O_DIRECT | O_CLOEXEC);
  if (ota.out_fd < 0) ota.out_fd = open(ota.device, O_WRONLY | O_CLOEXEC);
  if (ota.out_fd < 0) return -errno;
  ota.digest = EVP_MD_CTX_new();
  ota.logical = EVP_MD_CTX_new();
  if (!ota.digest || !ota.logical ||
      EVP_DigestInit_ex(ota.digest, EVP_sha256(), NULL) != 1 ||
      EVP_DigestInit_ex(ota.logical, EVP_sha256(), NULL) != 1) return -EIO;
  if (posix_memalign((void **)&ota.buffer, 4096, OTA_IO_CHUNK)) { ota.buffer = NULL; return -ENOMEM; }
  if (posix_memalign((void **)&ota.win, 4096, OTA_IO_CHUNK + 4096u)) { ota.win = NULL; return -ENOMEM; }
  ota.verify_at = 0;
  ota.win_base = 0;
  ota.win_len = 0;
  if (ota.compressed) {
    /* The decompressed-window reader is needed for the very first window
     * (the sparse header lives inside the compressed stream), so the zstd
     * context and its input buffer must exist before window_need runs.
     * zpayload_bytes was already measured in step_open. */
    ota.zcursor_raw = 0;
    ota.zstd_done = 0;
    memset(&ota.zin, 0, sizeof ota.zin);
    ota.dctx = ZSTD_createDCtx();
    if (!ota.dctx) return -EIO;
    if (posix_memalign((void **)&ota.zin_buf, 4096, OTA_IO_CHUNK + 4096u)) {
      ZSTD_freeDCtx(ota.dctx);
      ota.dctx = NULL;
      return -ENOMEM;
    }
  }
  int e = window_need(0, 28);
  if (e) return e;
  const unsigned char *header = ota.win;
  ota.cursor = 28;
  if (EVP_DigestUpdate(ota.digest, header, 28) != 1) return -EIO;
  uint32_t magic, total_blks;
  uint16_t major, minor, file_hdr_sz, chunk_hdr_sz;
  memcpy(&magic, header, 4); memcpy(&major, header + 4, 2); memcpy(&minor, header + 6, 2);
  memcpy(&file_hdr_sz, header + 8, 2); memcpy(&chunk_hdr_sz, header + 10, 2);
  memcpy(&ota.blk_sz, header + 12, 4); memcpy(&total_blks, header + 16, 4);
  memcpy(&ota.total_chunks, header + 20, 4);
  if (magic != SPARSE_MAGIC || major != 1 || file_hdr_sz != 28 || chunk_hdr_sz != 12 ||
      !ota.blk_sz || ota.blk_sz > OTA_IO_CHUNK || (ota.blk_sz & (ota.blk_sz - 1)))
    return -EINVAL;
  /* The image may be shorter than the slot (the boot-time grow fills the
   * rest), exactly like a fastboot flash; it may never be longer. */
  ota.image_total = (uint64_t)total_blks * ota.blk_sz;
  if (!ota.image_total || ota.image_total > OTA_SLOT_BYTES) return -EINVAL;
  if (!ota.total_chunks) return -EINVAL;
  ota.chunk_index = 0;
  ota.chunk_left = 0;
  ota.chunk_header_done = 1;
  return 0;
}

/* Make ota.win hold the decompressed logical stream starting at pos. The
 * chunk walk below only ever moves forward, so refills slide the window:
 * bytes at or after pos are kept, everything before them is dropped, and
 * the zstd stream is pumped until the window is full or the frame ends.
 * The compressed input is read from the package through a separate aligned
 * buffer; ota.win is never used as both zstd input and output. */
static int window_fill_compressed(uint64_t pos) {
  if (pos >= ota.win_base && pos <= ota.win_base + (uint64_t)ota.win_len) {
    size_t keep = (size_t)(ota.win_base + (uint64_t)ota.win_len - pos);
    if (keep && pos != ota.win_base)
      memmove(ota.win, ota.win + (pos - ota.win_base), keep);
    ota.win_base = pos;
    ota.win_len = keep;
  } else {
    ota.win_base = pos;
    ota.win_len = 0;
  }
  while (ota.win_len < OTA_IO_CHUNK && !ota.zstd_done) {
    if (ota.zin.pos >= ota.zin.size) {
      uint64_t remain = ota.zpayload_bytes - ota.zcursor_raw;
      if (!remain) break;
      size_t want = remain < (uint64_t)OTA_IO_CHUNK ? (size_t)remain : OTA_IO_CHUNK;
      want &= ~(size_t)4095u;
      if (!want) break;
      ssize_t n = pread(ota.pkg_fd, ota.zin_buf, want,
                        (off_t)(ota.payload_offset + ota.zcursor_raw));
      if (n <= 0) return n < 0 ? -errno : -EIO;
      ota.zcursor_raw += (uint64_t)n;
      ota.zin.src = ota.zin_buf;
      ota.zin.size = (size_t)n;
      ota.zin.pos = 0;
    }
    ZSTD_outBuffer out;
    out.dst = ota.win + ota.win_len;
    out.size = OTA_IO_CHUNK + 4096u - ota.win_len;
    out.pos = 0;
    size_t rc = ZSTD_decompressStream(ota.dctx, &out, &ota.zin);
    if (ZSTD_isError(rc)) return -EIO;
    ota.win_len += out.pos;
    if (rc == 0) ota.zstd_done = 1;   /* frame complete */
  }
  return ota.win_len ? 0 : -EIO;
}

static int slot_write(const unsigned char *data, size_t bytes, uint64_t at) {
  size_t done = 0;
  while (done < bytes) {
    ssize_t n = pwrite(ota.out_fd, data + done, bytes - done, (off_t)(at + done));
    if (n < 0) { if (errno == EINTR) continue; return -errno; }
    done += (size_t)n;
  }
  return 0;
}

/* Process up to OTA_TICK_BYTES of the payload. Returns 0 while more work
 * remains, 1 when the payload is fully written, or a negative errno. */
static int step_write_slice(void) {
  int e = 0;
  uint64_t budget = OTA_TICK_BYTES;
  while (budget) {
    if (ota.chunk_index >= ota.total_chunks) {
      /* Trailing padding up to the rootfs payload end is part of the
       * signed hash; the kernel payload beyond it is a separate stage. */
      if (ota.cursor >= ota.rootfs_bytes) break;
      size_t take = (size_t)(ota.rootfs_bytes - ota.cursor);
      if (take > OTA_IO_CHUNK) take = OTA_IO_CHUNK;
      int e2 = window_need(ota.cursor, take);
      if (e2) return e2;
      size_t avail = (size_t)(ota.win_base + (uint64_t)ota.win_len - ota.cursor);
      if (avail < take) take = avail;
      if (!take) return -EIO;
      if (EVP_DigestUpdate(ota.digest, ota.win + (ota.cursor - ota.win_base), take) != 1) return -EIO;
      ota.cursor += take;
      budget -= take;
      continue;
    }
    if (ota.chunk_header_done) {
      e = window_need(ota.cursor, 12);
      if (e) return e;
      unsigned char chunk_header[12];
      memcpy(chunk_header, ota.win + (ota.cursor - ota.win_base), 12);
      ota.cursor += sizeof chunk_header;
      if (EVP_DigestUpdate(ota.digest, chunk_header, sizeof chunk_header) != 1) return -EIO;
      uint16_t type;
      uint32_t chunk_sz, total_sz;
      memcpy(&type, chunk_header, 2); memcpy(&chunk_sz, chunk_header + 4, 4);
      memcpy(&total_sz, chunk_header + 8, 4);
      uint64_t bytes = (uint64_t)chunk_sz * ota.blk_sz;
      if (type == SPARSE_RAW) {
        if (total_sz != 12u + bytes) return -EINVAL;
      } else if (type == SPARSE_FILL) {
        if (total_sz != 16u) return -EINVAL;
      } else if (type == SPARSE_DONT_CARE) {
        if (total_sz != 12u) return -EINVAL;
      } else if (type == SPARSE_CRC32) {
        if (total_sz != 16u) return -EINVAL;
      } else return -EINVAL;
      ota.chunk_type = (int)type;
      ota.chunk_left = bytes;
      ota.chunk_header_done = 0;
      if (bytes > ota.image_total - ota.written) return -EINVAL;
    }
    if (ota.chunk_type == SPARSE_RAW) {
      size_t take = ota.chunk_left > budget ? (size_t)budget : (size_t)ota.chunk_left;
      if (take > OTA_IO_CHUNK) take = OTA_IO_CHUNK;
      take &= ~(size_t)511u;
      if (!take) return -EINVAL;
      e = window_need(ota.cursor, take);
      if (e) return e;
      size_t avail = (size_t)(ota.win_base + (uint64_t)ota.win_len - ota.cursor);
      if (avail < take) take = avail & ~(size_t)511u;
      if (!take) return -EINVAL;
      memcpy(ota.buffer, ota.win + (ota.cursor - ota.win_base), take);
      ota.cursor += take;
      if (EVP_DigestUpdate(ota.digest, ota.buffer, take) != 1) return -EIO;
      if (EVP_DigestUpdate(ota.logical, ota.buffer, take) != 1) return -EIO;
      if ((e = slot_write(ota.buffer, take, ota.written))) return e;
      ota.written += take;
      ota.chunk_left -= take;
      budget -= take;
    } else if (ota.chunk_type == SPARSE_FILL) {
      e = window_need(ota.cursor, 4);
      if (e) return e;
      unsigned char pattern[4];
      memcpy(pattern, ota.win + (ota.cursor - ota.win_base), 4);
      ota.cursor += sizeof pattern;
      if (EVP_DigestUpdate(ota.digest, pattern, sizeof pattern) != 1) return -EIO;
      for (size_t i = 0; i < OTA_IO_CHUNK; i++) ota.buffer[i] = pattern[i & 3u];
      uint64_t left = ota.chunk_left;
      while (left) {
        size_t take = left > OTA_IO_CHUNK ? OTA_IO_CHUNK : (size_t)left;
        take &= ~(size_t)511u;
        if (!take) take = (size_t)left;
        if (EVP_DigestUpdate(ota.logical, ota.buffer, take) != 1) return -EIO;
        if ((e = slot_write(ota.buffer, take, ota.written))) return e;
        ota.written += take;
        left -= take;
      }
      budget = budget > ota.chunk_left ? budget - ota.chunk_left : 0;
      ota.chunk_left = 0;
    } else if (ota.chunk_type == SPARSE_DONT_CARE) {
      /* Zero the region so the read-back verification is exact. */
      memset(ota.buffer, 0, OTA_IO_CHUNK);
      uint64_t left = ota.chunk_left;
      while (left) {
        size_t take = left > OTA_IO_CHUNK ? OTA_IO_CHUNK : (size_t)left;
        take &= ~(size_t)511u;
        if (!take) take = (size_t)left;
        if (EVP_DigestUpdate(ota.logical, ota.buffer, take) != 1) return -EIO;
        if ((e = slot_write(ota.buffer, take, ota.written))) return e;
        ota.written += take;
        left -= take;
      }
      budget = budget > ota.chunk_left ? budget - ota.chunk_left : 0;
      ota.chunk_left = 0;
    } else { /* SPARSE_CRC32 */
      e = window_need(ota.cursor, 4);
      if (e) return e;
      unsigned char crc[4];
      memcpy(crc, ota.win + (ota.cursor - ota.win_base), 4);
      ota.cursor += sizeof crc;
      if (EVP_DigestUpdate(ota.digest, crc, sizeof crc) != 1) return -EIO;
      ota.written += ota.chunk_left;
      ota.chunk_left = 0;
    }
    if (!ota.chunk_left) { ota.chunk_index++; ota.chunk_header_done = 1; }
    if (ota.written > ota.image_total) return -EINVAL;
  }
  return ota.chunk_index >= ota.total_chunks && !ota.chunk_left &&
         ota.cursor >= ota.rootfs_bytes ? 1 : 0;
}

static int step_hash(void) {
  if (ota.written != ota.image_total) return -EINVAL;
  if (ota.cursor > ota.rootfs_bytes) return -EINVAL;
  if (fsync(ota.out_fd)) return -errno;
  unsigned char digest[32];
  unsigned length = 0;
  if (EVP_DigestFinal_ex(ota.digest, digest, &length) != 1 || length != 32u) return -EIO;
  for (unsigned i = 0; i < 32u; i++) (void)snprintf(ota.payload_hash + i * 2u, 3, "%02x", digest[i]);
  ota.payload_hash[64] = 0;
  if (strcmp(ota.payload_hash, ota.payload_sha)) return -EBADMSG;
  unsigned llength = 0;
  unsigned char ldigest[32];
  if (EVP_DigestFinal_ex(ota.logical, ldigest, &llength) != 1 || llength != 32u) return -EIO;
  for (unsigned i = 0; i < 32u; i++) (void)snprintf(ota.logical_hash + i * 2u, 3, "%02x", ldigest[i]);
  ota.logical_hash[64] = 0;
  EVP_MD_CTX_free(ota.logical);
  ota.logical = NULL;
  EVP_MD_CTX_free(ota.digest);
  ota.digest = NULL;
  if (close(ota.out_fd)) { ota.out_fd = -1; return -errno; }
  ota.out_fd = -1;
  /* ota.buffer stays allocated: the read-back verification reuses it. */
  return 0;
}

/* Read the written slot back with O_DIRECT and compare against the logical
 * hash of what the package describes: this is what makes the update safe on
 * a platform whose buffered write path can flip bits. */
static int step_readback_slice(void) {
  if (ota.verify_fd < 0) {
    ota.verify_fd = open(ota.device, O_RDONLY | O_DIRECT | O_CLOEXEC);
    if (ota.verify_fd < 0) ota.verify_fd = open(ota.device, O_RDONLY | O_CLOEXEC);
    if (ota.verify_fd < 0) return -errno;
    ota.disk_digest = EVP_MD_CTX_new();
    if (!ota.disk_digest || EVP_DigestInit_ex(ota.disk_digest, EVP_sha256(), NULL) != 1) return -EIO;
    ota.verify_at = 0;
  }
  uint64_t budget = OTA_TICK_BYTES;
  while (budget && ota.verify_at < ota.image_total) {
    size_t take = OTA_IO_CHUNK;
    if ((uint64_t)take > ota.image_total - ota.verify_at)
      take = (size_t)(ota.image_total - ota.verify_at);
    if ((uint64_t)take > budget) take = (size_t)budget;
    ssize_t n = pread(ota.verify_fd, ota.buffer, take, (off_t)ota.verify_at);
    if (n < 0) { if (errno == EINTR) continue; return -errno; }
    if (!n) return -EIO;
    if (EVP_DigestUpdate(ota.disk_digest, ota.buffer, (size_t)n) != 1) return -EIO;
    ota.verify_at += (uint64_t)n;
    budget -= (uint64_t)n;
  }
  if (ota.verify_at < ota.image_total) return 0;
  unsigned length = 0;
  unsigned char digest[32];
  if (EVP_DigestFinal_ex(ota.disk_digest, digest, &length) != 1 || length != 32u) return -EIO;
  char hex[65];
  for (unsigned i = 0; i < 32u; i++) (void)snprintf(hex + i * 2u, 3, "%02x", digest[i]);
  hex[64] = 0;
  if (strcmp(hex, ota.logical_hash)) {
    fprintf(stderr, "y4-board ota: slot read-back mismatch\n");
    return -EBADMSG;
  }
  EVP_MD_CTX_free(ota.disk_digest);
  ota.disk_digest = NULL;
  if (ota.verify_fd >= 0) { close(ota.verify_fd); ota.verify_fd = -1; }
  free(ota.buffer);
  ota.buffer = NULL;
  free(ota.zin_buf);
  ota.zin_buf = NULL;
  return 1;
}

/* ---- schema 2: kernel payload into the boot FAT (design N1-N6) ---- */

/* Hold the kernel region of the package in the shared O_DIRECT window.
 * win_base is region-relative here; the rootfs walk is already finished. */
static int kernel_window(uint64_t pos, size_t need) {
  if (pos >= ota.win_base && pos + need <= ota.win_base + (uint64_t)ota.win_len)
    return 0;
  uint64_t base = pos & ~(uint64_t)4095u;
  if (base >= ota.k_padded) return -EIO;
  size_t want = OTA_IO_CHUNK + 4096u;
  if (base + (uint64_t)want > ota.k_padded)
    want = (size_t)(ota.k_padded - base);
  want &= ~(size_t)511u;
  if (!want) return -EIO;
  if (ota.win_len) {
    (void)posix_fadvise(ota.pkg_fd,
                        (off_t)(ota.payload_offset + ota.k_offset + ota.win_base),
                        (off_t)ota.win_len, POSIX_FADV_DONTNEED);
  }
  ssize_t n = pread(ota.pkg_fd, ota.win, want,
                    (off_t)(ota.payload_offset + ota.k_offset + base));
  if (n <= 0) return -EIO;
  ota.win_base = base;
  ota.win_len = (size_t)n;
  if (pos + need > ota.win_base + (uint64_t)ota.win_len) return -EIO;
  return 0;
}

static int write_all_fd(int fd, const unsigned char *data, size_t bytes) {
  size_t done = 0;
  while (done < bytes) {
    ssize_t n = write(fd, data + done, bytes - done);
    if (n < 0) { if (errno == EINTR) continue; return -errno; }
    done += (size_t)n;
  }
  return 0;
}

/* nftw callback: depth-first removal of a staging or GC'd directory. */
static int remove_walk(const char *fpath, const struct stat *st, int type, struct FTW *ftw) {
  (void)st; (void)type; (void)ftw;
  return remove(fpath);
}

/* Remove leftover <delivery>.new-<id> staging directories: a run killed
 * mid-publish leaves one behind and the next run or the next healthy
 * confirm cleans it (design 4.3/N4). */
static int kernels_clean_staging(void) {
  DIR *dir = opendir(OTA_KERNELS_DIR);
  if (!dir) return 0;
  struct dirent *entry;
  char path[PATH_MAX];
  while ((entry = readdir(dir))) {
    if (!strstr(entry->d_name, ".new-")) continue;
    if ((size_t)snprintf(path, sizeof path, OTA_KERNELS_DIR "/%s", entry->d_name)
        >= sizeof path) continue;
    fprintf(stderr, "y4-board ota: removing stale kernel staging %s\n", entry->d_name);
    (void)nftw(path, remove_walk, 16, FTW_PHYS | FTW_DEPTH);
  }
  closedir(dir);
  /* Publish the removals so a reset cannot resurrect them. */
  int fd = open(OTA_KERNELS_DIR, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (fd >= 0) { (void)fsync(fd); close(fd); }
  return 0;
}

/* Stream one slice of the kernel payload: hash it, write the Image file
 * and then the DTB file into the staging directory. FAT has no atomic
 * directory publish, so safety comes from ordering: the grubenv is only
 * touched after the renamed directory has been verified in place. */
static int step_kernel_write_slice(void) {
  if (!ota.k_digest) {
    /* N1: the boot FAT must hold the payload plus the safety margin
     * before anything is created. */
    struct statvfs space;
    if (statvfs("/boot/efi", &space)) return -errno;
    if ((uint64_t)space.f_bavail * (uint64_t)space.f_frsize <
        ota.k_bytes + OTA_KERNEL_MARGIN) {
      fprintf(stderr, "y4-board ota: boot FAT lacks space for the kernel payload\n");
      return -ENOSPC;
    }
    (void)kernels_clean_staging();
    if ((size_t)snprintf(ota.k_stage_dir, sizeof ota.k_stage_dir,
                         OTA_KERNELS_DIR "/%s.new-%.8s", ota.k_delivery, ota.id)
        >= sizeof ota.k_stage_dir) return -ENAMETOOLONG;
    if (mkdir(ota.k_stage_dir, 0755) && errno != EEXIST) return -errno;
    char file[PATH_MAX];
    if ((size_t)snprintf(file, sizeof file, "%s/Image", ota.k_stage_dir)
        >= sizeof file) return -ENAMETOOLONG;
    ota.k_fd = open(file, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (ota.k_fd < 0) return -errno;
    ota.k_digest = EVP_MD_CTX_new();
    if (!ota.k_digest || EVP_DigestInit_ex(ota.k_digest, EVP_sha256(), NULL) != 1)
      return -EIO;
    ota.k_cursor = 0;
    ota.k_written = 0;
    ota.k_write_phase = 0;  /* 0 = Image, 1 = DTB */
  }
  uint64_t budget = OTA_TICK_BYTES;
  while (budget) {
    if (ota.k_cursor >= ota.k_bytes) break;
    size_t take = OTA_IO_CHUNK;
    if ((uint64_t)take > ota.k_bytes - ota.k_cursor)
      take = (size_t)(ota.k_bytes - ota.k_cursor);
    if ((uint64_t)take > budget) take = (size_t)budget;
    int e = kernel_window(ota.k_cursor, take);
    if (e) return e;
    size_t avail = (size_t)(ota.win_base + (uint64_t)ota.win_len - ota.k_cursor);
    if (avail < take) return -EIO;
    const unsigned char *src = ota.win + (ota.k_cursor - ota.win_base);
    /* Split the window at the Image/DTB boundary; the tail continues into
     * the next file on the next loop iteration. */
    uint64_t file_left = ota.k_write_phase == 0
        ? ota.k_image_bytes - ota.k_written
        : ota.k_dtb_bytes - ota.k_written;
    size_t n = take < file_left ? (size_t)take : (size_t)file_left;
    int e2 = write_all_fd(ota.k_fd, src, n);
    if (e2) return e2;
    if (EVP_DigestUpdate(ota.k_digest, src, n) != 1) return -EIO;
    ota.k_cursor += n;
    ota.k_written += n;
    budget -= n;
    if (ota.k_written == (ota.k_write_phase == 0 ? ota.k_image_bytes : ota.k_dtb_bytes)) {
      if (fsync(ota.k_fd)) return -errno;
      if (close(ota.k_fd)) { ota.k_fd = -1; return -errno; }
      ota.k_fd = -1;
      if (ota.k_write_phase == 0 && ota.k_cursor < ota.k_bytes) {
        char file[PATH_MAX];
        if ((size_t)snprintf(file, sizeof file, "%s/%s", ota.k_stage_dir, ota.k_dtb_name)
            >= sizeof file) return -ENAMETOOLONG;
        ota.k_fd = open(file, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
        if (ota.k_fd < 0) return -errno;
        ota.k_write_phase = 1;
        ota.k_written = 0;
      }
    }
  }
  if (ota.k_cursor < ota.k_bytes) return 0;
  if (ota.k_fd >= 0) {
    if (fsync(ota.k_fd)) return -errno;
    if (close(ota.k_fd)) { ota.k_fd = -1; return -errno; }
    ota.k_fd = -1;
  }
  return 1;
}

/* One "Linux version <token>" candidate: 1 = judged, 0 = cut short (the
 * next window re-sees it), -1 = a version-shaped mismatch. */
static int uts_evaluate(const unsigned char *window, size_t len) {
  static const char token[] = "Linux version ";
  const size_t token_len = sizeof token - 1;
  const unsigned char *rest = window + token_len;
  size_t avail = len - token_len;
  size_t end = avail;
  for (size_t i = 0; i < avail; i++) {
    unsigned char c = rest[i];
    if (c == ' ' || c == '(' || c == '\n' || c == '\t' || c == 0) { end = i; break; }
  }
  if (end == avail) return 0;
  /* Printf templates ("Linux version %s") are not announcements; the same
   * rule the packing gate applies. */
  if (!end || !isdigit(rest[0]) || memchr(rest, '%', end)) return 1;
  if (end != strlen(ota.k_release) || memcmp(rest, ota.k_release, end)) return -1;
  ota.uts_found++;
  return 1;
}

/* N3 (part 1): stream the Image region of the payload and count the
 * version announcements. The 128-byte carry keeps a token split across
 * chunk boundaries contiguous in the next window. */
static int step_kernel_verify_slice(void) {
  if (!ota.buffer) {
    if (posix_memalign((void **)&ota.buffer, 4096, OTA_IO_CHUNK + 4096u))
      { ota.buffer = NULL; return -ENOMEM; }
    ota.uts_pos = 0;
    ota.uts_tail_len = 0;
    ota.uts_found = 0;
  }
  static const char token[] = "Linux version ";
  const size_t token_len = sizeof token - 1;
  if (ota.uts_pos < ota.k_image_bytes) {
    uint64_t left = ota.k_image_bytes - ota.uts_pos;
    size_t len = OTA_IO_CHUNK - 128u;
    if ((uint64_t)len > left) len = (size_t)left;
    /* O_DIRECT reads need 512-byte lengths; a final partial sector reads
     * the whole sector (the payload region is padded to 4 KiB). */
    size_t aligned = len & ~(size_t)511u;
    len = aligned ? aligned : 512;
    ssize_t n = pread(ota.pkg_fd, ota.buffer + 128, len,
                      (off_t)(ota.payload_offset + ota.k_offset + ota.uts_pos));
    if (n <= 0) return -EIO;
    size_t window_len = ota.uts_tail_len + (size_t)n;
    size_t at = 0;
    while (at + token_len <= window_len) {
      const unsigned char *hit = memmem(ota.buffer + at, window_len - at,
                                        token, token_len);
      if (!hit) break;
      size_t pos = (size_t)(hit - ota.buffer);
      int r = uts_evaluate(ota.buffer + pos, window_len - pos);
      if (r < 0) {
        fprintf(stderr, "y4-board ota: kernel Image announces a release other than %s\n",
                ota.k_release);
        return -EINVAL;
      }
      if (r == 0) break;   /* cut short; the next window re-sees it whole */
      at = pos + token_len;
    }
    size_t keep = window_len < 128u ? window_len : 128u;
    memmove(ota.buffer, ota.buffer + window_len - keep, keep);
    ota.uts_tail_len = keep;
    ota.uts_pos += (uint64_t)n;
    return 0;
  }
  /* N3 (part 2): the streamed digest must equal the manifest, and the
   * Image must have announced its release at least once. */
  unsigned char digest[32];
  unsigned length = 0;
  if (EVP_DigestFinal_ex(ota.k_digest, digest, &length) != 1 || length != 32u) return -EIO;
  for (unsigned i = 0; i < 32u; i++)
    (void)snprintf(ota.k_hash + i * 2u, 3, "%02x", digest[i]);
  ota.k_hash[64] = 0;
  EVP_MD_CTX_free(ota.k_digest);
  ota.k_digest = NULL;
  if (strcmp(ota.k_hash, ota.k_sha)) return -EBADMSG;
  if (!ota.uts_found) {
    fprintf(stderr, "y4-board ota: kernel Image does not announce release %s\n", ota.k_release);
    return -EINVAL;
  }
  free(ota.buffer);
  ota.buffer = NULL;
  return 1;
}

/* N4: verify the staged pair, rename it into place and confirm the
 * published directory before anything touches the grubenv. */
static int step_kernel_publish(void) {
  char file[PATH_MAX];
  struct stat st;
  if ((size_t)snprintf(file, sizeof file, "%s/Image", ota.k_stage_dir) >= sizeof file)
    return -ENAMETOOLONG;
  if (stat(file, &st) || !S_ISREG(st.st_mode) || (uint64_t)st.st_size != ota.k_image_bytes)
    return -EIO;
  if ((size_t)snprintf(file, sizeof file, "%s/%s", ota.k_stage_dir, ota.k_dtb_name)
      >= sizeof file) return -ENAMETOOLONG;
  if (stat(file, &st) || !S_ISREG(st.st_mode) || (uint64_t)st.st_size != ota.k_dtb_bytes)
    return -EIO;
  char final_dir[PATH_MAX];
  if ((size_t)snprintf(final_dir, sizeof final_dir, OTA_KERNELS_DIR "/%s", ota.k_delivery)
      >= sizeof final_dir) return -ENAMETOOLONG;
  if (stat(final_dir, &st) == 0) return -EEXIST;
  if (rename(ota.k_stage_dir, final_dir)) return -errno;
  int fd = open(OTA_KERNELS_DIR, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (fd >= 0) { (void)fsync(fd); close(fd); }
  ota.k_stage_dir[0] = 0;
  (void)kernels_clean_staging();
  fprintf(stderr, "y4-board ota: kernel %s published as %s\n", ota.k_release, final_dir);
  return 0;
}

/* Delete unreferenced delivery directories after a healthy confirm,
 * keeping the two slot bindings, the running delivery and (on a legacy
 * layout) the single boot_kernel directory (design 4.3 GC). The
 * Nothing unreferenced survives a confirm, so the live count stays
 * within the referenced set (at most the two slot bindings, the running
 * delivery and a legacy binding). */
int y4_board_kernels_gc(void) {
  struct y4_slot_state st;
  if (y4_grubenv_read(Y4_GRUBENV_DEFAULT, &st)) return 0;
  char release[2048], current[64] = {0};
  if (!read_all("/etc/y4-release.json", release, sizeof release, NULL))
    (void)json_string(release, "rootfs", current, sizeof current);
  const char *keep[4];
  unsigned keep_n = 0;
  if (st.have_boot_kernel_a) keep[keep_n++] = st.boot_kernel_a;
  if (st.have_boot_kernel_b) keep[keep_n++] = st.boot_kernel_b;
  if (current[0]) keep[keep_n++] = current;
  if (st.have_boot_kernel) keep[keep_n++] = st.boot_kernel;
  struct candidate { char name[96]; time_t mtime; };
  struct candidate *cands = NULL;
  unsigned cands_n = 0;
  DIR *dir = opendir(OTA_KERNELS_DIR);
  if (!dir) return 0;
  struct dirent *entry;
  while ((entry = readdir(dir))) {
    if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
    char path[PATH_MAX];
    if ((size_t)snprintf(path, sizeof path, OTA_KERNELS_DIR "/%s", entry->d_name)
        >= sizeof path) continue;
    struct stat st2;
    if (stat(path, &st2) || !S_ISDIR(st2.st_mode)) continue;
    if (strstr(entry->d_name, ".new-")) {
      fprintf(stderr, "y4-board ota: GC removes stale kernel staging %s\n", entry->d_name);
      (void)nftw(path, remove_walk, 16, FTW_PHYS | FTW_DEPTH);
      continue;
    }
    int kept = 0;
    for (unsigned i = 0; i < keep_n; i++)
      if (!strcmp(keep[i], entry->d_name)) { kept = 1; break; }
    if (kept) continue;
    struct candidate *grown = realloc(cands, (cands_n + 1) * sizeof *cands);
    if (!grown) continue;
    cands = grown;
    (void)snprintf(cands[cands_n].name, sizeof cands[cands_n].name, "%s", entry->d_name);
    cands[cands_n].mtime = st2.st_mtime;
    cands_n++;
  }
  closedir(dir);
  /* Every unreferenced delivery goes (design 4.3: a directory no slot
   * binding and no running release points at can never be booted again,
   * and a retry of the same delivery version needs its name back); the
   * Oldest first, so a failure mid-loop leaves the newest candidates
   * for the next confirm. */
  for (unsigned i = 0; i + 1 < cands_n; i++)
    for (unsigned j = i + 1; j < cands_n; j++)
      if (cands[j].mtime < cands[i].mtime) {
        struct candidate tmp = cands[i]; cands[i] = cands[j]; cands[j] = tmp;
      }
  for (unsigned i = 0; i < cands_n; i++) {
    char path[PATH_MAX];
    if ((size_t)snprintf(path, sizeof path, OTA_KERNELS_DIR "/%s", cands[i].name)
        >= sizeof path) continue;
    fprintf(stderr, "y4-board ota: GC removes kernel delivery %s\n", cands[i].name);
    (void)nftw(path, remove_walk, 16, FTW_PHYS | FTW_DEPTH);
  }
  free(cands);
  return 0;
}

static int step_grubenv(void) {
  struct y4_slot_state current = {0}, state;
  int e = y4_grubenv_read(Y4_GRUBENV_DEFAULT, &current);
  int new_layout = e == 0 && current.have_boot_kernel_a;
  if (e && e != -ENOENT) return e;   /* unreadable: never guess bindings */
  if (!new_layout) {
    /* Legacy board: the historical single-variable behaviour, unchanged.
       A schema 2 package cannot reach this point (refused in
       step_manifest), so boot_kernel is what the running grub.cfg reads. */
    state = (struct y4_slot_state){0};
    state.have_default = 1;
    state.default_entry = ota.target_index == 2 ? 0 : 1;
    state.have_slot = 1;
    state.slot[0] = ota.target_index == 2 ? 'A' : 'B';
    state.have_boot_kernel = 1;
    (void)snprintf(state.boot_kernel, sizeof state.boot_kernel, "%s", ota.kernel);
  } else {
    /* New layout: preserve the other slot's binding and any legacy key,
       update only this slot's kernel directory plus the boot counters. */
    state = current;
    state.have_default = 1;
    state.default_entry = ota.target_index == 2 ? 0 : 1;
    state.have_slot = 1;
    state.slot[0] = ota.target_index == 2 ? 'A' : 'B';
    if (ota.schema == 2) {
      char *bound = ota.target_index == 2 ? state.boot_kernel_a : state.boot_kernel_b;
      (void)snprintf(bound, sizeof state.boot_kernel_a, "%s", ota.k_delivery);
    } else {
      /* Schema 1 compatibility path (design V7): the rootfs-only package
         must match the kernel directory this slot will keep booting. */
      const char *bound = ota.target_index == 2 ? state.boot_kernel_a : state.boot_kernel_b;
      if (strcmp(bound, ota.kernel)) {
        fprintf(stderr, "y4-board ota: rootfs-only package built for kernel %s but slot %c "
                        "is bound to kernel directory %s; refusing a mismatched pair\n",
                ota.kernel, ota.target_index == 2 ? 'A' : 'B', bound);
        return -EINVAL;
      }
    }
  }
  state.boot_count = 0;
  state.boot_ok = 0;
  const char *const paths[] = {Y4_GRUBENV_DEFAULT, Y4_GRUBENV_BACKUP, NULL};
  return y4_grubenv_write(paths, &state);
}

/* ---- public API used by api.c ---- */

int y4_board_ota_phase(void) {
  return ota.state;
}

int y4_board_ota_busy(void) {
  return ota.state != OTA_IDLE && ota.state != OTA_FAILED;
}

static int release_upload(void) {
  int error = 0;
  if (ota_upload_fd >= 0) {
    if (close(ota_upload_fd)) error = -errno;
    ota_upload_fd = -1;
  }
  free(ota_upload_stage);
  ota_upload_stage = NULL;
  EVP_MD_CTX_free(ota_upload_digest);
  ota_upload_digest = NULL;
  ota_upload_used = 0;
  ota_received = 0;
  return error;
}

/* An accepted upload owns the staging path until finish transfers the package
 * to the apply state machine. Discard it on every failure while still owned. */
static void discard_upload(void) {
  (void)release_upload();
  if (unlink(ota.package) && errno != ENOENT)
    fprintf(stderr, "y4-board ota: failed to discard upload status=%d\n", -errno);
}

static int fail_upload(int error) {
  discard_upload();
  ota.code = error;
  ota.completed_ms = y4_board_now_ms();
  ota.state = OTA_FAILED;
  return error;
}

int y4_board_ota_admit(size_t expected) {
  if (y4_board_ota_busy()) return -EBUSY;
  if (!expected || expected > OTA_LIMIT) return -EFBIG;
  struct statvfs space;
  if (statvfs("/data", &space)) return -errno;
  if ((uint64_t)space.f_bavail * (uint64_t)space.f_frsize < expected + (256ULL << 20))
    return -ENOSPC;
  if (mkdir("/data/y4-board", 0700) && errno != EEXIST) return -errno;
  if (mkdir(OTA_DIR, 0700) && errno != EEXIST) return -errno;
  unsigned char random[16];
  if (getrandom(random, sizeof random, GRND_NONBLOCK) != (ssize_t)sizeof random) return -EIO;
  ota_release();
  memset(&ota, 0, sizeof ota);
  ota.pkg_fd = ota.out_fd = ota.verify_fd = ota.k_fd = -1;
  for (unsigned i = 0; i < 16u; i++) (void)snprintf(ota.id + i * 2u, 3, "%02x", random[i]);
  if ((size_t)snprintf(ota.package, sizeof ota.package, "%s/%s.y4pkg", OTA_DIR, ota.id) >=
      sizeof ota.package) return -ENAMETOOLONG;
  ota_upload_fd = open(ota.package, O_WRONLY | O_CREAT | O_EXCL | O_DIRECT | O_CLOEXEC, 0600);
  if (ota_upload_fd < 0) ota_upload_fd = open(ota.package, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
  if (ota_upload_fd < 0) return -errno;
  ota.started_ms = y4_board_now_ms();
  ota.state = OTA_ACCEPTED;
  if (posix_memalign((void **)&ota_upload_stage, 4096, OTA_IO_CHUNK))
    return fail_upload(-ENOMEM);
  ota_upload_digest = EVP_MD_CTX_new();
  if (!ota_upload_digest) return fail_upload(-ENOMEM);
  if (EVP_DigestInit_ex(ota_upload_digest, EVP_sha256(), NULL) != 1)
    return fail_upload(-EIO);
  return 0;
}

static int upload_flush(size_t bytes) {
  size_t at = 0;
  while (at < bytes) {
    ssize_t n = write(ota_upload_fd, ota_upload_stage + at, bytes - at);
    if (n < 0) { if (errno == EINTR) continue; return -errno; }
    if (!n) return -EIO;
    at += (size_t)n;
  }
  return 0;
}

int y4_board_ota_write(const char *data, size_t bytes) {
  if (ota.state != OTA_ACCEPTED) return -EINVAL;
  if (ota_upload_fd < 0 || !ota_upload_stage || !ota_upload_digest) return fail_upload(-EINVAL);
  if (EVP_DigestUpdate(ota_upload_digest, data, bytes) != 1) return fail_upload(-EIO);
  ota_received += bytes;
  const unsigned char *source = (const unsigned char *)data;
  size_t left = bytes;
  while (left) {
    size_t room = OTA_IO_CHUNK - ota_upload_used;
    size_t take = left < room ? left : room;
    memcpy(ota_upload_stage + ota_upload_used, source, take);
    ota_upload_used += take;
    source += take;
    left -= take;
    if (ota_upload_used == OTA_IO_CHUNK) {
      int e = upload_flush(ota_upload_used);
      if (e) return fail_upload(e);
      ota_upload_used = 0;
    }
  }
  return 0;
}

int y4_board_ota_finish(void) {
  if (ota.state != OTA_ACCEPTED) return -EINVAL;
  if (ota_upload_fd < 0 || !ota_upload_stage || !ota_upload_digest || !ota_received)
    return fail_upload(-EINVAL);
  if (ota_upload_used) {
    size_t padded = (ota_upload_used + 511u) & ~(size_t)511u;
    if (padded > ota_upload_used)
      memset(ota_upload_stage + ota_upload_used, 0, padded - ota_upload_used);
    int e = upload_flush(padded);
    if (e) return fail_upload(e);
    if (ftruncate(ota_upload_fd, (off_t)ota_received)) return fail_upload(-errno);
  }
  if (fsync(ota_upload_fd)) return fail_upload(-errno);
  int error = release_upload();
  if (error) return fail_upload(error);
  ota.state = OTA_OPEN;
  return 0;
}

void y4_board_ota_abort(void) {
  if (ota.state != OTA_ACCEPTED) return;
  discard_upload();
  ota.state = OTA_IDLE;
}

/* Advance the apply machine. Called from the daemon poll loop; every call is
 * bounded (16 MiB of payload or one fast step). */
void y4_board_ota_tick(void) {
  if (ota.state == OTA_IDLE || ota.state == OTA_FAILED) return;
  for (unsigned steps = 0; steps < 4u; steps++) {
    int e = 0;
    switch (ota.state) {
      case OTA_ACCEPTED: return;
      case OTA_OPEN: e = step_open(); if (!e) ota.state = OTA_VERIFY; break;
      case OTA_VERIFY: e = step_verify(); if (!e) ota.state = OTA_MANIFEST; break;
      case OTA_MANIFEST: e = step_manifest(); if (!e) ota.state = OTA_VERSION; break;
      case OTA_VERSION: e = step_version(); if (!e) ota.state = OTA_SLOT; break;
      case OTA_SLOT: e = step_slot(); if (!e) ota.state = OTA_DEVICE; break;
      case OTA_DEVICE:
        e = step_device();
        if (!e) { e = step_write_start(); if (!e) ota.state = OTA_WRITE; }
        break;
      case OTA_WRITE: {
        int slice = step_write_slice();
        if (slice < 0) { e = slice; break; }
        if (slice == 1) ota.state = OTA_HASH;
        return; /* a write slice is the whole budget for this tick */
      }
      case OTA_HASH: e = step_hash(); if (!e) ota.state = OTA_READBACK; break;
      case OTA_READBACK: {
        int slice = step_readback_slice();
        if (slice < 0) { e = slice; break; }
        if (slice == 1)
          ota.state = ota.schema == 2 ? OTA_KERNEL_WRITE : OTA_GRUBENV;
        return; /* a read-back slice is the whole budget for this tick */
      }
      case OTA_KERNEL_WRITE: {
        int slice = step_kernel_write_slice();
        if (slice < 0) { e = slice; break; }
        if (slice == 1) ota.state = OTA_KERNEL_VERIFY;
        return; /* a kernel write slice is the whole budget for this tick */
      }
      case OTA_KERNEL_VERIFY: {
        int slice = step_kernel_verify_slice();
        if (slice < 0) { e = slice; break; }
        if (slice == 1) ota.state = OTA_KERNEL_PUBLISH;
        return;
      }
      case OTA_KERNEL_PUBLISH:
        e = step_kernel_publish();
        if (!e) ota.state = OTA_GRUBENV;
        break;
      case OTA_GRUBENV:
        e = step_grubenv();
        if (!e) {
          sync();
          /* The uploaded package is disposable once the slot is written and
           * verified. Delete it and any older uploads to keep /data from
           * filling up across repeated updates (7.3 GB per package). */
          unlink(ota.package);
          DIR *dir = opendir(OTA_DIR);
          if (dir) {
            struct dirent *entry;
            while ((entry = readdir(dir))) {
              if (entry->d_name[0] == '.') continue;
              char stale[PATH_MAX];
              if ((size_t)snprintf(stale, sizeof stale, "%s/%s", OTA_DIR, entry->d_name) < sizeof stale)
                (void)unlink(stale);
            }
            closedir(dir);
          }
          ota_release();
          ota.state = OTA_READY;
          /* Let the API publish readiness before dispatching the reboot. */
          return;
        }
        break;
      case OTA_READY:
        e = request_reboot();
        if (!e) {
          ota.state = OTA_REBOOTING;
          ota.completed_ms = y4_board_now_ms();
        } else {
          ota_fail(e); /* a swallowed error would wedge the machine in ready */
          return;
        }
        return;
      default: return;
    }
    if (e) { ota_fail(e); return; }
  }
}

int y4_board_ota_status_json(char *out, size_t cap) {
  int n = snprintf(out, cap,
                   "{\"schema\":1,\"job\":\"%s\",\"phase\":\"%s\",\"status\":%d,"
                   "\"bytes_written\":%" PRIu64 ",\"image_bytes\":%" PRIu64 ","
                   "\"version\":\"%s\",\"slot\":\"%s\",\"busy\":%s,"
                   "\"payload_sha\":\"%s\",\"payload_hash\":\"%s\","
                   "\"package_schema\":%d,\"kernel_release\":\"%s\","
                   "\"kernel_bytes_written\":%" PRIu64 ",\"kernel_bytes\":%" PRIu64 "}\n",
                   ota.id, state_name(ota.state), ota.code, ota.written, ota.image_total,
                   ota.version, ota.slot, y4_board_ota_busy() ? "true" : "false",
                   ota.payload_sha, ota.payload_hash,
                   ota.schema, ota.k_release, ota.k_cursor, ota.k_bytes);
  return n < 0 || (size_t)n >= cap ? -EOVERFLOW : n;
}

int y4_board_ota_confirm(void) {
  struct y4_slot_state state;
  int e = y4_grubenv_read(Y4_GRUBENV_DEFAULT, &state);
  if (e == -ENOENT) return -ENOENT;
  if (e) return e;
  state.boot_ok = 1;
  state.boot_count = 0;
  if (!state.slot[0]) {
    state.slot[0] = state.default_entry ? 'B' : 'A';
    state.have_slot = 1;
  }
  const char *const paths[] = {Y4_GRUBENV_DEFAULT, Y4_GRUBENV_BACKUP, NULL};
  e = y4_grubenv_write(paths, &state);
  if (e) return e;
  /* A confirmed slot has demonstrated a working kernel: the boot FAT can
   * now drop delivery directories nothing references any more (design 4.3
   * GC). Best effort - a GC problem must never fail the confirm. */
  (void)y4_board_kernels_gc();
  return 0;
}
