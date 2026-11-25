/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "ssh_password.h"

#include <errno.h>
#include <pwd.h>
#include <security/pam_appl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

static bool name_start(unsigned char c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_';
}

static bool valid_username(const char *name)
{
    if (!name || !name_start((unsigned char)*name))
        return false;
    for (size_t i = 1; name[i]; i++) {
        unsigned char c = (unsigned char)name[i];
        if (i >= Y4_SSH_USERNAME_MAX ||
            !(name_start(c) || (c >= '0' && c <= '9') || c == '-'))
            return false;
    }
    return true;
}

static bool login_shell(const char *path)
{
    static const char *const denied[] = {
        "nologin", "false", "sync", "halt", "shutdown", "true",
    };
    if (*path != '/')
        return false;
    const char *name = strrchr(path, '/') + 1;
    for (size_t i = 0; i < sizeof denied / sizeof denied[0]; i++)
        if (strcmp(name, denied[i]) == 0)
            return false;
    struct stat st;
    return stat(path, &st) == 0 && S_ISREG(st.st_mode) &&
           (st.st_mode & (S_IXUSR | S_IXGRP | S_IXOTH)) != 0;
}

static int check_local_account(const char *username)
{
    FILE *accounts = fopen("/etc/passwd", "re");
    if (!accounts)
        return -errno;

    char local_buffer[4096];
    struct passwd local, *entry;
    int result;
    while ((result = fgetpwent_r(accounts, &local, local_buffer, sizeof local_buffer, &entry)) == 0) {
        if (strcmp(entry->pw_name, username) == 0)
            break;
    }
    if (ferror(accounts))
        result = EIO;
    if (fclose(accounts) != 0 && result == 0)
        result = errno;
    if (result != 0)
        return -result;
    if (!login_shell(local.pw_shell))
        return -EACCES;

    /* PAM resolves the name through NSS. Require it to resolve to our local
     * entry before asking PAM to change the credential. */
    char nss_buffer[4096];
    struct passwd nss;
    result = getpwnam_r(username, &nss, nss_buffer, sizeof nss_buffer, &entry);
    if (result != 0)
        return -result;
    if (!entry)
        return -ENOENT;
    if (local.pw_uid != nss.pw_uid || local.pw_gid != nss.pw_gid ||
        strcmp(local.pw_dir, nss.pw_dir) != 0 ||
        strcmp(local.pw_shell, nss.pw_shell) != 0)
        return -EACCES;
    return 0;
}

static int password_conversation(int count, const struct pam_message **messages,
                                 struct pam_response **responses, void *data)
{
    *responses = NULL;
    if (count <= 0 || count > PAM_MAX_NUM_MSG)
        return PAM_CONV_ERR;

    struct pam_response *reply = calloc((size_t)count, sizeof *reply);
    if (!reply)
        return PAM_BUF_ERR;
    const char *password = data;
    int error = PAM_CONV_ERR;
    for (int i = 0; i < count; i++) {
        if (!messages[i])
            goto failed;
        switch (messages[i]->msg_style) {
        case PAM_PROMPT_ECHO_OFF:
            reply[i].resp = strdup(password);
            if (!reply[i].resp) {
                error = PAM_BUF_ERR;
                goto failed;
            }
            break;
        case PAM_ERROR_MSG:
        case PAM_TEXT_INFO:
            /* PAM text may contain sensitive details. Ignore it here. */
            break;
        default:
            goto failed;
        }
    }
    *responses = reply;
    return PAM_SUCCESS;

failed:
    for (int i = 0; i < count; i++) {
        if (reply[i].resp)
            explicit_bzero(reply[i].resp, strlen(reply[i].resp));
        free(reply[i].resp);
    }
    free(reply);
    return error;
}

int y4_ssh_change_password(const char *username, const char *password)
{
    if (!valid_username(username) || !password)
        return -EINVAL;
    size_t length = strnlen(password, Y4_SSH_PASSWORD_MAX + 1u);
    if (length < Y4_SSH_PASSWORD_MIN || length > Y4_SSH_PASSWORD_MAX ||
        strpbrk(password, "\r\n"))
        return -EINVAL;

    int result = check_local_account(username);
    if (result != 0)
        return result;

    /* Use the same service and noninteractive password update as Debian's
     * chpasswd. Each call has a private conversation and PAM handle. */
    struct pam_conv conversation = {
        .conv = password_conversation,
        .appdata_ptr = (void *)password,
    };
    pam_handle_t *handle = NULL;
    int status = pam_start("chpasswd", username, &conversation, &handle);
    if (status == PAM_SUCCESS) {
        status = pam_chauthtok(handle, 0);
        (void)pam_end(handle, status);
    }
    switch (status) {
    case PAM_SUCCESS:
        return 0;
    case PAM_USER_UNKNOWN:
        return -ENOENT;
    case PAM_PERM_DENIED:
    case PAM_AUTHTOK_ERR:
    case PAM_TRY_AGAIN:
    case PAM_MAXTRIES:
    case PAM_CONV_ERR:
        return -EACCES;
    case PAM_AUTHTOK_LOCK_BUSY:
        return -EBUSY;
    case PAM_BUF_ERR:
        return -ENOMEM;
    default:
        return -EIO;
    }
}
