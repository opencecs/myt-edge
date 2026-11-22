#!/usr/bin/env python3
"""Test actual MVX poll/completion C bodies with a userspace vb2 model.

Real pthread/epoll/eventfd waits exercise the registration race. This does not
run an in-kernel driver or decode firmware. All generated files are temporary.
"""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile


def function(name, filename, base):
    source = (base / filename).read_text()
    match = re.search(r"(?m)^(?:void|unsigned int) " + re.escape(name) + r"\(", source)
    if match is None:
        raise ValueError("Required MVX function missing: " + name)
    start = match.start()
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        if source[end] == "{":
            depth += 1
        if source[end] == "}":
            depth -= 1
        end += 1
    return source[start:end]


prefix = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <unistd.h>
typedef unsigned int __poll_t;
#define POLLERR EPOLLERR
#define MVX_DIR_INPUT 0
#define MVX_DIR_OUTPUT 1
#define MVX_SESSION_WARN(...) ((void)0)
#define MVX_SESSION_VERBOSE(...) ((void)0)
struct wait_queue { int fd; unsigned long registered_key; unsigned int last_mask; };
struct vb2_queue {
    bool streaming, is_output;
    int done_count;
    struct wait_queue done_wq;
};
struct v4l2_fh { struct wait_queue wait; int pending; };
struct mvx_v4l2_session {
    struct v4l2_fh fh;
    pthread_mutex_t mutex;
    struct { int error; } session;
    struct { struct vb2_queue vb2_queue; } port[2];
};
struct file { void *private_data; struct mvx_v4l2_session *owner; };
struct poll_table_struct { int epfd; unsigned long key; bool register_enabled; };
struct vb2_buffer { struct vb2_queue *vb2_queue; };
enum vb2_buffer_state { VB2_BUF_STATE_DONE, VB2_BUF_STATE_ERROR, VB2_BUF_STATE_QUEUED };
static struct mvx_v4l2_session *file_to_session(struct file *f) { return f->owner; }
static unsigned long poll_requested_events(struct poll_table_struct *p) { return p->key; }
static int mutex_lock_interruptible(pthread_mutex_t *m) { return pthread_mutex_lock(m); }
static void mutex_unlock(pthread_mutex_t *m) { assert(pthread_mutex_unlock(m) == 0); }
static bool vb2_is_streaming(struct vb2_queue *q) { return q->streaming; }
static int v4l2_event_pending(struct v4l2_fh *fh) { return fh->pending; }
static void wait_init(struct wait_queue *q) {
    q->fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    assert(q->fd >= 0);
    q->registered_key = q->last_mask = 0;
}
static void poll_wait(struct file *f, struct wait_queue *q,
                      struct poll_table_struct *p) {
    struct epoll_event ev = { .events = EPOLLIN };
    (void)f;
    if (!p->register_enabled) return;
    assert(q->fd >= 0); /* Catch registration before queue initialization. */
    q->registered_key |= p->key;
    ev.data.fd = q->fd;
    if (epoll_ctl(p->epfd, EPOLL_CTL_ADD, q->fd, &ev) != 0)
        assert(errno == EEXIST);
}
static void wake_up_interruptible_poll(struct wait_queue *q, unsigned int mask) {
    uint64_t one = 1;
    assert(q->fd >= 0);
    q->last_mask = mask;
    if (q->registered_key & mask)
        assert(write(q->fd, &one, sizeof(one)) == sizeof(one));
}
static unsigned int vb2_poll(struct vb2_queue *q, struct file *f,
                              struct poll_table_struct *p) {
    poll_wait(f, &q->done_wq, p);
    if (!q->streaming) return EPOLLERR;
    if (q->done_count)
        return q->is_output ? EPOLLOUT | EPOLLWRNORM : EPOLLIN | EPOLLRDNORM;
    return 0;
}
static void vb2_buffer_done(struct vb2_buffer *vb, enum vb2_buffer_state state) {
    struct vb2_queue *q = vb->vb2_queue;
    if (state == VB2_BUF_STATE_DONE || state == VB2_BUF_STATE_ERROR) {
        q->done_count++;
        wake_up_interruptible_poll(&q->done_wq,
            q->is_output ? EPOLLOUT | EPOLLWRNORM : EPOLLIN | EPOLLRDNORM);
    }
    vb->vb2_queue = NULL; /* Completion transfers buffer ownership. */
}
'''
suffix = r'''
struct waiter {
    struct file file;
    struct poll_table_struct table;
    pthread_barrier_t registered;
    unsigned int result;
    bool timeout;
};
static void *wait_thread(void *arg) {
    struct waiter *w = arg;
    struct epoll_event ev[4];
    w->result = mvx_v4l2_poll(&w->file, &w->table);
    /* Kernel fs/select.c likewise clears _qproc after its first iteration. */
    w->table.register_enabled = false;
    pthread_barrier_wait(&w->registered);
    if (!w->result) {
        int n = epoll_wait(w->table.epfd, ev, 4, 80);
        assert(n >= 0);
        w->timeout = n == 0;
        if (n) w->result = mvx_v4l2_poll(&w->file, &w->table);
    }
    return NULL;
}
static void run_case(const char *name, bool fixed, int dir,
                     enum vb2_buffer_state state, int initial,
                     bool fatal_error, bool expect_ready) {
    struct mvx_v4l2_session session = { 0 };
    struct vb2_queue *q = &session.port[dir].vb2_queue;
    struct vb2_buffer vb;
    struct waiter w = { 0 };
    pthread_t thread;
    unsigned int wanted = dir == MVX_DIR_INPUT ? EPOLLOUT : EPOLLIN;
    pthread_mutex_init(&session.mutex, NULL);
    wait_init(&session.fh.wait);
    session.port[0].vb2_queue.done_wq.fd = -1;
    session.port[1].vb2_queue.done_wq.fd = -1;
    session.port[0].vb2_queue.is_output = true;
    if (initial) wait_init(&q->done_wq);
    if (initial == 2) q->streaming = true;
    w.file.private_data = &session.fh;
    w.file.owner = &session;
    w.table.epfd = epoll_create1(EPOLL_CLOEXEC);
    assert(w.table.epfd >= 0);
    w.table.key = wanted | EPOLLPRI | EPOLLERR;
    w.table.register_enabled = true;
    pthread_barrier_init(&w.registered, NULL, 2);
    assert(pthread_create(&thread, NULL, wait_thread, &w) == 0);
    pthread_barrier_wait(&w.registered);
    pthread_mutex_lock(&session.mutex);
    if (fatal_error) {
        struct mvx_v4l2_session *vsession = &session;
        vsession->session.error = -EIO;
        if (fixed) {
            ERROR_WAKE
        }
    } else {
        if (!initial) wait_init(&q->done_wq);
        q->streaming = true;
        vb.vb2_queue = q;
        if (fixed) mvx_v4l2_buffer_done(&session, &vb, state);
        else vb2_buffer_done(&vb, state);
        assert(vb.vb2_queue == NULL);
    }
    pthread_mutex_unlock(&session.mutex);
    assert(pthread_join(thread, NULL) == 0);
    assert((w.result != 0) == expect_ready);
    assert(w.timeout == !expect_ready);
    if (expect_ready) {
        if (fatal_error) assert(w.result == EPOLLERR);
        else {
            assert(w.result & wanted);
            assert(!(w.result & EPOLLERR)); /* ERROR frame is dequeueable data. */
            assert(q->done_count == 1); /* Wake must follow publication. */
        }
    }
    if (fixed && !fatal_error && state == VB2_BUF_STATE_QUEUED)
        assert(session.fh.wait.last_mask == 0);
    close(w.table.epfd);
    close(session.fh.wait.fd);
    if (q->done_wq.fd >= 0) close(q->done_wq.fd);
    pthread_barrier_destroy(&w.registered);
    pthread_mutex_destroy(&session.mutex);
    printf("PASS %s\n", name);
}
int main(void) {
    run_case("negative_old_late_capture_loses_wakeup", false, 1,
             VB2_BUF_STATE_DONE, 0, false, false);
    run_case("old_already_streaming_has_registered_queue", false, 1,
             VB2_BUF_STATE_DONE, 2, false, true);
    run_case("fixed_late_capture_allocation_done", true, 1,
             VB2_BUF_STATE_DONE, 0, false, true);
    run_case("fixed_allocated_capture_not_streaming", true, 1,
             VB2_BUF_STATE_DONE, 1, false, true);
    run_case("fixed_capture_error_is_readable", true, 1,
             VB2_BUF_STATE_ERROR, 0, false, true);
    run_case("fixed_late_output_done", true, 0,
             VB2_BUF_STATE_DONE, 0, false, true);
    run_case("fixed_late_output_error_is_writable", true, 0,
             VB2_BUF_STATE_ERROR, 0, false, true);
    run_case("queued_start_failure_is_not_completion", true, 1,
             VB2_BUF_STATE_QUEUED, 0, false, false);
    run_case("negative_old_session_error_loses_wakeup", false, 1,
             VB2_BUF_STATE_DONE, 0, true, false);
    run_case("fixed_session_error_before_allocation", true, 1,
             VB2_BUF_STATE_DONE, 0, true, true);
    return 0;
}
'''
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-source", type=Path, required=True,
                        help="Kernel source checkout containing the MVX V4L2 driver")
    args = parser.parse_args()
    base = args.kernel_source.resolve() / "drivers/media/platform/cix/amvx/driver/if/v4l2"
    if not (base / "mvx_v4l2_session.c").is_file():
        parser.error("--kernel-source does not contain the expected MVX driver")
    helper = function("mvx_v4l2_buffer_done", "mvx_v4l2_session.c", base)
    pollfn = function("mvx_v4l2_poll", "mvx_v4l2_fops.c", base)
    session = (base / "mvx_v4l2_session.c").read_text()
    errorcase = session.split("case MVX_SESSION_EVENT_ERROR: {", 1)[1].split("        break;", 1)[0]
    wake = "wake_up_interruptible_poll(&vsession->fh.wait, EPOLLERR);"
    assert wake in errorcase, "Session errors must wake file-handle pollers"
    code = prefix + "\n" + helper + "\n" + pollfn + "\n" + suffix.replace("ERROR_WAKE", wake)

    with tempfile.TemporaryDirectory(prefix="y4-mvx-poll-test-") as raw:
        output = Path(raw)
        c_source, executable = output / "mvx_poll_harness.c", output / "mvx_poll_harness"
        c_source.write_text(code)
        subprocess.run(["cc", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                        "-o", str(executable), str(c_source)], check=True)
        result = subprocess.run([str(executable)], capture_output=True, text=True, check=True)
        print(result.stdout, end="")

    # Enforce coverage: every real completion must pass through the helper.
    calls = []
    for path in base.glob("*.c"):
        for line_number, line in enumerate(path.read_text().splitlines(), 1):
            if re.match(r"\s*vb2_buffer_done\(", line):
                calls.append(f"{path.name}:{line_number}")
    assert len(calls) == 1, calls
    assert result.stdout.count("PASS ") == 10, result.stdout
    print("10 tests passed; actual C bodies, userspace vb2 model, no device access.")


if __name__ == "__main__":
    main()
