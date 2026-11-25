/* SPDX-License-Identifier: Apache-2.0 */
#ifndef Y4_SSH_API_H
#define Y4_SSH_API_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "ssh_service.h"

#define Y4_SSH_BODY_LIMIT 2048u

enum y4_ssh_operation {
    Y4_SSH_NONE,
    Y4_SSH_STATUS,
    Y4_SSH_SET,
    Y4_SSH_PASSWORD,
};

struct y4_ssh_reply {
    uint64_t id;
    enum y4_ssh_operation operation;
    int result;
    struct y4_ssh_status status;
};

/* The HTTP loop owns submission and completion. At most one operation runs
 * at a time. For a write, body has length bytes followed by a NUL. */
int y4_ssh_api_submit(enum y4_ssh_operation operation, uint64_t id,
                      const char *body, size_t length);
bool y4_ssh_api_take(struct y4_ssh_reply *reply);

#endif
