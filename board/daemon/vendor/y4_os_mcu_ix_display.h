#ifndef Y4_OS_MCU_IX_DISPLAY_H
#define Y4_OS_MCU_IX_DISPLAY_H
#include "y4_os_mcu.h"
struct y4_ix_thermal_sample;
/* Caller exclusively owns the MCU I2C descriptor and supplies a same-boot,
 * previously authenticated IX sample. Uses CLOCK_MONOTONIC, not sample.age_ms
 * alone. Only verified protocol/board MCU 0.1.x patch>=28 is supported; other
 * versions return -EOPNOTSUPP, no I2C. No reset/reconfiguration/retry.
 * 0 means frame committed AND publication readback verified; a normal invalid
 * sensor is sent as zero temperature/negative errno/valid=0 and still returns 0.
 * Negative result means transport/protocol/clock/argument failure, NOT a reason
 * to mark all MCU functions failed. Never feed this into fan validity.
 * A frame that reaches 15 seconds before commit is abandoned (-ESTALE);
 * MCU staging timeout must invalidate it. No second frame is attempted.
 */
int y4_mcu_ix_display(int, const struct y4_mcu_identity *,
                      const struct y4_ix_thermal_sample *);
#endif
