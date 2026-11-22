#ifndef Y4_OS_TEMPERATURE_QUERY_H
#define Y4_OS_TEMPERATURE_QUERY_H
#include "y4_os_command.h"
#include "y4_os_tmp75_pm.h"
struct y4_temperature_reading {int readable,error;int64_t millicelsius;};
struct y4_temperature_snapshot {
 int scmi_present,scan_error,soc_valid,soc_error;unsigned soc_count,unknown_count;
 int64_t soc_max;
 struct y4_temperature_reading amb,user;
 int pm_checked,pm_error;
 struct y4_tmp75_pm_sample pm;
};
struct y4_temperature_context {const char *sysroot;int require_sysfs;};
/* Readonly SCMI hwmon observation. PCB candidates are not confirmed TMP75;
 * no PM bus probing, controller initialization, stale fallback or fan changes.
 * Caller supplies trusted sysfs namespace; no user-provided filesystem paths. */
int y4_temperature_read(const struct y4_call *,struct y4_temperature_snapshot *);
extern const struct y4_command y4_temperature_command;
extern const struct y4_command y4_temperature_fault_command;
#endif
