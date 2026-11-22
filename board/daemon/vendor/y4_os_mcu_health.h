#ifndef Y4_OS_MCU_HEALTH_H
#define Y4_OS_MCU_HEALTH_H
#include <stddef.h>
#include <stdint.h>
#define Y4_MCU_HEALTH_REGISTER 0x90u
#define Y4_MCU_HEALTH_BYTES 84u
struct y4_mcu_health {
 uint64_t sampled_ms; /* host CLOCK_BOOTTIME read time; NOT fault UTC */
 uint8_t publication_sequence,flags;
 uint32_t sequence,reset_flags,fault_count,reason,cfsr,hfsr,dfsr,mmfar,bfar;
 uint32_t lr,pc,xpsr,exc_return,stack_pointer,uptime_ms;
 uint32_t communication_errors,minimum_stack_pointer;
};
/* Pure schema/CRC decoder. -ENOTSUP legacy/unknown schema, -ENODATA invalid
 * record, -EAGAIN odd publication, -EPROTO malformed. No output on failure. */
int y4_mcu_health_decode(const void *,size_t,struct y4_mcu_health *);
/* Exclusively owned identified MCU bus fd. At most three bounded transport
 * reads, no retry/reset/write. Full block sequence must equal both even
 * brackets. Output unchanged on any error. This is historical boot data. */
int y4_mcu_health_read(int,struct y4_mcu_health *);
#endif
