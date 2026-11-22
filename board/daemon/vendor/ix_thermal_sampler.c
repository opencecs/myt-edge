#define _GNU_SOURCE
#include "y4_os_ix_thermal.h"
#include "y4_os_ix_sampler.h"
#include "y4_os_ix_temperature.h"
#include <errno.h>
#include <poll.h>
#include <string.h>
#include <time.h>
static uint64_t now_ns(void){
 struct timespec t;
 if(clock_gettime(CLOCK_BOOTTIME,&t)||t.tv_sec<0||t.tv_nsec<0||t.tv_nsec>=1000000000L||
 (uint64_t)t.tv_sec>(UINT64_MAX-(uint64_t)t.tv_nsec)/1000000000u)return 0;
 return (uint64_t)t.tv_sec*1000000000u+(uint64_t)t.tv_nsec;
}
static uint64_t now_ms(void *ctx){(void)ctx;return now_ns()/1000000u;}
static int cancelled(void *ctx){
 struct pollfd p={.fd=*(int *)ctx,.events=POLLIN};
 int n=poll(&p,1,0);return n<0||!!(p.revents&(POLLIN|POLLHUP|POLLERR|POLLNVAL));
}
/* Dedicated owner only. Exposed internally for stubbed collection tests. */
int y4_ix_thermal_capture_at(int dir,const struct y4_ix_snapshot *snap,int stop_fd){
 if(!snap)return y4_ix_thermal_invalidate_at(dir);
 struct y4_ix_thermal_sample s={0};s.sampled_ns=now_ns();
 if(!s.sampled_ns)return -EIO;
 s.generation=snap->generation;memcpy(s.boot_id,snap->boot_id,16);
 struct y4_call call={.opaque=&stop_fd,.now_ms=now_ms,.cancelled=cancelled,
 .started_ms=s.sampled_ns/1000000u,.deadline_ms=s.sampled_ns/1000000u+2500u};
 s.status=y4_ix_bar_collect_snapshot(&call,snap,&s.result,s.bdf);
 if(s.status>0||s.status< -4095)s.status=-EPROTO;
 s.result.status=s.status;s.completed_ns=now_ns();
 if(!s.completed_ns||s.completed_ns<s.sampled_ns){(void)y4_ix_thermal_invalidate_at(dir);return -EIO;}
 int initialized=!s.status&&s.result.mapping_verified&&s.result.control0==0x11807u&&s.result.control1==0x1fu;
 y4_os_ix_temp_reading t;
 s.temperature_error=y4_os_ix_temp_decode(s.result.raw_temperature,s.result.calibration,
 initialized?Y4_OS_IX_TEMP_FLAG_INITIALIZED:0,s.status,s.sampled_ns,s.completed_ns,2000000000u,&t);
 s.valid=t.valid;s.millicelsius=t.valid?t.millicelsius:0;
 return y4_ix_thermal_store_at(dir,&s);
}
static int capture_hook(int dir,const struct y4_ix_snapshot *snap,void *context){
 return y4_ix_thermal_capture_at(dir,snap,*(int *)context);
}
int y4_ix_sampler_run_thermal(int dir,const char *root,int stopfd,const unsigned char boot[16],
 void (*note)(int,uint32_t,void *),void *opaque,struct y4_ix_sampler_result *out){
 return y4_ix_sampler_run_with_hook(dir,root,stopfd,boot,note,opaque,out,capture_hook,&stopfd);
}
