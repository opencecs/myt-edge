#ifndef Y4_OS_IX_SAMPLER_H
#define Y4_OS_IX_SAMPLER_H
#include "y4_os_ix_snapshot_store.h"
#define Y4_IX_SAMPLER_INTERVAL_MS 5000
struct y4_ix_sampler_result {int primary_error,invalidate_error;uint64_t samples;};
/* Blocking internal loop for a DEDICATED process, not MCU/control/API loop.
 * Borrow private same-UID0700 directory, read pipe stop_fd and nonzero boot ID,
 * trusted pci_root, result. Does NOT fork, close borrowed FDs, install signals,
 * access MCU or control hardware. Caller owns parent death/FD/stop/reap.
 * Set result deterministically even invalid args. No heap or capture retries.
 * Own ".sampler.lock" via openat RDWR|CREAT|NOFOLLOW|NONBLOCK|CLOEXEC0600;
 * require regular,selfUID,exact0600,nlink1, then flock EX|NB. No unlink lock.
 * Before lock success NEVER invalidate another owner's pcie.bin.
 * Once locked, invalidate previous record, poll stop pipe before each capture
 * and between cycles5s. Readability/HUP =>normal stop; ERR/NVAL=>error.
 * Validate stop FD is an open readable pipe; no consuming bytes or mutations.
 * Run y4_ix_inventory_read once per cycle. Captured EAGAIN/identity/scan failures
 * are published as error observations when codec-valid, not loop retries.
 * Published monotonic ns (checked range) is generation too; must strictly
 * increase each cycle. Do not invent gen sequence across owner restarts.
 * Failure to clock/encode/store is terminal; no republish old successful sample.
 * On every owned exit invalidate pcie.bin before releasing flock. Preserve
 * primary_error and invalidate_error separately; return primary if nonzero,
 * else invalidate error. Do not overwrite operation error with log status.
 * samples counts successful store calls; no physical-health implication.
 * Optional callback only on first capture and changed capture rc, not periodic
 * success spam; callback takes captured rc,count,opaque, has no return status.
 * Callback must be nonblocking/fork-safe for actual caller. No stdout/stderr.
 * Stop is checked between captures; sysfs kernel blocking is NOT hard realtime.
 */
int y4_ix_sampler_run(int,const char *,int,const unsigned char [16],
 void (*)(int,uint32_t,void *),void *,struct y4_ix_sampler_result *);
/* Hook executes under sampler lease, once per stored snapshot; NULL snapshot
 * invalidates hook state on owned start/exit. Failure terminates owner. */
int y4_ix_sampler_run_with_hook(int,const char *,int,const unsigned char [16],
 void (*)(int,uint32_t,void *),void *,struct y4_ix_sampler_result *,
 int (*)(int,const struct y4_ix_snapshot *,void *),void *);
int y4_ix_sampler_run_thermal(int,const char *,int,const unsigned char [16],
 void (*)(int,uint32_t,void *),void *,struct y4_ix_sampler_result *);
#endif
