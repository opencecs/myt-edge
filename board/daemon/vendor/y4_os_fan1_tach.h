/* SPDX-License-Identifier: Apache-2.0 */
#ifndef Y4_OS_FAN1_TACH_H
#define Y4_OS_FAN1_TACH_H
#include <stdint.h>
/* Read tach feedback from the kernel-owned SoC fan (1 or 2). */
int y4_soc_fan_tach_read(unsigned fan,uint8_t *valid,uint16_t *rpm);
#endif
