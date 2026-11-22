#ifndef Y4_OS_IX_PROBE_H
#define Y4_OS_IX_PROBE_H

#include <stdint.h>

#include "y4_os_command.h"

/* Read-only IX8024 (vendor 1ec8, device 8024) PCI diagnostics collector.
 *
 * The caller owns and passes an already-open trusted device directory fd (the
 * sysfs device directory of the single BDF under management). This module never
 * accepts a host path, never opens anything outside that directory, and never
 * writes, mmaps, ioctls or scans buses.
 *
 * Every error field is 0 on success and a negative errno-style value otherwise;
 * a failing section is zeroed so partial/garbage register values are never
 * reported as measurements. Capability registers are diagnostics only: nothing
 * here performs control, reset, power, or temperature addressing. */

#define Y4_IX_RESOURCE_COUNT 6
#define Y4_IX_DRIVER_BASENAME_MAX 64

struct y4_ix_resource {
    uint64_t start;
    uint64_t length;
    uint64_t flags;
    int present;
};

struct y4_ix_probe {
    /* Per-section status (0 = collected, negative = failure for that section).
     * config_error stays 0 on every successful return: the base identity block
     * (ID, header type, status) is read first and any failure there is fatal,
     * so it can never be reported as a partial section. */
    int config_error;
    int resource_error;
    int driver_error;
    int pcie_error;
    int aer_error;

    /* Base identity from configuration space. */
    uint16_t vendor;
    uint16_t device;
    uint8_t header_type;

    /* First six "resource" lines (the BARs). */
    struct y4_ix_resource resources[Y4_IX_RESOURCE_COUNT];

    /* Basename of the bound driver symlink target; empty when unbound. */
    char driver_basename[Y4_IX_DRIVER_BASENAME_MAX];

    /* PCIe capability (id 0x10) inside the classic capability list. */
    int pcie_present;
    uint16_t pcie_offset;
    uint8_t pcie_version;
    uint8_t pcie_type;
    int slot_implemented;
    uint32_t pcie_device_ctrl;
    uint32_t pcie_device_status;
    uint32_t pcie_link_cap;
    uint32_t pcie_link_ctrl;
    uint32_t pcie_link_status;
    uint32_t pcie_slot_cap;
    uint32_t pcie_slot_ctrl;
    uint32_t pcie_slot_status;

    /* AER extended capability (id 1). Status/mask/severity are read only and
     * are never cleared by this module. */
    int aer_present;
    uint32_t aer_uncorrectable_status;
    uint32_t aer_uncorrectable_mask;
    uint32_t aer_uncorrectable_severity;
    uint32_t aer_correctable_status;
    uint32_t aer_correctable_mask;
};

/* Collect diagnostics from an open device directory.
 *
 * call may be NULL (no cancellation source). Otherwise y4_command_poll() is
 * consulted before and after every filesystem operation.
 *
 * Returns 0 when the base identity is valid; per-section problems are reported
 * in the error fields above. Fatal returns (the whole record is cleared):
 *   -EINVAL  out == NULL, dirfd < 0, or header layout is not a PCI bridge
 *   -ENODEV  vendor/device != 1ec8:8024; I/O errors are preserved
 *   Actual cancellation/deadline errno is preserved; output cleared. */
int y4_ix_probe_read_fd(int dirfd, const struct y4_call *call,
                        struct y4_ix_probe *out);

#endif /* Y4_OS_IX_PROBE_H */
