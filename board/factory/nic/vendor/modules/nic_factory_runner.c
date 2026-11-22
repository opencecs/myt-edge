/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "opencecs_nic_factory.h"
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
static uint64_t stamp(void) {
  struct timespec t;
  if (clock_gettime(CLOCK_MONOTONIC, &t))
    return 0;
  return (uint64_t)t.tv_sec * 1000u + (uint64_t)t.tv_nsec / 1000000u;
}
int oc_nic_factory_reap(struct oc_nic_child *child) {
  if (!child || child->pid <= 0)
    return -EINVAL;
  if (child->reaped)
    return 0;
  int status = 0;
  pid_t done = waitpid(child->pid, &status, WNOHANG);
  if (done == child->pid) {
    child->reaped = 1;
    child->wait_status = status;
    return 0;
  }
  if (done == 0)
    return -EAGAIN;
  if (errno == EINTR)
    return -EAGAIN;
  child->ownership_error = -errno;
  return child->ownership_error;
}
int oc_nic_factory_run_guarded(const char *tool, const char *const *argv,
                               const char *cwd, int fd, unsigned timeout,
                               int kill_timeout, int lease_fd,
                               struct oc_nic_child *child) {
  if (!tool || !argv || !cwd || fd < 0 || !timeout || !child ||
      (kill_timeout != 0 && kill_timeout != 1) ||
      (!kill_timeout && lease_fd < 0))
    return -EINVAL;
  *child = (struct oc_nic_child){0};
  pid_t parent = getpid();
  pid_t pid = fork();
  if (pid < 0)
    return -errno;
  if (!pid) {
    /* Read commands may be killed with their parent. An irreversible PG
     * writer instead outlives unexpected owner death and retains a duplicate
     * of the already-held whole-NIC flock across exec. The durable intent
     * barrier remains authoritative if an opaque vendor closes unknown FDs. */
    if (prctl(PR_SET_PDEATHSIG, kill_timeout ? SIGKILL : 0) ||
        getppid() != parent)
      _exit(126);
    if (!kill_timeout) {
      int guard = fcntl(lease_fd, F_DUPFD, 64);
      struct stat st;
      if (guard < 0 || fstat(guard, &st) || !S_ISREG(st.st_mode) ||
          st.st_uid || st.st_nlink != 1 ||
          (st.st_mode & 07777u) != 0600u ||
          flock(guard, LOCK_EX | LOCK_NB))
        _exit(126);
      int flags = fcntl(guard, F_GETFD);
      if (flags < 0 || fcntl(guard, F_SETFD, flags & ~FD_CLOEXEC))
        _exit(126);
    }
    if (getppid() != parent || chdir(cwd) ||
        dup2(fd, 1) < 0 || dup2(fd, 2) < 0)
      _exit(126);
    int null = open("/dev/null", O_RDONLY | O_CLOEXEC);
    if (null < 0 || dup2(null, 0) < 0)
      _exit(126);
    if (null > 2)
      close(null);
    char *const env[] = {"PATH=/bin:/usr/bin", "LANG=C", "LC_ALL=C", NULL};
    execve(tool, (char *const *)argv, env);
    _exit(127);
  }
  child->pid = pid;
  uint64_t deadline = stamp() + timeout;
  for (;;) {
    int rc = oc_nic_factory_reap(child);
    if (!rc)
      return WIFEXITED(child->wait_status) &&
                     WEXITSTATUS(child->wait_status) == 0
                 ? 0
                 : -EIO;
    if (rc != -EAGAIN)
      return rc;
    if (stamp() >= deadline) {
      if (kill_timeout)
        (void)kill(pid, SIGKILL);
      /* A write timeout retains the live child without interrupting OTP. */
      return -ETIMEDOUT;
    }
    struct timespec t = {0, 10000000L};
    while (nanosleep(&t, &t) && errno == EINTR) {
    }
  }
}

/* Historical mode wrapper remains valid for read-only runs. A write must
 * use run_guarded with the actual whole-NIC lease; missing authority rejects
 * before fork rather than accidentally creating an unguarded writer. */
int oc_nic_factory_run_mode(const char *tool, const char *const *argv,
                            const char *cwd, int fd, unsigned timeout,
                            int kill_timeout, struct oc_nic_child *child) {
  return oc_nic_factory_run_guarded(tool, argv, cwd, fd, timeout,
                                    kill_timeout, -1, child);
}

int oc_nic_factory_run(const char *tool, const char *const *argv,
                       const char *cwd, int fd, unsigned timeout,
                       struct oc_nic_child *child) {
  return oc_nic_factory_run_mode(tool, argv, cwd, fd, timeout, 1, child);
}
