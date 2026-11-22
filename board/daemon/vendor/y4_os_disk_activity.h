#ifndef Y4_OS_DISK_ACTIVITY_H
#define Y4_OS_DISK_ACTIVITY_H
#include <stdint.h>
struct y4_disk_counters { uint64_t operations[4], generation[4]; uint8_t present; };
struct y4_disk_activity { struct y4_disk_counters previous; int valid; };
int y4_disk_counters_read(const char *block_root, struct y4_disk_counters *out);
uint8_t y4_disk_activity_step(struct y4_disk_activity *state, const struct y4_disk_counters *sample);
#endif
