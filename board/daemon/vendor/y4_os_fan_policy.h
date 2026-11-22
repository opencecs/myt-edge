/* SPDX-License-Identifier: Apache-2.0 */
#ifndef Y4_OS_FAN_POLICY_H
#define Y4_OS_FAN_POLICY_H
#include <stdint.h>
struct y4_mcu_identity;
/* MCU 0.1.39 fan policy registers, verbatim values:
 * mode 0=legacy 1=auto 2=manual; status 0=none 1=persisting 2=committed
 * 3=failed; duty[] = stored FAN3/4/5 duties in percent 0..100. */
struct y4_fan_policy_state { uint8_t mode,status,duty[3]; };
/* Read the policy registers from an already-owned MCU descriptor. The
 * caller holds the mcu.lock; no writes, failed output unchanged. */
int y4_fan_policy_fetch(int fd,const struct y4_mcu_identity *id,
                        struct y4_fan_policy_state *out);
/* Apply a policy: takes the mcu.lock itself (bounded ~2s wait against the
 * monitor's per-cycle window), writes the three stored duties and then the
 * mode register (the commit), and polls the persistence status until it
 * leaves PENDING. Returns 0 and the post-commit readback, -EOPNOTSUPP on
 * MCU <0.1.39, -EBUSY when the lock stays taken, -ETIMEDOUT when the MCU
 * does not finish persisting, or a negative IO errno. No exec, no threads. */
int y4_fan_policy_apply(unsigned mode,const unsigned duty[3],
                        struct y4_fan_policy_state *out);
#endif
