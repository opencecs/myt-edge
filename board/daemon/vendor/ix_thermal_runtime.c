#define _GNU_SOURCE
#include "y4_os_ix_thermal.h"
#include "y4_os_boot_update_context.h"
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
int y4_ix_thermal_read(struct y4_ix_thermal_sample *o){
 if(!o)return -EINVAL;
 memset(o,0,sizeof *o);
 unsigned char boot[16];int e=y4_boot_update_current_id(boot);
 struct timespec t;uint64_t now=0;
 /* CLOCK_BOOTTIME keeps counting through suspend, matching the sampler
 * and the pcie collector, so cache age cannot hide suspend time. */
 if(!e&&clock_gettime(CLOCK_BOOTTIME,&t))e=-errno;
 if(!e){if(t.tv_sec<0||t.tv_nsec<0||t.tv_nsec>=1000000000L||
 (uint64_t)t.tv_sec>(UINT64_MAX-(uint64_t)t.tv_nsec)/1000000000u)e=-ERANGE;
 else now=(uint64_t)t.tv_sec*1000000000u+(uint64_t)t.tv_nsec;}
 int d=-1;if(!e){d=open("/run/y4-pcie",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);if(d<0)e=-errno;}
 if(!e)e=y4_ix_thermal_load_at(d,boot,now,o);
 if(d>=0&&close(d)&&!e)e=-errno;
 if(e){memset(o,0,sizeof *o);o->status=e;o->temperature_error=e;}
 return e;
}
