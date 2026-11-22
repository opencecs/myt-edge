#ifndef Y4_OS_IX_SNAPSHOT_STORE_H
#define Y4_OS_IX_SNAPSHOT_STORE_H
#include "y4_os_ix_snapshot.h"
/* Borrowed private directory owned by current UID, exactly0700.
 * Sole producer owns snapshot namespace; readers only. No durable disk format.
 * store is atomic replacement of pcie.bin; no retry of capture/control.
 * On store error caller must invalidate old sample and report failure.
 * load never returns an older cached observation after any validation error.
 * No directory/descriptor sharing mutation by caller during operations.
 */
int y4_ix_snapshot_store(int,const struct y4_ix_snapshot *);
int y4_ix_snapshot_invalidate(int);
int y4_ix_snapshot_load(int,const unsigned char [16],uint64_t,struct y4_ix_snapshot *);
#endif
