/* SPDX-License-Identifier: Apache-2.0 */
/* Synthetic procfs input tests. Build on Linux with -DY4_DEVICE_ID='"Y4"'. */
#define _GNU_SOURCE
#include "resources.c"

#include <assert.h>

static FILE *fixture(const char *text)
{
    if (!text)
        return NULL;
    FILE *file = fmemopen((void *)text, strlen(text), "r");
    assert(file);
    return file;
}

static void snapshot(struct y4_resource_sampler *sampler, const char *cpu,
                     const char *memory, uint64_t now, char out[Y4_RESOURCE_JSON_CAP])
{
    FILE *stat = fixture(cpu);
    FILE *meminfo = fixture(memory);
    int n = resource_snapshot(sampler, stat, meminfo, now, out, Y4_RESOURCE_JSON_CAP);
    assert(n > 0 && (size_t)n == strlen(out));
    if (stat)
        fclose(stat);
    if (meminfo)
        fclose(meminfo);
}

int main(void)
{
    static const char first_cpu[] = "cpu  100 20 30 1000 80 5 6 0 10 2\ncpu0 99\n";
    static const char second_cpu[] = "cpu  110 23 35 1060 95 7 11 0 18 4\n";
    static const char reverted_cpu[] = "cpu  110 23 35 1060 94 7 11 0 18 4\n";
    static const char resumed_cpu[] = "cpu  120 23 35 1070 94 7 11 0 28 4\n";
    static const char meminfo[] = "MemTotal: 1000 kB\nMemFree: 200 kB\nMemAvailable: 600 kB\n";
    struct y4_resource_sampler sampler = {0};
    char json[Y4_RESOURCE_JSON_CAP];

    snapshot(&sampler, first_cpu, meminfo, 100, json);
    assert(strstr(json, "\"device\":\"" Y4_DEVICE_ID "\",\"sampled_ms\":100,\"cpu\":null"));
    assert(strstr(json, "\"memory\":{\"total_bytes\":1024000,\"available_bytes\":614400,"
                        "\"used_bytes\":409600,\"usage_percent\":40.0}"));

    /* I/O wait is idle. Guest deltas do not increase the denominator. */
    snapshot(&sampler, second_cpu, NULL, 1100, json);
    assert(strstr(json, "\"cpu\":{\"usage_percent\":25.0},\"memory\":null"));

    snapshot(&sampler, reverted_cpu, "MemTotal: 1000 kB\nMemAvailable: 1001 kB\n", 2100, json);
    assert(strstr(json, "\"cpu\":null,\"memory\":null"));

    snapshot(&sampler, resumed_cpu, "MemTotal: 1000 kB\nMemAvailable: 0 kB\n", 3100, json);
    assert(strstr(json, "\"cpu\":{\"usage_percent\":50.0}"));
    assert(strstr(json, "\"available_bytes\":0,\"used_bytes\":1024000,\"usage_percent\":100.0"));

    snapshot(&sampler, "cpu  1 2 3 4 5 6 7 0bad\n",
             "MemTotal: 1 kB\nMemAvailable: 1 kB\n", 4100, json);
    assert(strstr(json, "\"cpu\":null"));
    assert(strstr(json, "\"available_bytes\":1024,\"used_bytes\":0,\"usage_percent\":0.0"));

    snapshot(&sampler, second_cpu, "MemTotal: 18014398509481984 kB\nMemAvailable: 0 kB\n", 5100, json);
    assert(strstr(json, "\"cpu\":null,\"memory\":null"));

    snapshot(&sampler, second_cpu, "MemTotal: 1 kB\n", 6100, json);
    assert(strstr(json, "\"cpu\":null,\"memory\":null"));

    snapshot(&sampler, "cpu  18446744073709551616 0 0 0 0 0 0 0\n", meminfo, 7100, json);
    assert(!sampler.have_cpu_baseline);
    assert(strstr(json, "\"cpu\":null,\"memory\":{"));

    unsigned tenths;
    const uint64_t huge[8] = {UINT64_MAX, 1u};
    assert(!cpu_usage((uint64_t[8]){0}, huge, &tenths));

    assert(resource_snapshot(&sampler, NULL, NULL, 8100, json, 2) == -EOVERFLOW);
    puts("resource sampler regression checks passed");
    return 0;
}
