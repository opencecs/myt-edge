/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "board_daemon.h"
#include "vendor/y4_os_ix_thermal.h"
#include "vendor/y4_os_ix_sampler.h"
#include "vendor/y4_os_boot_update_context.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#if !defined(SYS_close_range) && defined(__aarch64__)
#define SYS_close_range 436
#endif
extern int y4_ix_thermal_capture_at(int,const struct y4_ix_snapshot *,int);
static int owner_stop=-1;
void y4_board_pcie_child_close(void){if(owner_stop>=0)close(owner_stop);owner_stop=-1;}
static unsigned nibble(unsigned char c) {
 return c>='0'&&c<='9'?(unsigned)(c-'0'):c>='a'&&c<='f'?(unsigned)(c-'a')+10u:16u;
}
/* Native same-boot identity adapter; no State or product boot context is read. */
int y4_boot_update_current_id(unsigned char out[16]) {
 if(!out)return -EINVAL;
 int fd=open("/proc/sys/kernel/random/boot_id",O_RDONLY|O_CLOEXEC|O_NOFOLLOW);
 if(fd<0)return -errno;char text[38];ssize_t n=read(fd,text,sizeof text);int e=n<0?-errno:0;
 if(close(fd)&&!e)e=-errno;if(e)return e;if(n!=37||text[36]!='\n')return -EPROTO;
 unsigned char value[16];unsigned at=0;
 for(unsigned i=0;i<36u;){
  if(i==8u||i==13u||i==18u||i==23u){if(text[i++]!='-')return -EPROTO;continue;}
  unsigned a=nibble((unsigned char)text[i++]),b=nibble((unsigned char)text[i++]);
  if(a>15u||b>15u||at>=16u)return -EPROTO;value[at++]=(unsigned char)((a<<4)|b);
 }
 if(at!=16u)return -EPROTO;memcpy(out,value,16);return 0;
}
/* The cache clock must include suspend time, so a sample cannot look
 * fresh after resume just because CLOCK_MONOTONIC paused. Producers and
 * consumers share CLOCK_BOOTTIME for sampled_ns and age comparisons. */
