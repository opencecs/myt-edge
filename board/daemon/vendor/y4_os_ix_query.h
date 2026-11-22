#ifndef Y4_OS_IX_QUERY_H
#define Y4_OS_IX_QUERY_H
#include "y4_os_command.h"
#include "y4_os_ix_snapshot_store.h"
#define Y4_IX_QUERY_FIELDS 38u
struct y4_ix_query_context {
 uint64_t checked_ns;
 int load_status,prepared;
 struct y4_ix_snapshot sample;
 struct y4_ix_node absent;
};
/* Internal trusted borrowed runtime directory and independently read current
 * boot/time. No hardware scan/control. One serialized context per call;
 * output text borrows this context and must survive response encoding.
 * Prepare returns load status but retains error metadata for the query.
 */
int y4_ix_query_prepare(struct y4_ix_query_context *,int,const unsigned char [16],uint64_t);
/* Product fixed /run/y4-pcie adapter, never caller-selected paths. */
int y4_ix_query_prepare_runtime(struct y4_ix_query_context *);
extern const struct y4_command y4_ix_query_command;
#endif
