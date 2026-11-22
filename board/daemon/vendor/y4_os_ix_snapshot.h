#ifndef Y4_OS_IX_SNAPSHOT_H
#define Y4_OS_IX_SNAPSHOT_H
#include <stddef.h>
#include "y4_os_ix_inventory.h"
#define Y4_IX_SNAPSHOT_HEADER 96u
#define Y4_IX_SNAPSHOT_NODE 112u
#define Y4_IX_SNAPSHOT_MAX (96u+112u*Y4_IX_TOPOLOGY_MAX_NODES+4u)
#define Y4_IX_SNAPSHOT_AGE_NS UINT64_C(15000000000)
struct y4_ix_snapshot {
 uint64_t generation,published_mono_ns;
 unsigned char boot_id[16];
 struct y4_ix_inventory inventory;
};
/* Same-boot private runtime wire, not persistent/public ABI. No raw structs.
 * Header LE: magic8="Y4IXS001", gen8@8,published8@16,begin8@24,end8@32,
 * count4@40,entries4@44,negative scan errno magnitude4@48,reserved4@52,
 * boot_id16@56,reserved24@72. Nodes112bytes, then CRC32C4 of preceding bytes.
 * Node: bdf13@0,parent13@13,reserved2@26,vendor4@28,device4@32,class4@36,
 * negative identity errno magnitude4@40, four fields12@44 (current speed,
 * max speed,current width,max width): status1,reserved3,errno magnitude4,value4.
 * unsupported4@92,reserved4@96,sample_mono_ns8@100,reserved4@108.
 * Count<=512,entries<=2048 and>=count, gen>0,published>0,nonzero boot_id.
 * Identity bounds16/16/24, canonical NUL-terminated BDFs (no junk after NUL).
 * Errnos0..4095. Field OK requires errno0; nonOK requires errno1..4095,value0.
 * sample0 requires allUNSET and mask0; sample>0 requires noUNSET and mask7,
 * begin<=sample<=end if end exists, otherwise sample<=published on failed scan.
 * Clean scan requires begin>0,end>=begin,end<=published. Failed capture permits
 * begin/end0; any nonzero begin/end<=published and end>=begin when both exist.
 * No inferred physical order or atomicity. Do not serialize topology indices:
 * decode recomputes reducer, force ERROR with roles-1 when scan_error!=0.
 * A valid observation of device errors is codec success, not hardware success.
 * Reject all unknown magic,reserved,length,trailing/CRC/status/range/identity.
 * encode leaves wire untouched on failure, sets *written=0, allows capacity>=n.
 * decode always resets output on failure (roles-1), uses trusted expected boot
 * and now; boot mismatch/future/age>15s =>ESTALE. Age uses begin if present else
 * published, so a long scan cannot falsely appear fresh from publication.
 * No input/output overlap, heap, IO, shell or retries.
 */
int y4_ix_snapshot_encode(const struct y4_ix_snapshot *,void *,size_t,size_t *);
int y4_ix_snapshot_decode(const void *,size_t,const unsigned char [16],uint64_t,struct y4_ix_snapshot *);
#endif