static uint64_t monotonic_ns(void) {
 struct timespec t;if(clock_gettime(CLOCK_BOOTTIME,&t)||t.tv_sec<0)return 0;
 return (uint64_t)t.tv_sec*UINT64_C(1000000000)+(uint64_t)t.tv_nsec;
}
static int stopped(int fd,int timeout) {
 struct pollfd p={.fd=fd,.events=POLLIN};int n;
 do{n=poll(&p,1,timeout);}while(n<0&&errno==EINTR);
 return n<0?1:!!(p.revents&(POLLIN|POLLHUP|POLLERR|POLLNVAL));
}
static int capture_loop(int directory,int stopfd,const unsigned char boot[16]) {
 int lease=openat(directory,".sampler.lock",O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0600);if(lease<0)return 1;
 struct stat st;if(fstat(lease,&st)||!S_ISREG(st.st_mode)||st.st_uid||st.st_nlink!=1u||
 (st.st_mode&07777u)!=0600u||flock(lease,LOCK_EX|LOCK_NB)){close(lease);return 1;}
 (void)y4_ix_thermal_invalidate_at(directory);
 while(!stopped(stopfd,0)) {
  struct y4_ix_snapshot snap={0};snap.generation=monotonic_ns();snap.published_mono_ns=snap.generation;
  memcpy(snap.boot_id,boot,16);
  int collected=y4_ix_inventory_read("/sys/bus/pci/devices",&snap.inventory);
  int e=y4_ix_thermal_capture_at(directory,&snap,stopfd);
  if(e)fprintf(stderr,"y4-board pcie switch capture status=%d inventory=%d\n",e,collected);
  /* A read-only BAR child can outlive its cooperative deadline. Retain and
   * reap that exact child before another capture, independently of MCU/API. */
  struct y4_ix_thermal_sample observation={0};
  int loaded=y4_ix_thermal_load_at(directory,boot,monotonic_ns(),&observation);
  pid_t pending=!loaded?observation.result.unreaped_pid:0;
  int child_failed=0;
  while(pending>0) {
   int ws;pid_t got=waitpid(pending,&ws,WNOHANG);
   if(got==pending){pending=0;break;}
   if(got<0&&errno!=EINTR){child_failed=1;break;}
   if(stopped(stopfd,100)){/* Read-only child: cooperative callback sees EOF. */
    while((got=waitpid(pending,&ws,0))<0&&errno==EINTR){}
    if(got!=pending)child_failed=1;
    pending=0;break;
   }
  }
  if(child_failed||e==-EINPROGRESS)break;
  if(stopped(stopfd,5000))break;
 }
 (void)y4_ix_thermal_invalidate_at(directory);close(lease);return 0;
}
pid_t y4_board_pcie_start(void) {
 if(owner_stop>=0)return -EBUSY;int e=y4_board_private_dir("/run/y4-pcie");if(e)return e;
 int dir=open("/run/y4-pcie",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);if(dir<0)return -errno;
 unsigned char boot[16];e=y4_boot_update_current_id(boot);if(e){close(dir);return e;}
 int p[2];if(pipe2(p,O_CLOEXEC)){e=-errno;close(dir);return e;}
 pid_t parent=getpid(),pid=fork();
 if(pid<0){e=-errno;close(dir);close(p[0]);close(p[1]);return e;}
 if(!pid) {
  close(p[1]);
  if(prctl(PR_SET_PDEATHSIG,SIGKILL)||getppid()!=parent)_exit(124);
  struct sigaction sa={.sa_handler=SIG_DFL};sigemptyset(&sa.sa_mask);
  if(sigaction(SIGTERM,&sa,NULL)||sigaction(SIGINT,&sa,NULL))_exit(124);
  int a=fcntl(dir,F_DUPFD_CLOEXEC,3),b=fcntl(p[0],F_DUPFD_CLOEXEC,3);
  if(a<0||b<0)_exit(124);
#ifdef SYS_close_range
  unsigned lo=(unsigned)(a<b?a:b),hi=(unsigned)(a>b?a:b);
  if((lo>3u&&syscall(SYS_close_range,3u,lo-1u,0))||
     (hi>lo+1u&&syscall(SYS_close_range,lo+1u,hi-1u,0))||
     syscall(SYS_close_range,hi+1u,UINT_MAX,0))_exit(124);
#else
  _exit(124);
#endif
  int rc=capture_loop(a,b,boot);close(a);close(b);_exit(rc);
 }
 close(p[0]);close(dir);owner_stop=p[1];return pid;
}
int y4_board_pcie_tick(pid_t *pid) {
 if(!pid||*pid<0)return -EINVAL;if(!*pid)return 0;int status;pid_t got=waitpid(*pid,&status,WNOHANG);
 if(got==0)return 0;if(got<0)return errno==EINTR?0:-errno;
 *pid=0;if(owner_stop>=0){close(owner_stop);owner_stop=-1;}
 return WIFEXITED(status)&&!WEXITSTATUS(status)?1:-EIO;
}
int y4_board_pcie_stop(pid_t *pid) {
 if(!pid)return -EINVAL;if(owner_stop>=0){close(owner_stop);owner_stop=-1;}
 for(unsigned i=0;i<200u;i++){int e=y4_board_pcie_tick(pid);if(e||!*pid)return e<0?e:0;
  struct timespec t={0,10000000};(void)nanosleep(&t,NULL);}
 return -ETIMEDOUT; /* Retain the exact PID; no fictitious reap. */
}
int y4_board_pcie_temperature(struct y4_board_temperature *out) {
 if(!out)return -EINVAL;struct y4_ix_thermal_sample s={0};int e=y4_ix_thermal_read(&s);
 *out=(struct y4_board_temperature){!e&&s.valid&&!s.status&&!s.temperature_error,
  e?e:s.status?s.status:s.temperature_error,s.millicelsius,
  e?0u:(uint32_t)(s.age_ms>UINT32_MAX?UINT32_MAX:s.age_ms)};return out->error;
}
