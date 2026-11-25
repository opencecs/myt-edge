/* SPDX-License-Identifier: Apache-2.0 */
#define _DEFAULT_SOURCE
#include "ssh_api.h"
#include "ssh_password.h"

#include <errno.h>
#include <glib.h>
#include <string.h>

struct ssh_request {
    bool enabled;
    char username[Y4_SSH_USERNAME_MAX + 1u];
    char password[Y4_SSH_PASSWORD_MAX + 1u];
};

static struct {
    bool pending;
    gint completed;
    struct ssh_request request;
    struct y4_ssh_reply reply;
} work;

static void skip_space(const char **at)
{
    while (**at && strchr(" \t\r\n", **at))
        ++*at;
}

static bool take(const char **at, char ch)
{
    skip_space(at);
    if (**at != ch)
        return false;
    ++*at;
    return true;
}

static bool read_hex4(const char **at, gunichar *value)
{
    *value = 0;
    for (unsigned i = 0; i < 4; ++i) {
        int digit = g_ascii_xdigit_value(**at);
        if (digit < 0)
            return false;
        *value = (*value << 4) | (gunichar)digit;
        ++*at;
    }
    return true;
}

static bool read_escape(const char **at, char bytes[6], size_t *length)
{
    char kind = **at;
    if (!kind)
        return false;
    ++*at;
    if (strchr("\"\\/", kind)) {
        bytes[0] = kind;
        *length = 1;
        return true;
    }
    if (kind == 't') {
        bytes[0] = '\t';
        *length = 1;
        return true;
    }
    if (kind != 'u')
        return false;

    gunichar value;
    if (!read_hex4(at, &value))
        return false;
    if (value >= 0xd800u && value <= 0xdbffu) {
        gunichar tail;
        if ((*at)[0] != '\\' || (*at)[1] != 'u')
            return false;
        *at += 2;
        if (!read_hex4(at, &tail) || tail < 0xdc00u || tail > 0xdfffu)
            return false;
        value = 0x10000u + ((value - 0xd800u) << 10) + (tail - 0xdc00u);
    }
    if ((value < 32u && value != '\t') || !g_unichar_validate(value))
        return false;
    *length = (size_t)g_unichar_to_utf8(value, bytes);
    return true;
}

static bool read_string(const char **at, char *out, size_t cap)
{
    if (!take(at, '"'))
        return false;
    size_t used = 0;
    while (**at && **at != '"') {
        char bytes[6];
        size_t length = 1;
        unsigned char ch = (unsigned char)*(*at)++;
        if (ch < 32u)
            return false;
        bytes[0] = (char)ch;
        if (ch == '\\' && !read_escape(at, bytes, &length))
            return false;
        if (length >= cap - used)
            return false;
        memcpy(out + used, bytes, length);
        used += length;
    }
    out[used] = '\0';
    if (**at != '"')
        return false;
    ++*at;
    return g_utf8_validate(out, (gssize)used, NULL);
}

static bool parse_request(enum y4_ssh_operation operation, const char *at,
                          struct ssh_request *request)
{
    if (!take(&at, '{'))
        return false;
    unsigned seen = 0;
    do {
        char key[16];
        if (!read_string(&at, key, sizeof key) || !take(&at, ':'))
            return false;
        if (operation == Y4_SSH_SET && !strcmp(key, "enabled") && !seen) {
            skip_space(&at);
            if (!strncmp(at, "true", 4)) {
                request->enabled = true;
                at += 4;
            } else if (!strncmp(at, "false", 5)) {
                at += 5;
            } else {
                return false;
            }
            seen = 1;
        } else if (operation == Y4_SSH_PASSWORD && !strcmp(key, "username") &&
                   !(seen & 1u)) {
            if (!read_string(&at, request->username, sizeof request->username))
                return false;
            seen |= 1u;
        } else if (operation == Y4_SSH_PASSWORD && !strcmp(key, "password") &&
                   !(seen & 2u)) {
            if (!read_string(&at, request->password, sizeof request->password))
                return false;
            seen |= 2u;
        } else {
            return false;
        }
    } while (take(&at, ','));
    if (!take(&at, '}'))
        return false;
    skip_space(&at);
    return !*at && seen == (operation == Y4_SSH_SET ? 1u : 3u);
}

static gpointer run_request(gpointer unused)
{
    (void)unused;
    struct y4_ssh_reply *reply = &work.reply;
    switch (reply->operation) {
    case Y4_SSH_STATUS:
        reply->result = y4_ssh_get_status(&reply->status);
        break;
    case Y4_SSH_SET:
        reply->result = y4_ssh_set_enabled(work.request.enabled, &reply->status);
        break;
    case Y4_SSH_PASSWORD:
        reply->result = y4_ssh_change_password(work.request.username, work.request.password);
        break;
    case Y4_SSH_NONE:
        reply->result = -EINVAL;
        break;
    }
    explicit_bzero(&work.request, sizeof work.request);
    g_atomic_int_set(&work.completed, 1);
    return NULL;
}

int y4_ssh_api_submit(enum y4_ssh_operation operation, uint64_t id,
                      const char *body, size_t length)
{
    if (work.pending)
        return -EBUSY;
    if (operation != Y4_SSH_STATUS) {
        if (!body || !length || length > Y4_SSH_BODY_LIMIT || body[length] ||
            memchr(body, 0, length) ||
            !parse_request(operation, body, &work.request)) {
            explicit_bzero(&work.request, sizeof work.request);
            return -EBADMSG;
        }
    }
    work.reply = (struct y4_ssh_reply){ .id = id, .operation = operation };
    g_atomic_int_set(&work.completed, 0);
    GThread *thread = g_thread_try_new("boardd-ssh", run_request, NULL, NULL);
    if (!thread) {
        explicit_bzero(&work.request, sizeof work.request);
        return -EAGAIN;
    }
    work.pending = true;
    g_thread_unref(thread);
    return 0;
}

bool y4_ssh_api_take(struct y4_ssh_reply *reply)
{
    if (!work.pending || !g_atomic_int_get(&work.completed))
        return false;
    *reply = work.reply;
    work.pending = false;
    return true;
}
