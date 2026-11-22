#include "y4_os_mcu.h"
#include <errno.h>
#include <string.h>
static int valid(const uint8_t raw[16])
{
 if(memcmp(raw,"UID1",4))return -EPROTONOSUPPORT;
 unsigned any=0,not_ff=0;for(unsigned i=4;i<16u;++i){any|=raw[i];not_ff|=(unsigned)(raw[i]^0xffu);}
 return any&&not_ff?0:-ENODATA;
}
int y4_mcu_factory_uid(int fd,uint8_t out[12])
{
 if(fd<0||!out)return -EINVAL;
 uint8_t first[16],second[16];int rc=y4_mcu_read(fd,0x80,first,sizeof(first));if(rc)return rc;
 rc=valid(first);if(rc)return rc;
 rc=y4_mcu_read(fd,0x80,second,sizeof(second));if(rc)return rc;
 rc=valid(second);if(rc)return rc;
 if(memcmp(first,second,sizeof(first)))return -ESTALE;
 memcpy(out,first+4,12);return 0;
}
int y4_mcu_board_identity(int fd,uint8_t out[16])
{
 if(!out)return -EINVAL;
 uint8_t uid[12];int rc=y4_mcu_factory_uid(fd,uid);if(rc)return rc;
 return y4_mcu_board_identity_from_uid(uid,out);
}

int y12_mcu_identify(int fd,struct y4_mcu_identity *identity,uint8_t out[12])
{
 if(fd<0||!identity||!out)return -EINVAL;
 uint8_t first[20],second[20];
 int rc=y4_mcu_read(fd,0x80,first,sizeof first);if(rc)return rc;
 if(memcmp(first,"Y12M",4)||first[4]!=2)return -EPROTONOSUPPORT;
 rc=y4_mcu_read(fd,0x80,second,sizeof second);if(rc)return rc;
 if(memcmp(first,second,sizeof first))return -ESTALE;
 unsigned any=0,not_ff=0;
 for(unsigned i=8;i<sizeof first;i++){any|=first[i];not_ff|=first[i]^0xffu;}
 if(!any||!not_ff)return -ENODATA;
 *identity=(struct y4_mcu_identity){{first[5],first[6],first[7]},0};
 memcpy(out,first+8,12);
 return 0;
}
