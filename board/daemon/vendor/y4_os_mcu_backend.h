#ifndef Y4_OS_MCU_BACKEND_H
#define Y4_OS_MCU_BACKEND_H
#include "y4_os_command.h"
#define Y4_MCU_BACKEND_REGISTER 0xe4u
#define Y4_MCU_BACKEND_BYTES 28u
#define Y4_MCU_BACKEND_CACHE_BYTES 64u
/* The hardware owner rewrites the cache once per ~1-2s loop and the tracker
 * timestamps an observed change at the preceding read, so a healthy frame can
 * already be one loop interval old when cached. 10s bounds a frozen sequence
 * while tolerating the owner's publication cadence. */
#define Y4_MCU_BACKEND_MAX_AGE 10000u
struct y4_mcu_backend_frame {
 unsigned char sequence,build,flags;
 uint32_t spi_hz,dma_completed,error_events,success_bytes;
 int spi_current,spi_last,pwm_status;
 unsigned pwm_hz;
};
struct y4_mcu_backend_snapshot {
 uint64_t sampled_ms;int status;
 unsigned char firmware_major,firmware_minor,firmware_patch;
 struct y4_mcu_backend_frame frame;
};
/* Stable-address producer owned by the hardware bus process. First coherent
 * frame only seeds a challenge. Changed sequence is timestamped at preceding
 * successful read, never restamped current time; frozen MCU data expires. */
struct y4_mcu_backend_tracker {
 uint64_t previous_read_ms,advance_ms;unsigned char sequence;int seeded;
};
int y4_mcu_backend_frame_encode(const struct y4_mcu_backend_frame *,void *,size_t);
int y4_mcu_backend_decode(const void *,size_t,struct y4_mcu_backend_frame *);
struct y4_mcu_identity;
/* Rejects old/unknown application versions before ANY extension I2C read. */
int y4_mcu_backend_observe(int,const struct y4_mcu_identity *,struct y4_mcu_backend_frame *);
/* Errors reset the sequence challenge; output is a new error observation.
 * Old firmware (<0.1.24) must be supplied -EOPNOTSUPP, never read as healthy. */
int y4_mcu_backend_track(struct y4_mcu_backend_tracker *,uint64_t,int,
 const struct y4_mcu_backend_frame *,struct y4_mcu_backend_snapshot *);
int y4_mcu_backend_snapshot_encode(const struct y4_mcu_backend_snapshot *,void *,size_t);
int y4_mcu_backend_snapshot_decode(const void *,size_t,uint64_t,struct y4_mcu_backend_snapshot *);
int y4_mcu_backend_snapshot_store(int,const struct y4_mcu_backend_snapshot *);
int y4_mcu_backend_snapshot_load(int,uint64_t,struct y4_mcu_backend_snapshot *);
struct y4_mcu_backend_query_context {
 int directory_fd;uint64_t (*boottime_ms)(void *);void *opaque;
};
extern const struct y4_command y4_mcu_backend_command;
#endif
