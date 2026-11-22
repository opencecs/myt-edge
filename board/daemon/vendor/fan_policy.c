/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "board_daemon.h" /* Y4_BOARD_RUNTIME (mcu.lock location) */
#include "y4_os_fan_policy.h"
#include "y4_os_mcu.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#define Y4_POLICY_REGISTER_STATUS 0x72u
#define Y4_POLICY_REGISTER_MODE 0x73u
#define Y4_POLICY_REGISTER_DUTY 0x7cu
#define Y4_POLICY_MODE_MAX 2u
#define Y4_POLICY_STATUS_MAX 3u
/* The MCU erase takes seconds; poll well past the worst case. */
#define Y4_POLICY_SETTLE_POLLS 120u
#define Y4_POLICY_SETTLE_PAUSE_MS 100u
#define Y4_LOCK_ATTEMPTS 100u
#define Y4_LOCK_PAUSE_MS 20u
static int supported(const struct y4_mcu_identity *id) {
 return id->version[0]==0u&&id->version[1]==1u&&id->version[2]>=39u;
}
static int lock_open(void) {
 int fd=open(Y4_BOARD_RUNTIME "/mcu.lock",O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0600);
 if(fd<0)return -errno;
 struct stat st;int e=fstat(fd,&st)?-errno:0;
 if(!e&&(!S_ISREG(st.st_mode)||st.st_uid||st.st_nlink!=1u||(st.st_mode&07777u)!=0600u))e=-EPERM;
 if(e){close(fd);return e;}
 return fd;
}
int y4_fan_policy_fetch(int fd,const struct y4_mcu_identity *id,
                        struct y4_fan_policy_state *out) {
 if(fd<0||!id||!out)return -EINVAL;
 if(!supported(id))return -EOPNOTSUPP;
 uint8_t head[2]={0,0},duty[3]={0,0,0};
 int rc=y4_mcu_read(fd,Y4_POLICY_REGISTER_STATUS,head,sizeof head);
 if(rc)return rc;
 if(head[0]>Y4_POLICY_STATUS_MAX||head[1]>Y4_POLICY_MODE_MAX)return -EPROTO;
 rc=y4_mcu_read(fd,Y4_POLICY_REGISTER_DUTY,duty,sizeof duty);
 if(rc)return rc;
 if(duty[0]>100u||duty[1]>100u||duty[2]>100u)return -EPROTO;
 out->mode=head[1];out->status=head[0];
 memcpy(out->duty,duty,sizeof duty);
 return 0;
}
static int settle(int fd) {
 /* Persistence runs on the MCU main loop (flash erase stalls it); the
 * status register leaves PENDING when done. Bounded wait, no retries
 * of the write itself. */
 for(unsigned poll=0;poll<Y4_POLICY_SETTLE_POLLS;poll++) {
  uint8_t status=0;
  int rc=y4_mcu_read(fd,Y4_POLICY_REGISTER_STATUS,&status,1);
  if(rc)return rc;
  if(status==1u){ /* PENDING: keep waiting */
   struct timespec pause={0,(long)Y4_POLICY_SETTLE_PAUSE_MS*1000000L};
   nanosleep(&pause,NULL);
   continue;
  }
  if(status==2u)return 0; /* COMMITTED */
  if(status==3u)return -EIO; /* FAILED: not durable */
  return -EPROTO; /* NONE or unknown while a write was accepted */
 }
 return -ETIMEDOUT;
}
int y4_fan_policy_apply(unsigned mode,const unsigned duty[3],
                        struct y4_fan_policy_state *out) {
 if(!out)return -EINVAL;
 if(mode>Y4_POLICY_MODE_MAX||!duty||duty[0]>100u||duty[1]>100u||duty[2]>100u)
  return -EINVAL;
 int lock=lock_open();
 if(lock<0)return lock;
 int held=0,fd=-1,e=0;
 for(unsigned attempt=0;;attempt++) {
  if(!flock(lock,LOCK_EX|LOCK_NB)){held=1;break;}
  if(errno!=EWOULDBLOCK&&errno!=EAGAIN){e=-errno;break;}
  if(attempt+1u>=Y4_LOCK_ATTEMPTS){e=-EBUSY;break;}
  struct timespec pause={0,(long)Y4_LOCK_PAUSE_MS*1000000L};
  nanosleep(&pause,NULL);
 }
 if(!held)goto done;
 fd=open("/dev/i2c-1",O_RDWR|O_CLOEXEC|O_NOFOLLOW);
 if(fd<0){e=-errno;goto done;}
 struct y4_mcu_identity id={0};
 e=y4_mcu_identify(fd,&id);
 if(e)goto done;
 if(!supported(&id)){e=-EOPNOTSUPP;goto done;}
 /* Only manual commits new stored duties; auto/legacy keep whatever is
 * stored so a later switch back to manual cannot surprise with zeros. */
 if(mode==2u)
  for(unsigned i=0;i<3u&&!e;i++)
   e=y4_mcu_write_verified(fd,(uint8_t)(Y4_POLICY_REGISTER_DUTY+i),(uint8_t)duty[i]);
 if(!e)e=y4_mcu_write_verified(fd,Y4_POLICY_REGISTER_MODE,(uint8_t)mode);
 if(!e)e=settle(fd);
 if(!e)e=y4_fan_policy_fetch(fd,&id,out);
done:
 if(fd>=0)close(fd);
 close(lock); /* closing only our flock releases bus ownership */
 if(e)return e;
 return 0;
}
