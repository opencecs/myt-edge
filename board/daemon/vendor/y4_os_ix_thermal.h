#ifndef Y4_OS_IX_THERMAL_H
#define Y4_OS_IX_THERMAL_H
#include <stdint.h>
#include "ix_bar_probe.h"
#include "y4_os_ix_snapshot.h"
#include "y4_os_command.h"
#define Y4_IX_THERMAL_MAX_AGE_NS UINT64_C(15000000000)
struct y4_ix_thermal_sample {
 uint64_t sampled_ns,completed_ns,age_ms,generation;
 int status,temperature_error,valid;
 int32_t millicelsius;
 unsigned char boot_id[16];
 ix_bar_probe_result result;
 char bdf[13];
};
/* Product read: fixed /run/y4-pcie, independently read boot ID and monotonic
 * clock. Never opens PCI, forks a probe, or retains a last-good temperature.
 * Returns zero for a well-formed error observation; inspect valid and errors.
 * Failure returns negative errno and clears the output with matching errors. */
int y4_ix_thermal_read(struct y4_ix_thermal_sample *out);
/* Owner-only internals, and explicit fixture seams. All paths are dirfd-relative.
 * Private directory0700, file0600, ownerUID; fixed endian wire/CRC, not C ABI.
 * Store failure invalidates the old file. Caller owns sampler lease. */
int y4_ix_thermal_store_at(int dir,const struct y4_ix_thermal_sample *);
int y4_ix_thermal_load_at(int dir,const unsigned char boot[16],uint64_t now,
                         struct y4_ix_thermal_sample *);
int y4_ix_thermal_invalidate_at(int dir);
/* This entry is the only hardware sampler; caller already owns live snapshot.
 * Production root remains /sys/bus/pci/devices, no client-selected address. */
int y4_ix_bar_collect_snapshot(const struct y4_call *,const struct y4_ix_snapshot *,
                              ix_bar_probe_result *,char bdf[13]);
#endif
