#ifndef Y4_OS_BOOT_UPDATE_CONTEXT_H
#define Y4_OS_BOOT_UPDATE_CONTEXT_H
#include <stddef.h>
#include <stdint.h>
#define Y4_BOOT_UPDATE_CONFIG_MAX 1024u
#define Y4_BOOT_UPDATE_ENCODED_MAX (52u+Y4_BOOT_UPDATE_CONFIG_MAX)
struct y4_boot_update_context {
 uint32_t slot,root_pid;
 uint64_t root_start_ticks;
 unsigned char boot_id[16],config[Y4_BOOT_UPDATE_CONFIG_MAX];
 size_t config_bytes;
};
struct y4_boot_update_publish {int status,published,synced;};
/* Boot-scoped internal supervisor record; CRC detects corruption, not a signer.
 * Exact Y4STATE4/5 bytes, root slot/PID/start ticks and kernel boot UUID. Caller authenticates
 * supervisor provenance and actual root lifecycle; PID alone is no owner proof.
 * Codec failures preserve output. Load requires private root-owned directory
 * and single-link 0600 regular file, plus independently observed current boot ID.
 * Publish once using atomic no-replace; no mount/format/update/reboot.
 * /run persistence is explicitly only for this boot, never across reboot. */
int y4_boot_update_process_start(uint32_t,uint64_t *);
int y4_boot_update_current_id(unsigned char[16]);
int y4_boot_update_context_encode(const struct y4_boot_update_context *,void *,size_t,size_t *);
int y4_boot_update_context_decode(const void *,size_t,const unsigned char[16],struct y4_boot_update_context *);
int y4_boot_update_context_publish_at(int,const struct y4_boot_update_context *,struct y4_boot_update_publish *);
int y4_boot_update_context_load_at(int,const unsigned char[16],struct y4_boot_update_context *);
#endif
