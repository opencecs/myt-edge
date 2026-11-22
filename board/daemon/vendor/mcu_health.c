#define _GNU_SOURCE
#include "y4_os_mcu_health.h"
#include "y4_os_mcu.h"
#include "y4_os_checksum.h"
#include <errno.h>
#include <time.h>
static uint32_t le32(const uint8_t *p) {
 return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
}
int y4_mcu_health_decode(const void *data,size_t n,struct y4_mcu_health *out) {
 if(!data||!out)return -EINVAL;
 if(n!=Y4_MCU_HEALTH_BYTES)return -EPROTO;
 const uint8_t *b=data,*p=b+4;
 if(b[0]&1u)return -EAGAIN;
 if(b[1]!=1)return -ENOTSUP;
 if(b[3]!=80||(b[2]&~7u))return -EPROTO;
 if(!(b[2]&1u)) {
  if(b[2])return -EPROTO;
  for(unsigned i=0;i<80;i++)if(p[i])return -EPROTO;
  return -ENODATA;
 }
 if(le32(p)!=UINT32_C(0x31483459)||p[4]!=1||p[5]||p[6]!=80||p[7]||
    le32(p+76)!=y4_crc32c(p,76))return -EPROTO;
 struct y4_mcu_health h={.publication_sequence=b[0],.flags=b[2]};
#define FIELD(name,offset) h.name=le32(p+offset)
 FIELD(sequence,8);FIELD(reset_flags,12);FIELD(fault_count,16);FIELD(reason,20);
 FIELD(cfsr,24);FIELD(hfsr,28);FIELD(dfsr,32);FIELD(mmfar,36);FIELD(bfar,40);
 FIELD(lr,44);FIELD(pc,48);FIELD(xpsr,52);FIELD(exc_return,56);
 FIELD(stack_pointer,60);FIELD(uptime_ms,64);FIELD(communication_errors,68);
 FIELD(minimum_stack_pointer,72);
#undef FIELD
 unsigned flags=1u|(h.fault_count?2u:0u)|
  ((h.fault_count&&(h.reason&UINT32_C(0x80000000)))?4u:0u);
 if(b[2]!=flags)return -EPROTO;
 *out=h;return 0;
}
int y4_mcu_health_read(int fd,struct y4_mcu_health *out) {
 if(fd<0||!out)return -EINVAL;
 uint8_t first,last,block[Y4_MCU_HEALTH_BYTES];
 int rc=y4_mcu_read(fd,Y4_MCU_HEALTH_REGISTER,&first,1);
 if(rc)return rc;
 if(first&1u)return -EAGAIN;
 rc=y4_mcu_read(fd,Y4_MCU_HEALTH_REGISTER,block,sizeof block);
 if(rc)return rc;
 rc=y4_mcu_read(fd,Y4_MCU_HEALTH_REGISTER,&last,1);
 if(rc)return rc;
 if(first!=last||first!=block[0]||(last&1u))return -EAGAIN;
 struct y4_mcu_health h;rc=y4_mcu_health_decode(block,sizeof block,&h);
 if(rc)return rc;
 struct timespec now;
 if(clock_gettime(CLOCK_BOOTTIME,&now))return -errno;
 h.sampled_ms=(uint64_t)now.tv_sec*1000u+(uint64_t)now.tv_nsec/1000000u;
 *out=h;return 0;
}
