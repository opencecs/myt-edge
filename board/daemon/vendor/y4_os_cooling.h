#ifndef Y4_OS_COOLING_H
#define Y4_OS_COOLING_H
#include "y4_os_temperature_query.h"
/* PCB_AMB mapping was proven on board. Control data must instead come
 * from a fresh native PM transaction with explicit I2C success, not SCMI's
 * unconditional success/zero. Use measured board and SoC temperatures for cooling; invalid samples
 * immediately restore full speed. */
#define Y4_TMP75_PCB_AMB_MAPPING_VERIFIED 1
#define Y4_TMP75_AUTOMATIC_PWM_ENABLED 1
#define Y4_INSTALLED_FAN_MASK 7u /* V11 populated FAN3–FAN5, channels 0–2. */
struct y4_cooling { unsigned duty,valid_samples,lower_samples; };
struct y4_cooling_output { unsigned duty; int valid,error; int16_t decicelsius; };
int y4_mcu_cooling_apply(int,const struct y4_cooling_output *);
struct y4_mcu_identity;
/* 0.1.26+: staged CRC frame, verified commit; older MCU uses fail-closed legacy. */
int y4_mcu_cooling_apply_for(int,const struct y4_mcu_identity *,const struct y4_cooling_output *);
struct y4_cooling_output y4_cooling_step(struct y4_cooling *,int,
    const struct y4_temperature_snapshot *,int);
/* MCU >=0.1.19 only; invalidate, write deci-Celsius, commit valid last. */
int y4_mcu_soc_display(int,int,const struct y4_temperature_snapshot *);
#endif
