#ifndef Y4_OS_TMP75_PM_H
#define Y4_OS_TMP75_PM_H
#include "y4_os_command.h"
#include <stdint.h>
struct y4_tmp75_pm_sample { int64_t millicelsius; uint32_t sequence; uint8_t config; uint16_t raw; int phase; unsigned attempts; int first_error; char diagnostic[1024]; };
/* Fixed PM diagnostic read-only operations; no PM reset, init, voltage or arbitrary command. */
int y4_tmp75_pm_read(const struct y4_call *, struct y4_tmp75_pm_sample *);
/* Maintenance-only NACK challenge; never selectable by normal temperature API. */
int y4_tmp75_pm_absent_address_probe(const struct y4_call *,struct y4_tmp75_pm_sample *);
int y4_tmp75_pm_parse(const char *,uint32_t,unsigned,uint8_t *,unsigned);
int y4_tmp75_decode(uint8_t,const uint8_t[2],int64_t *);
#endif
