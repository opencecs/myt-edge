/* SPDX-License-Identifier: Apache-2.0 */
#ifndef Y4_BOARD_SSH_PASSWORD_H
#define Y4_BOARD_SSH_PASSWORD_H

#define Y4_SSH_USERNAME_MAX 32u
#define Y4_SSH_PASSWORD_MIN 6u
#define Y4_SSH_PASSWORD_MAX 256u

/* Change the local Linux password through Debian's chpasswd PAM policy.
 * The input is NUL-terminated and remains owned by the caller. */
int y4_ssh_change_password(const char *username, const char *password);

#endif
