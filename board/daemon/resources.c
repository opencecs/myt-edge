/* SPDX-License-Identifier: Apache-2.0 */
#include "resources.h"

#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define KIBIBYTE_BYTES UINT64_C(1024)

static const char *skip_horizontal_space(const char *cursor)
{
    while (*cursor == ' ' || *cursor == '\t')
        cursor++;
    return cursor;
}

static bool read_uint64(const char **cursor, uint64_t *value)
{
    const char *start = skip_horizontal_space(*cursor);
    if (*start < '0' || *start > '9')
        return false;

    char *end;
    errno = 0;
    uintmax_t number = strtoumax(start, &end, 10);
    if (errno || number > UINT64_MAX)
        return false;

    *cursor = end;
    *value = (uint64_t)number;
    return true;
}

static bool parse_cpu_ticks(FILE *stat, uint64_t ticks[8])
{
    char line[256];
    if (!stat || !fgets(line, sizeof line, stat) ||
        strncmp(line, "cpu ", 4))
        return false;

    /* guest and guest_nice are already included in user and nice. */
    const char *cursor = line + 4;
    for (size_t i = 0; i < 8; i++) {
        if (!read_uint64(&cursor, &ticks[i]) ||
            (*cursor != ' ' && *cursor != '\t' && *cursor != '\n' && *cursor))
            return false;
    }
    return true;
}

static unsigned percent_tenths(uint64_t part, uint64_t whole)
{
    /* Form the ratio before multiplication to avoid overflowing a counter. */
    return (unsigned)(((double)part / (double)whole) * 1000.0 + 0.5);
}

static bool cpu_usage(const uint64_t previous[8], const uint64_t current[8],
                      unsigned *tenths)
{
    uint64_t total = 0;
    uint64_t idle = 0;
    for (size_t i = 0; i < 8; i++) {
        if (current[i] < previous[i])
            return false;
        uint64_t delta = current[i] - previous[i];
        if (delta > UINT64_MAX - total)
            return false;
        total += delta;
        if (i == 3 || i == 4)
            idle += delta;
    }
    if (!total)
        return false;

    *tenths = percent_tenths(total - idle, total);
    return true;
}

static bool sample_cpu(struct y4_resource_sampler *sampler, FILE *stat,
                       unsigned *tenths)
{
    uint64_t current[8];
    if (!parse_cpu_ticks(stat, current)) {
        sampler->have_cpu_baseline = false;
        return false;
    }

    bool valid = sampler->have_cpu_baseline &&
                 cpu_usage(sampler->cpu_ticks, current, tenths);
    memcpy(sampler->cpu_ticks, current, sizeof current);
    sampler->have_cpu_baseline = true;
    return valid;
}

static bool read_memory_bytes(const char *cursor, uint64_t *bytes)
{
    uint64_t kib;
    if (!read_uint64(&cursor, &kib) || kib > UINT64_MAX / KIBIBYTE_BYTES)
        return false;

    cursor = skip_horizontal_space(cursor);
    if (strncmp(cursor, "kB", 2))
        return false;
    cursor = skip_horizontal_space(cursor + 2);
    if (*cursor != '\n' && *cursor)
        return false;

    *bytes = kib * KIBIBYTE_BYTES;
    return true;
}

static bool sample_memory(FILE *meminfo, uint64_t *total, uint64_t *available)
{
    static const char total_key[] = "MemTotal:";
    static const char available_key[] = "MemAvailable:";
    bool have_total = false;
    bool have_available = false;
    char line[128];

    if (!meminfo)
        return false;
    while (fgets(line, sizeof line, meminfo)) {
        if (!strncmp(line, total_key, sizeof total_key - 1u)) {
            have_total = read_memory_bytes(line + sizeof total_key - 1u, total);
            if (!have_total)
                return false;
        } else if (!strncmp(line, available_key, sizeof available_key - 1u)) {
            have_available = read_memory_bytes(line + sizeof available_key - 1u, available);
            if (!have_available)
                return false;
        }
        if (have_total && have_available)
            return *total > 0 && *available <= *total;
    }
    return false;
}

static int resource_snapshot(struct y4_resource_sampler *sampler, FILE *stat,
                             FILE *meminfo, uint64_t now_ms, char *json, size_t cap)
{
    char cpu[40] = "null";
    unsigned cpu_tenths;
    if (sample_cpu(sampler, stat, &cpu_tenths))
        snprintf(cpu, sizeof cpu, "{\"usage_percent\":%u.%u}",
                 cpu_tenths / 10u, cpu_tenths % 10u);

    char memory[192] = "null";
    uint64_t total, available;
    if (sample_memory(meminfo, &total, &available)) {
        uint64_t used = total - available;
        unsigned memory_tenths = percent_tenths(used, total);
        snprintf(memory, sizeof memory,
                 "{\"total_bytes\":%" PRIu64 ",\"available_bytes\":%" PRIu64
                 ",\"used_bytes\":%" PRIu64 ",\"usage_percent\":%u.%u}",
                 total, available, used, memory_tenths / 10u, memory_tenths % 10u);
    }

    int n = snprintf(json, cap,
                     "{\"schema\":1,\"device\":\"%s\",\"sampled_ms\":%" PRIu64
                     ",\"cpu\":%s,\"memory\":%s}",
                     Y4_DEVICE_ID, now_ms, cpu, memory);
    return n < 0 || (size_t)n >= cap ? -EOVERFLOW : n;
}

int y4_resource_refresh(struct y4_resource_sampler *sampler, uint64_t now_ms,
                        char *json, size_t cap)
{
    FILE *stat = fopen("/proc/stat", "re");
    FILE *meminfo = fopen("/proc/meminfo", "re");
    int n = resource_snapshot(sampler, stat, meminfo, now_ms, json, cap);
    if (stat)
        fclose(stat);
    if (meminfo)
        fclose(meminfo);
    return n;
}
