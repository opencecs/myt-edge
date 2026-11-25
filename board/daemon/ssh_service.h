/* SPDX-License-Identifier: Apache-2.0 */
#ifndef Y4_SSH_SERVICE_H
#define Y4_SSH_SERVICE_H

#include <stdbool.h>

struct y4_ssh_status {
 bool enabled, running;
 bool service_enabled, service_running;
 bool socket_enabled, socket_running;
};

/* Serialize changes to the SSH units with other callers in the API layer. */
int y4_ssh_get_status(struct y4_ssh_status *status);
int y4_ssh_set_enabled(bool enabled, struct y4_ssh_status *status);

#endif
