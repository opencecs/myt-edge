#ifndef IX_BAR_PROBE_H
#define IX_BAR_PROBE_H
#include <stdint.h>
#include <sys/types.h>
#define IX_BAR_PROBE_BYTES (2u * 1024u * 1024u)
#define IX_BAR_PROBE_PORTS 5u
/* Order: USP, DSP0, DSP3, DSP6, DSP9. Caller must already verify fd identity.
 * poll_fn runs in child before/after every volatile 32-bit BAR read.
 * It must be fork-safe and must not depend on locks held by other threads. */
typedef int (*ix_bar_probe_poll)(void *);
typedef struct {
    int status;
    uint32_t stage; /* 1=config inputs,2=map,3=anchors,4=sensor reads,5=complete */
    uint32_t failed_offset;
    uint32_t reads_completed;
    uint32_t mapping_verified; /* multiple config anchors match, not vendor attestation */
    uint32_t ids[5];
    uint32_t classes[5];
    uint32_t pcie_headers[5];
    uint32_t link_dwords[5];
    uint32_t training_status[5]; /* USP12, DSP0,3,6,9: bit12PL,bit8DL,bits5:0LTSSM */
    uint32_t raw_temperature;
    uint32_t calibration;
    uint32_t management_mode;
    uint32_t control0; /* internal0x0a001000; raw read, not a readiness claim */
    uint32_t control1; /* internal0x0a001004 */
    pid_t unreaped_pid; /* nonzero only if a killed child cannot yet be reaped */
} ix_bar_probe_result;
/* No writes, no retry. barfd must name verified USP BAR0; config fds must name
 * the five matching PCI sysfs config resources in the order above.
 * Calls fork: invoke from an owner process with no unsafe fork locks.
 * 2000ms total parent deadline, then kill and bounded (100ms) reap.
 * A config mismatch stops before temperature/mode reads. */
int ix_bar_probe_run(int barfd, const int config_fds[5],
                     ix_bar_probe_poll poll_fn, void *context,
                     ix_bar_probe_result *out);
#endif
