/* SPDX-License-Identifier: Apache-2.0 */
/* Shared grubenv access for the Y4 slot machinery.
 *
 * The environment block is the exact grub-editenv format: 1024 bytes, two
 * header lines, KEY=VALUE lines, '#' padding. grub rejects anything else,
 * and in-place editors (sed) break the size, so writers always rebuild the
 * whole block and replace the file atomically. */
#ifndef Y4_GRUBENV_H
#define Y4_GRUBENV_H
#include <stddef.h>
#include <stdint.h>

#define Y4_GRUBENV_BYTES 1024
#define Y4_GRUBENV_DEFAULT "/boot/efi/grub/grubenv"
#define Y4_GRUBENV_BACKUP "/boot/efi/EFI/BOOT/grubenv"

struct y4_slot_state {
  int have_default;   /* default=<0|1>, numeric; drives the grub menu */
  int default_entry;  /* 0 = slot A, 1 = slot B */
  int have_slot;
  char slot[8];       /* "A" or "B", bookkeeping for the agent */
  int have_boot_kernel;
  char boot_kernel[96];  /* legacy single key: read and re-emitted only when
                          * present, so an old grub.cfg keeps working */
  int have_boot_kernel_a;
  char boot_kernel_a[96]; /* per-slot kernel directory (delivery version),
                           * kernel-ota-design 4.1 */
  int have_boot_kernel_b;
  char boot_kernel_b[96];
  long boot_count;
  int boot_ok;
  char boot_count_id[37]; /* Kernel boot UUID recorded with the trial count. */
  int rollback_pending;  /* Selected target still needs an orderly reboot. */
};

/* Parse a 1024-byte environment block. Returns 0 on success, -EINVAL when
 * the signature or size is wrong. Missing keys leave fields unset. */
int y4_grubenv_parse(const unsigned char *block, struct y4_slot_state *state);

int y4_boot_id_valid(const char *id);

/* Read + parse the environment file. Returns 0, -ENOENT or -EINVAL. */
int y4_grubenv_read(const char *path, struct y4_slot_state *state);

/* Rebuild the block from state (preserving boot_kernel when present in
 * the source) and replace every listed path atomically (tmp file + fsync
 * + rename). Returns 0 or a negative errno; all-or-nothing across paths. */
int y4_grubenv_write(const char *const *paths, const struct y4_slot_state *state);

/* Render the canonical 1024-byte block from state into out. Returns 0. */
int y4_grubenv_render(const struct y4_slot_state *state, unsigned char *out);
#endif
