/* SPDX-License-Identifier: Apache-2.0 */
#include "vendor/y4_os_command.h"
#include <errno.h>
int y4_command_poll(const struct y4_call *call) {
 if(!call||!call->now_ms)return -EINVAL;
 if(call->cancelled&&call->cancelled(call->opaque))return -ECANCELED;
 uint64_t now=call->now_ms(call->opaque);if(!now||now==UINT64_MAX||now<call->started_ms)return -EIO;
 return call->deadline_ms&&now>=call->deadline_ms?-ETIMEDOUT:0;
}
