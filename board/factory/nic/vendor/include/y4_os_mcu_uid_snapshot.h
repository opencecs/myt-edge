#ifndef Y4_OS_MCU_UID_SNAPSHOT_H
#define Y4_OS_MCU_UID_SNAPSHOT_H
#include <stddef.h>
#include <stdint.h>
#define Y4_MCU_UID_WIRE_BYTES 64u
struct y4_mcu_uid_snapshot {uint64_t sampled_ms;int status;uint8_t uid[12];};
int y4_mcu_uid_snapshot_encode(const struct y4_mcu_uid_snapshot *,void *,size_t);
int y4_mcu_uid_snapshot_decode(const void *,size_t,uint64_t,struct y4_mcu_uid_snapshot *);
/* Same-boot private runtime observation, single hardware worker; no persistent
 * provisioning/authentication receipt. Atomic replacement; missing/stale/error
 * must not fall back to cached UID. Borrowed directory must remain protected. */
int y4_mcu_uid_store(int,const struct y4_mcu_uid_snapshot *);
/* Sole producer revokes reserved runtime sample after publication failure.
 * No target-following, persistent files or hardware reset. Error must be reported. */
int y4_mcu_uid_invalidate(int);
int y4_mcu_uid_load(int,uint64_t,struct y4_mcu_uid_snapshot *);
#endif
