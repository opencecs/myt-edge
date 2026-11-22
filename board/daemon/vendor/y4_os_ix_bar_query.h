#ifndef Y4_OS_IX_BAR_QUERY_H
#define Y4_OS_IX_BAR_QUERY_H
#include "y4_os_ix_query.h"
#include "ix_bar_probe.h"
#include "y4_os_ix_temperature.h"
#define Y4_IX_BAR_FIELDS 38u
struct y4_ix_bar_context {
 struct y4_ix_query_context topology;
 ix_bar_probe_result result;
 y4_os_ix_temp_reading temperature;
 char bdf[13];
};
extern const struct y4_command y4_ix_bar_command;
#endif
