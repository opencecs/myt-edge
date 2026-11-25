/* SPDX-License-Identifier: Apache-2.0 */
#ifndef Y4_BOARD_RESOURCES_H
#define Y4_BOARD_RESOURCES_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define Y4_RESOURCE_JSON_CAP 512u

struct y4_resource_sampler {
    uint64_t cpu_ticks[8];
    bool have_cpu_baseline;
};

/* Return the serialized snapshot length, excluding the NUL, or negative errno.
 * Unavailable CPU or memory measurements are represented independently as null. */
int y4_resource_refresh(struct y4_resource_sampler *sampler, uint64_t now_ms,
                        char *json, size_t cap);

#endif
