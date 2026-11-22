#include "y4_os_mcu.h"
#include <errno.h>
int y4_mcu_fans_read(int fd,const struct y4_mcu_identity *id,struct y4_mcu_fans *out)
{
 if(fd<0||!id||!out)return -EINVAL;
 if((id->capabilities&3u)!=3u)return -EOPNOTSUPP;
 for(unsigned attempt=0;attempt<3u;++attempt){
  uint8_t begin,end,raw[8],masks[2];struct y4_mcu_fans sample={0};
  int rc=y4_mcu_read(fd,0x3e,&begin,1);if(rc)return rc;
  if(begin&1u)continue;
  rc=y4_mcu_read(fd,0x10,raw,8);if(rc)return rc;
  rc=y4_mcu_read(fd,0x60,sample.effective,4);if(rc)return rc;
  rc=y4_mcu_read(fd,0x3f,&sample.fault,1);if(rc)return rc;
  rc=y4_mcu_read(fd,0x49,masks,2);if(rc)return rc;
  rc=y4_mcu_read(fd,0x3e,&end,1);if(rc)return rc;
  if(begin!=end||(end&1u))continue;
  if(((unsigned)sample.fault&~y4_mcu_fan_known_faults(id->version))||masks[0]>15u||masks[1]>15u)return -EPROTO;
  for(unsigned i=0;i<4u;++i){if(sample.effective[i]>100u)return -EPROTO;sample.rpm[i]=(uint16_t)((uint16_t)raw[i*2u]|((uint16_t)raw[i*2u+1u]<<8));}
  sample.fan_mask=masks[0];sample.tach_mask=masks[1];*out=sample;return 0;
 }
 return -EAGAIN;
}
