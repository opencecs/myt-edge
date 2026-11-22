#ifndef Y4_OS_IX_TOPOLOGY_H
#define Y4_OS_IX_TOPOLOGY_H
#include <stddef.h>
#include <stdint.h>
#include "y4_os_ix_link.h"
#define Y4_IX_TOPOLOGY_MAX_NODES 512u
#define Y4_IX_DOWNSTREAM_MAX 4u
struct y4_ix_node {
 char bdf[13],parent_bdf[13];
 uint32_t vendor,device,pci_class;
 int32_t identity_error;
 struct y4_ix_link link;
};
enum y4_ix_topology_stage {Y4_IX_TOPOLOGY_UNSET=0,Y4_IX_TOPOLOGY_INITIALIZING,
 Y4_IX_TOPOLOGY_PARTIAL,Y4_IX_TOPOLOGY_READY,Y4_IX_TOPOLOGY_AMBIGUOUS,Y4_IX_TOPOLOGY_ERROR};
struct y4_ix_topology {
 enum y4_ix_topology_stage stage;
 int32_t error,upstream_index,soc_parent_index,downstream_index[4];
 uint32_t nodes_seen,ix_nodes,downstream_count;
};
/* Pure reduction of an already captured bounded inventory. No I/O, allocation,
 * shell, expected link-rate substitution or physical-silkscreen mapping.
 * Initialize every output on all returns. Missing indices=-1.
 * Ready topology does not certify link fields, atomicity, freshness or control.
 */
int y4_ix_topology_reduce(const struct y4_ix_node *,size_t,struct y4_ix_topology *);
#endif
