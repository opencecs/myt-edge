#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise OTA upload ownership with isolated allocation and I/O faults."""

import os
from pathlib import Path
import resource
import shlex
import subprocess
import tempfile


HARNESS = r"""
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <openssl/evp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>

enum fault {
  NONE, OPEN, MEMALIGN, DIGEST_NEW, DIGEST_INIT, DIGEST_UPDATE, WRITE_ZERO,
  WRITE_ERROR, TRUNCATE, SYNC, CLOSE
};
static enum fault fault;
static int file_exists, fd_open;
static void *stage;
static EVP_MD_CTX *digest;
static size_t file_bytes;

static int fake_statvfs(const char *path, struct statvfs *space) {
  assert(!strcmp(path, "/data"));
  space->f_frsize = 4096;
  space->f_bavail = 65536 + 4096;
  return 0;
}

static int fake_mkdir(const char *path, mode_t mode) {
  assert(!strncmp(path, "/data/y4-board", strlen("/data/y4-board")));
  assert(mode == 0700);
  return 0;
}

static ssize_t fake_getrandom(void *out, size_t size, unsigned flags) {
  assert(flags == GRND_NONBLOCK);
  memset(out, 0x74, size);
  return (ssize_t)size;
}

static int fake_open(const char *path, int flags, ...) {
  assert(!strncmp(path, "/data/y4-board/ota/", strlen("/data/y4-board/ota/")));
  assert((flags & (O_EXCL | O_CREAT | O_CLOEXEC)) == (O_EXCL | O_CREAT | O_CLOEXEC));
  assert(!fd_open);
  if (fault == OPEN) { errno = EEXIST; return -1; }
  assert(!file_exists);
  file_exists = fd_open = 1;
  file_bytes = 0;
  return 21;
}

static int fake_close(int fd) {
  assert(fd == 21 && fd_open);
  fd_open = 0;
  if (fault == CLOSE) { errno = EIO; return -1; }
  return 0;
}

static int fake_unlink(const char *path) {
  assert(!strncmp(path, "/data/y4-board/ota/", strlen("/data/y4-board/ota/")));
  assert(file_exists && !fd_open && !digest && !stage);
  file_exists = 0;
  return 0;
}

static int tracked_memalign(void **out, size_t alignment, size_t bytes) {
  assert(!stage);
  if (fault == MEMALIGN) return ENOMEM;
  int error = posix_memalign(out, alignment, bytes);
  if (!error) stage = *out;
  return error;
}

static void tracked_free(void *pointer) {
  if (pointer == stage) stage = NULL;
  free(pointer);
}

static EVP_MD_CTX *tracked_digest_new(void) {
  assert(!digest);
  if (fault == DIGEST_NEW) return NULL;
  return digest = EVP_MD_CTX_new();
}

static void tracked_digest_free(EVP_MD_CTX *ctx) {
  assert(ctx == digest);
  EVP_MD_CTX_free(ctx);
  digest = NULL;
}

static int fake_digest_init(EVP_MD_CTX *ctx, const EVP_MD *md, ENGINE *engine) {
  return fault == DIGEST_INIT ? 0 : EVP_DigestInit_ex(ctx, md, engine);
}

static int fake_digest_update(EVP_MD_CTX *ctx, const void *buf, size_t size) {
  return fault == DIGEST_UPDATE ? 0 : EVP_DigestUpdate(ctx, buf, size);
}

static ssize_t fake_write(int fd, const void *data, size_t bytes) {
  (void)data;
  assert(fd == 21 && fd_open && !(bytes % 512));
  if (fault == WRITE_ZERO) return 0;
  if (fault == WRITE_ERROR) { errno = ENOSPC; return -1; }
  file_bytes += bytes;
  return (ssize_t)bytes;
}

static int fake_ftruncate(int fd, off_t bytes) {
  assert(fd == 21 && fd_open && bytes == 513 && file_bytes == 1024);
  if (fault == TRUNCATE) { errno = EIO; return -1; }
  file_bytes = (size_t)bytes;
  return 0;
}

static int fake_fsync(int fd) {
  assert(fd == 21 && fd_open && file_bytes == 513);
  if (fault == SYNC) { errno = EIO; return -1; }
  return 0;
}

#define statvfs(...) fake_statvfs(__VA_ARGS__)
#define mkdir fake_mkdir
#define getrandom fake_getrandom
#define open fake_open
#define close fake_close
#define unlink fake_unlink
#define posix_memalign tracked_memalign
#define free tracked_free
#define EVP_MD_CTX_new tracked_digest_new
#define EVP_MD_CTX_free tracked_digest_free
#define EVP_DigestInit_ex fake_digest_init
#define EVP_DigestUpdate fake_digest_update
#define write fake_write
#define ftruncate fake_ftruncate
#define fsync fake_fsync
#include "ota.c"
#undef free

uint64_t y4_board_now_ms(void) { return 42; }

static void clean_resources(void) {
  assert(!fd_open && !stage && !digest);
  assert(ota_upload_fd == -1 && !ota_upload_stage && !ota_upload_digest);
  assert(!ota_upload_used && !ota_received);
}

static void failed(int error) {
  clean_resources();
  assert(!file_exists && !y4_board_ota_busy());
  assert(y4_board_ota_phase() == OTA_FAILED && ota.code == error);
  y4_board_ota_abort();
  assert(y4_board_ota_phase() == OTA_FAILED && ota.code == error);
  fault = NONE;
}

int main(void) {
  char part[513] = {0};

  /* A new context cannot clobber stdin or unlink a file it never opened. */
  fault = OPEN;
  file_exists = 1;
  assert(y4_board_ota_admit(sizeof part) == -EEXIST);
  assert(file_exists && !y4_board_ota_busy());
  clean_resources();
  file_exists = 0;

  fault = MEMALIGN;
  assert(y4_board_ota_admit(sizeof part) == -ENOMEM);
  failed(-ENOMEM);
  fault = DIGEST_NEW;
  assert(y4_board_ota_admit(sizeof part) == -ENOMEM);
  failed(-ENOMEM);
  fault = DIGEST_INIT;
  assert(y4_board_ota_admit(sizeof part) == -EIO);
  failed(-EIO);

  assert(!y4_board_ota_admit(sizeof part));
  fault = DIGEST_UPDATE;
  assert(y4_board_ota_write(part, sizeof part) == -EIO);
  failed(-EIO);
  assert(!y4_board_ota_admit(OTA_IO_CHUNK));
  char *large = calloc(1, OTA_IO_CHUNK);
  assert(large);
  fault = WRITE_ERROR;
  assert(y4_board_ota_write(large, OTA_IO_CHUNK) == -ENOSPC);
  free(large);
  failed(-ENOSPC);

  const enum fault finish_faults[] = {WRITE_ZERO, WRITE_ERROR, TRUNCATE, SYNC, CLOSE};
  for (size_t i = 0; i < sizeof finish_faults / sizeof finish_faults[0]; ++i) {
    assert(!y4_board_ota_admit(sizeof part));
    assert(!y4_board_ota_write(part, sizeof part));
    fault = finish_faults[i];
    int expected = fault == WRITE_ERROR ? -ENOSPC : -EIO;
    assert(y4_board_ota_finish() == expected);
    failed(expected);
  }

  assert(!y4_board_ota_admit(sizeof part));
  assert(y4_board_ota_busy() && y4_board_ota_phase() == OTA_ACCEPTED);
  assert(!y4_board_ota_write(part, 1));
  y4_board_ota_abort();
  y4_board_ota_abort();
  assert(!file_exists && !y4_board_ota_busy() && y4_board_ota_phase() == OTA_IDLE);
  clean_resources();

  assert(!y4_board_ota_admit(sizeof part));
  assert(!y4_board_ota_write(part, sizeof part));
  assert(!y4_board_ota_finish());
  clean_resources();
  assert(file_exists && file_bytes == sizeof part && y4_board_ota_phase() == OTA_OPEN);
  y4_board_ota_abort();
  assert(file_exists && y4_board_ota_busy() && y4_board_ota_phase() == OTA_OPEN);
  puts("OTA upload failures release ownership and allow retry; accepted job stays owned.");
}
"""


def main():
    root = Path(__file__).resolve().parents[1]
    compiler = shlex.split(os.environ.get("CC", "cc"))
    flags = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "--libs", "gio-2.0", "libcrypto", "libzstd"], text=True
    ))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with tempfile.TemporaryDirectory(prefix="ota-upload-test-") as work:
        source = Path(work) / "test.c"
        binary = Path(work) / "test"
        source.write_text(HARNESS)
        subprocess.run(compiler + [
            "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-Wno-misleading-indentation", "-Wno-format-truncation",
            '-DY4_DEVICE_ID="Y4"', "-ffunction-sections", "-fdata-sections",
            "-I", str(root / "board/daemon"), "-I", str(root / "board/factory"),
            str(source), "-Wl,--gc-sections", "-o", str(binary),
        ] + flags, check=True)
        subprocess.run([str(binary)], timeout=10, check=True)


if __name__ == "__main__":
    main()
