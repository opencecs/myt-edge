#ifndef Y4_OS_IX_INVENTORY_H
#define Y4_OS_IX_INVENTORY_H
#include "y4_os_ix_topology.h"
#define Y4_IX_SCAN_ENTRY_MAX 2048u
struct y4_ix_inventory {
 uint64_t begin_mono_ns,end_mono_ns;
 uint32_t count,entries_seen;
 int32_t scan_error;
 struct y4_ix_topology topology;
 struct y4_ix_node nodes[Y4_IX_TOPOLOGY_MAX_NODES];
};
/* Internal supervisor-controlled root, never public arbitrary path input.
 * No capture retries/heap/control; identity reads have bounded EINTR handling. Reset all output; bounded entry/node/text counts.
 * Enumerate canonical BDF only, ignore other names but bound all entries.
 * Capture identity/parent/link against SAME borrowed node FD with no reopen.
 * Parent is immediate actual ancestor BDF, empty for PCI root. Never infer
 * parent from numeric bus order. A path rename/deleted suffix is invalid.
 * count includes nodes with errors; identity_error preserves first failure.
 * Fatal argument/root/clock/scan errors set scan_error and ERROR, roles=-1.
 * Unopened-node links are UNSET with the node error, never valid-zero.
 * scan_error records incomplete enumeration (including overflow/IO). No valid
 * nodes => EAGAIN. Topology is never READY on scan_error. Return scan_error
 * if nonzero, otherwise reducer result. Fields remain inspectable on error.
 * This sequential sample is NOT an atomic hotplug snapshot. Consumers must
 * not derive power/reset authorization or persistent identity from it.
 * Caller owns output storage; no overlap with root string, no FD sharing races.
 */
int y4_ix_inventory_read(const char *,struct y4_ix_inventory *);
#endif
